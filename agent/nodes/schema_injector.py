"""
agent/nodes/schema_injector.py  —  动态 Schema 注入节点（流程的第 ③ 步）
=========================================================================

【在流程中的位置】supervisor → ★schema_injector★ → sql_worker / rag_worker

【这个节点干什么？】

Schema Injector 相当于给 SQL Worker 准备一份"备忘录"。

想象你是一个翻译员（SQL Worker），要把用户的中文问题翻译成 SQL 查询语句。
你需要知道数据库里有哪些表、每个字段叫什么、代表什么含义。
Schema Injector 就是提供这些信息的节点。

具体来说，它做四件事：
  1. 根据本次查询涉及的指标（metrics）和行业（industry），
     从 data_dictionary.py 中挑选相关的表结构信息（不是全部表，避免 prompt 过长）
  2. 预查 missing_data_log 表，看看哪些数据是已知缺失的
     （避免 SQL Worker 白白查询不存在的数据）
  3. 匹配 Few-Shot SQL 样例——给 LLM 看几个"好的 SQL 写法"作为参考
  4. 把以上所有信息组装成一段文本（schema_context），写入 State

【特点】
  - 纯数据处理节点，不调用 LLM，执行速度极快（< 50ms）
  - SQL Worker 会直接把 schema_context 拼进 prompt，无需额外处理

【写入 State 的关键字段】
  - schema_context: 完整的 Schema 提示文本
  - known_missing_preview: 已知缺失数据列表
"""

from __future__ import annotations

import logging

from agent.state import AgentState
from agent.tracing import trace_node, TraceLogger
from agent.data_dictionary import get_relevant_schema, query_known_missing

log = logging.getLogger(__name__)


@trace_node("schema_injector", tags=["preparation"])
def schema_injector_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("schema_injector", trace_id)

    entities = state.get("entities", {})
    plan     = state.get("plan", {})

    companies   = entities.get("companies", [])
    years       = entities.get("years", [])
    metrics     = entities.get("metrics", [])
    industry    = entities.get("industry", "")

    # SQL Worker 不参与时跳过
    if "sql" not in plan.get("workers", []):
        log.info("SQL Worker 未启用，跳过 Schema 注入")
        state["schema_context"]        = ""
        state["known_missing_preview"] = []
        return state

    log.info(
        "开始 Schema 注入",
        {"metrics": metrics, "industry": industry,
         "companies": companies[:3], "years": years},
    )

    # ── 预查已知缺失 ─────────────────────────────────────────────────────────
    known_missing = query_known_missing(
        companies=companies,
        years=years,
        metric_keys=metrics,
    )
    log.info(f"已知缺失预查：{len(known_missing)} 条")

    # ── 组装 schema 上下文 ───────────────────────────────────────────────────
    schema_context = get_relevant_schema(
        metric_keys=metrics,
        industry=industry,
        companies=companies,
        years=years,
    )

    log.info(f"Schema 上下文组装完成，长度：{len(schema_context)} 字符")

    state["schema_context"]        = schema_context
    state["known_missing_preview"] = known_missing

    return state