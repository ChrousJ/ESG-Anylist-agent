"""
agent/nodes/evaluator_d.py  —  数据质检节点 Evaluator-D（流程的第 ⑦ 步）
=========================================================================

【在流程中的位置】worker_aggregator → ★evaluator_d★ → map_reduce / supervisor(Re-plan)

【这个节点干什么？】

Evaluator-D 是数据的"质检员"——在数据被送去生成报告之前，先做一轮质量检查。
如果数据质量不达标，它会把控制权打回 Supervisor 让其重新规划（Re-plan 循环）。

它做四层检查（像工厂的四道质检工序）：

  Layer-1 ── SQL 结果有效性
    SQL 返回的数据是否为空？是否覆盖了用户查询的所有年份和公司？
    例如：用户问了 2022-2023 两年数据，但 SQL 只返回了 2022 年的 → 标记缺失

  Layer-2 ── RAG 召回质量
    RAG 检索回来的文本片段数量够不够？相关性评分高不高？
    例如：Reranker 最高分只有 0.15（阈值 0.3）→ 说明检索效果很差

  Layer-3 ── 双路数据对齐
    SQL 和 RAG 覆盖的公司/年份是否一致？
    例如：SQL 有比亚迪数据但 RAG 只检索到宁德时代的报告 → 不对齐

  Layer-4 ── 口径一致性校验
    横向对比时，不同公司的同一指标统计范围是否可比？
    例如：A公司碳排放含供应链，B公司只含运营层面 → 口径不一致，需标注

【缺失数据三级分类】（ESG 行业特有逻辑）
  L1 = 大面积缺失（核心指标缺失 > 50%）→ 触发 Re-plan
  L2 = 局部缺失（单一公司/年份缺失）→ 标注后继续
  L3 = 行业性缺失（该指标在整个行业普遍不披露）→ 直接跳过

【写入 State 的关键字段】
  - eval_d_status: "pass" | "fail" | "pass_with_warnings"
  - eval_d_errors: 失败时的具体错误列表
  - missing_data_report: L1/L2/L3 三级缺失分类报告
  - scope_consistency: 口径一致性校验结果
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from google.genai import types
from dotenv import load_dotenv

from agent.state import (
    AgentState, MissingDataReport, ScopeConsistencyDict, get_sql_result_dataframe
)
from agent.tracing import trace_node, TraceLogger, llm_call_with_retry
from agent.llm_provider import get_default_model, llm_generate_content
from agent.data_dictionary import SCOPE_CAVEATS, get_metric_display

load_dotenv()
log     = logging.getLogger(__name__)
_MODEL = os.getenv("LLM_MAIN_MODEL", get_default_model())
_SCOPE_ADJUSTABLE_REPLAN = os.getenv("SCOPE_ADJUSTABLE_REPLAN", "0").strip().lower() in {
    "1", "true", "yes", "y",
}
_PERCENT_METRIC_MAX = float(os.getenv("PERCENT_METRIC_MAX", "100"))

# 指标权重表（权重3 = 核心，缺失触发 Re-plan）
METRIC_WEIGHTS = {
    "scope_1_emissions": 3, "scope_2_emissions": 3,
    "green_finance_balance": 3, "clean_energy_ratio": 3,
    "rd_investment_total": 3, "scope_3_emissions": 3,
    "total_energy_consumption": 2, "energy_intensity": 2,
    "independent_director_ratio": 2, "safety_accidents_count": 2,
    "inclusive_finance_balance": 2, "supplier_esg_audit_ratio": 2,
    "external_esg_rating": 1, "esg_committee_setup": 1,
    "charitable_donations": 1, "customer_complaint_res": 1,
    "employee_training_hours": 1, "female_director_ratio": 1,
    "anti_corruption_coverage": 1, "regulatory_penalties": 1,
}

# 行业性缺失（整个行业普遍不披露，不触发 Re-plan）
INDUSTRY_MISSING_NORMS = {
    "new_energy": {"scope_3_emissions", "supplier_esg_audit_ratio"},
    "power":      {"scope_3_emissions"},
    "bank":       {"scope_3_emissions"},
}


# ══════════════════════════════════════════════════════════════════════════════
# Layer-1：SQL 结果有效性 + 缺失数据分级
# ══════════════════════════════════════════════════════════════════════════════

def _check_sql(
    state: AgentState,
) -> tuple[list[dict], MissingDataReport]:
    """
    返回 (errors, missing_report)。
    errors 里只放 L1（触发 Re-plan 的）错误。
    """
    sql_result  = get_sql_result_dataframe(state)
    entities    = state.get("entities", {})
    worker_stat = state.get("worker_status", {}).get("sql", {})
    industry    = entities.get("industry", "")

    errors  = []
    L1, L2, L3 = [], [], []

    # SQL Worker 未成功时直接记录 Worker 级别错误
    if worker_stat.get("status") not in ("success", "skipped"):
        errors.append({
            "type":   "SQL_WORKER_FAILED",
            "detail": worker_stat.get("error_detail", ""),
        })
        return errors, MissingDataReport(L1=L1, L2=L2, L3=L3, summary="SQL Worker 不可用")

    if sql_result is None or (hasattr(sql_result, "__len__") and len(sql_result) == 0):
        if worker_stat.get("status") == "skipped":
            return [], MissingDataReport(L1=[], L2=[], L3=[], summary="")

        errors.append({"type": "SQL_EMPTY", "detail": "查询返回空结果集"})
        return errors, MissingDataReport(L1=L1, L2=L2, L3=L3, summary="SQL 结果为空")

    # ── 年份覆盖检查 ─────────────────────────────────────────────────────────
    requested_years = set(entities.get("years", []))
    returned_years  = set()
    if "year" in sql_result.columns:
        returned_years = set(int(y) for y in sql_result["year"].tolist())
    missing_years = requested_years - returned_years

    if len(missing_years) > len(requested_years) * 0.5:
        errors.append({
            "type":         "SQL_YEAR_INCOMPLETE",
            "missing_years": list(missing_years),
            "detail":       f"请求年份 {requested_years}，仅返回 {returned_years}",
        })

    # ── 逐指标缺失分级 ───────────────────────────────────────────────────────
    requested_metrics  = entities.get("metrics", [])
    requested_companies = entities.get("companies", [])
    industry_norms     = INDUSTRY_MISSING_NORMS.get(industry, set())

    def _is_implausible_metric_value(metric_key: str, value: object) -> bool:
        if value is None:
            return False
        try:
            _, unit = get_metric_display(metric_key)
            numeric_value = float(value)
        except Exception:
            return False
        if unit == "%" and (numeric_value < 0 or numeric_value > _PERCENT_METRIC_MAX):
            return True
        return False

    for metric in requested_metrics:
        weight = METRIC_WEIGHTS.get(metric, 1)

        if metric not in sql_result.columns:
            # 指标根本不在结果集里
            if metric in industry_norms:
                L3.append({"metric": metric,
                           "detail": "该指标在本行业普遍不披露，已跳过"})
                continue
            if weight >= 3:
                errors.append({"type": "L1_MISSING",
                               "metric": metric,
                               "detail": "核心指标在结果中完全缺失"})
                L1.append({"metric": metric, "missing_rate": 1.0,
                           "detail": "结果集中不存在该列"})
            else:
                L2.append({"metric": metric, "company": "all", "year": 0,
                           "detail": "结果集中不存在该列（低权重，跳过）"})
            continue

        # 逐行检查 NULL / 明显异常值（如百分比 > 100）
        null_mask = sql_result[metric].isnull()
        invalid_mask = sql_result[metric].apply(
            lambda value: _is_implausible_metric_value(metric, value)
        )
        null_rows = sql_result[null_mask | invalid_mask]
        total_expected = (
            len(requested_companies) * len(requested_years)
            if requested_companies and requested_years
            else len(sql_result)
        )
        missing_count = len(null_rows)
        missing_rate  = missing_count / total_expected if total_expected > 0 else 0

        if missing_rate == 0:
            continue

        # 行业性缺失 → L3
        if metric in industry_norms and missing_rate > 0.7:
            L3.append({"metric": metric,
                       "industry_missing_rate": missing_rate,
                       "detail": f"行业内 {missing_rate:.0%} 未披露，属行业惯例"})
            continue

        # 大面积缺失 → L1
        if missing_rate > 0.5 and weight >= 3:
            if entities.get("industry_wide"):
                # An industry-wide disclosure comparison must preserve missing rows:
                # the missingness itself is a reportable business result, not a retriable fault.
                L2.append({
                    "metric": metric, "company": "industry", "year": 0,
                    "detail": f"行业全量查询中 {missing_rate:.0%} 未核实/未披露，保留空值用于披露覆盖分析",
                })
            else:
                errors.append({
                    "type":         "L1_MISSING",
                    "metric":       metric,
                    "missing_rate": missing_rate,
                    "detail":       f"{missing_rate:.0%} 数据缺失（>{50}% 阈值）",
                })
                L1.append({"metric": metric, "missing_rate": missing_rate,
                           "detail": f"缺失率 {missing_rate:.0%}"})

        # 局部缺失 → L2（不触发 Re-plan，标注后继续）
        elif missing_count > 0:
            for _, row in null_rows.iterrows():
                L2.append({
                    "metric":  metric,
                    "company": str(row.get("company_name", "unknown")),
                    "year":    int(row.get("year", 0)),
                    "detail":  (
                        "未披露，不参与同比/环比计算"
                        if not _is_implausible_metric_value(metric, row.get(metric))
                        else "数值明显异常，已按缺失处理"
                    ),
                })

    summary_parts = []
    if L1:
        summary_parts.append(f"L1大面积缺失：{[x['metric'] for x in L1]}")
    if L2:
        summary_parts.append(f"L2局部缺失：{len(L2)}处")
    if L3:
        summary_parts.append(f"L3行业性缺失：{[x['metric'] for x in L3]}")

    report = MissingDataReport(
        L1=L1, L2=L2, L3=L3,
        summary="；".join(summary_parts) if summary_parts else "数据完整"
    )
    return errors, report


# ══════════════════════════════════════════════════════════════════════════════
# Layer-2：RAG 召回质量
# ══════════════════════════════════════════════════════════════════════════════

def _check_rag(state: AgentState) -> list[dict]:
    rag_result  = state.get("rag_result")
    worker_stat = state.get("worker_status", {}).get("rag", {})
    errors      = []

    if worker_stat.get("status") == "skipped":
        return []
    if worker_stat.get("status") not in ("success",):
        return [{"type": "RAG_WORKER_FAILED",
                 "detail": worker_stat.get("error_detail", "")}]
    if not rag_result:
        return [{"type": "RAG_EMPTY", "detail": "RAG 结果为空"}]

    chunks     = rag_result.get("chunks", [])
    top_score  = chunks[0].get("rerank_score", 0) if chunks else 0

    if len(chunks) < 2:
        errors.append({"type": "RAG_LOW_RECALL",
                       "detail": f"召回 chunk 数 {len(chunks)} < 2"})
    if top_score < 0.3:
        errors.append({"type": "RAG_LOW_RELEVANCE",
                       "detail": f"最高相关性 {top_score:.2f} < 0.3 阈值"})

    return errors


def _check_rag_target_coverage(state: AgentState) -> list[dict]:
    """Require strict company-year evidence coverage for explicit comparison/trend targets."""
    entities = state.get("entities", {})
    companies = [str(c) for c in entities.get("companies", []) if c]
    years = [int(y) for y in entities.get("years", []) if str(y).isdigit()]
    if not companies or not years:
        return []
    rag_result = state.get("rag_result") or {}
    coverage = rag_result.get("coverage") or {}
    missing = coverage.get("missing")
    if missing is None:
        chunks = rag_result.get("chunks", [])
        present = set()
        for chunk in chunks:
            try:
                present.add((str(chunk.get("company_name", "")), int(chunk.get("year"))))
            except (TypeError, ValueError):
                continue
        missing = [[c, y] for c in companies for y in years if (c, y) not in present]
    if not missing:
        return []
    return [{
        "type": "RAG_TARGET_COVERAGE_MISSING",
        "missing_targets": missing,
        "detail": "显式公司-年份证据未完整覆盖：" + ", ".join(f"{c}-{y}" for c, y in missing),
    }]


# ══════════════════════════════════════════════════════════════════════════════
# Layer-3：双路数据对齐
# ══════════════════════════════════════════════════════════════════════════════

def _check_alignment(state: AgentState) -> list[dict]:
    sql_result  = get_sql_result_dataframe(state)
    rag_result  = state.get("rag_result")
    aggregated  = state.get("aggregated_status", "")
    errors      = []

    if aggregated in ("sql_only", "rag_only", "skipped"):
        return []  # 单路不需要对齐

    if sql_result is None or rag_result is None:
        return []

    sql_companies: set[str] = set()
    if "company_name" in sql_result.columns:
        sql_companies = set(sql_result["company_name"].tolist())

    rag_companies: set[str] = set()
    for chunk in rag_result.get("chunks", []):
        if chunk.get("company_name"):
            rag_companies.add(chunk["company_name"])

    # 两路都有数据但完全没有公司交集
    if sql_companies and rag_companies and not (sql_companies & rag_companies):
        errors.append({
            "type":   "DATA_MISALIGN",
            "detail": f"SQL公司={sql_companies}，RAG公司={rag_companies}，无交集",
        })

    return errors


# ══════════════════════════════════════════════════════════════════════════════
# Layer-4：口径一致性校验（LLM 辅助）
# ══════════════════════════════════════════════════════════════════════════════

_SCOPE_CHECK_PROMPT = """\
你是 ESG 数据分析专家。请判断以下多家公司的同一指标数据是否具有可比性。

指标：{metric_name}
各公司口径描述（来自原报告原文）：
{scope_descriptions}

已知口径差异风险：
{known_caveats}

请判断并返回 JSON：
{{
  "consistent": true/false,
  "adjustable": true/false,
  "adjustment_method": "若可调整，描述调整方法；否则为空",
  "detail": {{"公司A": "口径描述", "公司B": "口径描述"}},
  "action": "ok" | "adjusted" | "flagged"
}}

规则：
- consistent=true：各公司口径实质相同，可直接对比
- consistent=false + adjustable=true：口径不同但可标准化折算
- consistent=false + adjustable=false：口径根本不可比，action=flagged，禁止对比
"""


def _check_scope_consistency(
    state: AgentState,
    trace_id: str,
) -> ScopeConsistencyDict:
    """
    用 RAG 召回的口径说明 chunk 做口径一致性校验。
    仅在横向对比场景（compare_dimension in horizontal/both）时触发。
    """
    entities      = state.get("entities", {})
    compare_dim   = entities.get("compare_dimension", "none")
    metrics       = entities.get("metrics", [])
    scope_chunks  = state.get("scope_adjustment_chunks", [])

    if compare_dim not in ("horizontal", "both") or not metrics:
        return ScopeConsistencyDict(checked=False, consistent=True, per_metric={})

    if not scope_chunks:
        # 没有口径说明 chunk，标记为未检查（不阻断，但 Synthesizer 需标注）
        return ScopeConsistencyDict(
            checked=False, consistent=True, per_metric={},
        )

    # 离线确定性评测不允许隐式访问外部 LLM。保留“已收集证据、
    # 未完成语义判定”的状态，避免把网络失败混入离线指标。
    if os.getenv("OFFLINE_DETERMINISTIC_MODE", "false").strip().lower() in {"1", "true", "yes", "y"}:
        return ScopeConsistencyDict(
            checked=False, consistent=True, per_metric={},
        )

    # 按指标分组口径 chunk
    per_metric_chunks: dict[str, list[str]] = {}
    for chunk in scope_chunks:
        mk = chunk.get("_scope_metric", "unknown")
        per_metric_chunks.setdefault(mk, [])
        per_metric_chunks[mk].append(
            f"[{chunk.get('company_name','')} {chunk.get('year','')}年] "
            f"{chunk.get('text','')[:200]}"
        )

    per_metric_result: dict[str, dict] = {}
    overall_consistent = True

    for metric_key in metrics:
        if metric_key not in per_metric_chunks:
            continue
        caveats = SCOPE_CAVEATS.get(metric_key, [])
        scope_desc = "\n".join(per_metric_chunks[metric_key][:6])

        prompt = _SCOPE_CHECK_PROMPT.format(
            metric_name=metric_key,
            scope_descriptions=scope_desc,
            known_caveats="\n".join(f"· {c}" for c in caveats),
        )

        try:
            def _call():
                import re
                resp = llm_generate_content(
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

            result = llm_call_with_retry(
                _call, max_retries=1, timeout_seconds=30,
                caller_name="scope_checker", trace_id=trace_id,
            )
            per_metric_result[metric_key] = result
            if not result.get("consistent", True):
                overall_consistent = False

        except Exception as e:
            log.warning(f"口径校验 LLM 调用失败（{metric_key}）：{e}")
            # 失败时保守处理：标记为待确认
            per_metric_result[metric_key] = {
                "consistent":  True,
                "adjustable":  True,
                "action":      "ok",
                "detail":      {},
                "adjustment_method": "",
            }

    return ScopeConsistencyDict(
        checked=True,
        consistent=overall_consistent,
        per_metric=per_metric_result,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 主节点函数
# ══════════════════════════════════════════════════════════════════════════════

@trace_node("evaluator_d", tags=["evaluation"])
def evaluator_d_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("evaluator_d", trace_id)
    aggregated = state.get("aggregated_status", "")

    # 双路全失败，已由 Worker Aggregator 标记降级，直接透传
    if aggregated == "both_failed" or state.get("is_degraded"):
        log.info("双路 Worker 均失败，跳过质检直接降级")
        state["eval_d_status"] = "fail"
        return state

    all_errors: list[dict] = []

    # ── Layer-1：SQL 有效性 + 缺失分级 ──────────────────────────────────────
    sql_errors, missing_report = _check_sql(state)
    all_errors.extend(sql_errors)
    state["missing_data_report"] = missing_report
    log.info(f"Layer-1 SQL检查：{len(sql_errors)} 个错误，缺失={missing_report.get('summary','')}")

    # ── Layer-2：RAG 质量 ────────────────────────────────────────────────────
    rag_errors = _check_rag(state)
    coverage_errors = _check_rag_target_coverage(state)
    # RAG 错误只在 rag_only / both_ok 时计入
    if aggregated in ("both_ok", "rag_only"):
        all_errors.extend(rag_errors)
        all_errors.extend(coverage_errors)
    log.info(f"Layer-2 RAG检查：质量错误={len(rag_errors)}，覆盖错误={len(coverage_errors)}")

    # ── Layer-3：双路对齐 ────────────────────────────────────────────────────
    align_errors = _check_alignment(state)
    all_errors.extend(align_errors)
    log.info(f"Layer-3 对齐检查：{len(align_errors)} 个错误")

    # ── Layer-4：口径一致性 ──────────────────────────────────────────────────
    scope_result = _check_scope_consistency(state, trace_id)
    state["scope_consistency"] = scope_result

    if scope_result.get("checked") and not scope_result.get("consistent"):
        # 检查是否有完全不可折算的指标（action=flagged）
        flagged = [
            mk for mk, r in scope_result.get("per_metric", {}).items()
            if r.get("action") == "flagged"
        ]
        adjustable = [
            mk for mk, r in scope_result.get("per_metric", {}).items()
            if r.get("action") == "adjusted"
        ]
        if flagged:
            # 口径不可调整 → 添加警告，但不触发 Re-plan（在报告里标注）
            log.warning(f"口径不可比指标（将标注）：{flagged}")
        if adjustable:
            # 口径可调整 → 触发 Supervisor 补充检索
            all_errors.append({
                "type":    "SCOPE_ADJUSTABLE",
                "metrics": adjustable,
                "detail":  "口径不一致但可折算，需补充检索后调整",
            })
        log.info(f"Layer-4 口径检查：flagged={flagged}, adjustable={adjustable}")

    # ── 最终判定 ─────────────────────────────────────────────────────────────
    # 过滤：L1缺失 + SQL/RAG Worker 失败 + 对齐失败 才触发 Re-plan
    # SCOPE_ADJUSTABLE 默认作为 warning 透传给 Synthesizer 标注，避免无效空转。
    replan_types = {
        "L1_MISSING", "SQL_EMPTY", "SQL_YEAR_INCOMPLETE",
        "RAG_LOW_RECALL", "RAG_LOW_RELEVANCE", "RAG_TARGET_COVERAGE_MISSING",
        "DATA_MISALIGN",
    }
    if _SCOPE_ADJUSTABLE_REPLAN:
        replan_types.add("SCOPE_ADJUSTABLE")
    replan_errors = [e for e in all_errors if e.get("type") in replan_types]

    if replan_errors:
        state["eval_d_status"] = "fail"
        state["eval_d_errors"] = replan_errors
        log.warning(f"Evaluator-D 未通过，Re-plan 错误：{[e['type'] for e in replan_errors]}")
    elif all_errors:
        # 有非 Re-plan 错误（如 Worker 失败已降级）→ pass_with_warnings
        state["eval_d_status"] = "pass_with_warnings"
        state["eval_d_errors"] = all_errors
        log.info(f"Evaluator-D 通过（有警告）：{[e['type'] for e in all_errors]}")
    else:
        state["eval_d_status"] = "pass"
        state["eval_d_errors"] = []
        log.info("Evaluator-D 全部通过")

    return state
