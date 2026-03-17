"""
agent/nodes/synthesizer.py  —  四层分析合成节点（流程的第 ⑨ 步）
================================================================

【在流程中的位置】map_reduce → ★synthesizer★ → evaluator_o

【这个节点干什么？】

Synthesizer 是整个系统的"写作者"——它把前面收集到的所有数据
（SQL 查询结果 + RAG 文本段落 + 缺失报告）转化为一份专业的 ESG 分析报告。

报告严格遵循 MSCI ESG 分析框架的四层结构：

  Layer-1 ── 基础数据层
    标准化数据 + 同比变化 + 行业分位数 + 图表
    例如："比亚迪 2023 年 Scope 1 排放 12.3 万吨，同比下降 15%"

  Layer-2 ── 驱动因素分析层（回答"为什么"）
    纵向分析变化原因
    例如："下降主要因为光伏发电自用比例提升到 65%"

  Layer-3 ── 行业对标层（回答"在行业里什么水平"）
    横向对比同行
    例如："在新能源车企中处于第 2 位，低于宁德时代"

  Layer-4 ── 风险与合规层
    风险提示 + 政策锚定
    例如："需关注 EU CBAM 碳关税对出口影响"

此外还会：
  - 自动生成 Plotly 图表 JSON Spec（可视化数据趋势）
  - 提炼 3~5 条 key_findings（摘要要点）
  - 处理口径差异标注（flagged → 禁止对比，adjusted → 注明方法）

【写入 State 的关键字段】
  - analysis: 四层 Markdown 报告正文
  - chart_spec: Plotly 图表 JSON
  - key_findings: 关键发现列表
  - sources: 引用来源溯源
  - normalization_applied: 归一化方案记录
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

from agent.state import AgentState, NormalizationRecord, get_sql_result_dataframe
from agent.tracing import trace_node, TraceLogger, llm_call_with_retry
from agent.materiality import get_materiality_context_for_synthesizer
from agent.data_dictionary import get_metric_display, METRIC_DISPLAY_NAMES

load_dotenv()
log     = logging.getLogger(__name__)
_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
_MODEL  = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")

# ══════════════════════════════════════════════════════════════════════════════
# 规模归一化
# ══════════════════════════════════════════════════════════════════════════════

# 各指标的归一化分母优先级
_NORM_DENOMINATORS = {
    "scope_1_emissions":        ["营业收入(亿元)", "产量(万辆/GWh)", "员工数"],
    "scope_2_emissions":        ["营业收入(亿元)", "产量"],
    "scope_3_emissions":        ["营业收入(亿元)"],
    "total_energy_consumption": ["营业收入(亿元)", "产量"],
    "rd_investment_total":      ["营业收入(亿元)"],
    "charitable_donations":     ["营业收入(亿元)"],
    "green_finance_balance":    ["贷款总余额(亿元)"],
    "inclusive_finance_balance":["贷款总余额(亿元)"],
}


def _try_normalize(
    sql_result,
    metric_keys: list[str],
    compare_dimension: str,
) -> list[NormalizationRecord]:
    """
    尝试在 SQL 结果中做规模归一化。
    返回归一化记录列表。
    """
    import pandas as pd
    records: list[NormalizationRecord] = []

    if compare_dimension not in ("horizontal", "both"):
        return records
    if sql_result is None or len(sql_result) == 0:
        return records

    for metric_key in metric_keys:
        if metric_key not in _NORM_DENOMINATORS:
            continue
        denom_options = _NORM_DENOMINATORS[metric_key]

        # 找到可用的分母列（如果 SQL 结果里有）
        used_denom = None
        for denom in denom_options:
            # 简化：检查是否有 energy_intensity 等已归一化字段
            if metric_key == "total_energy_consumption" and "energy_intensity" in sql_result.columns:
                used_denom = "energy_intensity（已内置）"
                break

        records.append(NormalizationRecord(
            metric=metric_key,
            denominator=used_denom or denom_options[0],
            denom_source="sql" if used_denom else "not_found",
            formula=(
                f"{metric_key} ÷ {used_denom or denom_options[0]}"
            ),
            applied=used_denom is not None,
            fallback_note=(
                "" if used_denom
                else f"未找到分母数据（{denom_options[0]}），报告中使用绝对值并标注风险"
            ),
        ))

    return records


# ══════════════════════════════════════════════════════════════════════════════
# 上下文组装
# ══════════════════════════════════════════════════════════════════════════════

def _build_synthesis_context(state: AgentState) -> str:
    """把 SQL 数据 + RAG 上下文 + 质检信号拼成 Synthesizer 的输入上下文。"""
    parts: list[str] = []

    # SQL 数据表
    sql_result = get_sql_result_dataframe(state)
    if sql_result is not None and len(sql_result) > 0:
        parts.append("=== 结构化数据（来自数据库）===")
        parts.append(sql_result.to_string(index=False, max_rows=50))
        parts.append("")

    # RAG 上下文（压缩后或原始）
    if state.get("map_reduce_applied") and state.get("compressed_context"):
        parts.append("=== 原文检索摘要（经 Map-Reduce 压缩）===")
        parts.append(state["compressed_context"])
    else:
        rag_result = state.get("rag_result")
        if rag_result and rag_result.get("chunks"):
            from data.rag_retriever import format_chunks_for_llm
            parts.append("=== 原文检索段落 ===")
            parts.append(format_chunks_for_llm(rag_result["chunks"], max_chars=5000))
    parts.append("")

    # 缺失数据说明
    missing = state.get("missing_data_report", {})
    if missing.get("L2") or missing.get("L3"):
        parts.append("=== 数据缺失说明 ===")
        for item in missing.get("L2", []):
            parts.append(
                f"⚠️  {item.get('company','')} {item.get('year','')}年 "
                f"{item.get('metric','')}：未披露（不得按0处理）"
            )
        for item in missing.get("L3", []):
            parts.append(f"ℹ️  {item.get('metric','')}：{item.get('detail','')}")
        parts.append("")

    # 口径差异说明
    scope = state.get("scope_consistency", {})
    if scope.get("checked") and not scope.get("consistent"):
        parts.append("=== 口径差异说明 ===")
        for mk, info in scope.get("per_metric", {}).items():
            if info.get("action") == "flagged":
                parts.append(f"🔴 {mk}：口径不可比，禁止横向对比，仅做各自纵向分析")
                for co, desc in info.get("detail", {}).items():
                    parts.append(f"   {co}：{desc}")
            elif info.get("action") == "adjusted":
                parts.append(f"🟡 {mk}：已做口径调整（{info.get('adjustment_method','')}）")
        parts.append("")

    # 归一化说明
    norm_records = state.get("normalization_applied", [])
    if norm_records:
        parts.append("=== 规模归一化方案 ===")
        for r in norm_records:
            if r.get("applied"):
                parts.append(f"✅ {r['metric']}：{r.get('formula','')}")
            else:
                parts.append(f"⚠️  {r['metric']}：{r.get('fallback_note','')}，使用绝对值")
        parts.append("")

    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# Synthesizer Prompt
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
你是一位专业的 ESG 研究分析师，具备 MSCI ESG 评级方法论知识。
你的分析报告将被专业投资者和企业 ESG 管理者使用，必须达到卖方研究报告级别的专业深度。

## 核心原则
1. 绝对不允许只罗列数字，每个数字都必须配套"为什么"和"意味着什么"
2. 未披露数据标注"未披露"，严禁按0处理或参与对比计算
3. 口径不可比的指标，禁止做横向对比结论
4. 分析结论必须有数据支撑，不得凭空推断

## 强制输出结构（缺任何一层 Evaluator-O 将打回）

### 📊 Layer-1：基础数据层
- 标准化后的指标数据表（含单位、归一化方式、数据质量标注）
- 同比/环比变化（%），未披露年份用"—"表示
- 同业分位值（基于当前数据库中同行业公司的相对位置）
- 口径差异标注（红色⚠️标注不可比项，黄色🟡标注已调整项）

### 🔍 Layer-2：驱动因素分析层（纵向）
- 必须回答"指标为什么变化？"
- 必须区分：主动改善（技术/管理驱动）vs 被动变化（规模/市场驱动）
- 必须引用原文中的具体战略举措/落地动作作为佐证
- 严禁只写"同比增加X%"而不给原因

### 📐 Layer-3：行业对标层（横向）
- 必须回答"这个指标在行业里是什么水平？"
- 对标行业均值/中位数/头部水平
- 投入vs产出ROI分析（高投入有无带来高产出？）
- 如口径不可比，只做各公司自身纵向分析，明确说明无法横向对比原因

### ⚠️ Layer-4：风险与合规层
- 数据风险：未披露指标、低置信度数据、口径不一致
- 目标完成率：如有承诺的减排/ESG目标，分析完成进度
- 合规风险：处罚记录、政策匹配度
- 政策锚定：结合最新监管要求（A股ESG披露规则、双碳政策等）判断达标情况
"""

_USER_PROMPT_TEMPLATE = """\
## 分析任务
问题：{query}
分析公司：{companies}
分析年份：{years}
分析意图：{intent}
对比维度：{compare_dimension}

## 行业实质性议题背景
{materiality_context}

## 数据与上下文
{synthesis_context}

## 生成要求
请按照四层结构生成完整的 ESG 分析报告（Markdown 格式）。
报告末尾额外输出一个 JSON 块，格式如下：

```json
{{
  "key_findings": ["发现1（≤30字）", "发现2", "发现3"],
  "chart_spec": {{
    "type": "line|bar|grouped_bar|radar",
    "title": "图表标题",
    "x_axis": ["2022", "2023", "2024"],
    "series": [
      {{"name": "公司A", "data": [100, 120, null], "unit": "万tCO2e"}}
    ]
  }}
}}
```
null 代表数据未披露。如果数据不适合出图，chart_spec 填 null。
"""


# ══════════════════════════════════════════════════════════════════════════════
# 主节点函数
# ══════════════════════════════════════════════════════════════════════════════

@trace_node("synthesizer", tags=["synthesis"])
def synthesizer_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("synthesizer", trace_id)

    entities  = state.get("entities", {})
    companies = entities.get("companies", [])
    years     = entities.get("years", [])
    metrics   = entities.get("metrics", [])
    intent    = entities.get("intent", "qa")
    compare_dimension = entities.get("compare_dimension", "none")
    industry  = entities.get("industry", "")
    query     = state.get("resolved_query", state.get("user_query", ""))

    # ── 规模归一化 ────────────────────────────────────────────────────────────
    norm_records = _try_normalize(
        get_sql_result_dataframe(state),
        metrics,
        compare_dimension,
    )
    state["normalization_applied"] = norm_records

    # ── 组装上下文 ────────────────────────────────────────────────────────────
    synthesis_context   = _build_synthesis_context(state)
    materiality_context = get_materiality_context_for_synthesizer(industry, metrics)

    # ── 处理 Evaluator-O 打回的局部修正 ──────────────────────────────────────
    eval_o_errors = state.get("eval_o_errors", [])
    retry_count   = state.get("eval_o_retry_count", 0)
    fix_instruction = ""
    if eval_o_errors and retry_count > 0:
        fix_instruction = (
            "\n\n## ⚠️ 修正要求（上一版报告被质检打回，必须修正以下问题）\n"
            + "\n".join(f"- {e.get('type')}: {e.get('detail', '')}"
                        for e in eval_o_errors)
        )
        log.info(f"修正模式，第 {retry_count} 次，错误：{[e['type'] for e in eval_o_errors]}")

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        query=query,
        companies=companies,
        years=years,
        intent=intent,
        compare_dimension=compare_dimension,
        materiality_context=materiality_context,
        synthesis_context=synthesis_context,
    ) + fix_instruction

    log.info(f"开始生成四层分析报告，context={len(synthesis_context)}字符")

    # ── LLM 生成 ─────────────────────────────────────────────────────────────
    def _call():
        resp = _client.models.generate_content(
            model=_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=0.3,  # 分析类任务略提高创意度
                max_output_tokens=8192,
            ),
        )
        return resp.text

    raw_output = llm_call_with_retry(
        _call,
        max_retries=2,
        timeout_seconds=300,
        caller_name="synthesizer_llm",
        trace_id=trace_id,
    )

    # ── 解析 JSON 尾部（key_findings + chart_spec） ───────────────────────────
    analysis_text = raw_output
    key_findings  = []
    chart_spec    = None

    json_match = re.search(r"```json\s*([\s\S]+?)\s*```", raw_output)
    if json_match:
        try:
            parsed      = json.loads(json_match.group(1))
            key_findings = parsed.get("key_findings", [])
            chart_spec   = parsed.get("chart_spec")
            # 从正文中移除 JSON 块
            analysis_text = raw_output[:json_match.start()].strip()
        except json.JSONDecodeError as e:
            log.warning(f"JSON 解析失败：{e}")

    # ── 构建来源溯源列表 ──────────────────────────────────────────────────────
    sources: list[dict] = list(state.get("sources", []))
    rag_result = state.get("rag_result") or {}
    for chunk in rag_result.get("chunks", [])[:5]:
        sources.append({
            "type":        "rag",
            "company":     chunk.get("company_name", ""),
            "year":        chunk.get("year", ""),
            "page":        chunk.get("page_num", ""),
            "file":        chunk.get("source_file", ""),
            "score":       chunk.get("rerank_score", 0),
            "excerpt":     chunk.get("text", "")[:80],
        })
    if state.get("sql_query_executed"):
        sources.append({
            "type":  "sql",
            "query": state["sql_query_executed"][:200],
        })

    log.info(
        f"报告生成完成",
        {
            "analysis_chars": len(analysis_text),
            "findings":       len(key_findings),
            "has_chart":      chart_spec is not None,
        },
    )

    state["analysis"]      = analysis_text
    state["key_findings"]  = key_findings
    state["chart_spec"]    = chart_spec
    state["sources"]       = sources

    return state
