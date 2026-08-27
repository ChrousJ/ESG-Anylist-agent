"""Disclosure scorer node.

Turns ESG evidence quality into a deterministic score before the final output
quality evaluator runs. Keeping this as an explicit LangGraph node makes the
quality-engineering story visible in traces and the frontend pipeline.
"""
from __future__ import annotations

import logging

from agent.disclosure_quality import (
    render_disclosure_quality_markdown,
    score_disclosure_quality,
)
from agent.state import AgentState
from agent.tracing import TraceLogger, trace_node

log = logging.getLogger(__name__)


@trace_node("disclosure_scorer", tags=["evaluation", "disclosure"])
def disclosure_scorer_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log_ = TraceLogger("disclosure_scorer", trace_id)

    try:
        disclosure_quality = score_disclosure_quality(state)
        appendix = render_disclosure_quality_markdown(disclosure_quality)

        analysis = state.get("analysis", "")
        if "Disclosure Quality Score" not in analysis:
            state["analysis"] = analysis.rstrip() + appendix

        state["disclosure_quality"] = disclosure_quality

        risk_flags = disclosure_quality.get("risk_flags", [])
        if risk_flags:
            findings = list(state.get("key_findings", []) or [])
            summary = (
                f"披露质量评分 {disclosure_quality.get('score')}/100"
                f"（{disclosure_quality.get('band')}），"
                f"触发 {len(risk_flags)} 个披露/证据风险信号"
            )
            if summary not in findings:
                findings.append(summary)
            state["key_findings"] = findings

        log_.info(
            f"disclosure score={disclosure_quality.get('score')} "
            f"band={disclosure_quality.get('band')} "
            f"risks={len(risk_flags)}"
        )
    except Exception as exc:
        log_.warning(f"disclosure scoring skipped: {str(exc)[:160]}")

    return state
