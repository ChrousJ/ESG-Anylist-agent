"""Greenwashing detector node.

Adds a business-specific risk radar based on RAG evidence. It is deliberately
rule-based in v1 so the result is cheap, deterministic, and easy to explain.
"""
from __future__ import annotations

import logging

from agent.greenwashing import detect_greenwashing_risks, render_greenwashing_markdown
from agent.state import AgentState
from agent.tracing import TraceLogger, trace_node

log = logging.getLogger(__name__)


@trace_node("greenwashing_detector", tags=["evaluation", "greenwashing"])
def greenwashing_detector_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log_ = TraceLogger("greenwashing_detector", trace_id)

    try:
        result = detect_greenwashing_risks(state)
        state["greenwashing_risks"] = result

        analysis = state.get("analysis", "")
        if "Greenwashing Risk Radar" not in analysis:
            state["analysis"] = analysis.rstrip() + render_greenwashing_markdown(result)

        if result.get("risk_count", 0) > 0:
            findings = list(state.get("key_findings", []) or [])
            summary = f"潜在绿漂核查点：{result.get('risk_count')} 个（规则型 claim-evidence mismatch）"
            if summary not in findings:
                findings.append(summary)
            state["key_findings"] = findings

        log_.info(f"greenwashing risks={result.get('risk_count', 0)}")
    except Exception as exc:
        log_.warning(f"greenwashing detection skipped: {str(exc)[:160]}")

    return state
