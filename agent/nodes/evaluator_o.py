"""
agent/nodes/evaluator_o.py  —  输出质检节点 Evaluator-O（流程的第 ⑩ 步）
=========================================================================

【在流程中的位置】synthesizer → ★evaluator_o★ → memory_updater / synthesizer(修正循环)

【这个节点干什么？】

Evaluator-O 是报告的"终审编辑"——检查 Synthesizer 生成的报告是否合格。
就像发表论文前的同行评审一样，确保报告没有错误才能交给用户。

它做五项检查：

  Check-1 ── 四层结构完整性
    报告是否包含了 Layer-1 到 Layer-4 的完整结构？
    缺任何一层 → 打回重写

  Check-2 ── 数字交叉核验（防止数字幻觉）
    报告里引用的每个数字，必须能在 SQL 结果或 RAG 文本中找到来源。
    例如：报告说"碳排放 12.3 万吨"，但 SQL 返回的是 15.6 → 数字幻觉！

  Check-3 ── 实体幻觉检测
    报告中提到的公司名/年份必须在用户查询范围内。
    例如：用户问比亚迪，报告里突然提了特斯拉的数据 → 实体幻觉！

  Check-4 ── 结论方向一致性
    结论的方向不得与数据方向矛盾。
    例如：数据显示碳排放增加，但结论说"减排成效显著" → 方向矛盾！

  Check-5 ── 口径差异标注完整性
    如果 evaluator_d 标记了口径问题，报告里必须有明确标注。

【打回机制】
  - 最多打回 Synthesizer 2 次（eval_o_retry_count ≤ 2）
  - 第 3 次仍不通过 → 降级输出（加免责声明，不再打回）
  - 每次打回只修正最高优先级的错误，避免无限修正循环

【写入 State 的关键字段】
  - eval_o_status: "pass" | "fail"
  - eval_o_errors: 发现的具体错误列表
  - eval_o_retry_count: 打回次数
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from google import genai
from google.genai import types
from dotenv import load_dotenv

from agent.state import AgentState, get_sql_result_dataframe, apply_degraded_state
from agent.tracing import trace_node, TraceLogger, llm_call_with_retry

load_dotenv()
log     = logging.getLogger(__name__)
_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
_MODEL  = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")

MAX_EVAL_O_RETRY = 2

# 四层结构的必要标记（报告里必须包含这些关键词）
_LAYER_MARKERS = {
    "layer1": ["Layer-1", "基础数据层", "📊"],
    "layer2": ["Layer-2", "驱动因素", "🔍"],
    "layer3": ["Layer-3", "行业对标", "📐"],
    "layer4": ["Layer-4", "风险", "⚠️"],
}


# ══════════════════════════════════════════════════════════════════════════════
# Check-1：四层结构完整性（规则检查，无 LLM）
# ══════════════════════════════════════════════════════════════════════════════

def _check_structure(analysis: str) -> list[dict]:
    errors = []
    for layer_id, markers in _LAYER_MARKERS.items():
        found = any(m in analysis for m in markers)
        if not found:
            errors.append({
                "type":     "MISSING_LAYER",
                "layer":    layer_id,
                "detail":   f"报告缺少 {layer_id}（{markers[1]}），必须补充",
                "priority": 1,
            })
    return errors


# ══════════════════════════════════════════════════════════════════════════════
# Check-2：数字交叉核验（规则 + 轻量 LLM）
# ══════════════════════════════════════════════════════════════════════════════

def _extract_numbers_from_text(text: str) -> list[str]:
    """从文本里提取所有数字字符串（含单位）。"""
    # 匹配如：1,234.5万tCO2e、45.6%、100亿元、2.3GJ/亿元
    pattern = r"[\d,]+\.?\d*\s*(?:万?[tT][Cc][Oo]2?[eE]|亿元|万元|%|GJ|MWh|小时|次|人|亿|万|GW|MW)?"
    return re.findall(pattern, text)


_NUMBER_CHECK_PROMPT = """\
你是 ESG 数据核验专家。检查以下报告中的数字是否能在数据来源中找到支撑。

## 报告片段（前2000字）
{analysis_excerpt}

## 数据来源（SQL结果）
{sql_data}

## 数据来源（RAG片段，前1000字）
{rag_excerpt}

## 任务
找出报告中出现但在数据来源中找不到依据的数字（可能是幻觉）。
返回 JSON 数组，每项格式：{{"number": "具体数字", "location": "出现在报告哪句话里", "risk": "high/medium"}}
如果所有数字都有来源支撑，返回空数组 []。
只关注关键业务数字（碳排放量、贷款余额、研发投入等），忽略序号、年份、百分比符号。
"""


def _check_numbers(state: AgentState, trace_id: str) -> list[dict]:
    analysis   = state.get("analysis", "")
    sql_result = get_sql_result_dataframe(state)
    rag_result = state.get("rag_result") or {}

    sql_str = ""
    if sql_result is not None and len(sql_result) > 0:
        sql_str = sql_result.to_string(index=False, max_rows=30)[:1500]

    rag_str = ""
    for chunk in rag_result.get("chunks", [])[:3]:
        rag_str += chunk.get("text", "")[:300] + "\n"

    prompt = _NUMBER_CHECK_PROMPT.format(
        analysis_excerpt=analysis[:2000],
        sql_data=sql_str or "（无SQL数据）",
        rag_excerpt=rag_str or "（无RAG数据）",
    )

    try:
        def _call():
            resp = _client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            txt = resp.text.strip()
            txt = re.sub(r"^```json\s*", "", txt)
            txt = re.sub(r"\s*```$", "", txt)
            return json.loads(txt)

        suspicious = llm_call_with_retry(
            _call, max_retries=1, timeout_seconds=30,
            caller_name="number_checker", trace_id=trace_id,
        )

        # 只把 high-risk 的数字幻觉报错
        errors = []
        for item in (suspicious or []):
            if item.get("risk") == "high":
                errors.append({
                    "type":     "NUMBER_HALLUCINATION",
                    "detail":   f"数字 '{item.get('number')}' 无数据来源支撑：{item.get('location','')}",
                    "priority": 2,
                })
        return errors

    except Exception as e:
        log.warning(f"数字核验 LLM 调用失败：{e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Check-3：实体幻觉检测（规则检查）
# ══════════════════════════════════════════════════════════════════════════════

def _check_entity_hallucination(state: AgentState) -> list[dict]:
    """
    报告中出现的公司名和年份必须在请求范围内。
    检测幻觉公司名（完全不在 entities 里的公司）。
    """
    entities         = state.get("entities", {})
    requested_cos    = set(entities.get("companies", []))
    requested_years  = set(str(y) for y in entities.get("years", []))
    analysis         = state.get("analysis", "")

    errors = []

    # 检测年份幻觉（报告中出现 2025+ 或 2021- 的年份）
    year_mentions = re.findall(r"\b20(\d{2})\b", analysis)
    for y in year_mentions:
        full_year = "20" + y
        if full_year not in requested_years and int(full_year) > 2024:
            errors.append({
                "type":     "ENTITY_HALLUCINATION",
                "detail":   f"报告出现未请求年份 {full_year}（请求范围：{requested_years}）",
                "priority": 2,
            })
            break  # 只报一次

    return errors


# ══════════════════════════════════════════════════════════════════════════════
# Check-4：结论方向一致性（LLM 判断）
# ══════════════════════════════════════════════════════════════════════════════

_DIRECTION_CHECK_PROMPT = """\
你是 ESG 数据审核专家。检查以下报告结论的方向是否与数据一致。

## 数据摘要
{data_summary}

## 报告结论片段（Layer-2 和 Layer-3 部分）
{conclusion_excerpt}

## 任务
找出结论方向与数据明显矛盾的地方（如数据显示排放量上升，但结论说减排效果良好）。
返回 JSON 数组，每项：{{"contradiction": "矛盾描述", "data_says": "数据实际显示", "report_says": "报告结论"}}
没有矛盾则返回 []。
"""


def _check_direction_consistency(state: AgentState, trace_id: str) -> list[dict]:
    sql_result = get_sql_result_dataframe(state)
    analysis   = state.get("analysis", "")

    if sql_result is None or len(sql_result) == 0:
        return []  # 没有结构化数据，无法做方向核验

    # 提取数值趋势摘要
    data_summary = sql_result.to_string(index=False, max_rows=15)[:800]

    # 提取报告的 Layer-2 和 Layer-3 部分
    l2_match = re.search(r"Layer-2.*?(?=Layer-3|$)", analysis, re.DOTALL)
    l3_match = re.search(r"Layer-3.*?(?=Layer-4|$)", analysis, re.DOTALL)
    conclusion_excerpt = ""
    if l2_match:
        conclusion_excerpt += l2_match.group()[:600]
    if l3_match:
        conclusion_excerpt += l3_match.group()[:600]

    if not conclusion_excerpt:
        return []

    prompt = _DIRECTION_CHECK_PROMPT.format(
        data_summary=data_summary,
        conclusion_excerpt=conclusion_excerpt,
    )

    try:
        def _call():
            resp = _client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            txt = resp.text.strip()
            txt = re.sub(r"^```json\s*", "", txt)
            txt = re.sub(r"\s*```$", "", txt)
            return json.loads(txt)

        contradictions = llm_call_with_retry(
            _call, max_retries=1, timeout_seconds=30,
            caller_name="direction_checker", trace_id=trace_id,
        )

        return [
            {
                "type":     "DIRECTION_CONTRADICTION",
                "detail":   (
                    f"{item.get('contradiction','')}｜"
                    f"数据显示：{item.get('data_says','')}｜"
                    f"报告写：{item.get('report_says','')}"
                ),
                "priority": 1,
            }
            for item in (contradictions or [])
        ]

    except Exception as e:
        log.warning(f"方向一致性检查 LLM 调用失败：{e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Check-5：口径差异标注完整性（规则检查）
# ══════════════════════════════════════════════════════════════════════════════

def _check_scope_annotation(state: AgentState) -> list[dict]:
    """
    flagged 的指标（口径不可比）必须在报告 Layer-1 里有明确标注。
    """
    scope    = state.get("scope_consistency", {})
    analysis = state.get("analysis", "")
    errors   = []

    if not scope.get("checked"):
        return []

    for mk, info in scope.get("per_metric", {}).items():
        if info.get("action") == "flagged":
            # 报告里必须包含该指标名 + 某个警告标记
            metric_in_report = mk in analysis or mk.replace("_", " ") in analysis
            has_warning      = "口径" in analysis or "不可比" in analysis or "⚠️" in analysis

            if not (metric_in_report and has_warning):
                errors.append({
                    "type":     "MISSING_SCOPE_ANNOTATION",
                    "detail":   f"指标 {mk} 口径不可比，必须在 Layer-1 明确标注，但报告中未找到",
                    "priority": 2,
                })

    return errors


# ══════════════════════════════════════════════════════════════════════════════
# 降级输出组装
# ══════════════════════════════════════════════════════════════════════════════

_DEGRADED_DISCLAIMER = """
---
> **⚠️ 报告质量说明**
> 本报告经过 {retry_count} 次质检修正后仍存在以下问题，已降级输出：
{error_list}
> 建议使用方核实上述数据后再做决策。
"""


def _append_disclaimer(analysis: str, errors: list[dict], retry_count: int) -> str:
    error_list = "\n".join(
        f"> - {e.get('type')}: {e.get('detail', '')[:100]}"
        for e in errors[:5]
    )
    disclaimer = _DEGRADED_DISCLAIMER.format(
        retry_count=retry_count,
        error_list=error_list,
    )
    return analysis + disclaimer


# ══════════════════════════════════════════════════════════════════════════════
# 主节点函数
# ══════════════════════════════════════════════════════════════════════════════

@trace_node("evaluator_o", tags=["evaluation"])
def evaluator_o_node(state: AgentState) -> AgentState:
    trace_id    = state.get("trace_id", "")
    log         = TraceLogger("evaluator_o", trace_id)
    retry_count = state.get("eval_o_retry_count", 0)
    analysis    = state.get("analysis", "")

    if not analysis:
        log.warning("analysis 为空，直接标记失败")
        state["eval_o_status"] = "fail"
        state["eval_o_errors"] = [{"type": "EMPTY_OUTPUT", "detail": "Synthesizer 未生成内容", "priority": 0}]
        return state

    log.info(f"开始输出质检（第 {retry_count+1} 次），报告长度：{len(analysis)} 字符")

    all_errors: list[dict] = []

    # Check-1：四层结构完整性（最高优先级，规则检查）
    structure_errors = _check_structure(analysis)
    all_errors.extend(structure_errors)
    log.info(f"Check-1 结构完整性：{len(structure_errors)} 个错误")

    # Check-2：数字核验（LLM，较慢，只在结构完整时跑）
    if not structure_errors:
        number_errors = _check_numbers(state, trace_id)
        all_errors.extend(number_errors)
        log.info(f"Check-2 数字核验：{len(number_errors)} 个错误")
    else:
        log.info("Check-2 跳过（结构不完整，先修结构）")

    # Check-3：实体幻觉（规则检查，轻量）
    entity_errors = _check_entity_hallucination(state)
    all_errors.extend(entity_errors)
    log.info(f"Check-3 实体幻觉：{len(entity_errors)} 个错误")

    # Check-4：结论方向一致性（LLM，只在结构完整时跑）
    if not structure_errors:
        direction_errors = _check_direction_consistency(state, trace_id)
        all_errors.extend(direction_errors)
        log.info(f"Check-4 方向一致性：{len(direction_errors)} 个错误")
    else:
        log.info("Check-4 跳过（结构不完整）")

    # Check-5：口径标注完整性（规则）
    scope_errors = _check_scope_annotation(state)
    all_errors.extend(scope_errors)
    log.info(f"Check-5 口径标注：{len(scope_errors)} 个错误")

    # ── 判定与路由 ────────────────────────────────────────────────────────────
    if not all_errors:
        log.info("✅ Evaluator-O 全部通过")
        state["eval_o_status"]     = "pass"
        state["eval_o_errors"]     = []
        return state

    # 按优先级排序（priority 越小越紧急）
    all_errors.sort(key=lambda e: e.get("priority", 9))

    if retry_count >= MAX_EVAL_O_RETRY:
        # max retry -> degraded output with unified template
        log.warning(
            f"Evaluator-O exceeded max retries ({MAX_EVAL_O_RETRY}), degraded output"
        )
        state["eval_o_errors"] = all_errors
        reason_detail = f"errors={ [e['type'] for e in all_errors[:3]] }"
        partial = _append_disclaimer(analysis, all_errors, retry_count)
        return apply_degraded_state(
            state,
            reason_code="EVAL_O_MAX_RETRY",
            reason_detail=reason_detail,
            partial_analysis=partial,
        )

    # 打回 Synthesizer 修正（只传最高优先级的错误）
    priority0_errors = [e for e in all_errors if e.get("priority") == all_errors[0].get("priority")]
    log.warning(
        f"Evaluator-O 未通过，打回 Synthesizer（第 {retry_count+1} 次），"
        f"错误：{[e['type'] for e in priority0_errors]}"
    )
    state["eval_o_status"]     = "fail"
    state["eval_o_errors"]     = priority0_errors   # 只传最高优先级
    state["eval_o_retry_count"] = retry_count + 1

    return state
