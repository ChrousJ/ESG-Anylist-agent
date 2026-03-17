"""
agent/graph.py  —  LangGraph 图结构与路由函数
================================================

【给初学者的说明】

本文件是整个 ESG Agent 的"总装车间"——它把所有独立的节点（node）组装成一张
有向图（Graph），定义了"谁先执行、谁后执行、什么条件走哪条路"。

你可以把它想象成一条工厂流水线的蓝图：
  - 每道工序 = 一个节点函数（在 agent/nodes/*.py 中定义）
  - 流水线的流向 = 边（Edge），有些是固定流向，有些根据条件分叉
  - 产品 = AgentState（定义在 state.py），经过每道工序被逐步加工

【核心概念速查】

  · StateGraph(AgentState)  — 创建一张以 AgentState 为数据载体的图
  · builder.add_node("名字", 函数)  — 注册一道工序
  · builder.add_edge(A, B)  — A 完成后无条件执行 B
  · builder.add_conditional_edges(A, 路由函数, 目标映射)
        — A 完成后，根据路由函数的返回值决定走哪条路
  · fan-out  — 一个节点同时触发多个节点并行执行（如 SQL + RAG 同时跑）
  · fan-in   — 多个并行节点都完成后，再汇聚到下一个节点
  · Re-plan 循环  — evaluator_d 发现数据质量不够 → 打回 supervisor 重新规划
  · 修正循环  — evaluator_o 发现报告有问题 → 打回 synthesizer 修正

【完整执行流程图】

  START （用户发来一个问题）
    │
    ▼
  context  （理解用户问的是什么：提取公司、年份、指标等实体）
    ├── knowledge  → 纯知识问题（如"什么是ESG"）→ 直接 LLM 回答 → END
    ├── clarify    → 信息不足（如"帮我查一下"，没说查谁）→ 反问用户 → END
    └── complex    → 业务分析问题 → 进入完整流程 ↓
          │
          ▼
       supervisor  （制定执行计划：用 SQL 查数据库？用 RAG 查报告？还是两个都用？）
          │
          ▼
     schema_injector  （给 SQL Worker 准备数据库表结构信息）
          │
        fan-out（并行执行）
       ┌──┴──┐
       ▼     ▼
  sql_worker  rag_worker  （SQL 查结构化数据 / RAG 检索报告原文）
       └──┬──┘
        fan-in（等两个都完成）
          ▼
    worker_aggregator  （汇总两个 Worker 的结果：都成功？一个成功？都失败？）
          │
          ├── both_failed → degraded_response → END（降级输出）
          │
          ▼
      evaluator_d  （数据质量检查：SQL 返回的数据对不对？RAG 召回的内容相关吗？）
          │
          ├── fail → supervisor（Re-plan 循环，最多重试 4 次）
          │
          └── pass / pass_with_warnings
                │
                ▼
           map_reduce  （如果 RAG 文本太长，先压缩）
                │
                ▼
           synthesizer  （生成 MSCI 框架的四层分析报告）
                │
                ▼
           evaluator_o  （输出质量检查：报告格式对吗？数字有没有编造？）
                │
                ├── fail → synthesizer（修正循环，最多 2 次）
                │
                └── pass / degraded
                      │
                      ▼
               memory_updater  （保存对话历史和用户偏好）
                      │
                      ▼
                     END  （返回最终结果给用户）
"""

from __future__ import annotations

import logging
import os
from typing import Literal

# ── LangGraph 核心组件 ────────────────────────────────────────────────────────
# StateGraph: 创建有状态的工作流图
# START / END: 特殊节点，代表流程的入口和出口
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

# ── 导入状态定义和所有节点函数 ─────────────────────────────────────────────────
# 每个节点函数的签名都是：(state: AgentState) -> AgentState
# 即接收当前状态，返回修改后的状态
from agent.state import AgentState, apply_degraded_state
from agent.nodes.context         import context_node          # ① 上下文理解
from agent.nodes.supervisor      import supervisor_node       # ② 任务规划
from agent.nodes.schema_injector import schema_injector_node  # ③ 数据库 Schema 注入
from agent.nodes.sql_worker      import sql_worker_node       # ④ SQL 查询执行
from agent.nodes.rag_worker      import rag_worker_node       # ⑤ RAG 检索执行
from agent.nodes.worker_aggregator import worker_aggregator_node  # ⑥ 结果汇总
from agent.nodes.evaluator_d     import evaluator_d_node      # ⑦ 数据质量检查
from agent.nodes.map_reduce      import map_reduce_node       # ⑧ 大文本压缩
from agent.nodes.synthesizer     import synthesizer_node       # ⑨ 报告生成
from agent.nodes.evaluator_o     import evaluator_o_node       # ⑩ 输出质量检查
from agent.nodes.memory_updater  import memory_updater_node    # ⑪ 记忆更新

log = logging.getLogger(__name__)

_checkpointer_instance = None


def _build_checkpointer():
    """
    Prefer AsyncSqliteSaver when available; fall back to SqliteSaver or MemorySaver.
    """
    db_path = os.getenv("CHECKPOINT_DB_PATH", "./data/checkpoints.sqlite")
    prefer_async = os.getenv("CHECKPOINTER_ASYNC", "true").strip().lower() in {
        "1", "true", "yes", "y",
    }
    try:
        from langgraph.checkpoint.sqlite import AsyncSqliteSaver, SqliteSaver
        if prefer_async:
            try:
                return AsyncSqliteSaver(db_path)
            except Exception as exc:
                log.warning(f"AsyncSqliteSaver init failed; fallback to SqliteSaver: {exc}")
        return SqliteSaver(db_path)
    except Exception as exc:
        log.warning(f"SqliteSaver unavailable; fallback to MemorySaver: {exc}")
        try:
            from langgraph.checkpoint.memory import MemorySaver
            return MemorySaver()
        except Exception:
            return None


def _get_checkpointer():
    global _checkpointer_instance
    if _checkpointer_instance is None:
        _checkpointer_instance = _build_checkpointer()
    return _checkpointer_instance

# ══════════════════════════════════════════════════════════════════════════════
# 终止节点：知识问答直答、澄清反问、降级响应
# ══════════════════════════════════════════════════════════════════════════════
# 【说明】这些节点逻辑很简单，不需要单独的文件，直接写在这里。
# 它们是流程中的"快速出口"——不走完整的分析流程，直接返回结果。

def knowledge_answer_node(state: AgentState) -> AgentState:
    """
    纯知识问题：直接用 Gemini 回答，不做任何检索。
    """
    import os, re
    from google import genai
    from google.genai import types
    from agent.tracing import TraceLogger, llm_call_with_retry

    trace_id = state.get("trace_id", "")
    log_     = TraceLogger("knowledge_answer", trace_id)
    query    = state.get("resolved_query", state.get("user_query", ""))

    log_.info(f"纯知识问答：{query[:60]}")

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    model  = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")

    def _call():
        resp = client.models.generate_content(
            model=model,
            contents=query,
            config=types.GenerateContentConfig(
                system_instruction=(
                    "你是专业的 ESG 知识助手。"
                    "请用准确、简洁的中文回答用户关于 ESG、可持续发展、"
                    "绿色金融等概念性问题。不需要引用具体公司数据。"
                ),
                temperature=0.3,
            ),
        )
        return resp.text

    try:
        answer = llm_call_with_retry(
            _call, max_retries=2, timeout_seconds=30,
            caller_name="knowledge_llm", trace_id=trace_id,
        )
    except Exception as e:
        answer = f"抱歉，知识问答暂时不可用：{e}"

    state["analysis"]     = answer
    state["eval_o_status"] = "pass"
    state["key_findings"]  = []
    return state


def clarify_node(state: AgentState) -> AgentState:
    """
    实体不足：返回反问，不做任何检索。
    """
    question = state.get("clarify_question", "请提供更多信息以便我帮您查询。")
    state["analysis"]     = question
    state["eval_o_status"] = "pass"
    state["is_degraded"]   = False
    return state


def degraded_response_node(state: AgentState) -> AgentState:
    """
    Build a unified degraded response for out-of-scope or failed runs.
    """
    from agent.tracing import TraceLogger

    trace_id = state.get('trace_id', '')
    log_ = TraceLogger('degraded_response', trace_id)

    raw_reason = state.get('degraded_reason', 'UNKNOWN')
    if ':' in raw_reason:
        reason_code, reason_detail = raw_reason.split(':', 1)
        reason_code = reason_code.strip() or 'UNKNOWN'
        reason_detail = reason_detail.strip()
    else:
        reason_code = raw_reason.strip() or 'UNKNOWN'
        reason_detail = ''

    log_.warning(f"enter degraded response: {raw_reason}")
    return apply_degraded_state(
        state,
        reason_code=reason_code,
        reason_detail=reason_detail,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 路由函数（条件边 / Conditional Edges）
# ══════════════════════════════════════════════════════════════════════════════
# 【说明】路由函数是 LangGraph 中实现"选择分支"的关键。
# 每个路由函数接收当前 State，根据某些字段的值返回"下一个节点的名字"。
# LangGraph 根据返回值决定接下来执行哪个节点。
# 这就像交通路口的红绿灯——根据当前状况决定走哪条路。

def route_after_context(
    state: AgentState,
) -> Literal["supervisor", "knowledge_answer", "clarify", "degraded_response"]:
    """
    上下文理解节点之后的路由。
    knowledge → knowledge_answer_node（直接回答，不走完整流程）
    clarify   → clarify_node（反问用户）
    complex   → supervisor（进入完整分析流程）
    """
    query_class = state.get("query_class", "complex")
    need_clarify = state.get("need_clarify", False)

    if query_class == "knowledge":
        log.info("路由：knowledge → knowledge_answer")
        return "knowledge_answer"

    if query_class == "refuse":
        log.info("route refuse -> degraded_response")
        return "degraded_response"

    if query_class == "clarify" or need_clarify:
        log.info("路由：clarify → clarify_node")
        return "clarify"

    log.info("路由：complex → supervisor")
    return "supervisor"


def route_after_schema_injector(
    state: AgentState,
) -> list[str]:
    """
    Schema 注入之后：fan-out 到 SQL Worker 和 / 或 RAG Worker。
    使用 LangGraph 的 Send API 实现真正并行。
    返回要激活的节点名称列表。
    """
    workers = state.get("plan", {}).get("workers", ["sql", "rag"])
    targets = []
    if "sql" in workers:
        targets.append("sql_worker")
    if "rag" in workers:
        targets.append("rag_worker")

    log.info(f"fan-out → {targets}")
    return targets if targets else ["sql_worker"]  # 兜底


def route_after_aggregator(
    state: AgentState,
) -> Literal["evaluator_d", "degraded_response"]:
    """
    Worker Aggregator 之后的路由判断：

    - 如果 SQL + RAG 两个 Worker 全部失败 → 直接走降级路径，生成友好的错误提示
    - 如果至少有一个 Worker 成功 → 继续走 evaluator_d 做数据质量检查

    【为什么要检查 is_degraded？】
    is_degraded 可能在 Re-plan 循环中被设置为 True（重试次数超限），
    此时即使 aggregated_status 不是 both_failed，也应该走降级路径。
    """
    if state.get("is_degraded") or state.get("aggregated_status") == "both_failed":
        log.info("路由：both_failed → degraded_response")
        return "degraded_response"
    return "evaluator_d"


def route_after_evaluator_d(
    state: AgentState,
) -> Literal["supervisor", "map_reduce"]:
    """
    Evaluator-D 之后：
    fail（有 Re-plan 错误）→ supervisor（调整策略重新执行）
    pass / pass_with_warnings → map_reduce（继续合成）

    防死循环：Re-plan 超限时 is_degraded=True，
              此时不应再路由回 supervisor，而应走 map_reduce 做降级输出。
    """
    if state.get("is_degraded"):
        log.info("路由：已降级 → map_reduce（降级路径）")
        return "map_reduce"

    eval_d_status = state.get("eval_d_status", "")
    eval_d_errors = state.get("eval_d_errors", [])

    if eval_d_status == "fail" and eval_d_errors:
        log.info(f"路由：eval_d fail → supervisor（Re-plan，错误：{[e['type'] for e in eval_d_errors]}）")
        return "supervisor"

    log.info("路由：eval_d pass → map_reduce")
    return "map_reduce"


def route_after_evaluator_o(
    state: AgentState,
) -> Literal["synthesizer", "memory_updater"]:
    """
    Evaluator-O 之后：
    fail（retry < MAX）→ synthesizer（局部修正）
    pass / degraded   → memory_updater（写入记忆，返回响应）
    """
    eval_o_status = state.get("eval_o_status", "")
    retry_count   = state.get("eval_o_retry_count", 0)

    if eval_o_status == "fail" and retry_count <= 2:
        log.info(f"路由：eval_o fail → synthesizer（第 {retry_count} 次修正）")
        return "synthesizer"

    log.info(f"路由：eval_o {eval_o_status} → memory_updater")
    return "memory_updater"


def route_after_knowledge_or_clarify(
    state: AgentState,
) -> Literal["memory_updater"]:
    """
    知识问答和澄清反问都需要更新记忆。
    """
    return "memory_updater"


# ══════════════════════════════════════════════════════════════════════════════
# 图构建
# ══════════════════════════════════════════════════════════════════════════════

def build_graph() -> StateGraph:
    """
    构建完整的 LangGraph 图（这是整个 Agent 的核心组装函数）。
    """
    # ── 创建图构建器 ──────────────────────────────────────────────────────────
    # StateGraph(AgentState) 创建一个以 AgentState 为数据载体的图构建器。
    # 所有注册到这个图上的节点函数，都会接收和返回 AgentState 类型的字典。
    builder = StateGraph(AgentState)

    # ── 注册所有节点 ──────────────────────────────────────────────────────────
    builder.add_node("context",           context_node)
    builder.add_node("knowledge_answer",  knowledge_answer_node)
    builder.add_node("clarify",           clarify_node)
    builder.add_node("supervisor",        supervisor_node)
    builder.add_node("schema_injector",   schema_injector_node)
    builder.add_node("sql_worker",        sql_worker_node)
    builder.add_node("rag_worker",        rag_worker_node)
    builder.add_node("worker_aggregator", worker_aggregator_node)
    builder.add_node("evaluator_d",       evaluator_d_node)
    builder.add_node("map_reduce",        map_reduce_node)
    builder.add_node("synthesizer",       synthesizer_node)
    builder.add_node("evaluator_o",       evaluator_o_node)
    builder.add_node("memory_updater",    memory_updater_node)
    builder.add_node("degraded_response", degraded_response_node)

    # ── 起点 → context ────────────────────────────────────────────────────────
    builder.add_edge(START, "context")

    # ── context → 三分支路由 ──────────────────────────────────────────────────
    builder.add_conditional_edges(
        "context",
        route_after_context,
        {
            "knowledge_answer": "knowledge_answer",
            "clarify":          "clarify",
            "supervisor":       "supervisor",
            "degraded_response": "degraded_response",
        },
    )

    # ── 知识问答 / 澄清 → memory_updater → END ───────────────────────────────
    builder.add_edge("knowledge_answer", "memory_updater")
    builder.add_edge("clarify",          "memory_updater")

    # ── supervisor → schema_injector ─────────────────────────────────────────
    builder.add_edge("supervisor", "schema_injector")

    # ── schema_injector → fan-out（并行 SQL + RAG）────────────────────────────
    # 【并行执行原理】
    # 当路由函数返回一个列表（如 ["sql_worker", "rag_worker"]）时，
    # LangGraph 会同时启动列表中的所有节点，实现真正的并行执行。
    # 这叫 "fan-out"，就像一条水管分叉成两条。
    builder.add_conditional_edges(
        "schema_injector",
        route_after_schema_injector,
        # 声明所有可能被路由到的目标节点
        ["sql_worker", "rag_worker"],
    )

    # ── sql_worker, rag_worker → fan-in → worker_aggregator ──────────────────
    # 【fan-in 原理】两条边都指向同一个 aggregator 节点。
    # LangGraph 会自动等待 sql_worker 和 rag_worker 都完成后，
    # 才执行 worker_aggregator。这就像两条支流汇入一条主河。
    builder.add_edge("sql_worker",  "worker_aggregator")
    builder.add_edge("rag_worker",  "worker_aggregator")

    # ── worker_aggregator → evaluator_d 或 降级 ──────────────────────────────
    builder.add_conditional_edges(
        "worker_aggregator",
        route_after_aggregator,
        {
            "evaluator_d":      "evaluator_d",
            "degraded_response": "degraded_response",
        },
    )

    # ── evaluator_d → supervisor（Re-plan 循环）或 map_reduce ─────────────────
    builder.add_conditional_edges(
        "evaluator_d",
        route_after_evaluator_d,
        {
            "supervisor":  "supervisor",
            "map_reduce":  "map_reduce",
        },
    )

    # ── map_reduce → synthesizer ──────────────────────────────────────────────
    builder.add_edge("map_reduce", "synthesizer")

    # ── synthesizer → evaluator_o ─────────────────────────────────────────────
    builder.add_edge("synthesizer", "evaluator_o")

    # ── evaluator_o → synthesizer（修正循环）或 memory_updater ────────────────
    builder.add_conditional_edges(
        "evaluator_o",
        route_after_evaluator_o,
        {
            "synthesizer":    "synthesizer",
            "memory_updater": "memory_updater",
        },
    )

    # ── degraded_response → memory_updater ───────────────────────────────────
    builder.add_edge("degraded_response", "memory_updater")

    # ── memory_updater → END ──────────────────────────────────────────────────
    # 所有路径最终都汇聚到 memory_updater → END，确保每次执行都会保存记忆。
    builder.add_edge("memory_updater", END)

    # compile() 将构建器中的节点和边"编译"成一个可执行的图对象。
    # 编译后的图是不可变的，可以反复调用 graph.invoke(state)。
    checkpointer = _get_checkpointer()
    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


# ══════════════════════════════════════════════════════════════════════════════
# 单例：编译好的图对象（供 FastAPI 层直接调用）
# ══════════════════════════════════════════════════════════════════════════════

_graph_instance = None


def get_graph():
    """
    返回编译好的图单例。
    首次调用时编译，后续调用直接复用（编译耗时约 200ms）。
    """
    global _graph_instance
    if _graph_instance is None:
        log.info("编译 LangGraph 图...")
        _graph_instance = build_graph()
        log.info("LangGraph 图编译完成")
    return _graph_instance


# ══════════════════════════════════════════════════════════════════════════════
# 本地调试入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    import sys
    import os
    # 把 data/ 目录加入 sys.path，使 rag_retriever 模块可达
    sys.path.insert(0, os.path.abspath("data"))
    # Windows 控制台 GBK 编码无法输出 emoji，强制 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from agent.state import make_initial_state
    from agent.tracing import generate_trace_id, generate_request_id

    query = sys.argv[1] if len(sys.argv) > 1 else "比亚迪2023年碳排放情况如何？"
    print(f"\n{'='*60}\n查询：{query}\n{'='*60}")

    trace_id   = generate_trace_id()
    request_id = generate_request_id()
    init_state = make_initial_state(
        user_query=query,
        conversation_id="debug-session-001",
        trace_id=trace_id,
        request_id=request_id,
    )

    graph      = get_graph()
    final_state = graph.invoke(
        init_state,
        config={"configurable": {"thread_id": init_state.get("conversation_id", "")}},
    )

    print(f"\n{'='*60}")
    print(f"TraceID: {final_state.get('trace_id')}")
    print(f"QueryClass: {final_state.get('query_class')}")
    print(f"Eval-O: {final_state.get('eval_o_status')}")
    print(f"Degraded: {final_state.get('is_degraded')}")
    print(f"\n--- 分析报告 ---")
    print(final_state.get("analysis", "（无输出）")[:2000])
    if final_state.get("key_findings"):
        print(f"\n--- 关键发现 ---")
        for f in final_state["key_findings"]:
            print(f"  · {f}")
    if final_state.get("langsmith_run_url"):
        print(f"\n🔍 LangSmith 追踪：{final_state['langsmith_run_url']}")
