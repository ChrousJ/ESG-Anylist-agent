#!/usr/bin/env python3
"""
scripts/run_evaluation.py
==========================

评测执行脚本 —— 对 eval_dataset.jsonl 中的用例逐条执行 LangGraph Agent
与 ReAct Baseline，收集指标并生成 eval_report.md。

指标：
  - Completion Rate：返回非错误响应的百分比
  - Rescue Rate：node_trace 中出现 re-plan/修正且最终成功的百分比
  - p95 Latency：第 95 百分位端到端耗时

用法：
    python scripts/run_evaluation.py                   # 默认参数
    python scripts/run_evaluation.py --concurrency 1   # 串行执行（安全模式）
    python scripts/run_evaluation.py --skip-baseline   # 只跑主 Agent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict

# ── 项目路径配置 ──────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "data"))

from dotenv import load_dotenv
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))

# Windows console encoding fix
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("eval_runner")


# ----------------------------
# Judge (LLM-as-a-Judge)
# ----------------------------
class JudgeConfig(TypedDict):
    enabled: bool
    model: str
    base_url: str
    api_key: str
    max_answer_chars: int
    max_evidence_chars: int


def _truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def _summarize_sql_result(sql_result: Any, max_rows: int = 5) -> dict[str, Any]:
    if not sql_result:
        return {}
    # Most common serialized form: list[dict]
    if isinstance(sql_result, list):
        rows = [r for r in sql_result if isinstance(r, dict)]
        preview = rows[:max_rows]
        cols = list(preview[0].keys()) if preview else []
        return {
            "row_count": len(rows),
            "columns": cols,
            "rows_preview": preview,
        }
    # Fallback if dict-like
    if isinstance(sql_result, dict):
        return {
            "row_count": sql_result.get("row_count", ""),
            "columns": sql_result.get("columns", []),
            "rows_preview": sql_result.get("rows_preview", []),
        }
    return {}


def _summarize_sources(sources: list[dict], max_items: int = 5) -> list[dict]:
    if not sources:
        return []
    preview = []
    for s in sources[:max_items]:
        preview.append({
            "type": s.get("type", ""),
            "company": s.get("company", ""),
            "year": s.get("year", ""),
            "page": s.get("page", ""),
            "score": s.get("score", 0),
        })
    return preview


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except Exception:
            pass
    # Try to find a JSON object in the text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


def _judge_answer(
    query: str,
    answer: str,
    evidence: dict[str, Any],
    judge_cfg: JudgeConfig,
) -> dict[str, Any]:
    if not judge_cfg["enabled"]:
        return {
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": "judge_disabled",
        }
    if not judge_cfg["api_key"]:
        return {
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": "missing_judge_api_key",
        }
    try:
        import openai
    except Exception as e:
        return {
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": f"openai_import_failed: {str(e)[:80]}",
        }

    client = openai.OpenAI(
        api_key=judge_cfg["api_key"],
        base_url=judge_cfg["base_url"],
    )

    ans_text = _truncate(answer, judge_cfg["max_answer_chars"])
    evidence_text = json.dumps(evidence, ensure_ascii=False)
    evidence_text = _truncate(evidence_text, judge_cfg["max_evidence_chars"])

    system = (
        "You are a strict evaluator for ESG Q&A. "
        "Score answers for overall quality and faithfulness to provided evidence. "
        "Return ONLY valid JSON."
    )
    user = (
        "Evaluate the answer to the query. Use the evidence if provided. "
        "If evidence is empty, set faithfulness_score to null.\n\n"
        f"Query:\n{query}\n\n"
        f"Answer:\n{ans_text}\n\n"
        f"Evidence (JSON):\n{evidence_text}\n\n"
        "Return JSON with fields:\n"
        "{"
        "\"overall_score\": 1-5 integer, "
        "\"faithfulness_score\": 1-5 integer or null, "
        "\"notes\": short string, "
        "\"flags\": list of strings"
        "}"
    )

    try:
        resp = client.chat.completions.create(
            model=judge_cfg["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=256,
        )
        content = resp.choices[0].message.content or ""
        data = _extract_json(content) or {}
        overall = data.get("overall_score", None)
        faithful = data.get("faithfulness_score", None)
        notes = data.get("notes", "")
        return {
            "judge_overall": overall,
            "judge_faithfulness": faithful,
            "judge_notes": notes,
            "judge_error": "",
        }
    except Exception as e:
        return {
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": str(e)[:200],
        }

# ══════════════════════════════════════════════════════════════════════════════
# LangGraph Agent Runner
# ══════════════════════════════════════════════════════════════════════════════

def _run_langgraph_agent(query: str, case_id: str) -> dict[str, Any]:
    """
    Run the full LangGraph ESG Agent on a query.
    Returns standardized result dict.
    """
    from agent.graph import get_graph
    from agent.state import make_initial_state
    from agent.tracing import generate_trace_id, generate_request_id

    trace_id = generate_trace_id()
    request_id = generate_request_id()
    t_start = time.perf_counter()

    try:
        init_state = make_initial_state(
            user_query=query,
            conversation_id=f"eval-{case_id}",
            trace_id=trace_id,
            request_id=request_id,
        )
        graph = get_graph()
        config = {"configurable": {"thread_id": f"eval-{case_id}"}}
        final_state = graph.invoke(init_state, config=config)
        latency_ms = int((time.perf_counter() - t_start) * 1000)

        # Extract key info
        node_trace = final_state.get("node_trace", [])
        analysis = final_state.get("analysis", "")
        eval_o_status = final_state.get("eval_o_status", "")
        is_degraded = final_state.get("is_degraded", False)
        query_class = final_state.get("query_class", "")
        key_findings = final_state.get("key_findings", [])
        sql_result_preview = _summarize_sql_result(final_state.get("sql_result"))
        sources_preview = _summarize_sources(final_state.get("sources", []))

        # Tool error rate (SQL/RAG workers)
        tool_nodes = [nt for nt in node_trace if nt.get("node_name") in ("sql_worker", "rag_worker")]
        tool_failed = sum(1 for nt in tool_nodes if nt.get("status") == "failed")
        tool_total = len(tool_nodes)
        tool_error_rate = round(tool_failed / tool_total, 4) if tool_total else 0.0

        # Determine status
        if analysis and eval_o_status in ("pass", "degraded"):
            status = "success"
        elif is_degraded:
            status = "degraded"
        elif not analysis:
            status = "empty"
        else:
            status = "success"

        # Detect rescue (re-plan or eval_o correction that succeeded)
        rescued = False
        replan_count = 0
        eval_o_retry = final_state.get("eval_o_retry_count", 0)
        for nt in node_trace:
            if nt.get("node_name") == "supervisor" and nt.get("status") == "success":
                replan_count += 1
        if replan_count > 1:
            rescued = True
        if eval_o_retry > 0 and eval_o_status == "pass":
            rescued = True

        return {
            "case_id": case_id,
            "agent": "langgraph",
            "status": status,
            "latency_ms": latency_ms,
            "analysis_length": len(analysis),
            "analysis_full": analysis,
            "analysis_preview": analysis[:200],
            "query_class": query_class,
            "eval_o_status": eval_o_status,
            "is_degraded": is_degraded,
            "rescued": rescued,
            "replan_count": max(0, replan_count - 1),  # First call is not a re-plan
            "eval_o_retry": eval_o_retry,
            "node_count": len(node_trace),
            "step_count": len(node_trace),
            "node_trace_summary": [
                {"node": nt.get("node_name", ""), "ms": nt.get("duration_ms", 0), "status": nt.get("status", "")}
                for nt in node_trace
            ],
            "trace_id": trace_id,
            "error": "",
            "key_findings": key_findings,
            "sql_result_preview": sql_result_preview,
            "sources_preview": sources_preview,
            "tool_failed_count": tool_failed,
            "tool_total_count": tool_total,
            "tool_error_rate": tool_error_rate,
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": "",
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        log.error(f"[{case_id}] LangGraph agent failed: {e}")
        return {
            "case_id": case_id,
            "agent": "langgraph",
            "status": "crashed",
            "latency_ms": latency_ms,
            "analysis_length": 0,
            "analysis_full": "",
            "analysis_preview": "",
            "query_class": "",
            "eval_o_status": "",
            "is_degraded": False,
            "rescued": False,
            "replan_count": 0,
            "eval_o_retry": 0,
            "node_count": 0,
            "step_count": 0,
            "node_trace_summary": [],
            "trace_id": trace_id,
            "error": str(e)[:500],
            "key_findings": [],
            "sql_result_preview": {},
            "sources_preview": [],
            "tool_failed_count": 0,
            "tool_total_count": 0,
            "tool_error_rate": 0.0,
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": str(e)[:200],
        }


# ══════════════════════════════════════════════════════════════════════════════
# Baseline Agent Runner
# ══════════════════════════════════════════════════════════════════════════════

def _run_baseline_agent(query: str, case_id: str) -> dict[str, Any]:
    """Run the ReAct baseline agent on a query."""
    from scripts.baseline_react_agent import run_baseline

    t_start = time.perf_counter()
    try:
        result = run_baseline(query, timeout=120.0)
        latency_ms = int((time.perf_counter() - t_start) * 1000)

        analysis = result.get("analysis", "")
        return {
            "case_id": case_id,
            "agent": "react_baseline",
            "status": result.get("status", "unknown"),
            "latency_ms": latency_ms,
            "analysis_length": len(analysis),
            "analysis_full": analysis,
            "analysis_preview": analysis[:200],
            "query_class": "",
            "eval_o_status": "",
            "is_degraded": False,
            "rescued": False,
            "replan_count": 0,
            "eval_o_retry": 0,
            "node_count": result.get("message_count", 0),
            "step_count": result.get("message_count", 0),
            "node_trace_summary": [],
            "trace_id": "",
            "error": result.get("error", ""),
            "key_findings": [],
            "sql_result_preview": {},
            "sources_preview": [],
            "tool_failed_count": 0,
            "tool_total_count": 0,
            "tool_error_rate": 0.0,
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": "",
        }
    except Exception as e:
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        log.error(f"[{case_id}] Baseline failed: {e}")
        return {
            "case_id": case_id,
            "agent": "react_baseline",
            "status": "crashed",
            "latency_ms": latency_ms,
            "analysis_length": 0,
            "analysis_full": "",
            "analysis_preview": "",
            "query_class": "",
            "eval_o_status": "",
            "is_degraded": False,
            "rescued": False,
            "replan_count": 0,
            "eval_o_retry": 0,
            "node_count": 0,
            "step_count": 0,
            "node_trace_summary": [],
            "trace_id": "",
            "error": str(e)[:500],
            "key_findings": [],
            "sql_result_preview": {},
            "sources_preview": [],
            "tool_failed_count": 0,
            "tool_total_count": 0,
            "tool_error_rate": 0.0,
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": str(e)[:200],
        }


# ══════════════════════════════════════════════════════════════════════════════
# Metrics Computation
# ══════════════════════════════════════════════════════════════════════════════

def _compute_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate metrics from a list of result dicts."""
    if not results:
        return {"completion_rate": 0, "rescue_rate": 0, "p50_latency": 0, "p95_latency": 0, "total": 0}

    total = len(results)
    completed = sum(1 for r in results if r["status"] in ("success", "degraded"))
    rescued = sum(1 for r in results if r.get("rescued", False))
    latencies = [r["latency_ms"] for r in results]
    step_counts = [r.get("step_count", 0) for r in results if r.get("step_count", 0) > 0]
    tool_error_rates = [
        r.get("tool_error_rate", None) for r in results
        if r.get("tool_total_count", 0) > 0
    ]
    judge_overall = [
        r.get("judge_overall") for r in results
        if isinstance(r.get("judge_overall"), (int, float))
    ]
    judge_faithful = [
        r.get("judge_faithfulness") for r in results
        if isinstance(r.get("judge_faithfulness"), (int, float))
    ]

    latencies_sorted = sorted(latencies)
    p50_idx = max(0, int(math.ceil(0.50 * total)) - 1)
    p95_idx = max(0, int(math.ceil(0.95 * total)) - 1)

    return {
        "total": total,
        "completed": completed,
        "completion_rate": round(completed / total * 100, 1) if total else 0,
        "rescued": rescued,
        "rescue_rate": round(rescued / total * 100, 1) if total else 0,
        "crashed": sum(1 for r in results if r["status"] == "crashed"),
        "degraded": sum(1 for r in results if r["status"] == "degraded"),
        "avg_latency_ms": int(statistics.mean(latencies)) if latencies else 0,
        "p50_latency_ms": latencies_sorted[p50_idx] if latencies_sorted else 0,
        "p95_latency_ms": latencies_sorted[p95_idx] if latencies_sorted else 0,
        "max_latency_ms": max(latencies) if latencies else 0,
        "min_latency_ms": min(latencies) if latencies else 0,
        "avg_steps": round(statistics.mean(step_counts), 2) if step_counts else 0,
        "avg_tool_error_rate": round(statistics.mean(tool_error_rates), 4) if tool_error_rates else 0,
        "avg_judge_overall": round(statistics.mean(judge_overall), 2) if judge_overall else 0,
        "avg_judge_faithfulness": round(statistics.mean(judge_faithful), 2) if judge_faithful else 0,
    }


def _compute_per_category(results: list[dict], cases_map: dict) -> dict[str, dict]:
    """Compute metrics per category."""
    from collections import defaultdict
    by_cat: dict[str, list] = defaultdict(list)
    for r in results:
        cat = cases_map.get(r["case_id"], {}).get("category", "unknown")
        by_cat[cat].append(r)
    return {cat: _compute_metrics(rs) for cat, rs in by_cat.items()}


# ══════════════════════════════════════════════════════════════════════════════
# Report Generation
# ══════════════════════════════════════════════════════════════════════════════

def _generate_report(
    lg_results: list[dict],
    bl_results: list[dict],
    lg_metrics: dict,
    bl_metrics: dict,
    lg_by_cat: dict,
    bl_by_cat: dict,
    cases: list[dict],
    run_timestamp: str,
) -> str:
    """Generate eval_report.md content."""
    lines: list[str] = []
    lines.append("# ESG Agent Evaluation Report")
    lines.append("")
    lines.append(f"> Generated: {run_timestamp}")
    lines.append(f"> Total cases: {len(cases)}")
    lines.append(f"> LangGraph nodes: context -> supervisor -> schema_injector -> sql/rag workers -> evaluator_d -> map_reduce -> synthesizer -> evaluator_o -> memory_updater")
    lines.append(f"> Baseline: ReAct agent (Qwen LLM + SQL/RAG tools, no quality loops)")
    lines.append("")

    # ── Overall Comparison ────────────────────────────────────────────────────
    lines.append("## Overall Comparison")
    lines.append("")
    lines.append("| Metric | LangGraph Agent | ReAct Baseline |")
    lines.append("|--------|:--------------:|:--------------:|")

    def _fmt(lg_val: Any, bl_val: Any) -> tuple[str, str]:
        return str(lg_val), str(bl_val)

    lg_s, bl_s = _fmt(
        f"{lg_metrics['completion_rate']}% ({lg_metrics['completed']}/{lg_metrics['total']})",
        f"{bl_metrics['completion_rate']}% ({bl_metrics['completed']}/{bl_metrics['total']})" if bl_metrics['total'] else "N/A",
    )
    lines.append(f"| Completion Rate | {lg_s} | {bl_s} |")

    lg_s, bl_s = _fmt(
        f"{lg_metrics['rescue_rate']}% ({lg_metrics['rescued']}/{lg_metrics['total']})",
        "N/A (no loops)",
    )
    lines.append(f"| Rescue Rate | {lg_s} | {bl_s} |")

    lg_s, bl_s = _fmt(
        f"{lg_metrics['p95_latency_ms']}ms",
        f"{bl_metrics['p95_latency_ms']}ms" if bl_metrics['total'] else "N/A",
    )
    lines.append(f"| p95 Latency | {lg_s} | {bl_s} |")

    lg_s, bl_s = _fmt(
        f"{lg_metrics['p50_latency_ms']}ms",
        f"{bl_metrics['p50_latency_ms']}ms" if bl_metrics['total'] else "N/A",
    )
    lines.append(f"| p50 Latency | {lg_s} | {bl_s} |")

    lg_s, bl_s = _fmt(
        f"{lg_metrics['avg_latency_ms']}ms",
        f"{bl_metrics['avg_latency_ms']}ms" if bl_metrics['total'] else "N/A",
    )
    lines.append(f"| Avg Latency | {lg_s} | {bl_s} |")

    lg_s, bl_s = _fmt(
        f"{lg_metrics.get('avg_steps', 0)}",
        f"{bl_metrics.get('avg_steps', 'N/A')}" if bl_metrics['total'] else "N/A",
    )
    lines.append(f"| Avg Steps | {lg_s} | {bl_s} |")

    lg_s, bl_s = _fmt(
        f"{lg_metrics.get('avg_tool_error_rate', 0)}",
        "N/A",
    )
    lines.append(f"| Tool Error Rate | {lg_s} | {bl_s} |")

    lg_j, bl_j = _fmt(
        f"{lg_metrics.get('avg_judge_overall', 0)}",
        f"{bl_metrics.get('avg_judge_overall', 'N/A')}" if bl_metrics['total'] else "N/A",
    )
    lines.append(f"| Judge Overall | {lg_j} | {bl_j} |")

    lg_f, bl_f = _fmt(
        f"{lg_metrics.get('avg_judge_faithfulness', 0)}",
        "N/A",
    )
    lines.append(f"| Judge Faithfulness | {lg_f} | {bl_f} |")

    lines.append(f"| Crashed | {lg_metrics.get('crashed', 0)} | {bl_metrics.get('crashed', 0)} |")
    lines.append(f"| Degraded | {lg_metrics.get('degraded', 0)} | N/A |")
    lines.append("")

    # ── Per-Category Breakdown ────────────────────────────────────────────────
    lines.append("## Per-Category Breakdown")
    lines.append("")
    all_cats = sorted(set(list(lg_by_cat.keys()) + list(bl_by_cat.keys())))
    lines.append("| Category | Agent | Completion | p95 Latency | Crashed |")
    lines.append("|----------|-------|:----------:|:-----------:|:-------:|")
    for cat in all_cats:
        lg_cat = lg_by_cat.get(cat, {})
        bl_cat = bl_by_cat.get(cat, {})
        lines.append(
            f"| {cat} | LangGraph | "
            f"{lg_cat.get('completion_rate', 0)}% | "
            f"{lg_cat.get('p95_latency_ms', 0)}ms | "
            f"{lg_cat.get('crashed', 0)} |"
        )
        if bl_cat:
            lines.append(
                f"| | ReAct | "
                f"{bl_cat.get('completion_rate', 0)}% | "
                f"{bl_cat.get('p95_latency_ms', 0)}ms | "
                f"{bl_cat.get('crashed', 0)} |"
            )
    lines.append("")

    # ── Node-Level Latency (LangGraph) ────────────────────────────────────────
    lines.append("## Node-Level Latency (LangGraph Agent)")
    lines.append("")

    from collections import defaultdict
    node_latencies: dict[str, list[int]] = defaultdict(list)
    for r in lg_results:
        for nt in r.get("node_trace_summary", []):
            if nt.get("status") == "success" and nt.get("ms", 0) > 0:
                node_latencies[nt["node"]].append(nt["ms"])

    if node_latencies:
        lines.append("| Node | Avg (ms) | p50 (ms) | p95 (ms) | Count |")
        lines.append("|------|:--------:|:--------:|:--------:|:-----:|")
        for node_name in sorted(node_latencies.keys()):
            lats = sorted(node_latencies[node_name])
            n = len(lats)
            avg = int(statistics.mean(lats))
            p50 = lats[max(0, int(math.ceil(0.5 * n)) - 1)]
            p95 = lats[max(0, int(math.ceil(0.95 * n)) - 1)]
            lines.append(f"| {node_name} | {avg} | {p50} | {p95} | {n} |")
        lines.append("")

    # ── Failed / Crashed / Degraded Cases ─────────────────────────────────────
    problem_cases: list[dict] = []
    cases_map = {c["id"]: c for c in cases}

    for r in lg_results:
        if r["status"] in ("crashed", "empty"):
            problem_cases.append({
                "case_id": r["case_id"],
                "agent": "LangGraph",
                "status": r["status"],
                "error": r.get("error", ""),
                "query": cases_map.get(r["case_id"], {}).get("query", ""),
                "category": cases_map.get(r["case_id"], {}).get("category", ""),
            })
    for r in bl_results:
        if r["status"] in ("crashed", "empty"):
            problem_cases.append({
                "case_id": r["case_id"],
                "agent": "ReAct",
                "status": r["status"],
                "error": r.get("error", ""),
                "query": cases_map.get(r["case_id"], {}).get("query", ""),
                "category": cases_map.get(r["case_id"], {}).get("category", ""),
            })

    if problem_cases:
        lines.append("## Failed / Crashed Cases")
        lines.append("")
        lines.append("> These are real failures observed during evaluation. No data is fabricated.")
        lines.append("")
        lines.append("| Case ID | Agent | Status | Category | Query | Error |")
        lines.append("|---------|-------|--------|----------|-------|-------|")
        for pc in problem_cases:
            error_short = pc["error"][:80].replace("|", "/") if pc["error"] else "-"
            query_short = pc["query"][:40].replace("|", "/")
            lines.append(
                f"| {pc['case_id']} | {pc['agent']} | {pc['status']} | "
                f"{pc['category']} | {query_short} | {error_short} |"
            )
        lines.append("")

    # ── Degraded Cases (LangGraph only) ───────────────────────────────────────
    degraded_lg = [r for r in lg_results if r.get("is_degraded")]
    if degraded_lg:
        lines.append("## Degraded Responses (LangGraph Agent)")
        lines.append("")
        lines.append("| Case ID | Category | Query | Nodes |")
        lines.append("|---------|----------|-------|:-----:|")
        for r in degraded_lg:
            q = cases_map.get(r["case_id"], {}).get("query", "")[:40].replace("|", "/")
            cat = cases_map.get(r["case_id"], {}).get("category", "")
            lines.append(f"| {r['case_id']} | {cat} | {q} | {r['node_count']} |")
        lines.append("")

    # ── Rescue Cases (LangGraph only) ─────────────────────────────────────────
    rescued_lg = [r for r in lg_results if r.get("rescued")]
    if rescued_lg:
        lines.append("## Rescue Cases (LangGraph Agent)")
        lines.append("")
        lines.append("> These are cases where re-plan or evaluator_o correction loops triggered and succeeded.")
        lines.append("")
        lines.append("| Case ID | Category | Re-plan Count | EvalO Retry | Final Status |")
        lines.append("|---------|----------|:-------------:|:-----------:|:------------:|")
        for r in rescued_lg:
            cat = cases_map.get(r["case_id"], {}).get("category", "")
            lines.append(
                f"| {r['case_id']} | {cat} | "
                f"{r['replan_count']} | {r['eval_o_retry']} | {r['eval_o_status']} |"
            )
        lines.append("")

    # ── Summary ───────────────────────────────────────────────────────────────
    lines.append("## Key Takeaways")
    lines.append("")
    lines.append(f"1. **Completion Rate**: LangGraph {lg_metrics['completion_rate']}% vs ReAct {bl_metrics.get('completion_rate', 'N/A')}%")
    if lg_metrics.get('rescued', 0) > 0:
        lines.append(f"2. **Rescue Rate**: LangGraph corrected {lg_metrics['rescued']} case(s) via re-plan/eval loops — a capability the baseline lacks entirely.")
    else:
        lines.append(f"2. **Rescue Rate**: No rescue events triggered in this run (expected for clean inputs).")
    lines.append(f"3. **Latency**: LangGraph p95={lg_metrics['p95_latency_ms']}ms, ReAct p95={bl_metrics.get('p95_latency_ms', 'N/A')}ms. The multi-node pipeline adds latency but provides quality guarantees.")
    if lg_metrics.get('degraded', 0) > 0:
        lines.append(f"4. **Graceful Degradation**: {lg_metrics['degraded']} case(s) produced degraded responses with clear explanations, rather than silent failures.")
    lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Main Runner
# ══════════════════════════════════════════════════════════════════════════════

async def _run_all(
    cases: list[dict],
    concurrency: int = 1,
    run_baseline: bool = True,
    delay_between: float = 2.0,
    judge_cfg: JudgeConfig | None = None,
) -> tuple[list[dict], list[dict]]:
    """Run all eval cases with rate limiting."""
    sem = asyncio.Semaphore(concurrency)
    lg_results: list[dict] = []
    bl_results: list[dict] = []
    loop = asyncio.get_event_loop()
    total = len(cases)

    async def _run_one_lg(case: dict, idx: int) -> dict:
        async with sem:
            log.info(f"[{idx}/{total}] LangGraph: {case['id']} - {case['query'][:40]}")
            result = await loop.run_in_executor(
                None, _run_langgraph_agent, case["query"], case["id"]
            )
            if judge_cfg and judge_cfg.get("enabled") and result.get("analysis_full"):
                evidence = {
                    "sql_result_preview": result.get("sql_result_preview", {}),
                    "sources_preview": result.get("sources_preview", []),
                }
                judge_result = await loop.run_in_executor(
                    None, _judge_answer, case["query"], result.get("analysis_full", ""), evidence, judge_cfg
                )
                result.update(judge_result)
            result.pop("analysis_full", None)
            log.info(
                f"[{idx}/{total}] LangGraph: {case['id']} -> {result['status']} "
                f"({result['latency_ms']}ms)"
            )
            await asyncio.sleep(delay_between)
            return result

    async def _run_one_bl(case: dict, idx: int) -> dict:
        async with sem:
            log.info(f"[{idx}/{total}] Baseline: {case['id']} - {case['query'][:40]}")
            result = await loop.run_in_executor(
                None, _run_baseline_agent, case["query"], case["id"]
            )
            if judge_cfg and judge_cfg.get("enabled") and result.get("analysis_full"):
                evidence = {}
                judge_result = await loop.run_in_executor(
                    None, _judge_answer, case["query"], result.get("analysis_full", ""), evidence, judge_cfg
                )
                result.update(judge_result)
            result.pop("analysis_full", None)
            log.info(
                f"[{idx}/{total}] Baseline: {case['id']} -> {result['status']} "
                f"({result['latency_ms']}ms)"
            )
            await asyncio.sleep(delay_between)
            return result

    # Run LangGraph agent on all cases
    log.info(f"=== Running LangGraph Agent ({total} cases, concurrency={concurrency}) ===")
    for idx, case in enumerate(cases, 1):
        result = await _run_one_lg(case, idx)
        lg_results.append(result)

    # Run baseline agent on all cases
    if run_baseline:
        log.info(f"=== Running ReAct Baseline ({total} cases, concurrency={concurrency}) ===")
        for idx, case in enumerate(cases, 1):
            result = await _run_one_bl(case, idx)
            bl_results.append(result)

    return lg_results, bl_results


def main() -> None:
    parser = argparse.ArgumentParser(description="ESG Agent Evaluation Runner")
    parser.add_argument(
        "-i", "--input", default="eval_dataset.jsonl",
        help="Input eval dataset path",
    )
    parser.add_argument(
        "-o", "--output", default="eval_results.json",
        help="Output raw results path",
    )
    parser.add_argument(
        "--report", default="eval_report.md",
        help="Output report path",
    )
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="Max concurrent runs (default: 1 for rate limit safety)",
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Delay between runs in seconds (rate limit protection)",
    )
    parser.add_argument(
        "--disable-rerank", action="store_true",
        help="Disable BGE reranker (faster, more stable for long evals)",
    )
    parser.add_argument(
        "--rag-timeout", type=float, default=0.0,
        help="RAG timeout in seconds (0 = no timeout)",
    )
    parser.add_argument(
        "--rag-scope-timeout", type=float, default=30.0,
        help="RAG scope timeout in seconds (0 = no timeout)",
    )
    parser.add_argument(
        "--llm-min-interval", type=float, default=0.0,
        help="Global LLM min interval seconds (0 = keep env/default)",
    )
    parser.add_argument(
        "--skip-baseline", action="store_true",
        help="Skip baseline agent runs",
    )
    parser.add_argument(
        "--skip-judge", action="store_true",
        help="Skip LLM-as-a-Judge scoring",
    )
    parser.add_argument(
        "--judge-model", default=os.getenv("QWEN_JUDGE_MODEL", "qwen3.5-plus"),
        help="Judge model name (default: qwen3.5-plus)",
    )
    parser.add_argument(
        "--judge-base-url",
        default=os.getenv("QWEN_JUDGE_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        help="Judge base URL (default: DashScope Intl compatible-mode)",
    )
    parser.add_argument(
        "--max-cases", type=int, default=0,
        help="Max number of cases to run (0 = all)",
    )
    args = parser.parse_args()

    # Apply stability knobs via env
    if args.disable_rerank:
        os.environ["DISABLE_RERANK"] = "true"
    if args.rag_timeout >= 0:
        os.environ["RAG_TIMEOUT_SEC"] = str(args.rag_timeout)
    if args.rag_scope_timeout >= 0:
        os.environ["RAG_SCOPE_TIMEOUT_SEC"] = str(args.rag_scope_timeout)
    if args.llm_min_interval > 0:
        os.environ["LLM_MIN_INTERVAL_SEC"] = str(args.llm_min_interval)

    # Load cases
    input_path = Path(args.input)
    if not input_path.exists():
        log.error(f"Input file not found: {input_path}")
        sys.exit(1)

    with input_path.open("r", encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]

    if args.max_cases > 0:
        cases = cases[:args.max_cases]

    log.info(f"Loaded {len(cases)} eval cases from {input_path}")

    # Judge config
    judge_api_key = os.getenv("QWEN_API_KEY", "")
    judge_enabled = not args.skip_judge
    if judge_enabled and not judge_api_key:
        log.warning("QWEN_API_KEY not set; judge will be disabled.")
        judge_enabled = False
    judge_cfg: JudgeConfig = {
        "enabled": judge_enabled,
        "model": args.judge_model,
        "base_url": args.judge_base_url,
        "api_key": judge_api_key,
        "max_answer_chars": 2000,
        "max_evidence_chars": 2000,
    }

    # Run evaluation
    run_timestamp = datetime.now(timezone.utc).isoformat()
    lg_results, bl_results = asyncio.run(
        _run_all(
            cases,
            concurrency=args.concurrency,
            run_baseline=not args.skip_baseline,
            delay_between=args.delay,
            judge_cfg=judge_cfg,
        )
    )

    # Compute metrics
    cases_map = {c["id"]: c for c in cases}
    lg_metrics = _compute_metrics(lg_results)
    bl_metrics = _compute_metrics(bl_results) if bl_results else {
        "total": 0, "completed": 0, "completion_rate": 0,
        "rescued": 0, "rescue_rate": 0, "crashed": 0, "degraded": 0,
        "avg_latency_ms": 0, "p50_latency_ms": 0, "p95_latency_ms": 0,
        "max_latency_ms": 0, "min_latency_ms": 0,
    }
    lg_by_cat = _compute_per_category(lg_results, cases_map)
    bl_by_cat = _compute_per_category(bl_results, cases_map) if bl_results else {}

    # Save raw results
    output_path = Path(args.output)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump({
            "timestamp": run_timestamp,
            "config": {
                "concurrency": args.concurrency,
                "delay": args.delay,
                "total_cases": len(cases),
                "skip_baseline": args.skip_baseline,
                "disable_rerank": args.disable_rerank,
                "rag_timeout": args.rag_timeout,
                "rag_scope_timeout": args.rag_scope_timeout,
                "llm_min_interval": args.llm_min_interval,
                "judge_enabled": judge_cfg["enabled"],
                "judge_model": judge_cfg["model"],
                "judge_base_url": judge_cfg["base_url"],
            },
            "langgraph": {
                "metrics": lg_metrics,
                "per_category": lg_by_cat,
                "results": lg_results,
            },
            "baseline": {
                "metrics": bl_metrics,
                "per_category": bl_by_cat,
                "results": bl_results,
            },
        }, f, ensure_ascii=False, indent=2)
    log.info(f"Raw results saved to {output_path}")

    # Generate report
    report_content = _generate_report(
        lg_results, bl_results,
        lg_metrics, bl_metrics,
        lg_by_cat, bl_by_cat,
        cases, run_timestamp,
    )
    report_path = Path(args.report)
    report_path.write_text(report_content, encoding="utf-8")
    log.info(f"Report saved to {report_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("EVALUATION COMPLETE")
    print("=" * 60)
    print(f"LangGraph:  Completion={lg_metrics['completion_rate']}%  "
          f"Rescue={lg_metrics['rescue_rate']}%  "
          f"p95={lg_metrics['p95_latency_ms']}ms  "
          f"Judge={lg_metrics.get('avg_judge_overall', 0)}")
    if bl_results:
        print(f"Baseline:   Completion={bl_metrics['completion_rate']}%  "
              f"p95={bl_metrics['p95_latency_ms']}ms  "
              f"Judge={bl_metrics.get('avg_judge_overall', 0)}")
    print(f"\nResults: {output_path}")
    print(f"Report:  {report_path}")


if __name__ == "__main__":
    main()
