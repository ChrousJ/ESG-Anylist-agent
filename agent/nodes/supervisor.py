"""
agent/nodes/supervisor.py  —  Supervisor 总控调度器（流程的第 ② 步）
====================================================================

【在流程中的位置】context → ★supervisor★ → schema_injector
                   evaluator_d → ★supervisor★ (Re-plan 循环)

【这个节点干什么？】

Supervisor 是整个分析流程的"指挥官"，它做两件关键决策：

  1. 首次规划（Planning）：
     根据 context 节点提取的实体信息，决定数据获取策略：
     - "sql_only"：只查数据库（适合纯数字查询，如"比亚迪碳排放多少"）
     - "rag_only"：只查报告（适合定性分析，如"碳中和策略"）
     - "parallel"：同时查数据库和报告（最完整，大多数情况会走这条路）

  2. Re-plan（重新规划）：
     当 evaluator_d 发现数据质量不达标时（如 SQL 返回空、RAG 召回率低），
     会把控制权打回给 Supervisor。Supervisor 会调整策略，比如：
     - 放宽年份范围
     - 降低 RAG 相关性阈值
     - 切换到只用 RAG

  此外，Supervisor 还会注入"实质性议题"（Materiality Topics）——
  根据行业（如新能源），注入行业特有的 ESG 议题（如"电池全生命周期碳足迹"），
  帮助 RAG Worker 检索到更相关的内容。

【写入 State 的关键字段】

  - plan: PlanDict（执行策略：用哪些 Worker、怎么检索）
  - materiality_topics: 行业实质性议题列表
  - retry_count: 重试计数器
  - is_degraded: 超限后标记为降级
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache

from agent.state import (
    AgentState,
    PlanDict,
    WorkerStatusDict,
    increment_retry,
    get_retry_count,
    get_sql_result_dataframe,
)
from agent.tracing import trace_node, TraceLogger
from agent.materiality import (
    build_materiality_query_variants,
    get_topics_for_industry,
    get_topics_for_metrics,
)

log = logging.getLogger(__name__)

# 各 Worker 最大重试次数
MAX_SQL_RETRY  = 2
MAX_RAG_RETRY  = 2
MAX_TOTAL_REPLAN = 4   # 超过此数直接降级
_DB_PATH = os.getenv("DB_PATH", os.path.join("data", "esg_data.db"))
_INDUSTRY_TABLES: dict[str, str] = {
    "new_energy": "esg_auto_metrics",
    "bank": "esg_banking_metrics",
    "power": "esg_power_metrics",
}
_TERMINAL_COVERAGE_ERRORS = {
    "SQL_EMPTY",
    "L1_MISSING",
    "RAG_LOW_RECALL",
    "RAG_LOW_RELEVANCE",
}


def _skipped_worker_status() -> WorkerStatusDict:
    return WorkerStatusDict(
        status="skipped",
        error_type="",
        error_detail="",
        latency_ms=0,
        retried=False,
    )


@lru_cache(maxsize=1)
def _get_supported_companies() -> set[str]:
    """加载结构化库当前覆盖的公司集合，用于识别 coverage gap。"""
    supported: set[str] = set()
    try:
        conn = sqlite3.connect(_DB_PATH)
    except Exception:
        return supported

    try:
        for table_name in _INDUSTRY_TABLES.values():
            try:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT company_name
                    FROM {table_name}
                    WHERE company_name IS NOT NULL AND TRIM(company_name) <> ''
                    """
                ).fetchall()
            except Exception:
                continue
            supported.update(str(row[0]).strip() for row in rows if row and row[0])
    finally:
        conn.close()

    return supported


def _classify_terminal_response(
    state: AgentState,
    error_type: str,
) -> tuple[str, str] | None:
    """
    当失败原因是“证据确实不存在/当前库不覆盖”时，直接走可控终态响应，
    不再继续 replan 空转到 generic degraded。
    """
    if error_type not in _TERMINAL_COVERAGE_ERRORS:
        return None

    entities = state.get("entities", {}) or {}
    companies = [c for c in entities.get("companies", []) if c]
    metrics = [m for m in entities.get("metrics", []) if m]
    if not companies:
        return None

    worker_status = state.get("worker_status", {}) or {}
    sql_attempted = (
        worker_status.get("sql", {}).get("status") not in ("", None, "skipped")
        or get_retry_count(state, "sql") > 0
    )
    rag_attempted = (
        worker_status.get("rag", {}).get("status") not in ("", None, "skipped")
        or get_retry_count(state, "rag") > 0
    )
    if not (sql_attempted and rag_attempted):
        return None

    sql_result = get_sql_result_dataframe(state)
    sql_empty = sql_result is None or (hasattr(sql_result, "__len__") and len(sql_result) == 0)
    rag_result = state.get("rag_result") if isinstance(state.get("rag_result"), dict) else {}
    rag_chunks = rag_result.get("chunks", []) if isinstance(rag_result, dict) else []
    rag_empty = len(rag_chunks) == 0
    if not (sql_empty and rag_empty):
        return None

    supported_companies = set(entities.get("supported_companies", []))
    unsupported_companies = set(entities.get("unsupported_companies", []))
    if not (supported_companies or unsupported_companies):
        supported_set = _get_supported_companies()
        supported_companies = {c for c in companies if c in supported_set}
        unsupported_companies = set(companies) - supported_companies

    if unsupported_companies and not supported_companies:
        company_text = "、".join(sorted(unsupported_companies))
        return (
            "coverage_gap",
            f"当前项目数据集未覆盖公司：{company_text}；结构化库与报告检索均无可用证据。",
        )

    if supported_companies and metrics:
        company_text = "、".join(sorted(supported_companies))
        metric_text = "、".join(metrics[:3])
        return (
            "not_disclosed",
            f"已覆盖公司 {company_text} 在当前年份/指标范围内缺少可用证据：{metric_text}。",
        )

    return None

# ══════════════════════════════════════════════════════════════════════════════
# 数据源策略决策矩阵
# ══════════════════════════════════════════════════════════════════════════════

# 纯定性指标（RAG-only，不需要 SQL）
_QUALITATIVE_METRICS = {
    "esg_committee_setup",
    "external_esg_rating",
    "anti_corruption_coverage",  # 有时只是定性描述
}

# 纯定量指标（SQL-first）
_QUANTITATIVE_METRICS = {
    "scope_1_emissions", "scope_2_emissions", "scope_3_emissions",
    "total_energy_consumption", "energy_intensity",
    "green_finance_balance", "inclusive_finance_balance",
    "rd_investment_total", "supplier_esg_audit_ratio",
    "employee_training_hours", "safety_accidents_count",
    "customer_complaint_res", "charitable_donations",
    "independent_director_ratio", "female_director_ratio",
    "regulatory_penalties", "clean_energy_ratio",
}


def _decide_strategy(entities: dict, replan_error: dict | None = None) -> str:
    """
    根据实体信息决定数据源策略。
    返回 "sql_only" | "rag_only" | "parallel"
    """
    intent  = entities.get("intent", "qa")
    metrics = set(entities.get("metrics", []))

    # Re-plan 场景：强制切换策略
    if replan_error:
        error_type = replan_error.get("type", "")
        if error_type in ("SQL_EMPTY", "L1_MISSING"):
            return "rag_only"       # SQL 拿不到数据，降级用 RAG
        if error_type == "RAG_LOW_RELEVANCE":
            return "sql_only"       # RAG 质量差，只用 SQL
        if error_type == "RAG_LOW_RECALL":
            return "parallel"       # 保留 SQL，并对 RAG 做召回兜底

    # 定性问答 → 纯 RAG
    if intent == "qa":
        if not metrics or metrics.issubset(_QUALITATIVE_METRICS):
            return "rag_only"

    # 有明确定量指标 → SQL 为主（但 RAG 补充背景，即并行）
    if metrics & _QUANTITATIVE_METRICS:
        return "parallel"

    # trend / compare / ranking → 需要 SQL 数值
    if intent in ("trend", "compare", "ranking"):
        return "parallel"

    # summary → 双路
    if intent == "summary":
        return "parallel"

    # 兜底：并行
    return "parallel"


def _expand_years_for_replan(years: list[int]) -> list[int]:
    """Re-plan 时放宽年份范围（向前扩展一年）。"""
    if not years:
        return [2022, 2023, 2024]
    min_y = max(2022, min(years) - 1)
    max_y = min(2024, max(years))
    return list(range(min_y, max_y + 1))


def _intersect_entities(
    entities: dict,
    sql_result,
    rag_result: dict | None,
) -> dict:
    """
    DATA_MISALIGN 时缩小分析范围到数据交集。
    """
    import pandas as pd
    new_entities = dict(entities)

    sql_companies: set[str] = set()
    sql_years:     set[int] = set()

    if sql_result is not None and len(sql_result) > 0:
        if "company_name" in sql_result.columns:
            sql_companies = set(sql_result["company_name"].tolist())
        if "year" in sql_result.columns:
            sql_years = set(int(y) for y in sql_result["year"].tolist())

    rag_companies: set[str] = set()
    rag_years:     set[int] = set()
    if rag_result:
        for chunk in rag_result.get("chunks", []):
            if chunk.get("company_name"):
                rag_companies.add(chunk["company_name"])
            if chunk.get("year"):
                rag_years.add(int(chunk["year"]))

    # 取交集（如果某路数据为空，跳过该维度）
    if sql_companies and rag_companies:
        new_entities["companies"] = list(sql_companies & rag_companies)
    if sql_years and rag_years:
        new_entities["years"] = sorted(sql_years & rag_years)

    return new_entities


# ══════════════════════════════════════════════════════════════════════════════
# 主节点函数
# ══════════════════════════════════════════════════════════════════════════════

@trace_node("supervisor", tags=["planning"])
def supervisor_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("supervisor", trace_id)
    state["terminal_response_mode"] = ""
    state["terminal_response_reason"] = ""

    entities       = state.get("entities", {})
    eval_d_errors  = state.get("eval_d_errors", [])
    replan_history = state.get("replan_history", [])

    # ── 判断是首次规划还是 Re-plan ───────────────────────────────────────────
    is_replan = bool(eval_d_errors)

    if is_replan:
        log.info(f"Re-plan 触发，错误：{eval_d_errors}")
        replan_error = eval_d_errors[0] if eval_d_errors else {}
        error_type   = replan_error.get("type", "")

        terminal_response = _classify_terminal_response(state, error_type)
        if terminal_response:
            mode, reason = terminal_response
            log.warning(f"识别为可控终态响应：{mode}，停止继续 Re-plan")
            state["terminal_response_mode"] = mode
            state["terminal_response_reason"] = reason
            state["eval_d_errors"] = []
            state["is_degraded"] = False
            state["degraded_reason"] = ""
            return state

        # 防止循环 Re-plan
        if len(replan_history) >= MAX_TOTAL_REPLAN:
            log.warning(f"Re-plan 次数超限（{len(replan_history)}次），路由到降级")
            state["is_degraded"]    = True
            state["degraded_reason"] = (
                f"已尝试 {len(replan_history)} 次策略调整，数据仍不满足要求。"
                f"最后错误：{error_type}"
            )
            return state

        # 根据错误类型调整实体/策略
        if error_type == "SQL_EMPTY":
            # 放宽年份
            new_years = _expand_years_for_replan(entities.get("years", []))
            entities  = {**entities, "years": new_years}
            reason    = f"SQL_EMPTY → 年份范围扩展至 {new_years}"
            state["retry_count"] = increment_retry(state, "sql")
            log.info(reason)

        elif error_type == "L1_MISSING":
            # NOTE: L1 缺失优先走 RAG 召回兜底，减少直接 degraded。
            new_years = _expand_years_for_replan(entities.get("years", []))
            entities = {**entities, "years": new_years}
            reason = f"L1_MISSING → 切换 RAG 召回兜底并放宽年份至 {new_years}"
            state["retry_count"] = increment_retry(state, "rag")
            log.info(reason)

        elif error_type == "RAG_LOW_RECALL":
            # NOTE: 召回不足时不直接放弃 RAG，进入 recall_fallback 扩召回。
            reason = "RAG_LOW_RECALL → 启用 recall_fallback 扩召回策略"
            state["retry_count"] = increment_retry(state, "rag")
            log.info(reason)

        elif error_type == "RAG_LOW_RELEVANCE":
            # 降低检索阈值（通过 plan 里的 rag_strategy 传递给 RAG Worker）
            reason = "RAG_LOW_RELEVANCE → 切换 relaxed 检索策略"
            state["retry_count"] = increment_retry(state, "rag")
            log.info(reason)

        elif error_type == "DATA_MISALIGN":
            # 缩小到数据交集
            entities = _intersect_entities(
                entities,
                get_sql_result_dataframe(state),
                state.get("rag_result"),
            )
            reason = f"DATA_MISALIGN → 缩小分析范围至交集 {entities.get('companies')}"
            log.info(reason)

        else:
            reason = f"未知错误类型 {error_type}，保持原策略重试"

        state["entities"]        = entities
        state["replan_history"]  = replan_history + [reason]
        state["eval_d_errors"]   = []   # 清空错误信号，开始新一轮

    else:
        log.info("首次规划")

    # ── 决定数据源策略 ───────────────────────────────────────────────────────
    replan_error_for_strategy = eval_d_errors[0] if (is_replan and eval_d_errors) else None
    strategy = _decide_strategy(entities, replan_error_for_strategy)

    # Re-plan 时强制使用调整后的策略
    if is_replan:
        error_type = (eval_d_errors[0].get("type", "") if eval_d_errors else "")
        if error_type in ("SQL_EMPTY", "L1_MISSING"):
            strategy = "rag_only"
        elif error_type == "RAG_LOW_RECALL":
            strategy = "parallel"
        elif error_type == "RAG_LOW_RELEVANCE":
            strategy = "sql_only"

    workers = []
    if strategy == "sql_only":
        workers = ["sql"]
    elif strategy == "rag_only":
        workers = ["rag"]
    else:
        workers = ["sql", "rag"]

    # Re-plan 切换数据源后，主动清理被禁用 worker 的旧结果，避免后续评估读到脏状态。
    worker_status = dict(state.get("worker_status", {}))
    if "sql" not in workers:
        state["sql_result"] = None
        state["sql_query_executed"] = ""
        worker_status["sql"] = _skipped_worker_status()
    if "rag" not in workers:
        state["rag_result"] = None
        state["scope_adjustment_chunks"] = []
        worker_status["rag"] = _skipped_worker_status()
    if worker_status:
        state["worker_status"] = worker_status

    # ── 行业实质性议题注入 ───────────────────────────────────────────────────
    industry  = entities.get("industry", "")
    metrics   = entities.get("metrics", [])
    query     = state.get("resolved_query", state.get("user_query", ""))

    materiality_variants = build_materiality_query_variants(
        original_query=query,
        industry=industry,
        metric_keys=metrics if metrics else None,
        max_topics=4,
    )

    # 提取议题名称列表（存入 state 供 Synthesizer 使用）
    if metrics:
        topics = get_topics_for_metrics(metrics)
    else:
        topics = get_topics_for_industry(industry, priority_threshold=1)

    materiality_topics = [t.name for t in topics[:6]]

    # ── 是否需要规模归一化 ───────────────────────────────────────────────────
    compare_dimension  = entities.get("compare_dimension", "none")
    normalization_needed = (
        compare_dimension in ("horizontal", "both")
        and bool(entities.get("companies", []))
        and len(entities.get("companies", [])) > 1
    )

    # ── 组装执行计划 ─────────────────────────────────────────────────────────
    replan_type_for_rag = (eval_d_errors[0].get("type", "") if (is_replan and eval_d_errors) else "")
    # NOTE: expose recall_fallback to RAG worker for L1_MISSING / LOW_RECALL cases.
    rag_strategy = "standard"
    if replan_type_for_rag == "RAG_LOW_RELEVANCE":
        rag_strategy = "relaxed"
    elif replan_type_for_rag in ("RAG_LOW_RECALL", "L1_MISSING"):
        rag_strategy = "recall_fallback"

    plan = PlanDict(
        workers=workers,
        strategy=strategy,
        rag_strategy=rag_strategy,
        replan_reason=(replan_history[-1] if replan_history else ""),
        schema_tables=(
            ["esg_universal_metrics"]
            + (["esg_banking_metrics"] if industry == "bank" else [])
            + (["esg_auto_metrics"]    if industry == "new_energy" else [])
            + (["esg_power_metrics"]   if industry == "power" else [])
        ),
        normalization_needed=normalization_needed,
    )

    log.info(
        "规划完成",
        {
            "strategy":   strategy,
            "workers":    workers,
            "topics":     materiality_topics[:3],
            "replan":     is_replan,
            "normalize":  normalization_needed,
        },
    )

    # ── 把议题变体存到 state，RAG Worker 直接读 ──────────────────────────────
    # 借用 resolved_query 扩展字段，实际用单独字段存
    state["plan"]               = plan
    state["materiality_topics"] = materiality_topics
    state["entities"]           = entities

    # 把 materiality_variants 挂在 plan 里传给 RAG Worker
    state["plan"]["_materiality_variants"] = materiality_variants

    return state
