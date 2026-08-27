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
  - Evidence / Citation Presence：是否带有 SQL/RAG 证据或来源
  - Disclosure Score Presence：是否输出披露质量评分
  - Greenwashing Radar Presence：是否输出绿漂风险雷达结构

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

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency in lightweight test envs
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False

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


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return str(value)


def _summarize_sql_result(sql_result: Any, max_rows: int = 30) -> dict[str, Any]:
    if sql_result is None:
        return {}
    if hasattr(sql_result, "empty") and hasattr(sql_result, "head") and hasattr(sql_result, "to_dict"):
        if bool(getattr(sql_result, "empty")):
            return {}
        preview_df = sql_result.head(max_rows)
        return {
            "row_count": int(len(sql_result)),
            "columns": [str(c) for c in list(getattr(sql_result, "columns", []))],
            "rows_preview": _json_safe(preview_df.to_dict(orient="records")),
        }
    if isinstance(sql_result, list):
        rows = [r for r in sql_result if isinstance(r, dict)]
        preview = rows[:max_rows]
        cols = list(preview[0].keys()) if preview else []
        return {
            "row_count": len(rows),
            "columns": cols,
            "rows_preview": _json_safe(preview),
        }
    if isinstance(sql_result, dict):
        if "_type" in sql_result and "data" in sql_result:
            rows = list(sql_result.get("data", []))
            return {
                "row_count": len(rows),
                "columns": list(sql_result.get("columns", [])),
                "rows_preview": _json_safe(rows[:max_rows]),
            }
        return {
            "row_count": sql_result.get("row_count", ""),
            "columns": sql_result.get("columns", []),
            "rows_preview": _json_safe(sql_result.get("rows_preview", [])),
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
            "file": s.get("file", ""),
            "score": s.get("score", 0),
            "excerpt": _truncate(s.get("excerpt", "") or s.get("content", ""), 220),
        })
    return preview


def _has_sql_rows(sql_preview: dict[str, Any]) -> bool:
    try:
        return int(sql_preview.get("row_count") or 0) > 0
    except Exception:
        return False


def _has_rag_sources(sources_preview: list[dict]) -> bool:
    return bool(sources_preview)


def _has_disclosure_quality(value: Any) -> bool:
    return isinstance(value, dict) and value.get("score") is not None


def _has_greenwashing_radar(value: Any) -> bool:
    return isinstance(value, dict) and "risks" in value and "risk_count" in value


def _expected_evidence_required(case: dict[str, Any]) -> bool:
    if "expected_evidence" in case:
        return bool(case["expected_evidence"])
    return case.get("category") not in {"knowledge", "clarify", "missing_degradation"}


def _normalize_numeric_token(token: str) -> float | None:
    try:
        return float(token.replace(",", "").replace("，", ""))
    except Exception:
        return None


def _extract_numeric_values(text: str) -> list[float]:
    values: list[float] = []
    for raw in re.findall(r"(?<![A-Za-z0-9_])[-+]?\d[\d,]*(?:\.\d+)?", text or ""):
        value = _normalize_numeric_token(raw)
        if value is None:
            continue
        # Years and small structural labels are not business numeric claims.
        if value.is_integer() and 1900 <= value <= 2100:
            continue
        values.append(value)
    return values


def _number_is_supported(value: float, evidence_values: list[float]) -> bool:
    for evidence in evidence_values:
        tolerance = max(0.02, abs(evidence) * 0.001)
        if abs(value - evidence) <= tolerance:
            return True
        # Support common unit rendering: report stores 万/亿 while answer expands it.
        for factor in (100, 1000, 10000, 1e8):
            if abs(value - evidence * factor) <= max(0.02, abs(evidence * factor) * 0.001):
                return True
            if abs(value * factor - evidence) <= max(0.02, abs(evidence) * 0.001):
                return True
    return False


def _packaged_numeric_support(answer: str, evidence: dict[str, Any]) -> dict[str, Any]:
    answer_values = _extract_numeric_values(answer)
    evidence_values = _extract_numeric_values(json.dumps(evidence, ensure_ascii=False))
    if not answer_values:
        return {"claim_count": 0, "supported_count": 0, "support_rate": None}
    supported = sum(1 for value in answer_values if _number_is_supported(value, evidence_values))
    return {
        "claim_count": len(answer_values),
        "supported_count": supported,
        "support_rate": round(supported / len(answer_values) * 100, 1),
    }


def _evidence_target_coverage(result: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("expected_entities", {}) or {}
    companies = [str(c) for c in expected.get("companies", []) if c]
    years = [int(y) for y in expected.get("years", []) if str(y).isdigit()]
    if not companies or not years:
        return {"required": [], "covered": [], "missing": [], "rate": None}
    required = [(company, year) for company in companies for year in years]
    covered: set[tuple[str, int]] = set()
    sql_preview = result.get("sql_result_preview") or {}
    for row in sql_preview.get("rows_preview", []) or []:
        try:
            covered.add((str(row.get("company_name", "")), int(row.get("year"))))
        except (TypeError, ValueError):
            continue
    for source in result.get("sources_preview", []) or []:
        try:
            covered.add((str(source.get("company", "")), int(source.get("year"))))
        except (TypeError, ValueError):
            continue
    covered_required = [target for target in required if target in covered]
    missing = [target for target in required if target not in covered]
    return {
        "required": [list(t) for t in required],
        "covered": [list(t) for t in covered_required],
        "missing": [list(t) for t in missing],
        "rate": round(len(covered_required) / len(required) * 100, 1) if required else None,
    }


def _annotate_expected_behavior(result: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    answer = result.get("analysis_full", "") or result.get("analysis_preview", "") or ""
    category = case.get("category", "")
    expected_class = case.get("expected_class", "")
    evidence_required = _expected_evidence_required(case)
    result["category"] = category
    result["expected_class"] = expected_class
    result["expected_evidence_required"] = evidence_required
    result["expected_class_match"] = (
        result.get("query_class") == expected_class
        if expected_class and result.get("query_class") else None
    )
    result["strict_success"] = result.get("status") == "success"
    result["evidence_requirement_met"] = (not evidence_required) or bool(result.get("has_any_evidence"))
    result["no_data_safe"] = None
    if category == "missing_degradation":
        lower = answer.lower()
        boundary_text = (
            any(m in lower for m in ("未覆盖", "无可用证据", "拒绝输出具体数值", "not covered", "no evidence"))
            or ("不在" in lower and "覆盖范围" in lower)
        )
        # A safe no-data response must not package evidence from unrelated entities.
        no_evidence_leak = not result.get("has_any_evidence")
        result["no_data_safe"] = result.get("status") == "success" and boundary_text and no_evidence_leak
    result["clarify_success"] = None
    if category == "clarify":
        if expected_class == "knowledge":
            # Knowledge cases are stored in the clarify bucket for dataset balance,
            # but should pass on a substantive knowledge route rather than a follow-up question.
            result["clarify_success"] = (
                result.get("status") == "success"
                and result.get("query_class") == "knowledge"
                and bool(answer.strip())
                and not any(m in answer.lower() for m in ("请补充", "您想了解哪家公司"))
            )
        else:
            clarify_markers = ("请问", "请补充", "需要了解", "想分析", "您想了解", "provide", "clarify")
            result["clarify_success"] = result.get("status") == "success" and any(m in answer.lower() for m in clarify_markers)
    result["golden_fact_score"] = _score_golden_facts(result, case)
    if evidence_required:
        result["target_coverage"] = _evidence_target_coverage(result, case)
        result["numeric_support"] = _packaged_numeric_support(answer, result.get("judge_evidence") or {})
    else:
        result["target_coverage"] = {"required": [], "covered": [], "missing": [], "rate": None}
        result["numeric_support"] = {"claim_count": 0, "supported_count": 0, "support_rate": None}

    expected_entities = case.get("expected_entities", {}) or {}
    expected_companies = {str(c) for c in expected_entities.get("companies", []) if c}
    evidence_companies = {
        str(source.get("company", "")) for source in result.get("sources_preview", []) or []
        if source.get("company")
    }
    if expected_companies and evidence_companies:
        relevant = len(evidence_companies & expected_companies)
        result["entity_evidence_precision"] = round(relevant / len(evidence_companies) * 100, 1)
    elif expected_companies and not evidence_companies:
        result["entity_evidence_precision"] = None
    else:
        result["entity_evidence_precision"] = None

    result["unnecessary_tool_use"] = (not evidence_required) and bool(result.get("has_any_evidence"))
    golden = result.get("golden_fact_score") or {}
    target_rate = (result.get("target_coverage") or {}).get("rate")
    case_pass = result.get("status") == "success" and bool(result.get("evidence_requirement_met"))
    if category == "missing_degradation":
        case_pass = case_pass and bool(result.get("no_data_safe"))
    if category == "clarify":
        case_pass = case_pass and bool(result.get("clarify_success"))
    if golden.get("total", 0):
        case_pass = case_pass and golden.get("accuracy") == 100.0
    if isinstance(target_rate, (int, float)):
        case_pass = case_pass and target_rate == 100.0
    result["case_pass"] = bool(case_pass)
    return result


def _score_golden_facts(result: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    facts = case.get("golden_facts", []) or []
    if not facts:
        return {"total": 0, "matched": 0, "accuracy": None, "details": []}
    rows = (result.get("sql_result_preview") or {}).get("rows_preview", []) or []
    details = []
    matched = 0
    for fact in facts:
        found = None
        for row in rows:
            try:
                same_year = int(row.get("year")) == int(fact["year"])
            except (TypeError, ValueError):
                same_year = False
            if str(row.get("company_name", "")) == str(fact["company"]) and same_year:
                found = row.get(fact["metric"]); break
        ok = False
        if isinstance(found, (int, float)):
            expected = float(fact["value"]); tolerance = max(float(fact.get("tolerance", 0.01)), abs(expected) * 1e-6)
            if fact.get("quality") == "lower_bound":
                ok = float(found) >= expected - tolerance
            else:
                ok = abs(float(found) - expected) <= tolerance
        matched += int(ok)
        details.append({**fact, "actual": found, "matched": ok})
    return {"total": len(facts), "matched": matched, "accuracy": round(matched / len(facts) * 100, 1), "details": details}


def _diagnose_bad_case(result: dict[str, Any], case: dict[str, Any]) -> tuple[str, str]:
    if result.get("status") == "crashed":
        return "Runtime Failure", "Inspect the captured exception and runtime dependency/configuration checks."
    eval_d_types = [e.get("type", "") for e in result.get("eval_d_errors", [])]
    eval_o_types = [e.get("type", "") for e in result.get("eval_o_errors", [])]
    target_missing = (result.get("target_coverage") or {}).get("missing", [])
    if "RAG_TARGET_COVERAGE_MISSING" in eval_d_types or target_missing:
        return "Evidence Target Coverage", "Run strict company-year targeted retrieval and return a partial comparison if coverage remains incomplete."
    if result.get("is_degraded") and eval_o_types:
        return "Output Quality Gate", "Inspect evaluator_o error types; avoid repeated rewrite when the underlying problem is missing evidence."
    if _expected_evidence_required(case) and not result.get("has_sql_evidence"):
        return "Structured Evidence Missing", "Populate/verify the structured seed for this metric and preserve SQL provenance in the final sources."
    if result.get("no_data_safe") is False:
        return "Unsafe No-data Behavior", "Return a coverage-gap response without packaging unrelated SQL/RAG evidence."
    if result.get("clarify_success") is False:
        return "Clarification Behavior", "Ask for company, year, or ESG metric instead of proceeding with an answer."
    golden = result.get("golden_fact_score") or {}
    if golden.get("total", 0) and golden.get("accuracy") != 100.0:
        return "Golden Fact Miss", "Use the structured SQL tool and preserve returned company-year metric rows in evaluation evidence."
    if result.get("is_degraded"):
        return "Controlled Degradation", "Inspect degraded_reason and the last evaluator/worker state."
    return "Expectation Mismatch", "Inspect expected behavior, entity/year coverage, and packaged evidence support."


def _build_langgraph_judge_evidence(final_state: dict[str, Any]) -> dict[str, Any]:
    rag_result = final_state.get("rag_result") or {}
    rag_chunks = []
    for chunk in (rag_result.get("chunks") or [])[:5]:
        rag_chunks.append({
            "company": chunk.get("company_name", ""),
            "year": chunk.get("year", ""),
            "page": chunk.get("page_num", ""),
            "file": chunk.get("source_file", ""),
            "score": chunk.get("rerank_score", 0),
            "excerpt": _truncate(chunk.get("text", ""), 500),
        })

    return {
        "sql": {
            "query": _truncate(final_state.get("sql_query_executed", ""), 400),
            "result": _summarize_sql_result(final_state.get("sql_result")),
        },
        "rag": {
            "top_chunks": rag_chunks,
        },
        "citations": _summarize_sources(final_state.get("sources", [])),
        "business_outputs": {
            "disclosure_quality": final_state.get("disclosure_quality") or {},
            "greenwashing_risks": final_state.get("greenwashing_risks") or {},
        },
        "quality_state": {
            "eval_o_status": final_state.get("eval_o_status", ""),
            "is_degraded": final_state.get("is_degraded", False),
            "missing_summary": _truncate(final_state.get("missing_summary", ""), 300),
        },
    }


def _build_baseline_judge_evidence(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_observations": result.get("tool_observations", []),
        "quality_state": {
            "status": result.get("status", ""),
        },
    }


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
        "When evidence coverage is partial, mention that limitation in notes. "
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

    Dependency failures are captured as per-case ``crashed`` results instead of
    aborting the whole evaluation run. This is useful for interview/demo smoke
    checks: a missing LangGraph/LLM/RAG dependency should produce an auditable
    bad case and report, not an empty output directory.
    """
    trace_id = f"trace-unavailable-{case_id}"
    request_id = f"request-unavailable-{case_id}"
    t_start = time.perf_counter()

    try:
        from agent.graph import get_graph
        from agent.state import deserialize_sql_result, make_initial_state
        from agent.tracing import generate_trace_id, generate_request_id

        trace_id = generate_trace_id()
        request_id = generate_request_id()

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
        sql_result_value = deserialize_sql_result(final_state.get("sql_result"))
        sql_result_preview = _summarize_sql_result(sql_result_value)
        sources_preview = _summarize_sources(final_state.get("sources", []))
        disclosure_quality = final_state.get("disclosure_quality") or {}
        greenwashing_risks = final_state.get("greenwashing_risks") or {}
        has_sql_evidence = _has_sql_rows(sql_result_preview)
        has_rag_evidence = _has_rag_sources(sources_preview)
        has_any_evidence = has_sql_evidence or has_rag_evidence
        has_disclosure_quality = _has_disclosure_quality(disclosure_quality)
        has_greenwashing_radar = _has_greenwashing_radar(greenwashing_risks)
        judge_evidence = _build_langgraph_judge_evidence(
            {**final_state, "sql_result": sql_result_value}
        )

        # Tool error rate (SQL/RAG workers)
        tool_nodes = [nt for nt in node_trace if nt.get("node_name") in ("sql_worker", "rag_worker")]
        tool_failed = sum(1 for nt in tool_nodes if nt.get("status") == "failed")
        tool_total = len(tool_nodes)
        tool_error_rate = round(tool_failed / tool_total, 4) if tool_total else 0.0

        # Determine status
        if is_degraded or eval_o_status == "degraded":
            status = "degraded"
        elif analysis and eval_o_status == "pass":
            status = "success"
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
            "has_sql_evidence": has_sql_evidence,
            "has_rag_evidence": has_rag_evidence,
            "has_any_evidence": has_any_evidence,
            "has_disclosure_quality": has_disclosure_quality,
            "disclosure_quality_score": disclosure_quality.get("score"),
            "disclosure_quality_band": disclosure_quality.get("band") or disclosure_quality.get("level"),
            "has_greenwashing_radar": has_greenwashing_radar,
            "greenwashing_risk_count": greenwashing_risks.get("risk_count", 0) if isinstance(greenwashing_risks, dict) else 0,
            "judge_evidence": judge_evidence,
            "rag_coverage": (final_state.get("rag_result") or {}).get("coverage", {}),
            "eval_d_errors": final_state.get("eval_d_errors", []),
            "eval_o_errors": final_state.get("eval_o_errors", []),
            "degraded_reason": final_state.get("degraded_reason", ""),
            "terminal_response_mode": final_state.get("terminal_response_mode", ""),
            "resolved_entities": final_state.get("entities", {}),
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
            "has_sql_evidence": False,
            "has_rag_evidence": False,
            "has_any_evidence": False,
            "has_disclosure_quality": False,
            "disclosure_quality_score": None,
            "disclosure_quality_band": None,
            "has_greenwashing_radar": False,
            "greenwashing_risk_count": 0,
            "judge_evidence": {},
            "rag_coverage": {},
            "eval_d_errors": [],
            "eval_o_errors": [],
            "degraded_reason": "",
            "terminal_response_mode": "",
            "resolved_entities": {},
            "tool_failed_count": 0,
            "tool_total_count": 0,
            "tool_error_rate": 0.0,
            "judge_overall": None,
            "judge_faithfulness": None,
            "judge_notes": "",
            "judge_error": str(e)[:200],
        }


def _parse_baseline_tool_evidence(tool_observations: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sql_rows: list[dict[str, Any]] = []
    rag_sources: list[dict[str, Any]] = []
    for observation in tool_observations or []:
        name = str(observation.get("tool_name", ""))
        content = str(observation.get("content", ""))
        if name == "query_esg_database":
            try:
                parsed = json.loads(content)
                if isinstance(parsed, list):
                    sql_rows.extend(row for row in parsed if isinstance(row, dict))
            except Exception:
                pass
        elif name == "search_esg_reports":
            pattern = re.compile(
                r"\[(?P<idx>\d+)\]\s+(?P<company>\S+)\s+(?P<year>20\d{2})\s+p\.(?P<page>\S+)\s+"
                r"\(score=(?P<score>[\d.]+)\):\s*(?P<excerpt>.*?)(?=\n\n\[\d+\]|\Z)",
                re.DOTALL,
            )
            for match in pattern.finditer(content):
                rag_sources.append({
                    "type": "rag", "company": match.group("company"),
                    "year": match.group("year"), "page": match.group("page"),
                    "file": "", "score": float(match.group("score")),
                    "excerpt": _truncate(match.group("excerpt").strip(), 220),
                })
    sql_preview = _summarize_sql_result(sql_rows) if sql_rows else {}
    return sql_preview, rag_sources[:30]


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
        sql_result_preview, sources_preview = _parse_baseline_tool_evidence(result.get("tool_observations", []))
        has_sql_evidence = _has_sql_rows(sql_result_preview)
        has_rag_evidence = _has_rag_sources(sources_preview)
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
            "sql_result_preview": sql_result_preview,
            "sources_preview": sources_preview,
            "has_sql_evidence": has_sql_evidence,
            "has_rag_evidence": has_rag_evidence,
            "has_any_evidence": has_sql_evidence or has_rag_evidence,
            "has_disclosure_quality": False,
            "disclosure_quality_score": None,
            "disclosure_quality_band": None,
            "has_greenwashing_radar": False,
            "greenwashing_risk_count": 0,
            "judge_evidence": _build_baseline_judge_evidence(result),
            "rag_coverage": {},
            "eval_d_errors": [],
            "eval_o_errors": [],
            "degraded_reason": "",
            "terminal_response_mode": "",
            "resolved_entities": {},
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
            "has_sql_evidence": False,
            "has_rag_evidence": False,
            "has_any_evidence": False,
            "has_disclosure_quality": False,
            "disclosure_quality_score": None,
            "disclosure_quality_band": None,
            "has_greenwashing_radar": False,
            "greenwashing_risk_count": 0,
            "judge_evidence": {},
            "rag_coverage": {},
            "eval_d_errors": [],
            "eval_o_errors": [],
            "degraded_reason": "",
            "terminal_response_mode": "",
            "resolved_entities": {},
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
        return {
            "completion_rate": 0, "rescue_rate": 0, "p50_latency": 0, "p95_latency": 0, "total": 0,
            "evidence_presence_rate": 0, "disclosure_score_presence_rate": 0,
            "greenwashing_radar_presence_rate": 0, "avg_disclosure_score": 0,
        }

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
    with_any_evidence = sum(1 for r in results if r.get("has_any_evidence"))
    with_sql_evidence = sum(1 for r in results if r.get("has_sql_evidence"))
    with_rag_evidence = sum(1 for r in results if r.get("has_rag_evidence"))
    with_disclosure = sum(1 for r in results if r.get("has_disclosure_quality"))
    with_greenwashing = sum(1 for r in results if r.get("has_greenwashing_radar"))
    greenwashing_nonzero = sum(1 for r in results if (r.get("greenwashing_risk_count") or 0) > 0)
    dq_scores = [
        r.get("disclosure_quality_score") for r in results
        if isinstance(r.get("disclosure_quality_score"), (int, float))
    ]

    strict_success = sum(1 for r in results if r.get("strict_success", r.get("status") == "success"))
    case_pass_count = sum(1 for r in results if r.get("case_pass"))
    unnecessary_rows = [r for r in results if not r.get("expected_evidence_required")]
    entity_precision_values = [r.get("entity_evidence_precision") for r in results if isinstance(r.get("entity_evidence_precision"), (int, float))]
    expected_class_rows = [r for r in results if r.get("expected_class_match") is not None]
    evidence_required_rows = [r for r in results if r.get("expected_evidence_required")]
    no_data_rows = [r for r in results if r.get("no_data_safe") is not None]
    clarify_rows = [r for r in results if r.get("clarify_success") is not None]
    target_rates = [
        r.get("target_coverage", {}).get("rate") for r in results
        if isinstance(r.get("target_coverage", {}).get("rate"), (int, float))
    ]
    numeric_rates = [
        r.get("numeric_support", {}).get("support_rate") for r in results
        if isinstance(r.get("numeric_support", {}).get("support_rate"), (int, float))
    ]

    golden_total = sum(int((r.get("golden_fact_score") or {}).get("total", 0)) for r in results)
    golden_matched = sum(int((r.get("golden_fact_score") or {}).get("matched", 0)) for r in results)

    latencies_sorted = sorted(latencies)
    p50_idx = max(0, int(math.ceil(0.50 * total)) - 1)
    p95_idx = max(0, int(math.ceil(0.95 * total)) - 1)

    return {
        "total": total,
        "completed": completed,
        "completion_rate": round(completed / total * 100, 1) if total else 0,
        "strict_success_count": strict_success,
        "strict_success_rate": round(strict_success / total * 100, 1) if total else 0,
        "case_pass_count": case_pass_count,
        "case_pass_rate": round(case_pass_count / total * 100, 1) if total else 0,
        "expected_class_accuracy": round(sum(1 for r in expected_class_rows if r.get("expected_class_match")) / len(expected_class_rows) * 100, 1) if expected_class_rows else None,
        "unnecessary_tool_use_rate": round(sum(1 for r in unnecessary_rows if r.get("unnecessary_tool_use")) / len(unnecessary_rows) * 100, 1) if unnecessary_rows else 0,
        "avg_entity_evidence_precision": round(statistics.mean(entity_precision_values), 1) if entity_precision_values else 0,
        "evidence_required_count": len(evidence_required_rows),
        "evidence_required_met_count": sum(1 for r in evidence_required_rows if r.get("evidence_requirement_met")),
        "evidence_required_coverage_rate": round(sum(1 for r in evidence_required_rows if r.get("evidence_requirement_met")) / len(evidence_required_rows) * 100, 1) if evidence_required_rows else 0,
        "no_data_safe_response_rate": round(sum(1 for r in no_data_rows if r.get("no_data_safe")) / len(no_data_rows) * 100, 1) if no_data_rows else 0,
        "clarify_success_rate": round(sum(1 for r in clarify_rows if r.get("clarify_success")) / len(clarify_rows) * 100, 1) if clarify_rows else 0,
        "avg_target_coverage_rate": round(statistics.mean(target_rates), 1) if target_rates else 0,
        "avg_packaged_numeric_support_rate": round(statistics.mean(numeric_rates), 1) if numeric_rates else 0,
        "golden_fact_total": golden_total,
        "golden_fact_matched": golden_matched,
        "golden_fact_accuracy": round(golden_matched / golden_total * 100, 1) if golden_total else 0,
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
        "evidence_presence_count": with_any_evidence,
        "evidence_presence_rate": round(with_any_evidence / total * 100, 1) if total else 0,
        "sql_evidence_presence_rate": round(with_sql_evidence / total * 100, 1) if total else 0,
        "rag_evidence_presence_rate": round(with_rag_evidence / total * 100, 1) if total else 0,
        "disclosure_score_presence_count": with_disclosure,
        "disclosure_score_presence_rate": round(with_disclosure / total * 100, 1) if total else 0,
        "avg_disclosure_score": round(statistics.mean(dq_scores), 2) if dq_scores else 0,
        "greenwashing_radar_presence_count": with_greenwashing,
        "greenwashing_radar_presence_rate": round(with_greenwashing / total * 100, 1) if total else 0,
        "greenwashing_nonzero_count": greenwashing_nonzero,
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
    *,
    judge_enabled: bool = True,
) -> str:
    """Generate eval_report.md content."""
    lines: list[str] = []
    lines.append("# ESG Agent Evaluation Report")
    lines.append("")
    lines.append(f"> Generated: {run_timestamp}")
    lines.append(f"> Total cases: {len(cases)}")
    lines.append(f"> LangGraph nodes: context -> supervisor -> schema_injector -> sql/rag workers -> evaluator_d -> map_reduce -> synthesizer -> disclosure_scorer -> greenwashing_detector -> evaluator_o -> memory_updater")
    baseline_available = bool(bl_metrics.get("total"))
    lines.append(
        "> Baseline: ReAct agent (same configured LLM + SQL/RAG tools, no quality loops)"
        if baseline_available
        else "> Baseline: skipped for this run"
    )
    if judge_enabled:
        lines.append("> Judge note: scores are only as reliable as the packaged evidence. This report includes SQL row previews and raw RAG excerpts when available.")
    else:
        lines.append("> Judge: disabled for this run; judge scores are reported as N/A.")
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

    lines.append(f"| Strict Success Rate | {lg_metrics.get('strict_success_rate', 0)}% | {(str(bl_metrics.get('strict_success_rate', 'N/A')) + '%') if baseline_available else 'N/A'} |")
    lines.append(f"| Case Pass Rate | {lg_metrics.get('case_pass_rate', 0)}% ({lg_metrics.get('case_pass_count', 0)}/{lg_metrics.get('total', 0)}) | {(str(bl_metrics.get('case_pass_rate', 'N/A')) + '% (' + str(bl_metrics.get('case_pass_count', 0)) + '/' + str(bl_metrics.get('total', 0)) + ')') if baseline_available else 'N/A'} |")
    lg_class = lg_metrics.get('expected_class_accuracy')
    bl_class = bl_metrics.get('expected_class_accuracy')
    lines.append(f"| Expected Class Accuracy | {str(lg_class) + '%' if lg_class is not None else 'N/A'} | {str(bl_class) + '%' if baseline_available and bl_class is not None else 'N/A'} |")
    lines.append(f"| Evidence Coverage on Required Cases | {lg_metrics.get('evidence_required_coverage_rate', 0)}% ({lg_metrics.get('evidence_required_met_count', 0)}/{lg_metrics.get('evidence_required_count', 0)}) | {bl_metrics.get('evidence_required_coverage_rate', 'N/A') if baseline_available else 'N/A'} |")
    lines.append(f"| No-data Safe Response Rate | {lg_metrics.get('no_data_safe_response_rate', 0)}% | {bl_metrics.get('no_data_safe_response_rate', 'N/A') if baseline_available else 'N/A'} |")
    lines.append(f"| Clarify Success Rate | {lg_metrics.get('clarify_success_rate', 0)}% | {bl_metrics.get('clarify_success_rate', 'N/A') if baseline_available else 'N/A'} |")
    lines.append(f"| Unnecessary Tool Use (non-evidence cases) | {lg_metrics.get('unnecessary_tool_use_rate', 0)}% | {bl_metrics.get('unnecessary_tool_use_rate', 'N/A') if baseline_available else 'N/A'} |")
    lines.append(f"| Avg Entity Evidence Precision | {lg_metrics.get('avg_entity_evidence_precision', 0)}% | {bl_metrics.get('avg_entity_evidence_precision', 'N/A') if baseline_available else 'N/A'} |")
    lines.append(f"| Avg Target Entity-Year Coverage | {lg_metrics.get('avg_target_coverage_rate', 0)}% | {bl_metrics.get('avg_target_coverage_rate', 'N/A') if baseline_available else 'N/A'} |")
    lines.append(f"| Packaged Numeric Support (directional) | {lg_metrics.get('avg_packaged_numeric_support_rate', 0)}% | {bl_metrics.get('avg_packaged_numeric_support_rate', 'N/A') if baseline_available else 'N/A'} |")

    lines.append(f"| Golden Structured Fact Accuracy | {lg_metrics.get('golden_fact_accuracy', 0)}% ({lg_metrics.get('golden_fact_matched', 0)}/{lg_metrics.get('golden_fact_total', 0)}) | {bl_metrics.get('golden_fact_accuracy', 'N/A') if baseline_available else 'N/A'} |")

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

    baseline_evidence = (
        f"{bl_metrics.get('evidence_presence_rate', 0)}% "
        f"({bl_metrics.get('evidence_presence_count', 0)}/{bl_metrics.get('total', 0)})"
        if baseline_available else "N/A"
    )
    lines.append(f"| Evidence Presence | {lg_metrics.get('evidence_presence_rate', 0)}% ({lg_metrics.get('evidence_presence_count', 0)}/{lg_metrics.get('total', 0)}) | {baseline_evidence} |")
    lines.append(f"| SQL Evidence Presence | {lg_metrics.get('sql_evidence_presence_rate', 0)}% | N/A |")
    lines.append(f"| RAG Citation Presence | {lg_metrics.get('rag_evidence_presence_rate', 0)}% | N/A |")
    lines.append(f"| Disclosure Score Presence | {lg_metrics.get('disclosure_score_presence_rate', 0)}% ({lg_metrics.get('disclosure_score_presence_count', 0)}/{lg_metrics.get('total', 0)}) | N/A |")
    lines.append(f"| Avg Disclosure Score | {lg_metrics.get('avg_disclosure_score', 0)} | N/A |")
    lines.append(f"| Greenwashing Radar Presence | {lg_metrics.get('greenwashing_radar_presence_rate', 0)}% ({lg_metrics.get('greenwashing_radar_presence_count', 0)}/{lg_metrics.get('total', 0)}) | N/A |")
    lines.append(f"| Greenwashing Non-zero Cases | {lg_metrics.get('greenwashing_nonzero_count', 0)} | N/A |")

    lg_j, bl_j = _fmt(
        f"{lg_metrics.get('avg_judge_overall', 0)}" if judge_enabled else "N/A",
        f"{bl_metrics.get('avg_judge_overall', 'N/A')}" if judge_enabled and baseline_available else "N/A",
    )
    lines.append(f"| Judge Overall | {lg_j} | {bl_j} |")

    lg_f, bl_f = _fmt(
        f"{lg_metrics.get('avg_judge_faithfulness', 0)}" if judge_enabled else "N/A",
        "N/A",
    )
    lines.append(f"| Judge Faithfulness | {lg_f} | {bl_f} |")

    baseline_crashed = bl_metrics.get('crashed', 0) if baseline_available else "N/A"
    lines.append(f"| Crashed | {lg_metrics.get('crashed', 0)} | {baseline_crashed} |")
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

    # ── ESG Business Capability Cards ────────────────────────────────────────
    lines.append("## ESG Business Capability Checks")
    lines.append("")
    lines.append("| Check | Value | Why it matters |")
    lines.append("|-------|:-----:|----------------|")
    lines.append(f"| Evidence / Citation Presence | {lg_metrics.get('evidence_presence_rate', 0)}% | 关键结论是否有 SQL/RAG 证据支撑 |")
    lines.append(f"| Disclosure Score Presence | {lg_metrics.get('disclosure_score_presence_rate', 0)}% | 复杂 ESG 分析是否输出披露质量结构 |")
    lines.append(f"| Average Disclosure Score | {lg_metrics.get('avg_disclosure_score', 0)} | 便于观察披露质量整体分布 |")
    lines.append(f"| Greenwashing Radar Presence | {lg_metrics.get('greenwashing_radar_presence_rate', 0)}% | 是否输出潜在绿漂风险雷达结构 |")
    lines.append(f"| Greenwashing Non-zero Cases | {lg_metrics.get('greenwashing_nonzero_count', 0)} | 有多少 case 触发人工核查点 |")
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

    # ── Case-Level Business Signals ─────────────────────────────────────────
    lines.append("## Case-Level Business Signals (LangGraph)")
    lines.append("")
    lines.append("| Case ID | Category | Pass | Evidence | Target Cov. | Numeric Support | Golden Facts | DQ | DQ Score | GW Radar | GW Risks | Status |")
    lines.append("|---------|----------|:----:|:--------:|:-----------:|:---------------:|:------------:|:--:|:--------:|:--------:|:--------:|--------|")
    cases_map = {c["id"]: c for c in cases}
    for r in lg_results:
        cat = cases_map.get(r["case_id"], {}).get("category", "")
        lines.append(
            f"| {r['case_id']} | {cat} | "
            f"{'Y' if r.get('case_pass') else 'N'} | "
            f"{'Y' if r.get('has_any_evidence') else 'N'} | "
            f"{r.get('target_coverage', {}).get('rate') if r.get('target_coverage', {}).get('rate') is not None else '-'} | "
            f"{r.get('numeric_support', {}).get('support_rate') if r.get('numeric_support', {}).get('support_rate') is not None else '-'} | "
            f"{r.get('golden_fact_score', {}).get('accuracy') if r.get('golden_fact_score', {}).get('accuracy') is not None else '-'} | "
            f"{'Y' if r.get('has_disclosure_quality') else 'N'} | "
            f"{r.get('disclosure_quality_score') if r.get('disclosure_quality_score') is not None else '-'} | "
            f"{'Y' if r.get('has_greenwashing_radar') else 'N'} | "
            f"{r.get('greenwashing_risk_count', 0)} | {r.get('status', '')} |"
        )
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
        lines.append("> These are cases where re-plan or evaluator_o correction loops triggered. Check Final Status to distinguish successful recovery from eventual degradation.")
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
    if baseline_available:
        lines.append(f"1. **Completion Rate**: LangGraph {lg_metrics['completion_rate']}% vs ReAct {bl_metrics.get('completion_rate', 'N/A')}%")
    else:
        lines.append(f"1. **Completion Rate**: LangGraph {lg_metrics['completion_rate']}%; ReAct baseline was not run.")
    if lg_metrics.get('rescued', 0) > 0:
        lines.append(f"2. **Repair-loop Activation**: LangGraph triggered re-plan/eval correction in {lg_metrics['rescued']} case(s); final status must be checked separately.")
    else:
        lines.append(f"2. **Repair-loop Activation**: No re-plan or output-correction events triggered in this run.")
    if baseline_available:
        lines.append(f"3. **Latency**: LangGraph p95={lg_metrics['p95_latency_ms']}ms, ReAct p95={bl_metrics.get('p95_latency_ms', 'N/A')}ms. The multi-node pipeline trades latency for explicit control, repair loops, and safer degradation.")
    else:
        lines.append(f"3. **Latency**: LangGraph p95={lg_metrics['p95_latency_ms']}ms; no baseline latency comparison is available for this run.")
    takeaway_idx = 4
    if lg_metrics.get('degraded', 0) > 0:
        lines.append(f"4. **Graceful Degradation**: {lg_metrics['degraded']} case(s) produced degraded responses with clear explanations, rather than silent failures.")
        takeaway_idx = 5
    lines.append(f"{takeaway_idx}. **Business Signals**: disclosure_score_presence={lg_metrics.get('disclosure_score_presence_rate', 0)}%, greenwashing_radar_presence={lg_metrics.get('greenwashing_radar_presence_rate', 0)}%, evidence_presence={lg_metrics.get('evidence_presence_rate', 0)}%.")
    if lg_metrics.get("avg_judge_overall", 0) or bl_metrics.get("avg_judge_overall", 0):
        lines.append("5. **Judge Interpretation**: Treat judge scores as directional. Low evidence coverage or truncated citations can depress faithfulness scores even when the answer text itself is reasonable.")
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
            result = _annotate_expected_behavior(result, case)
            if judge_cfg and judge_cfg.get("enabled") and result.get("analysis_full"):
                evidence = {
                    **result.get("judge_evidence", {}),
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
            result = _annotate_expected_behavior(result, case)
            if judge_cfg and judge_cfg.get("enabled") and result.get("analysis_full"):
                evidence = result.get("judge_evidence", {})
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
        "--run-id", default="",
        help="Run id for outputs/eval_runs/{run_id}; default uses timestamp when --run-dir is not provided",
    )
    parser.add_argument(
        "--run-dir", default="",
        help="If set, write eval_results.json, metrics.json, bad_cases.jsonl, trace_summary.csv and eval_report.md into this directory",
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

    # Resolve output paths
    if args.run_dir:
        run_dir = Path(args.run_dir)
    elif args.run_id:
        run_dir = Path("outputs") / "eval_runs" / args.run_id
    else:
        run_dir = None

    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        output_path = run_dir / "eval_results.json"
        report_path = run_dir / "eval_report.md"
    else:
        output_path = Path(args.output)
        report_path = Path(args.report)

    # Save raw results
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

    if run_dir is not None:
        metrics_path = run_dir / "metrics.json"
        metrics_path.write_text(json.dumps({
            "timestamp": run_timestamp,
            "langgraph": lg_metrics,
            "baseline": bl_metrics,
            "langgraph_per_category": lg_by_cat,
            "baseline_per_category": bl_by_cat,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        bad_cases_path = run_dir / "bad_cases.jsonl"
        with bad_cases_path.open("w", encoding="utf-8") as bf:
            for r in lg_results + bl_results:
                if r.get("status") in ("crashed", "empty") or r.get("is_degraded") or not r.get("case_pass", True):
                    case = cases_map.get(r.get("case_id"), {})
                    primary_error, suggested_fix = _diagnose_bad_case(r, case)
                    bf.write(json.dumps({
                        "case_id": r.get("case_id"),
                        "agent": r.get("agent"),
                        "category": case.get("category", ""),
                        "query": case.get("query", ""),
                        "status": r.get("status"),
                        "error": r.get("error", ""),
                        "primary_error": primary_error,
                        "eval_d_errors": r.get("eval_d_errors", []),
                        "eval_o_errors": r.get("eval_o_errors", []),
                        "degraded_reason": r.get("degraded_reason", ""),
                        "target_coverage": r.get("target_coverage", {}),
                        "numeric_support": r.get("numeric_support", {}),
                        "suggested_fix": suggested_fix,
                    }, ensure_ascii=False) + "\n")

        trace_path = run_dir / "trace_summary.csv"
        with trace_path.open("w", encoding="utf-8") as tf:
            tf.write("case_id,agent,node,ms,status\n")
            for r in lg_results:
                for nt in r.get("node_trace_summary", []):
                    tf.write(f"{r.get('case_id')},{r.get('agent')},{nt.get('node','')},{nt.get('ms',0)},{nt.get('status','')}\n")

    # Generate report
    report_content = _generate_report(
        lg_results, bl_results,
        lg_metrics, bl_metrics,
        lg_by_cat, bl_by_cat,
        cases, run_timestamp,
        judge_enabled=judge_cfg["enabled"],
    )
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
    if run_dir is not None:
        print(f"Run dir: {run_dir}")


if __name__ == "__main__":
    main()
