"""
agent/state.py  —  ESG Agent 全局状态定义
==========================================

本文件定义了整个 Agent 系统中最核心的数据结构 —— AgentState。
在 LangGraph 框架中，所有"节点"（node）共享同一个 State 对象。
每个节点读取 State 中需要的字段、做处理、然后把结果写回 State。
这样，数据就像流水线上的产品一样，经过每道工序被逐步加工。

【什么是 LangGraph？】

LangGraph 是 LangChain 团队开发的一个框架，用来构建"有状态的多步骤 AI 工作流"。
核心概念：
  - Graph（图）：定义节点之间的连接关系和执行顺序
  - Node（节点）：每个节点是一个 Python 函数，接收 State → 返回修改后的 State
  - Edge（边）：定义节点之间的执行顺序，支持条件分支和并行执行
  - State（状态）：唯一的数据容器，在所有节点之间传递

【为什么用 TypedDict？】

TypedDict 是 Python 的类型注解工具。与普通 dict 不同，TypedDict 让我们：
  1. 明确定义每个字段的名称和类型 → IDE 能自动补全
  2. 便于团队协作 → 一看就知道 State 里有什么字段
  3. total=False 表示所有字段都是可选的 → 节点只需写自己负责的字段

【State 的字段分组】

  A. 请求标识       — trace_id / conversation_id / 时间戳（用于追踪和日志）
  B. 上下文理解     — 实体抽取 / 问题分类 / 实质性议题（Context 节点填写）
  C. 执行计划       — Supervisor 生成的计划 + 重试计数
  D. Schema注入     — 数据字典上下文，供 SQL Worker 消费（Schema Injector 填写）
  E. Worker 结果    — SQL / RAG 各自的执行状态与返回数据
  F. 数据质检       — Evaluator-D 的各项校验结果
  G. Map-Reduce     — 大文本压缩节点的统计与输出
  H. 合成输出       — Synthesizer 生成的四层分析报告
  I. 输出质检       — Evaluator-O 的校验结果
  J. 记忆与响应     — 对话历史 / 降级标记 / 节点执行追踪

【Annotated 字段的特殊含义】

某些字段使用 Annotated[type, reducer_func] 标注，这是 LangGraph 的"归约器"机制：
  - 普通字段：新值直接覆盖旧值（最后写入者胜）
  - Annotated 字段：新值和旧值通过 reducer_func 合并
  例如：node_trace 使用 operator.add，意思是新的追踪条目会 *追加* 到列表尾部，
  而不是覆盖整个列表。这在并行执行（SQL + RAG 同时写 worker_status）时特别重要。
"""

from __future__ import annotations

import operator
from typing import Any, Optional, Annotated
from typing_extensions import TypedDict, NotRequired


# ══════════════════════════════════════════════════════════════════════════════
# 辅助子类型（嵌套在 AgentState 中的结构化字段）
# ══════════════════════════════════════════════════════════════════════════════

class EntityDict(TypedDict, total=False):
    """上下文理解节点提取的实体信息。"""
    companies:  list[str]    # ["比亚迪", "宁德时代"]
    years:      list[int]    # [2022, 2023, 2024]
    metrics:    list[str]    # ["scope_1_emissions", "green_finance_balance"]
    intent:     str          # "trend" | "compare" | "qa" | "summary" | "ranking"
    industry:   str          # "new_energy" | "power" | "bank" | "mixed"
    compare_dimension: str   # "vertical"（纵向）| "horizontal"（横向）| "both" | "none"


class PlanDict(TypedDict, total=False):
    """Supervisor 生成的执行计划。"""
    workers:          list[str]   # ["sql"] | ["rag"] | ["sql", "rag"]
    strategy:         str         # "sql_only" | "rag_only" | "parallel"
    rag_strategy:     str         # "standard" | "relaxed"（Re-plan 时降低阈值）
    replan_reason:    str         # Re-plan 时记录原因
    schema_tables:    list[str]   # Schema Injector 需要检索的表名
    normalization_needed: bool    # 是否需要规模归一化


class WorkerStatusDict(TypedDict, total=False):
    """单个 Worker 的执行状态记录。"""
    status:       str    # "success" | "failed" | "timeout" | "skipped"
    error_type:   str    # "DB_CONN_FAIL" | "SQL_ERROR" | "TIMEOUT" | "UNKNOWN"
    error_detail: str    # 原始异常信息
    latency_ms:   int    # 执行耗时（毫秒）
    retried:      bool   # 是否经过了重试


class MissingDataReport(TypedDict, total=False):
    """
    Evaluator-D 的缺失数据分级报告。
    L1 → 大面积缺失，触发 Re-plan
    L2 → 局部缺失，标注后继续
    L3 → 行业性缺失，直接跳过并说明
    """
    L1: list[dict]   # [{"metric": str, "missing_rate": float, "detail": str}]
    L2: list[dict]   # [{"metric": str, "company": str, "year": int}]
    L3: list[dict]   # [{"metric": str, "industry_missing_rate": float}]
    summary: str     # 给 Synthesizer 用的缺失摘要文本


class ScopeConsistencyDict(TypedDict, total=False):
    """
    口径一致性校验结果。
    per_metric 存每个指标的一致性状态。
    """
    checked:     bool
    consistent:  bool          # 全部一致则为 True
    per_metric:  dict[str, dict]
    # per_metric 格式：
    # {
    #   "carbon_emission": {
    #     "consistent": False,
    #     "adjustable": True,
    #     "adjustment_method": "按运营边界重新计算",
    #     "detail": {"比亚迪": "全口径含供应链", "宁德时代": "仅运营边界"},
    #     "action": "adjusted" | "flagged" | "ok"
    #   }
    # }


class MapReduceStats(TypedDict, total=False):
    """Map-Reduce 压缩节点的执行统计。"""
    triggered:          bool
    original_tokens:    int
    compressed_tokens:  int
    compression_ratio:  float
    summaries_count:    int
    map_latency_ms:     int
    reduce_latency_ms:  int


class NormalizationRecord(TypedDict, total=False):
    """规模归一化方案记录（每个指标一条）。"""
    metric:        str    # "scope_1_emissions"
    denominator:   str    # "营业收入" | "员工总数" | "产量"
    denom_source:  str    # "sql" | "rag" | "not_found"
    formula:       str    # "scope_1_emissions(tCO2e) / 营业收入(亿元)"
    applied:       bool   # 是否成功应用归一化
    fallback_note: str    # 归一化失败时的说明


class NodeTraceEntry(TypedDict, total=False):
    """单个节点的执行记录，绑定 trace_id。"""
    node_name:    str
    started_at:   str    # ISO 8601 时间戳
    finished_at:  str
    duration_ms:  int
    status:       str    # "success" | "failed" | "skipped"
    decision:     str    # 节点做了什么关键决策（自然语言摘要）
    error:        str    # 如有异常


def update_worker_status(left: dict, right: dict) -> dict:
    res = dict(left or {})
    res.update(right or {})
    return res

def update_retry_count(left: dict, right: dict) -> dict:
    res = dict(left or {})
    res.update(right or {})
    return res


SQL_RESULT_TYPE = "dataframe"


def serialize_sql_result(value: Any) -> Any:
    if value is None:
        return None
    try:
        import pandas as pd
        if isinstance(value, pd.DataFrame):
            return {
                "_type": SQL_RESULT_TYPE,
                "data": value.to_dict(orient="records"),
                "columns": list(value.columns),
            }
    except Exception:
        pass
    return value


def _coerce_dataframe_from_records(
    records: list[dict],
    columns: list[str] | None = None,
) -> Any:
    try:
        import pandas as pd
        df = pd.DataFrame(records or [])
        if columns:
            existing = [c for c in columns if c in df.columns]
            if existing:
                df = df[existing]
        return df
    except Exception:
        return records


def deserialize_sql_result(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict) and value.get("_type") == SQL_RESULT_TYPE:
        data = value.get("data", [])
        cols = value.get("columns", [])
        return _coerce_dataframe_from_records(list(data), list(cols))
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return _coerce_dataframe_from_records(value)
    return value


def get_sql_result_dataframe(state: "AgentState") -> Any:
    raw = state.get("sql_result")
    return deserialize_sql_result(raw)

# ══════════════════════════════════════════════════════════════════════════════
# 核心：AgentState
# ══════════════════════════════════════════════════════════════════════════════

class AgentState(TypedDict, total=False):
    """
    流经 LangGraph 所有节点的全局状态容器。

    total=False：所有字段均为可选（有默认值），
    节点只需写自己负责的字段，其余字段保持原值透传。
    """

    # ── A. 请求标识 ─────────────────────────────────────────────────────────
    trace_id:           str
    # 格式："esg-{YYYYMMDD}-{6位随机hex}"，全局唯一，绑定所有 Span 和日志

    conversation_id:    str
    # 多轮对话 ID，跨请求持久化（存 Redis）

    request_id:         str
    # 单次 HTTP 请求 ID（FastAPI 层生成，与 trace_id 1:1 对应）

    started_at:         str
    # 整个请求的开始时间，ISO 8601

    # ── B. 上下文理解 ────────────────────────────────────────────────────────
    user_query:         str
    # 用户原始输入，不做任何修改

    resolved_query:     str
    # 指代消解后的完整问题（多轮追问时替换指代词）

    query_class:        str
    # "knowledge"  → 纯知识问题，直接 LLM 回答
    # "complex"    → 业务场景问题，走完整流程
    # "clarify"    → 实体信息不足，需要反问

    entities:           EntityDict
    # 结构化实体抽取结果

    need_clarify:       bool
    # True 时生成反问，False 时继续流程

    clarify_question:   str
    # 反问节点生成的问题文本

    materiality_topics: list[str]
    # Supervisor 根据行业注入的实质性议题列表
    # 示例：["电池回收与生命周期碳足迹", "供应链劳工标准", "碳排放Scope3"]

    # ── C. 执行计划 ──────────────────────────────────────────────────────────
    plan:               PlanDict
    # Supervisor 生成的执行计划

    retry_count:        Annotated[dict[str, int], update_retry_count]
    # {"sql": 0, "rag": 0, "synth": 0}，各环节独立计数，上限在各节点硬编码

    replan_history:     list[str]
    # Re-plan 原因的历史记录，防止循环 Re-plan
    # 示例：["SQL_EMPTY → 放宽年份范围", "RAG_LOW_RELEVANCE → 降低阈值"]

    # ── D. Schema 注入 ───────────────────────────────────────────────────────
    schema_context:     str
    # Schema Injector 组装的完整 prompt 片段，直接拼入 SQL Worker 的 prompt
    # 包含：表结构 + 字段语义 + 计算公式 + 已知缺失 + Few-Shot 样例

    known_missing_preview: list[dict]
    # Schema Injector 从 missing_data_log 预查的已知缺失
    # [{"company": "比亚迪", "year": 2022, "metric": "scope_3_emissions"}]

    # ── E. Worker 结果 ───────────────────────────────────────────────────────
    worker_status:      Annotated[dict[str, WorkerStatusDict], update_worker_status]
    # {"sql": WorkerStatusDict, "rag": WorkerStatusDict}

    sql_result:         Optional[Any]
    # pandas DataFrame 或 None
    # None 有两种含义：未执行（skipped）或执行失败（check worker_status）

    sql_query_executed: str
    # 实际执行的 SQL 语句，用于溯源和调试

    rag_result:         Optional[dict]
    # retrieve() 的完整返回值
    # {chunks, query_variants, vector_count, bm25_count, final_count, low_relevance}

    aggregated_status:  str
    # Worker Aggregator 的聚合决策
    # "both_ok" | "sql_only" | "rag_only" | "both_failed" | "degraded"

    # ── F. 数据质检（Evaluator-D） ───────────────────────────────────────────
    eval_d_status:      str
    # "pass" | "fail" | "pass_with_warnings"

    eval_d_errors:      list[dict]
    # 失败时的错误列表
    # [{"type": "L1_MISSING", "metric": str, "detail": str}]

    missing_data_report: MissingDataReport
    # 三级缺失分类报告（不论通过与否都填写，供 Synthesizer 生成风险提示）

    scope_consistency:  ScopeConsistencyDict
    # 口径一致性校验结果

    scope_adjustment_chunks: list[dict]
    # 口径补充检索后，RAG 返回的口径说明 chunks
    # （Worker Aggregator 触发补充检索后写入）

    # ── G. Map-Reduce 压缩节点 ───────────────────────────────────────────────
    map_reduce_applied: bool
    # True 表示本次请求触发了 Map-Reduce 压缩

    compressed_context: Optional[str]
    # Reduce 阶段输出的高密度上下文字符串，直接喂给 Synthesizer
    # 若 map_reduce_applied=False，则 Synthesizer 直接用原始 rag_result

    map_reduce_stats:   Optional[MapReduceStats]

    # ── H. 合成输出（Synthesizer） ───────────────────────────────────────────
    normalization_applied: list[NormalizationRecord]
    # 记录每个指标的归一化方案（有则记录，无则空列表）

    analysis:           str
    # MSCI 四层 Markdown 格式报告正文

    chart_spec:         Optional[dict]
    # Plotly JSON Spec
    # {
    #   "type": "line" | "bar" | "radar" | "grouped_bar",
    #   "title": str,
    #   "x_axis": list,
    #   "series": [{"name": str, "data": list, "unit": str}]
    # }

    table_data:         Optional[dict]
    # 原始数据表（JSON格式，供前端渲染）
    # {"columns": [...], "rows": [...], "footnotes": [...]}

    sources:            list[dict]
    # 来源溯源列表
    # [{"type": "sql"/"rag", "company": str, "year": int,
    #   "page": str, "file": str, "excerpt": str}]

    key_findings:       list[str]
    # Synthesizer 提炼的 3~5 条关键发现（用于前端摘要展示）

    # ── I. 输出质检（Evaluator-O） ───────────────────────────────────────────
    eval_o_status:      str
    # "pass" | "fail"

    eval_o_errors:      list[dict]
    # 失败时的具体错误定位
    # [{"type": "NUMBER_HALLUCINATION"/"MISSING_LAYER"/"ENTITY_HALLUCINATION",
    #   "detail": str, "location": str}]

    eval_o_retry_count: int
    # Evaluator-O → Synthesizer 的局部修正次数（上限2次）

    # ── J. 记忆与响应 ────────────────────────────────────────────────────────
    history:            list[dict]
    # 对话历史，最近10轮
    # [{"role": "user"/"assistant", "content": str, "turn_id": int}]

    user_preferences:   dict
    # 从长期记忆读取的用户偏好
    # {"preferred_companies": [...], "preferred_metrics": [...]}

    is_degraded:        bool
    # True 表示本次响应走了降级路径（数据不足/全部 Worker 失败）

    degraded_reason:    str
    # 降级原因说明，透传给用户

    node_trace:         Annotated[list[NodeTraceEntry], operator.add]
    # 所有节点的执行记录（TraceID 绑定），用于可观测性

    langsmith_run_url:  str
    # LangSmith 本次请求的追踪链接
    # "https://smith.langchain.com/projects/esg-agent/runs/{run_id}"


# ══════════════════════════════════════════════════════════════════════════════
# 状态初始化工厂函数
# ══════════════════════════════════════════════════════════════════════════════

def make_initial_state(
    user_query: str,
    conversation_id: str,
    trace_id: str,
    request_id: str,
    history: list[dict] | None = None,
    user_preferences: dict | None = None,
) -> AgentState:
    """
    创建一个带默认值的初始 AgentState。
    FastAPI 层在收到请求后调用此函数，然后传入 LangGraph。
    """
    from datetime import datetime, timezone

    return AgentState(
        # A. 请求标识
        trace_id=trace_id,
        conversation_id=conversation_id,
        request_id=request_id,
        started_at=datetime.now(timezone.utc).isoformat(),

        # B. 上下文理解
        user_query=user_query,
        resolved_query=user_query,   # 初始等于原始 query，context 节点会更新
        query_class="",
        entities=EntityDict(),
        need_clarify=False,
        clarify_question="",
        materiality_topics=[],

        # C. 执行计划
        plan=PlanDict(),
        retry_count={"sql": 0, "rag": 0, "synth": 0},
        replan_history=[],

        # D. Schema 注入
        schema_context="",
        known_missing_preview=[],

        # E. Worker 结果
        worker_status={},
        sql_result=None,
        sql_query_executed="",
        rag_result=None,
        aggregated_status="",

        # F. 数据质检
        eval_d_status="",
        eval_d_errors=[],
        missing_data_report=MissingDataReport(L1=[], L2=[], L3=[], summary=""),
        scope_consistency=ScopeConsistencyDict(
            checked=False, consistent=True, per_metric={}
        ),
        scope_adjustment_chunks=[],

        # G. Map-Reduce
        map_reduce_applied=False,
        compressed_context=None,
        map_reduce_stats=None,

        # H. 合成输出
        normalization_applied=[],
        analysis="",
        chart_spec=None,
        table_data=None,
        sources=[],
        key_findings=[],

        # I. 输出质检
        eval_o_status="",
        eval_o_errors=[],
        eval_o_retry_count=0,

        # J. 记忆与响应
        history=history or [],
        user_preferences=user_preferences or {},
        is_degraded=False,
        degraded_reason="",
        node_trace=[],
        langsmith_run_url="",
    )


# ══════════════════════════════════════════════════════════════════════════════
# 工具函数：节点内使用
# ══════════════════════════════════════════════════════════════════════════════

def append_node_trace(
    state: AgentState,
    node_name: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    status: str,
    decision: str = "",
    error: str = "",
) -> list[NodeTraceEntry]:
    """
    返回追加了新条目的 node_trace 列表。
    用法：state["node_trace"] = append_node_trace(state, ...)
    """
    entry = NodeTraceEntry(
        node_name=node_name,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        status=status,
        decision=decision,
        error=error,
    )
    return list(state.get("node_trace", [])) + [entry]


def increment_retry(state: AgentState, worker: str) -> dict[str, int]:
    """
    返回更新后的 retry_count 字典。
    用法：state["retry_count"] = increment_retry(state, "sql")
    """
    counts = dict(state.get("retry_count", {"sql": 0, "rag": 0, "synth": 0}))
    counts[worker] = counts.get(worker, 0) + 1
    return counts


def get_retry_count(state: AgentState, worker: str) -> int:
    """获取某个 Worker 的当前重试次数。"""
    return state.get("retry_count", {}).get(worker, 0)


def is_worker_ok(state: AgentState, worker: str) -> bool:
    """判断某个 Worker 是否执行成功。"""
    return state.get("worker_status", {}).get(worker, {}).get("status") == "success"


def has_sql_data(state: AgentState) -> bool:
    """SQL 结果是否有效（非 None 且有行数据）。"""
    result = get_sql_result_dataframe(state)
    if result is None:
        return False
    try:
        return len(result) > 0
    except Exception:
        return False


def has_rag_data(state: AgentState) -> bool:
    """RAG 结果是否有效（有 chunks 且非空）。"""
    result = state.get("rag_result")
    if not result:
        return False
    return len(result.get("chunks", [])) > 0


DEGRADED_REASON_CODES = {
    "WORKERS_BOTH_FAILED": "both sql and rag failed",
    "WORKER_FAILED": "one worker failed",
    "EVAL_O_MAX_RETRY": "evaluator_o max retry reached",
    "OUT_OF_SCOPE": "query out of ESG scope",
    "UNKNOWN": "unknown failure",
}


def format_degraded_reason(reason_code: str, detail: str = "") -> str:
    if detail:
        return f"{reason_code}: {detail}"
    return reason_code


def build_degraded_message(
    query: str,
    reason_code: str,
    reason_detail: str = "",
    trace_id: str = "",
    partial_analysis: str = "",
) -> str:
    lines: list[str] = []
    lines.append("**Degraded Response**")
    lines.append("")
    lines.append(f"**Query**: {query}")
    lines.append(f"**Reason**: {format_degraded_reason(reason_code, reason_detail)}")
    if trace_id:
        lines.append(f"**Trace**: {trace_id}")
    if partial_analysis:
        lines.append("")
        lines.append("**Partial Analysis**")
        lines.append(partial_analysis)
    lines.append("")
    lines.append("**Next Steps**")
    lines.append("- Clarify company/metric/year if missing.")
    lines.append("- Try a narrower query within 2022-2024 coverage.")
    lines.append("- If needed, request only SQL or only RAG data.")
    return "\n".join(lines)


def apply_degraded_state(
    state: "AgentState",
    reason_code: str,
    reason_detail: str = "",
    partial_analysis: str = "",
) -> "AgentState":
    query = state.get("resolved_query", state.get("user_query", ""))
    trace_id = state.get("trace_id", "")
    state["analysis"] = build_degraded_message(
        query=query,
        reason_code=reason_code,
        reason_detail=reason_detail,
        trace_id=trace_id,
        partial_analysis=partial_analysis,
    )
    state["eval_o_status"] = "degraded"
    state["is_degraded"] = True
    state["degraded_reason"] = format_degraded_reason(reason_code, reason_detail)
    return state
