"""
agent/nodes/memory_updater.py  —  记忆更新节点（流程的第 ⑪ 步，最后一步）
==========================================================================

【在流程中的位置】evaluator_o / degraded_response → ★memory_updater★ → END

【这个节点干什么？】

Memory Updater 是流程的"最后一站"——在把结果返回给用户之前，
先把本次交互的信息保存下来，方便下次使用。

它管理两种记忆：

  1. 短期记忆（Redis）── 对话历史
     存储最近 10 轮对话（用户问了什么、系统回答了什么）。
     设置 TTL=2小时，2小时内的追问可以利用上下文。
     用途：支持多轮对话（"那宁德时代呢？"→ 知道"那"指什么）

  2. 长期记忆（SQLite）── 用户偏好
     统计用户经常查询的公司、指标和行业，作为个性化推荐的依据。
     例如：发现用户总是查"比亚迪的碳排放" → 下次自动推荐相关议题

【降级处理】
  Redis 或 SQLite 写入失败时，只打 warning 日志，不阻断响应。
  即使记忆保存失败，用户仍然能收到分析结果。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from agent.state import AgentState
from agent.tracing import trace_node, TraceLogger

load_dotenv()
log = logging.getLogger(__name__)

REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MEMORY_DB   = os.getenv("MEMORY_DB", "./memory.db")
HISTORY_TTL = 60 * 60 * 2   # 2小时，单位秒
MAX_HISTORY = 10             # 最多保留10轮对话


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y"}


LEGACY_MEMORY_WRITES_ENABLED = _env_flag("LEGACY_MEMORY_WRITES_ENABLED", False)
QUERY_ANALYTICS_ENABLED = _env_flag("QUERY_ANALYTICS_ENABLED", True)


# ══════════════════════════════════════════════════════════════════════════════
# Redis 短期记忆
# ══════════════════════════════════════════════════════════════════════════════

def _update_redis_history(
    conversation_id: str,
    history: list[dict],
    log: TraceLogger,
) -> None:
    """把更新后的对话历史写入 Redis，设 TTL。失败只警告。"""
    try:
        import redis  # 延迟导入
        r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)
        key = f"esg:history:{conversation_id}"
        r.setex(key, HISTORY_TTL, json.dumps(history, ensure_ascii=False))
        log.info(f"Redis 历史写入成功，key={key}，轮数={len(history)}")
    except ImportError:
        log.warning("redis 库未安装，跳过短期记忆更新")
    except Exception as e:
        log.warning(f"Redis 写入失败（不影响响应）：{e}")


def _load_redis_history(
    conversation_id: str,
    log: TraceLogger,
) -> list[dict]:
    """从 Redis 读取历史记录。失败返回空列表。"""
    try:
        import redis
        r = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)
        key  = f"esg:history:{conversation_id}"
        data = r.get(key)
        if data:
            return json.loads(data)
    except Exception as e:
        log.warning(f"Redis 读取失败：{e}")
    return []


# ══════════════════════════════════════════════════════════════════════════════
# SQLite 长期记忆
# ══════════════════════════════════════════════════════════════════════════════

def _init_memory_db(db_path: str) -> None:
    """初始化长期记忆数据库（首次运行时创建表）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            conversation_id   TEXT PRIMARY KEY,
            preferred_companies TEXT DEFAULT '[]',
            preferred_metrics   TEXT DEFAULT '[]',
            preferred_industry  TEXT DEFAULT '',
            query_count         INTEGER DEFAULT 0,
            last_updated        TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            trace_id        TEXT,
            query           TEXT,
            query_class     TEXT,
            industry        TEXT,
            companies       TEXT,
            metrics         TEXT,
            eval_o_status   TEXT,
            is_degraded     INTEGER,
            latency_ms      INTEGER,
            created_at      TEXT
        )
    """)
    conn.commit()
    conn.close()


def _update_user_preferences(
    conversation_id: str,
    entities: dict,
    log: TraceLogger,
    db_path: str = MEMORY_DB,
) -> None:
    """
    累积更新用户偏好：常用公司、常用指标、行业倾向。
    采用频次累加策略：出现越多的实体权重越高。
    """
    try:
        _init_memory_db(db_path)
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            "SELECT * FROM user_preferences WHERE conversation_id = ?",
            (conversation_id,)
        ).fetchone()

        if row:
            pref_cos     = json.loads(row["preferred_companies"])
            pref_metrics = json.loads(row["preferred_metrics"])
            count        = row["query_count"]
        else:
            pref_cos, pref_metrics, count = [], [], 0

        # 频次累加（用列表记录，有重复则代表高频）
        new_cos     = pref_cos     + entities.get("companies", [])
        new_metrics = pref_metrics + entities.get("metrics", [])

        # 去重保留 top-10（按出现频次）
        def _top_n(lst: list, n: int = 10) -> list:
            from collections import Counter
            return [item for item, _ in Counter(lst).most_common(n)]

        conn.execute("""
            INSERT INTO user_preferences
                (conversation_id, preferred_companies, preferred_metrics,
                 preferred_industry, query_count, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                preferred_companies = excluded.preferred_companies,
                preferred_metrics   = excluded.preferred_metrics,
                preferred_industry  = excluded.preferred_industry,
                query_count         = excluded.query_count,
                last_updated        = excluded.last_updated
        """, (
            conversation_id,
            json.dumps(_top_n(new_cos),     ensure_ascii=False),
            json.dumps(_top_n(new_metrics), ensure_ascii=False),
            entities.get("industry", ""),
            count + 1,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        conn.close()
        log.info(f"用户偏好更新成功，query_count={count+1}")

    except Exception as e:
        log.warning(f"长期记忆写入失败（不影响响应）：{e}")


def _log_query(state: AgentState, log: TraceLogger, db_path: str = MEMORY_DB) -> None:
    """把本次请求摘要记录到 query_log，用于后续分析和调试。"""
    try:
        _init_memory_db(db_path)
        entities = state.get("entities", {})

        # 计算总耗时（从 node_trace 求和）
        total_ms = sum(
            n.get("duration_ms", 0)
            for n in state.get("node_trace", [])
        )

        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("""
            INSERT INTO query_log
                (conversation_id, trace_id, query, query_class, industry,
                 companies, metrics, eval_o_status, is_degraded, latency_ms, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            state.get("conversation_id", ""),
            state.get("trace_id", ""),
            state.get("resolved_query", "")[:500],
            state.get("query_class", ""),
            entities.get("industry", ""),
            json.dumps(entities.get("companies", []), ensure_ascii=False),
            json.dumps(entities.get("metrics", []),   ensure_ascii=False),
            state.get("eval_o_status", ""),
            1 if state.get("is_degraded") else 0,
            total_ms,
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        conn.close()

    except Exception as e:
        log.warning(f"query_log 写入失败：{e}")


# ══════════════════════════════════════════════════════════════════════════════
# 主节点函数
# ══════════════════════════════════════════════════════════════════════════════

@trace_node("memory_updater", tags=["memory"])
def memory_updater_node(state: AgentState) -> AgentState:
    trace_id        = state.get("trace_id", "")
    conversation_id = state.get("conversation_id", "")
    log             = TraceLogger("memory_updater", trace_id)

    # ── 1. 更新对话历史 ───────────────────────────────────────────────────────
    history = list(state.get("history", []))

    # 追加本轮对话
    user_turn = {
        "role":    "user",
        "content": state.get("user_query", ""),
        "turn_id": len(history) + 1,
    }
    assistant_turn = {
        "role":    "assistant",
        "content": state.get("analysis", "")[:500],   # 只存摘要，节省空间
        "turn_id": len(history) + 2,
        "key_findings": state.get("key_findings", []),
    }
    history = (history + [user_turn, assistant_turn])[-MAX_HISTORY * 2:]

    state["history"] = history

    # ── 2. 写入 Redis（短期） ──────────────────────────────────────────────────
    if LEGACY_MEMORY_WRITES_ENABLED:
        _update_redis_history(conversation_id, history, log)
    else:
        log.info("skip legacy redis history write; checkpointer is primary memory backend")

    # ── 3. 更新用户偏好（长期） ───────────────────────────────────────────────
    entities = state.get("entities", {})
    if LEGACY_MEMORY_WRITES_ENABLED and (entities.get("companies") or entities.get("metrics")):
        _update_user_preferences(conversation_id, entities, log)
    elif not LEGACY_MEMORY_WRITES_ENABLED:
        log.info("skip legacy sqlite preference write; checkpointer is primary memory backend")

    # ── 4. 记录 query_log ────────────────────────────────────────────────────
    if QUERY_ANALYTICS_ENABLED:
        _log_query(state, log)
    else:
        log.info("skip query_log write; analytics disabled by config")

    log.info("记忆更新完成")
    return state
