"""
agent/nodes/sql_worker.py  —  SQL Worker（流程的第 ④ 步，与 RAG Worker 并行）
==============================================================================

【在流程中的位置】schema_injector → ★sql_worker★ → worker_aggregator
                                     ↕ （与 rag_worker 并行执行）

【这个节点干什么？】

SQL Worker 负责从结构化数据库中获取精确数据。工作流程：

  1. 接收 Schema 上下文（schema_context，由 Schema Injector 准备）
  2. 调用 LLM（Gemini）做 Text2SQL：把用户的自然语言问题翻译成 SQL 查询
     例如："比亚迪2023碳排放" → SELECT scope_1_emissions FROM esg_universal_metrics
                                  WHERE company_name='比亚迪' AND year=2023
  3. SQL 安全校验：检查生成的 SQL 是否安全
     - 白名单校验：只允许 SELECT 语句
     - 注入防御：禁止 DROP, DELETE, UPDATE 等危险操作
  4. 执行 SQL：在 SQLite 数据库上运行查询，获取结果
  5. 结果封装：把查询结果封装为 pandas DataFrame

【错误处理】
  - LLM 调用失败 → 最多重试 3 次（指数退避）
  - SQL 执行失败 → 最多重试 1 次（让 LLM 看错误信息后修正 SQL）
  - 超时保护：单次 SQL 执行 30s，LLM 生成 60s

【写入 State 的关键字段】
  - sql_result: DataFrame 或 None
  - sql_query_executed: 实际执行的 SQL（供溯源和调试）
  - worker_status["sql"]: 执行状态记录
"""

from __future__ import annotations

import re
import os
import json
import sqlite3
import logging
from datetime import datetime, timezone

import pandas as pd
from google.genai import types
from dotenv import load_dotenv

from agent.state import AgentState, WorkerStatusDict, serialize_sql_result
from agent.tracing import (
    trace_node, TraceLogger,
    run_with_timeout, llm_call_with_retry,
)
from agent.llm_provider import get_default_model, llm_generate_content

load_dotenv()

log    = logging.getLogger(__name__)
_MODEL = os.getenv("LLM_MAIN_MODEL", get_default_model())
DB_PATH = os.getenv("DB_PATH", "./data/esg_data.db")

# ══════════════════════════════════════════════════════════════════════════════
# SQL 安全校验
# ══════════════════════════════════════════════════════════════════════════════

_ALLOWED_STATEMENTS = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|"
    r"ATTACH|DETACH|PRAGMA|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)
_COMMENT_STRIP = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _validate_sql(sql: str) -> tuple[bool, str]:
    """
    返回 (is_safe, reason)。
    只允许 SELECT 语句，禁止任何写操作和危险关键字。
    """
    clean = _COMMENT_STRIP.sub("", sql).strip()

    if not _ALLOWED_STATEMENTS.match(clean):
        return False, f"SQL 不以 SELECT 开头：{clean[:80]}"

    if _FORBIDDEN_KEYWORDS.search(clean):
        m = _FORBIDDEN_KEYWORDS.search(clean)
        return False, f"SQL 包含禁止关键字：{m.group()}"

    # 防止多语句注入（分号分隔）
    statements = [s.strip() for s in clean.split(";") if s.strip()]
    if len(statements) > 1:
        return False, "SQL 包含多条语句，拒绝执行"

    return True, "ok"


# ══════════════════════════════════════════════════════════════════════════════
# Text2SQL 生成
# ══════════════════════════════════════════════════════════════════════════════

_TEXT2SQL_SYSTEM = """\
你是一个专业的 ESG 数据库 SQL 生成专家。
根据用户问题和提供的数据库结构，生成精确的 SQLite 查询语句。

## 核心原则
1. 只生成 SELECT 语句，严禁任何写操作
2. NULL 代表"未披露"，严禁用 0 替代，严禁把含 NULL 的行纳入 AVG() 计算
3. 跨表查询必须用 (company_name, year) 双字段 JOIN
4. 排序字段可能为 NULL 时必须加 NULLS LAST
5. 同时查出 data_quality 和 confidence_scores 字段，供下游质检使用
6. 生成的 SQL 必须能在 SQLite 中直接执行

## 输出格式
只输出 SQL 语句，不加任何解释、不加 markdown 代码块标记。
"""


def _build_text2sql_prompt(
    question: str,
    schema_context: str,
    entities: dict,
) -> str:
    companies = entities.get("companies", [])
    years     = entities.get("years", [])
    metrics   = entities.get("metrics", [])

    return f"""{schema_context}

=== 查询信息 ===
问题：{question}
目标公司：{companies}
目标年份：{years}
目标指标：{metrics}

请生成 SQL 查询语句："""


def _generate_sql(
    question: str,
    schema_context: str,
    entities: dict,
    trace_id: str,
) -> str:
    prompt = _build_text2sql_prompt(question, schema_context, entities)

    def _call():
        resp = llm_generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_TEXT2SQL_SYSTEM,
                temperature=0.0,
            ),
        )
        sql = resp.text.strip()
        # 去掉可能的 markdown 代码块
        sql = re.sub(r"^```sql\s*", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"^```\s*",    "", sql)
        sql = re.sub(r"\s*```$",    "", sql)
        return sql.strip()

    return llm_call_with_retry(
        _call,
        max_retries=2,
        timeout_seconds=60,
        caller_name="text2sql",
        trace_id=trace_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# SQL 执行
# ══════════════════════════════════════════════════════════════════════════════

def _execute_sql(sql: str, db_path: str = DB_PATH) -> pd.DataFrame:
    """执行 SQL，返回 DataFrame。连接失败抛出 ConnectionError。"""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        df = pd.read_sql_query(sql, conn)
        conn.close()
        return df
    except sqlite3.OperationalError as e:
        raise RuntimeError(f"SQL执行错误: {e}") from e
    except Exception as e:
        if "unable to open" in str(e).lower() or "no such file" in str(e).lower():
            raise ConnectionError(f"数据库连接失败: {e}") from e
        raise


def _run_sql_with_timeout(sql: str, trace_id: str) -> dict:
    """带超时保护地执行 SQL。"""
    return run_with_timeout(
        _execute_sql,
        args=(sql,),
        timeout_seconds=30,
        worker_name="sql_executor",
        trace_id=trace_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 主节点函数
# ══════════════════════════════════════════════════════════════════════════════

@trace_node("sql_worker", tags=["worker", "sql"])
def sql_worker_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("sql_worker", trace_id)

    plan     = state.get("plan", {})
    entities = state.get("entities", {})

    # 未被调度时直接跳过
    if "sql" not in plan.get("workers", []):
        log.info("SQL Worker 未被调度，跳过")
        return {
            "worker_status": {
                "sql": WorkerStatusDict(
                    status="skipped", latency_ms=0,
                    error_type="", error_detail="", retried=False,
                ),
            },
        }

    schema_context = state.get("schema_context", "")
    question       = state.get("resolved_query", state.get("user_query", ""))

    log.info(f"开始 Text2SQL，question='{question[:60]}'")

    # ── Step 1：生成 SQL ──────────────────────────────────────────────────────
    sql_gen_result = run_with_timeout(
        _generate_sql,
        args=(question, schema_context, entities, trace_id),
        timeout_seconds=65,
        worker_name="sql_generator",
        trace_id=trace_id,
    )

    if sql_gen_result["status"] != "success":
        log.error(f"SQL 生成失败：{sql_gen_result['error_type']}")
        return {
            "worker_status": {
                "sql": WorkerStatusDict(
                    status=sql_gen_result["status"],
                    error_type=sql_gen_result["error_type"],
                    error_detail=sql_gen_result["error_detail"],
                    latency_ms=sql_gen_result["latency_ms"],
                    retried=False,
                ),
            },
            "sql_result": None,
            "sql_query_executed": "",
        }

    generated_sql = sql_gen_result["result"]
    log.info(f"SQL 生成完成：{generated_sql[:200]}")

    # ── Step 2：安全校验 ──────────────────────────────────────────────────────
    is_safe, reason = _validate_sql(generated_sql)
    if not is_safe:
        log.error(f"SQL 安全校验失败：{reason}")
        return {
            "worker_status": {
                "sql": WorkerStatusDict(
                    status="failed",
                    error_type="SQL_UNSAFE",
                    error_detail=reason,
                    latency_ms=sql_gen_result["latency_ms"],
                    retried=False,
                ),
            },
            "sql_result": None,
            "sql_query_executed": generated_sql,
        }

    # ── Step 3：执行 SQL ──────────────────────────────────────────────────────
    exec_result = _run_sql_with_timeout(generated_sql, trace_id)

    if exec_result["status"] != "success":
        log.error(f"SQL 执行失败：{exec_result['error_type']}")

        # 判断是否值得重试（数据库连接失败不重试，SQL 错误可以重试）
        retried = False
        if exec_result["error_type"] == "SQL_ERROR":
            log.info("SQL 语法错误，尝试修正重试...")
            # 把错误信息反馈给 LLM 重新生成
            fix_prompt = (
                f"以下 SQL 执行报错：\n{generated_sql}\n\n"
                f"错误信息：{exec_result['error_detail'][:300]}\n\n"
                f"请修正 SQL，只输出修正后的 SQL 语句："
            )
            fix_result = run_with_timeout(
                lambda: llm_generate_content(
                    model=_MODEL,
                    contents=fix_prompt,
                    config=types.GenerateContentConfig(temperature=0.0),
                ).text.strip(),
                timeout_seconds=30,
                worker_name="sql_fixer",
                trace_id=trace_id,
            )
            if fix_result["status"] == "success":
                fixed_sql = fix_result["result"]
                fixed_sql = re.sub(r"^```sql\s*", "", fixed_sql, flags=re.IGNORECASE)
                fixed_sql = re.sub(r"\s*```$", "", fixed_sql).strip()

                is_safe2, _ = _validate_sql(fixed_sql)
                if is_safe2:
                    exec_result = _run_sql_with_timeout(fixed_sql, trace_id)
                    if exec_result["status"] == "success":
                        generated_sql = fixed_sql
                        retried = True
                        log.info("SQL 修正重试成功")

        if exec_result["status"] != "success":
            return {
                "worker_status": {
                    "sql": WorkerStatusDict(
                        status=exec_result["status"],
                        error_type=exec_result["error_type"],
                        error_detail=exec_result["error_detail"],
                        latency_ms=exec_result["latency_ms"],
                        retried=retried,
                    ),
                },
                "sql_result": None,
                "sql_query_executed": generated_sql,
            }

    # ── Step 4：结果后处理 ────────────────────────────────────────────────────
    df: pd.DataFrame = exec_result["result"]
    total_latency    = sql_gen_result["latency_ms"] + exec_result["latency_ms"]

    log.info(
        f"SQL 执行成功",
        {"rows": len(df), "columns": list(df.columns), "latency_ms": total_latency},
    )

    return {
        "sql_result": serialize_sql_result(df),
        "sql_query_executed": generated_sql,
        "worker_status": {
            "sql": WorkerStatusDict(
                status="success",
                error_type="",
                error_detail="",
                latency_ms=total_latency,
                retried=False,
            ),
        },
    }
