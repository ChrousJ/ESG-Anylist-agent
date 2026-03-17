"""
agent/nodes/worker_aggregator.py  —  Worker 聚合与降级矩阵（流程的第 ⑥ 步）
=============================================================================

【在流程中的位置】sql_worker + rag_worker → ★worker_aggregator★ → evaluator_d / degraded_response

【这个节点干什么？】

Worker Aggregator 是 SQL Worker 和 RAG Worker 的"汇合点"——
两个 Worker 并行执行完毕后，这个节点汇总它们的结果，做一个关键决策：
"我们拿到的数据够不够用？应该继续分析还是降级？"

它使用一个"降级矩阵"来做判断：

  ┌─────────────┬─────────────┬──────────────┬──────────────────────┐
  │ SQL 状态     │ RAG 状态    │ 聚合结果      │ 后续                  │
  ├─────────────┼─────────────┼──────────────┼──────────────────────┤
  │ success     │ success     │ both_ok      │ 继续 → evaluator_d   │
  │ success     │ failed      │ sql_only     │ 继续（标注 RAG 不可用）│
  │ failed      │ success     │ rag_only     │ 继续（标注 SQL 不可用）│
  │ failed      │ failed      │ both_failed  │ 降级 → 友好提示用户    │
  │ success     │ timeout     │ sql_only     │ 继续                  │
  │ skipped     │ success     │ rag_only     │ 继续（策略本就仅 RAG）│
  └─────────────┴─────────────┴──────────────┴──────────────────────┘

【写入 State 的关键字段】
  - aggregated_status: "both_ok" | "sql_only" | "rag_only" | "both_failed"
  - degraded_reason: 降级原因说明
  - is_degraded: 是否降级
"""

from __future__ import annotations

import logging

from agent.state import AgentState, format_degraded_reason
from agent.tracing import trace_node, TraceLogger

log = logging.getLogger(__name__)


@trace_node("worker_aggregator", tags=["aggregation"])
def worker_aggregator_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("worker_aggregator", trace_id)

    worker_status = state.get("worker_status", {})
    plan          = state.get("plan", {})
    scheduled     = set(plan.get("workers", []))

    sql_status = worker_status.get("sql", {}).get("status", "skipped")
    rag_status = worker_status.get("rag", {}).get("status", "skipped")

    sql_ok = sql_status == "success"
    rag_ok = rag_status == "success"

    log.info(f"聚合状态：sql={sql_status}  rag={rag_status}")

    # ── 降级矩阵 ─────────────────────────────────────────────────────────────
    if sql_ok and rag_ok:
        aggregated = "both_ok"
        note       = ""

    elif sql_ok and not rag_ok and "rag" in scheduled:
        aggregated = "sql_only"
        err        = worker_status.get("rag", {}).get("error_type", "UNKNOWN")
        note       = f"RAG Worker 不可用（{err}），仅使用结构化数据，定性分析受限。"
        log.warning(note)

    elif not sql_ok and rag_ok and "sql" in scheduled:
        aggregated = "rag_only"
        err        = worker_status.get("sql", {}).get("error_type", "UNKNOWN")
        note       = f"SQL Worker 不可用（{err}），仅使用原文检索，数值精度受限。"
        log.warning(note)

    elif sql_ok and rag_status == "skipped":
        aggregated = "sql_only"
        note       = ""

    elif sql_status == "skipped" and rag_ok:
        aggregated = "rag_only"
        note       = ""

    elif not sql_ok and not rag_ok:
        aggregated = "both_failed"
        sql_err    = worker_status.get("sql", {}).get("error_type", "N/A")
        rag_err    = worker_status.get("rag", {}).get("error_type", "N/A")
        note       = f"SQL({sql_err}) 和 RAG({rag_err}) 均不可用，无法生成分析。"
        log.error(note)

        state["is_degraded"]     = True
        state["degraded_reason"] = format_degraded_reason("WORKERS_BOTH_FAILED", note)
        state["aggregated_status"] = aggregated
        return state

    else:
        aggregated = "degraded"
        note       = "Worker 状态异常，进入降级响应。"
        log.error(note)
        state["is_degraded"]     = True
        state["degraded_reason"] = format_degraded_reason("WORKER_FAILED", note)

    state["aggregated_status"] = aggregated

    # 把降级说明追加到 sources 里，供最终响应透出给用户
    if note:
        sources = list(state.get("sources", []))
        sources.append({"type": "system_note", "content": note})
        state["sources"] = sources

    log.info(f"聚合完成：{aggregated}")
    return state
