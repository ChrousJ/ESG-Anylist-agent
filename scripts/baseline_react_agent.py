#!/usr/bin/env python3
"""
scripts/baseline_react_agent.py
================================

ReAct baseline agent — 使用 langgraph.prebuilt.create_react_agent 封装
原项目的 SQL / RAG 检索逻辑，作为对照基线。

与 LangGraph 主 Agent 的核心差异：
  - 无 evaluator_d / evaluator_o 质检循环
  - 无 Re-plan / 修正循环
  - 无缺失数据降级处理
  - 无口径一致性校验
  - 单次 ReAct 循环（Thought -> Action -> Observation -> Answer）

LLM 后端：通义千问 (Qwen) via OpenAI-compatible API，
密钥从 .env 环境变量 QWEN_API_KEY 读取。

用法：
    python scripts/baseline_react_agent.py "比亚迪2023年碳排放"
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from typing import Any, Optional, Iterator

from dotenv import load_dotenv

# ── 确保项目根目录和 data/ 在 sys.path 中 ────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "data"))

load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("baseline_react")

# ══════════════════════════════════════════════════════════════════════════════
# 1. Qwen Chat LLM — BaseChatModel 封装
# ══════════════════════════════════════════════════════════════════════════════

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

# Qwen API 配置
QWEN_API_KEY: str = os.getenv("QWEN_API_KEY", "")
QWEN_BASE_URL: str = os.getenv(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
QWEN_MODEL: str = os.getenv("QWEN_EVAL_MODEL", "qwen3-235b-a22b")
# NOTE: Baseline must keep enable_thinking=false, otherwise DashScope non-streaming
# calls can fail (HTTP 400) and invalidate baseline comparison results.
QWEN_BASELINE_ENABLE_THINKING: bool = False

if not QWEN_API_KEY:
    raise EnvironmentError(
        "QWEN_API_KEY not found in environment. "
        "Please set it in your .env file: QWEN_API_KEY=sk-xxx"
    )


class QwenChatModel(BaseChatModel):
    """
    Thin wrapper around Qwen (DashScope) OpenAI-compatible API,
    exposing it as a LangChain BaseChatModel so it can be used
    with langgraph.prebuilt.create_react_agent.
    """

    model_name: str = QWEN_MODEL
    temperature: float = 0.1
    max_tokens: int = 4096
    _client: Any = None

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "qwen-chat"

    def _get_client(self) -> Any:
        if self._client is None:
            import openai
            self._client = openai.OpenAI(
                api_key=QWEN_API_KEY,
                base_url=QWEN_BASE_URL,
            )
        return self._client

    def _convert_messages(self, messages: list[BaseMessage]) -> list[dict]:
        """Convert LangChain messages to OpenAI format."""
        result: list[dict] = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                result.append({"role": "system", "content": msg.content})
            elif isinstance(msg, HumanMessage):
                result.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                entry: dict[str, Any] = {"role": "assistant"}
                if msg.content:
                    entry["content"] = msg.content
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": (
                                    json.dumps(tc["args"], ensure_ascii=False)
                                    if isinstance(tc["args"], dict)
                                    else tc["args"]
                                ),
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                    if not entry.get("content"):
                        entry["content"] = ""
                result.append(entry)
            elif isinstance(msg, ToolMessage):
                result.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content if isinstance(msg.content, str) else json.dumps(msg.content, ensure_ascii=False),
                })
            else:
                result.append({"role": "user", "content": str(msg.content)})
        return result

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        client = self._get_client()
        oai_messages = self._convert_messages(messages)

        # Build tools spec if bound
        tools_spec = kwargs.get("tools")
        api_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": oai_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        # NOTE: OpenAI SDK does not accept `enable_thinking` as a top-level argument.
        # Pass it via `extra_body` so DashScope receives `enable_thinking=false`.
        api_kwargs["extra_body"] = {"enable_thinking": QWEN_BASELINE_ENABLE_THINKING}
        if tools_spec:
            api_kwargs["tools"] = tools_spec
        if stop:
            api_kwargs["stop"] = stop

        resp = client.chat.completions.create(**api_kwargs)
        choice = resp.choices[0]

        # Handle tool calls
        if choice.message.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": json.loads(tc.function.arguments)
                    if tc.function.arguments
                    else {},
                }
                for tc in choice.message.tool_calls
            ]
            ai_msg = AIMessage(
                content=choice.message.content or "",
                tool_calls=tool_calls,
            )
        else:
            ai_msg = AIMessage(content=choice.message.content or "")

        return ChatResult(generations=[ChatGeneration(message=ai_msg)])

    def bind_tools(self, tools: list, **kwargs: Any) -> "QwenChatModel":
        """Return a runnable that includes tool definitions."""
        from langchain_core.runnables import RunnableBinding

        tool_defs = []
        for t in tools:
            if hasattr(t, "name") and hasattr(t, "args_schema"):
                schema = t.args_schema.schema() if t.args_schema else {}
                properties = schema.get("properties", {})
                required = schema.get("required", [])
                # Remove 'title' keys from properties
                clean_props = {}
                for k, v in properties.items():
                    clean_v = {kk: vv for kk, vv in v.items() if kk != "title"}
                    clean_props[k] = clean_v
                tool_defs.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": {
                            "type": "object",
                            "properties": clean_props,
                            "required": required,
                        },
                    },
                })

        return RunnableBinding(bound=self, kwargs={"tools": tool_defs}, **kwargs)


# ══════════════════════════════════════════════════════════════════════════════
# 2. 工具定义 — 封装 SQL 查询和 RAG 检索
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = os.path.join(_PROJECT_ROOT, "data", "esg_data.db")


@tool
def query_esg_database(sql: str) -> str:
    """Execute a SELECT SQL query against the ESG SQLite database.

    The database contains ESG metrics for ~30 Chinese companies across
    banking, new energy, and power industries (2022-2024).

    Main tables:
    - esg_universal_metrics: company_name, year, industry, scope_1_emissions,
      scope_2_emissions, total_energy_consumption, energy_intensity, etc.
    - esg_banking_metrics: green_finance_balance, inclusive_finance_balance
    - esg_auto_metrics: scope_3_emissions, rd_investment_total, supplier_esg_audit_ratio
    - esg_power_metrics: scope_3_emissions, clean_energy_ratio, rd_investment_total

    JOIN sub-tables with: ON (company_name, year)
    Only SELECT statements are allowed.

    Args:
        sql: A valid SQLite SELECT query.
    """
    # Safety check
    clean = sql.strip()
    if not re.match(r"^\s*SELECT\b", clean, re.IGNORECASE):
        return "ERROR: Only SELECT statements are allowed."
    forbidden = re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", re.IGNORECASE
    )
    if forbidden.search(clean):
        return "ERROR: Dangerous SQL keyword detected."

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(clean)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        if not rows:
            return "Query returned 0 rows."
        # Truncate if too many rows
        display = rows[:20]
        return json.dumps(display, ensure_ascii=False, default=str)
    except Exception as e:
        return f"SQL ERROR: {e}"


@tool
def search_esg_reports(query: str) -> str:
    """Search ESG annual reports (PDF documents) using hybrid retrieval (vector + BM25).

    Use this tool when you need qualitative information, policy descriptions,
    or context that is not available in the structured database.

    Args:
        query: Natural language search query about ESG topics.
    """
    try:
        from data.rag_retriever import retrieve
        result = retrieve(query=query, top_k=5, rewrite=False)
        chunks = result.get("chunks", [])
        if not chunks:
            return "No relevant documents found."
        # Format top chunks
        formatted = []
        for i, c in enumerate(chunks[:5], 1):
            company = c.get("company_name", c.get("company", ""))
            year = c.get("year", "")
            text = c.get("text", c.get("content", ""))[:300]
            page = c.get("page_num", "")
            score = c.get("rerank_score", 0)
            formatted.append(
                f"[{i}] {company} {year} p.{page} (score={score:.2f}): {text}"
            )
        return "\n\n".join(formatted)
    except Exception as e:
        return f"RAG ERROR: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. ReAct Agent 构建
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """\
You are an ESG (Environmental, Social, Governance) analyst assistant.
You have access to a structured database and ESG report documents.

When answering questions:
1. Use query_esg_database for structured data (metrics, numbers, trends)
2. Use search_esg_reports for qualitative info (policies, descriptions)
3. Always cite your data sources
4. Use Chinese (Mandarin) for your final answers
5. If data is missing (NULL), clearly state it rather than guessing

Database coverage: ~30 Chinese companies, 3 industries (bank, new_energy, power), years 2022-2024.
"""


def build_react_agent():
    """Build and return the ReAct baseline agent."""
    from langgraph.prebuilt import create_react_agent

    llm = QwenChatModel(
        model_name=QWEN_MODEL,
        temperature=0.1,
        max_tokens=4096,
    )

    tools = [query_esg_database, search_esg_reports]

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=_SYSTEM_PROMPT,
    )

    return agent


def run_baseline(query: str, timeout: float = 120.0) -> dict[str, Any]:
    """
    Run the ReAct baseline agent on a single query.

    Returns:
        dict with keys: analysis, latency_ms, status, error
    """
    agent = build_react_agent()
    t_start = time.perf_counter()

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=query)]},
        )
        latency_ms = int((time.perf_counter() - t_start) * 1000)

        # Extract final answer from the last AI message
        messages = result.get("messages", [])
        final_answer = ""
        tool_observations: list[dict[str, Any]] = []
        for msg in messages:
            if isinstance(msg, ToolMessage):
                tool_observations.append({
                    "tool_name": getattr(msg, "name", ""),
                    "content": str(msg.content)[:1200],
                })
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
                final_answer = msg.content
                break

        return {
            "analysis": final_answer,
            "latency_ms": latency_ms,
            "status": "success" if final_answer else "empty",
            "error": "",
            "message_count": len(messages),
            "tool_observations": tool_observations[:6],
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        log.error(f"Baseline failed: {e}", exc_info=True)
        return {
            "analysis": "",
            "latency_ms": latency_ms,
            "status": "failed",
            "error": str(e)[:300],
            "message_count": 0,
            "tool_observations": [],
        }


# ══════════════════════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Windows console encoding fix
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    query = sys.argv[1] if len(sys.argv) > 1 else "比亚迪2023年碳排放情况如何？"
    print(f"\n{'='*60}")
    print(f"Query: {query}")
    print(f"Model: {QWEN_MODEL}")
    print(f"{'='*60}\n")

    result = run_baseline(query)
    print(f"\nStatus: {result['status']}")
    print(f"Latency: {result['latency_ms']}ms")
    print(f"Messages: {result['message_count']}")
    print(f"\n--- Analysis ---")
    print(result["analysis"][:2000])
    if result["error"]:
        print(f"\n--- Error ---")
        print(result["error"])
