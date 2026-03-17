"""
agent/nodes/map_reduce.py  —  Map-Reduce 上下文压缩节点（流程的第 ⑧ 步）
=========================================================================

【在流程中的位置】evaluator_d → ★map_reduce★ → synthesizer

【这个节点要解决什么问题？】

RAG Worker 可能检索回来几十个文本段落（chunk），总共上万字。
如果直接把这么多文字喂给 Synthesizer 的 LLM，会：
  1. 超出 LLM 的 token 上限
  2. LLM 被大量信息淹没，生成质量下降

Map-Reduce 是一种经典的"分而治之"策略（来源于 Google 的 MapReduce 论文）：

  Map 阶段 ── 把 RAG chunks 按「公司×年份」分组，
               每组独立调用 LLM 生成 200 字摘要
               （多组可并行执行，使用 ThreadPoolExecutor）

  Reduce 阶段 ── 把所有摘要 + SQL 数据合并为一份
                   "高密度上下文"（< 8000 token）

【何时触发】
  - 如果输入上下文 > 12,000 token → 触发 Map-Reduce
  - 如果输入上下文 ≤ 12,000 token → 直接透传，不做任何处理

【写入 State 的关键字段】
  - map_reduce_applied: 是否触发了压缩
  - compressed_context: 压缩后的高密度上下文
  - map_reduce_stats: 执行统计（压缩率等）
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from google import genai
from google.genai import types
from dotenv import load_dotenv

from agent.state import AgentState, MapReduceStats, get_sql_result_dataframe
from agent.tracing import trace_node, TraceLogger, llm_call_with_retry
import time

load_dotenv()
log     = logging.getLogger(__name__)
_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
_MODEL  = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")

# token 阈值（粗估：中文1字≈1.5token）
TOKEN_THRESHOLD   = 12000
CHARS_THRESHOLD   = int(TOKEN_THRESHOLD / 1.5)   # ≈ 8000 字
TARGET_CHARS      = int(8000 / 1.5)              # ≈ 5333 字
MAX_MAP_WORKERS   = 8

# ── Map 压缩 prompt ───────────────────────────────────────────────────────────
_MAP_PROMPT = """\
你是 ESG 数据压缩助手。将以下 {company} {year}年 的 ESG 报告段落压缩为 ≤200字的结构化摘要。

压缩规则：
1. 保留所有数字、比率、具体措施名称、目标年份
2. 保留因果关系关键描述（"因为…所以…"、"由于…导致…"）
3. 删除公司简介、重复声明、套话、模板文字
4. 用"•"分点，每点 ≤30字
5. 如有口径说明（计算方法/统计范围），必须保留

原文段落：
{raw_text}

请直接输出压缩摘要，不加任何前言和解释："""

# ── Reduce 汇总 prompt ────────────────────────────────────────────────────────
_REDUCE_PROMPT = """\
你是 ESG 数据整合助手。将以下多家公司的分组摘要整合为一份高密度的分析上下文。

要求：
1. 按公司分组，保持结构清晰
2. 保留所有数字和关键发现
3. 标注未披露项（若某公司某指标摘要中未提及，标注"未披露"）
4. 总输出 ≤ {target_chars}字

各公司分组摘要：
{summaries}

请直接输出整合后的上下文："""


# ══════════════════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════════════════

def _estimate_chars(state: AgentState) -> int:
    """粗略估算 Synthesizer 将要消费的上下文字符数。"""
    total = 0
    rag_result = state.get("rag_result")
    if rag_result:
        for chunk in rag_result.get("chunks", []):
            total += len(chunk.get("text", ""))
    sql_result = get_sql_result_dataframe(state)
    if sql_result is not None:
        try:
            total += len(sql_result.to_string())
        except Exception:
            pass
    return total


def _group_chunks_by_entity(
    chunks: list[dict],
) -> dict[tuple[str, str], list[dict]]:
    """按 (company_name, year) 分组 chunk。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for chunk in chunks:
        key = (
            chunk.get("company_name", "unknown"),
            str(chunk.get("year", "unknown")),
        )
        groups.setdefault(key, []).append(chunk)
    return groups


def _map_compress_group(
    company: str,
    year: str,
    chunks: list[dict],
) -> tuple[str, str, str]:
    """
    Map 任务：压缩单个 (company, year) 分组的 chunk。
    返回 (company, year, summary)。
    """
    raw_text = "\n\n".join(
        f"[第{c.get('page_num','')}页] {c.get('text','')}"
        for c in chunks
    )[:4000]  # 单组最多 4000 字原文

    prompt = _MAP_PROMPT.format(
        company=company, year=year, raw_text=raw_text
    )

    def _call():
        resp = _client.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0),
        )
        return resp.text.strip()

    try:
        summary = llm_call_with_retry(
            _call, max_retries=1, timeout_seconds=30,
            caller_name=f"map_{company}_{year}", trace_id="",
        )
        return company, year, summary
    except Exception as e:
        log.warning(f"Map 压缩失败 [{company} {year}]：{e}")
        # 失败时返回截断原文
        return company, year, raw_text[:200] + "..."


def _reduce_summaries(
    summaries: dict[tuple[str, str], str],
    target_chars: int = TARGET_CHARS,
) -> str:
    """
    Reduce：把所有分组摘要汇总为高密度上下文。
    """
    summaries_text = ""
    for (company, year), summary in sorted(summaries.items()):
        summaries_text += f"\n### {company} {year}年\n{summary}\n"

    if len(summaries_text) <= target_chars:
        return summaries_text  # 已经够短，无需二次压缩

    prompt = _REDUCE_PROMPT.format(
        target_chars=target_chars,
        summaries=summaries_text[:8000],  # 防止 prompt 本身过长
    )

    def _call():
        resp = _client.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0),
        )
        return resp.text.strip()

    try:
        return llm_call_with_retry(
            _call, max_retries=1, timeout_seconds=45,
            caller_name="reduce", trace_id="",
        )
    except Exception as e:
        log.warning(f"Reduce 汇总失败：{e}，返回截断摘要")
        return summaries_text[:target_chars]


# ══════════════════════════════════════════════════════════════════════════════
# 主节点函数
# ══════════════════════════════════════════════════════════════════════════════

@trace_node("map_reduce", tags=["compression"])
def map_reduce_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("map_reduce", trace_id)

    # ── 检查是否需要压缩 ─────────────────────────────────────────────────────
    estimated_chars = _estimate_chars(state)
    log.info(f"上下文预估：{estimated_chars} 字符（阈值 {CHARS_THRESHOLD}）")

    if estimated_chars <= CHARS_THRESHOLD:
        log.info("上下文未超阈值，跳过 Map-Reduce 压缩")
        state["map_reduce_applied"] = False
        state["compressed_context"] = None
        state["map_reduce_stats"]   = MapReduceStats(
            triggered=False,
            original_tokens=int(estimated_chars * 1.5),
            compressed_tokens=0,
            compression_ratio=1.0,
            summaries_count=0,
            map_latency_ms=0,
            reduce_latency_ms=0,
        )
        return state

    log.info(f"上下文超阈值，启动 Map-Reduce 压缩")

    rag_result = state.get("rag_result") or {}
    chunks     = rag_result.get("chunks", [])

    # ── Map 阶段（并行） ─────────────────────────────────────────────────────
    t_map_start = time.perf_counter()
    groups      = _group_chunks_by_entity(chunks)

    summaries: dict[tuple[str, str], str] = {}
    max_workers = min(MAX_MAP_WORKERS, len(groups))

    if max_workers > 0:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_map_compress_group, company, year, grp_chunks): (company, year)
                for (company, year), grp_chunks in groups.items()
            }
            for future in as_completed(futures):
                try:
                    comp, yr, summary = future.result(timeout=40)
                    summaries[(comp, yr)] = summary
                except Exception as e:
                    key = futures[future]
                    log.warning(f"Map 任务异常 {key}：{e}")
                    summaries[key] = f"[{key[0]} {key[1]}年摘要生成失败]"

    map_latency_ms = int((time.perf_counter() - t_map_start) * 1000)
    log.info(f"Map 阶段完成：{len(summaries)} 组，耗时 {map_latency_ms}ms")

    # ── Reduce 阶段 ───────────────────────────────────────────────────────────
    t_reduce_start    = time.perf_counter()
    compressed_context = _reduce_summaries(summaries)
    reduce_latency_ms  = int((time.perf_counter() - t_reduce_start) * 1000)
    log.info(f"Reduce 完成：{len(compressed_context)} 字符，耗时 {reduce_latency_ms}ms")

    original_tokens    = int(estimated_chars * 1.5)
    compressed_tokens  = int(len(compressed_context) * 1.5)
    compression_ratio  = round(compressed_tokens / original_tokens, 2) if original_tokens else 1.0

    log.info(
        f"压缩完成",
        {
            "original_tokens":   original_tokens,
            "compressed_tokens": compressed_tokens,
            "ratio":             compression_ratio,
        },
    )

    state["map_reduce_applied"] = True
    state["compressed_context"] = compressed_context
    state["map_reduce_stats"]   = MapReduceStats(
        triggered=True,
        original_tokens=original_tokens,
        compressed_tokens=compressed_tokens,
        compression_ratio=compression_ratio,
        summaries_count=len(summaries),
        map_latency_ms=map_latency_ms,
        reduce_latency_ms=reduce_latency_ms,
    )

    return state
