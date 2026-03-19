# -*- coding: utf-8 -*-
"""Evaluator-O: output quality checks for synthesized ESG reports."""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from dotenv import load_dotenv
from google.genai import types

from agent.data_dictionary import get_metric_display
from agent.llm_provider import get_default_model, llm_generate_content
from agent.state import AgentState, apply_degraded_state, get_sql_result_dataframe
from agent.tracing import TraceLogger, llm_call_with_retry, trace_node

load_dotenv()
log = logging.getLogger(__name__)
_MODEL = os.getenv("LLM_MAIN_MODEL", get_default_model())

MAX_EVAL_O_RETRY = int(os.getenv("EVAL_O_MAX_RETRY", "2"))
_EVAL_O_DEBUG = os.getenv("EVAL_O_DEBUG", "").strip().lower() in {"1", "true", "yes", "y"}
_EVAL_O_DEBUG_CHARS = int(os.getenv("EVAL_O_DEBUG_CHARS", "2000"))
_EVAL_O_LOG_FAIL_ANALYSIS = os.getenv("EVAL_O_LOG_FAIL_ANALYSIS", "1").strip().lower() in {"1", "true", "yes", "y"}
_EVAL_O_FAIL_ANALYSIS_CHARS = int(os.getenv("EVAL_O_FAIL_ANALYSIS_CHARS", "1200"))

_NUMBER_CHECKER_MAX_RETRIES = int(os.getenv("NUMBER_CHECKER_MAX_RETRIES", "0"))
_DIRECTION_CHECKER_MAX_RETRIES = int(os.getenv("DIRECTION_CHECKER_MAX_RETRIES", "0"))
_NUMBER_LLM_SAMPLE_SIZE = int(os.getenv("NUMBER_LLM_SAMPLE_SIZE", "0"))
_NUMBER_CHECK_MAX_TOKENS = int(os.getenv("NUMBER_CHECK_MAX_TOKENS", "192"))
_DIRECTION_CHECK_MAX_TOKENS = int(os.getenv("DIRECTION_CHECK_MAX_TOKENS", "192"))
_NUMBER_REL_TOL = float(os.getenv("NUMBER_NORMALIZE_REL_TOL", "0.001"))
_NUMBER_FAST_MATCH_RAG_CHUNKS = int(os.getenv("NUMBER_FAST_MATCH_RAG_CHUNKS", "12"))
_NUMBER_FAST_MATCH_RAG_CHARS = int(os.getenv("NUMBER_FAST_MATCH_RAG_CHARS", "800"))
_EVAL_O_SHORT_CIRCUIT_ON_SYNTH_FALLBACK = (
    os.getenv("EVAL_O_SHORT_CIRCUIT_ON_SYNTH_FALLBACK", "0").strip().lower()
    in {"1", "true", "yes", "y"}
)
_EVAL_O_SKIP_DIRECTION_IF_BLOCKING = (
    os.getenv("EVAL_O_SKIP_DIRECTION_IF_BLOCKING", "0").strip().lower()
    in {"1", "true", "yes", "y"}
)
_DIRECTION_CHECK_ENABLED = (
    os.getenv("DIRECTION_CHECK_ENABLED", "1").strip().lower()
    in {"1", "true", "yes", "y"}
)

_BLOCKING_ERROR_TYPES = {
    "MISSING_LAYER",
    "NUMBER_HALLUCINATION",
    "ENTITY_HALLUCINATION",
    "DIRECTION_CONTRADICTION",
    "MISSING_SCOPE_ANNOTATION",
    "EVAL_API_ERROR",
}

_POLICY_YEAR_KEYWORDS = {
    "碳中和", "碳达峰", "双碳", "目标", "愿景", "路线图", "规划", "承诺",
    "净零", "net zero", "carbon neutral", "target", "roadmap", "long-term", "pathway",
}

_STANDARD_ID_KEYWORDS = {
    "pas", "iso", "gri", "sasb", "tcfd", "cdp", "esrs", "csrd", "ipcc", "ghg protocol",
}


def _is_policy_year_mention(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - 80):min(len(text), end + 80)].lower()
    if any(k in window for k in _POLICY_YEAR_KEYWORDS):
        return True

    # Fallback: sentence-level check to reduce false positives when line breaks are present.
    sentence_start = max(
        text.rfind("。", 0, start),
        text.rfind("\n", 0, start),
        text.rfind(".", 0, start),
    )
    sentence_end_candidates = [idx for idx in (
        text.find("。", end),
        text.find("\n", end),
        text.find(".", end),
    ) if idx != -1]
    sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
    sentence = text[sentence_start + 1:sentence_end].lower()
    return any(k in sentence for k in _POLICY_YEAR_KEYWORDS)


def _is_standard_identifier_mention(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - 24):min(len(text), end + 24)].lower()
    if any(keyword in window for keyword in _STANDARD_ID_KEYWORDS):
        return True
    return bool(
        re.search(
            r"(?:pas|iso|gri|sasb|tcfd|cdp|esrs|csrd|ipcc)\s*[-/]?\s*20\d{2}",
            window,
            flags=re.IGNORECASE,
        )
    )


# NOTE: Use regex to reduce false positives when layer headers vary in format.
_LAYER_PATTERNS = {
    "layer1": re.compile(r"(?:\blayer[\s\-]*1\b|\u7b2c\u4e00\u5c42|\u5c42\s*[\u4e001])", re.IGNORECASE),
    "layer2": re.compile(r"(?:\blayer[\s\-]*2\b|\u7b2c\u4e8c\u5c42|\u5c42\s*[\u4e8c2])", re.IGNORECASE),
    "layer3": re.compile(r"(?:\blayer[\s\-]*3\b|\u7b2c\u4e09\u5c42|\u5c42\s*[\u4e093])", re.IGNORECASE),
    "layer4": re.compile(r"(?:\blayer[\s\-]*4\b|\u7b2c\u56db\u5c42|\u5c42\s*[\u56db4])", re.IGNORECASE),
}


def _check_structure(analysis: str) -> list[dict]:
    errors = []
    for layer_id, pattern in _LAYER_PATTERNS.items():
        found = bool(pattern.search(analysis))
        if not found:
            errors.append({
                "type":     "MISSING_LAYER",
                "layer":    layer_id,
                "detail":   f"Missing {layer_id} marker in report.",
                "priority": 1,
            })
    return errors


def _has_blocking_error(errors: list[dict]) -> bool:
    return any((e.get("type") in _BLOCKING_ERROR_TYPES) for e in errors)


def _build_checker_config(
    *,
    temperature: float,
    max_output_tokens: int,
    timeout_s: float,
) -> object:
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    if provider == "qwen":
        return {
            "temperature": temperature,
            "response_mime_type": "application/json",
            "max_output_tokens": max_output_tokens,
            "timeout": timeout_s,
        }
    return types.GenerateContentConfig(
        temperature=temperature,
        response_mime_type="application/json",
        max_output_tokens=max_output_tokens,
    )


def _resolve_checker_model() -> str:
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    if provider == "qwen":
        return os.getenv(
            "LLM_CHECKER_MODEL",
            os.getenv("QWEN_CHECKER_MODEL", "qwen3.5-plus-2026-02-15"),
        )
    return os.getenv("LLM_CHECKER_MODEL", _MODEL)


def _collect_sql_growth_rate_candidates(sql_result: object) -> list[float]:
    """
    Collect potential growth-rate ratios from SQL data (decimal form).
    Example: 12% -> 0.12
    """
    if sql_result is None:
        return []
    try:
        import pandas as pd  # local import to avoid hard dependency at module import time
    except Exception:
        return []

    try:
        df = sql_result.copy() if hasattr(sql_result, "copy") else pd.DataFrame(sql_result)
    except Exception:
        return []

    if getattr(df, "empty", True):
        return []

    if "year" in df.columns:
        try:
            df = df.sort_values("year")
        except Exception:
            pass

    skip_cols = {"year", "quality", "confidence", "company_name", "company", "metric"}
    candidates: list[float] = []

    for col in df.columns:
        if col in skip_cols:
            continue
        try:
            series = pd.to_numeric(df[col], errors="coerce").dropna()
        except Exception:
            continue
        values = [float(v) for v in series.tolist()]
        if len(values) < 2:
            continue

        first_v = values[0]
        last_v = values[-1]
        if abs(first_v) > 1e-12:
            candidates.append((last_v - first_v) / abs(first_v))

        for prev_v, curr_v in zip(values, values[1:]):
            if abs(prev_v) <= 1e-12:
                continue
            candidates.append((curr_v - prev_v) / abs(prev_v))

    deduped: list[float] = []
    for v in candidates:
        if not any(_is_close_number(v, seen, rel_tol=1e-6) for seen in deduped):
            deduped.append(v)
    return deduped


def normalize_financial_number(text: str) -> float | None:
    """
    Normalize financial-like numeric expressions to float.
    Supports commas, %, ‰, 万/亿/万亿, and thousand/million/billion/trillion.
    """
    if not text:
        return None

    try:
        s = str(text).strip().lower()
        if not s:
            return None

        s = (
            s.replace(",", "")
            .replace("，", "")
            .replace(" ", "")
            .replace("＋", "+")
            .replace("－", "-")
        )

        is_percent = ("%" in s) or ("％" in s)
        is_permille = "‰" in s
        s = s.replace("%", "").replace("％", "").replace("‰", "")

        multiplier = 1.0
        if "万亿" in s:
            multiplier = 1e12
        elif "亿" in s:
            multiplier = 1e8
        elif "万" in s:
            multiplier = 1e4
        elif "trillion" in s:
            multiplier = 1e12
        elif "billion" in s:
            multiplier = 1e9
        elif "million" in s:
            multiplier = 1e6
        elif "thousand" in s:
            multiplier = 1e3

        m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
        if not m:
            return None
        value = float(m.group(0))

        if is_percent:
            return value / 100.0
        if is_permille:
            return value / 1000.0
        return value * multiplier
    except (TypeError, ValueError):
        return None


def _extract_context_number_tokens(text: str) -> list[str]:
    pattern = re.compile(
        r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
        r"(?:万亿|亿|万|%|％|‰|trillion|billion|million|thousand|[tT][cC][oO]2?[eE]|GJ|MWh|GW|MW)?"
    )
    return [m.group(0).strip() for m in pattern.finditer(text) if m.group(0).strip()]


def _is_close_number(a: float, b: float, rel_tol: float = _NUMBER_REL_TOL) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= (rel_tol * scale)


def _extract_numbers_from_text(text: str) -> list[str]:
    """Extract numeric patterns for fast matching, while filtering structural noise."""
    pattern = re.compile(
        r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
        r"(?:万亿|亿|万|[tT][cC][oO]2?[eE]|%|％|‰|GJ|MWh|GW|MW|trillion|billion|million|thousand)?"
    )
    numbers: list[str] = []
    for m in pattern.finditer(text):
        raw = m.group(0).strip()
        if not raw:
            continue

        left = text[max(0, m.start() - 20):m.start()]
        right = text[m.end():min(len(text), m.end() + 20)]
        left_lower = left.lower()

        # Ignore heading ordinal numbers like "Layer-1/2/3/4".
        if "layer-" in left_lower or "layer " in left_lower:
            continue

        core = (
            raw.replace(",", "")
            .replace("，", "")
            .replace("%", "%")
            .replace("％", "%")
            .replace(" ", "")
            .strip()
        )
        core_plain = core.replace("%", "").replace("‰", "")

        # Ignore years; year consistency is checked by _check_entity_hallucination.
        if re.fullmatch(r"20\d{2}", core_plain):
            continue

        # Ignore page/citation indices, e.g. "第7页", "[来源 9]".
        if re.fullmatch(r"\d+", core_plain):
            if left.endswith("第") and right.startswith("页"):
                continue
            if left.rstrip().endswith("[") and right.lstrip().startswith("]"):
                continue
            if re.search(r"(来源|source)\s*$", left_lower):
                continue
            # Ignore tiny ordinal integers without units.
            if int(core_plain) <= 99 and not re.search(r"[%％‰a-zA-Z万亿]", raw):
                continue

        numbers.append(core)

    return numbers


def _sort_suspicious_numbers(numbers: list[str]) -> list[str]:
    """
    Sort suspicious numeric tokens by normalized value (desc), unparsable last.
    This improves hit rate by prioritizing high-impact absolute numbers.
    """

    def _key(token: str) -> tuple[int, float]:
        value = normalize_financial_number(token)
        if value is None:
            return (1, 0.0)
        return (0, -value)

    return sorted(numbers, key=_key)


_NUMBER_CHECK_PROMPT = """\
You are an ESG report auditor. Decide whether the suspicious numbers in the report
are supported by the provided context. Only judge the numbers listed.

## Report excerpt
{analysis_excerpt}

## Suspicious numbers (JSON list)
{suspected_numbers}

## Full context (SQL + RAG + Synthesis)
{full_context}

## Output
Return a JSON array of objects like:
{{"number": "...", "location": "...", "risk": "high|medium"}}
Only include numbers that fail verification.
If all suspicious numbers are supported, return [].
Do not output explanations or markdown.
"""


def _check_numbers(state: AgentState, trace_id: str) -> list[dict]:
    analysis = state.get("analysis", "")
    sql_result = get_sql_result_dataframe(state)
    rag_result = state.get("rag_result") or {}
    checker_model = _resolve_checker_model()
    analysis_limit = int(os.getenv("CHECKER_ANALYSIS_CHARS", "0"))
    # NOTE: Increase default checker context windows to reduce false hallucination flags.
    sql_limit = int(os.getenv("CHECKER_SQL_CHARS", "3000"))
    rag_chunk_chars = int(os.getenv("CHECKER_RAG_CHUNK_CHARS", "600"))
    rag_chunks = int(os.getenv("CHECKER_RAG_CHUNKS", "5"))
    full_context_limit = int(os.getenv("CHECKER_FULL_CONTEXT_CHARS", "0"))

    sql_str = ""
    if sql_result is not None and len(sql_result) > 0:
        sql_str = sql_result.to_string(index=False, max_rows=30)
        if sql_limit > 0:
            sql_str = sql_str[:sql_limit]

    rag_str = ""
    for chunk in rag_result.get("chunks", [])[:rag_chunks]:
        chunk_text = chunk.get("text", "")
        if rag_chunk_chars > 0:
            chunk_text = chunk_text[:rag_chunk_chars]
        rag_str += chunk_text + "\n"

    synth_ctx = state.get("synthesis_context") or ""

    # NOTE: Preserve SQL first, then RAG; trim from synth tail when over limit.
    if full_context_limit > 0:
        sql_budget = max(0, min(len(sql_str), full_context_limit))
        kept_sql = sql_str[:sql_budget]
        remaining_after_sql = max(0, full_context_limit - len(kept_sql))

        rag_budget = max(0, min(len(rag_str), remaining_after_sql))
        kept_rag = rag_str[:rag_budget]
        remaining_after_rag = max(0, remaining_after_sql - len(kept_rag))

        # Truncate synth from tail (keep prefix), instead of global head-cut truncation.
        kept_synth = synth_ctx[:remaining_after_rag]
        full_context = "\n".join([kept_sql, kept_rag, kept_synth]).strip()
    else:
        full_context = "\n".join([sql_str, rag_str, synth_ctx]).strip()

    analysis_excerpt = analysis if analysis_limit <= 0 else analysis[:analysis_limit]

    numbers = _extract_numbers_from_text(analysis)
    if not numbers:
        return []

    fast_match_rag = ""
    for chunk in rag_result.get("chunks", [])[:_NUMBER_FAST_MATCH_RAG_CHUNKS]:
        chunk_text = chunk.get("text", "")
        if _NUMBER_FAST_MATCH_RAG_CHARS > 0:
            chunk_text = chunk_text[:_NUMBER_FAST_MATCH_RAG_CHARS]
        fast_match_rag += chunk_text + "\n"

    fast_match_context = "\n".join([full_context, fast_match_rag]).strip()
    normalized_context = fast_match_context.replace(",", "").replace("，", "").replace(" ", "")
    normalized_context_values: list[float] = []
    for token in _extract_context_number_tokens(fast_match_context):
        v = normalize_financial_number(token)
        if v is not None:
            normalized_context_values.append(v)
    growth_rate_candidates = _collect_sql_growth_rate_candidates(sql_result)

    suspicious_numbers: list[str] = []
    seen: set[str] = set()
    for num in numbers:
        if not num:
            continue
        if num in seen:
            continue
        seen.add(num)
        if num in full_context:
            continue
        normalized_num = num.replace(",", "").replace("，", "").replace(" ", "")
        if normalized_num and normalized_num in normalized_context:
            continue
        numeric_value = normalize_financial_number(num)
        if numeric_value is not None and any(
            _is_close_number(numeric_value, candidate) for candidate in normalized_context_values
        ):
            continue
        if ("%" in num or "％" in num) and numeric_value is not None:
            if any(_is_close_number(numeric_value, rate) for rate in growth_rate_candidates):
                continue
        suspicious_numbers.append(num)

    if not suspicious_numbers:
        return []

    # Short-circuit safe sampling:
    # We only need a few confirmed hallucinations to reject the report.
    suspicious_numbers = _sort_suspicious_numbers(suspicious_numbers)
    if _NUMBER_LLM_SAMPLE_SIZE > 0 and len(suspicious_numbers) > _NUMBER_LLM_SAMPLE_SIZE:
        log.info(
            "number_checker suspicious numbers sampled: "
            f"{len(suspicious_numbers)} -> {_NUMBER_LLM_SAMPLE_SIZE}"
        )
        suspicious_numbers = suspicious_numbers[:_NUMBER_LLM_SAMPLE_SIZE]

    prompt = _NUMBER_CHECK_PROMPT.format(
        analysis_excerpt=analysis_excerpt,
        suspected_numbers=json.dumps(suspicious_numbers, ensure_ascii=False),
        full_context=full_context or "NO_CONTEXT",
    )

    timeout_s = float(os.getenv("NUMBER_CHECK_TIMEOUT_SEC", "45"))

    try:
        def _call():
            resp = llm_generate_content(
                model=checker_model,
                contents=prompt,
                config=_build_checker_config(
                    temperature=0.0,
                    max_output_tokens=_NUMBER_CHECK_MAX_TOKENS,
                    timeout_s=timeout_s,
                ),
            )
            return _parse_json_array_response(resp.text)

        suspicious = llm_call_with_retry(
            _call,
            max_retries=_NUMBER_CHECKER_MAX_RETRIES,
            timeout_seconds=timeout_s,
            base_delay=0.5,
            caller_name="number_checker", trace_id=trace_id,
        )

        errors = []
        for item in (suspicious or []):
            if item.get("risk") == "high":
                errors.append({
                    "type":     "NUMBER_HALLUCINATION",
                    "detail":   (
                        f"Number '{item.get('number')}' not grounded "
                        f"({item.get('location','')})"
                    ),
                    "priority": 2,
                })
        return errors

    except Exception as e:
        log.warning(f"number_checker LLM failed: {e}")
        return [{
            "type": "EVAL_API_ERROR",
            "detail": "璐ㄦ妯″瀷璋冪敤寮傚父锛屼负淇濊瘉鏁版嵁瀹夊叏瀹炴柦鎷︽埅",
            "priority": 1,
        }]


def _check_entity_hallucination(state: AgentState) -> list[dict]:
    """Guard against years beyond requested range (policy years exempt)."""
    entities = state.get("entities", {})
    requested_years = set(str(y) for y in entities.get("years", []))
    analysis = state.get("analysis", "")

    errors = []

    max_requested = max((int(y) for y in requested_years), default=0)
    min_requested = min((int(y) for y in requested_years), default=0)
    for match in re.finditer(r"(?<![\d.])20(\d{2})(?![\d.])", analysis):
        full_year = "20" + match.group(1)
        year_int = int(full_year)

        # Skip citation/page noise such as "第2024页".
        left = analysis[max(0, match.start() - 1):match.start()]
        right = analysis[match.end():min(len(analysis), match.end() + 1)]
        if left == "第" and right == "页":
            continue

        # If request gives a bounded range (e.g. 2022-2024), allow in-range years.
        if min_requested and max_requested and min_requested <= year_int <= max_requested:
            continue

        if full_year not in requested_years and year_int > max_requested:
            if _is_standard_identifier_mention(analysis, match.start(), match.end()):
                continue
            if _is_policy_year_mention(analysis, match.start(), match.end()):
                continue
            errors.append({
                "type":     "ENTITY_HALLUCINATION",
                "detail":   f"Year {full_year} out of requested range {sorted(requested_years)}.",
                "priority": 2,
            })
            break

    return errors


def _select_direction_metric_columns(state: AgentState, sql_result) -> list[str]:
    requested_metrics = [
        metric
        for metric in (state.get("entities", {}) or {}).get("metrics", [])
        if metric in sql_result.columns
    ]
    if requested_metrics:
        return requested_metrics[:6]

    skip_cols = {"year", "quality", "confidence", "company_name", "company", "metric"}
    metric_cols: list[str] = []
    for col in sql_result.columns:
        if col in skip_cols:
            continue
        try:
            if len(sql_result[col].dropna()) >= 2:
                metric_cols.append(col)
        except Exception:
            continue
        if len(metric_cols) >= 6:
            break
    return metric_cols


def _build_direction_metric_catalog(state: AgentState, sql_result) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for metric_key in _select_direction_metric_columns(state, sql_result):
        metric_name, unit = get_metric_display(metric_key)
        catalog.append({
            "metric_key": metric_key,
            "metric_name": metric_name,
            "unit": unit or "",
        })
    return catalog


def _build_direction_trend_ledger(state: AgentState, sql_result) -> str:
    catalog = _build_direction_metric_catalog(state, sql_result)
    if not catalog:
        return "NO_TREND_LEDGER"

    if "year" in sql_result.columns:
        try:
            sql_result = sql_result.sort_values("year")
        except Exception:
            pass

    lines: list[str] = []
    for item in catalog:
        metric_key = item["metric_key"]
        try:
            series = sql_result[metric_key].dropna()
        except Exception:
            continue
        if len(series) < 2:
            continue
        valid_rows = sql_result.loc[series.index]
        start_row = valid_rows.iloc[0]
        end_row = valid_rows.iloc[-1]
        start_value = str(start_row.get(metric_key, "")).strip()
        end_value = str(end_row.get(metric_key, "")).strip()
        start_year = str(start_row.get("year", "")).strip()
        end_year = str(end_row.get("year", "")).strip()
        start_num = normalize_financial_number(start_value)
        end_num = normalize_financial_number(end_value)
        direction = "mixed"
        if start_num is not None and end_num is not None:
            if _is_close_number(start_num, end_num, rel_tol=0.01):
                direction = "flat"
            elif end_num > start_num:
                direction = "up"
            elif end_num < start_num:
                direction = "down"
        lines.append(
            "- "
            f"metric_key={metric_key}; metric_name={item['metric_name']}; "
            f"time_window={start_year or '?'}->{end_year or '?'}; "
            f"start={start_value}; end={end_value}; direction={direction}"
        )
    return "\n".join(lines) or "NO_TREND_LEDGER"


_DIRECTION_CHECK_PROMPT = """\
You are a strict ESG direction contradiction checker.
Only detect HARD direction contradictions between report and data.

## Data summary
{data_summary}

## Report excerpt (Layer-2 and Layer-3)
{conclusion_excerpt}

## Output
Return a JSON array of objects like:
{{"contradiction": "...", "data_says": "...", "report_says": "..."}}
Ignore tiny rounding differences (e.g. 179.4% vs 179.5%).
Keep each string short (<= 80 chars), and return at most 3 contradictions.
If no contradiction, return [].
Do not output explanations or markdown.

## Strict rules
- A contradiction exists ONLY when the same metric over the same time window has opposite direction:
  data says up but report says down, or data says down but report says up.
- If the metric in report is not clearly present in data summary, do not flag contradiction.
- Do NOT flag these as contradictions:
  1) absolute value up while intensity down (can coexist),
  2) incomplete disclosure / omitted details / insufficient discussion,
  3) causal interpretation, risk opinion, or tone mismatch,
  4) scope mismatch (e.g., scope1 vs scope1+2) unless report explicitly claims equivalence.
- If uncertain, return [].
"""

_DIRECTION_CLAIM_EXTRACT_PROMPT = """\
You are an ESG report claim extractor.
Extract only explicit direction-related claims from the report excerpt.

## Allowed metrics
{allowed_metrics}

## Trend ledger
{trend_ledger}

## Report excerpt (Layer-2 and Layer-3)
{conclusion_excerpt}

## Output
Return JSON only, one of:
1) {{"claims":[...]}}
2) [...]

Each claim item must be:
{{
  "metric_key": "one of the allowed metric_key values, or empty string if not confident",
  "metric_name": "short readable metric name",
  "claim_type": "trend|causal|benchmark|risk|unknown",
  "time_window": "e.g. 2022-2024 or 2023->2024",
  "direction": "up|down|flat|mixed|unknown",
  "report_quote": "short quote from report"
}}

Rules:
- Mark claim_type="trend" ONLY when the excerpt explicitly states a metric moved up/down/flat over a time window.
- If the sentence is mainly causal interpretation, benchmark commentary, risk opinion, or generic narrative, do NOT mark it as trend.
- If you cannot confidently map the claim to one allowed metric_key, set metric_key="" and claim_type="unknown".
- Keep at most 4 claims.
- Keep each field concise (prefer <= 40 chars).
- If no clear direction claim, return {{"claims":[]}}.
- Return compact JSON only, no extra commentary.
- Do not output markdown.
"""

_DIRECTION_CONTRADICTION_JUDGE_PROMPT = """\
You are a strict ESG direction contradiction checker.
Given extracted trend claims, a trend ledger, and the report excerpt, return only HARD contradictions.

## Allowed metrics
{allowed_metrics}

## Trend ledger (authoritative)
{trend_ledger}

## Extracted claims (JSON)
{claims_json}

## Report excerpt
{conclusion_excerpt}

## Output
Return a JSON array of objects:
{{"contradiction": "...", "data_says": "...", "report_says": "..."}}

Strict rules:
- Contradiction only when the SAME metric_key over the SAME time window has opposite direction.
- If metric/time window is unclear, do not flag.
- Do NOT flag:
  1) absolute up + intensity down,
  2) omission/incomplete disclosure,
  3) causal or risk interpretation differences,
  4) tone/quality judgment,
  5) scope mismatch or generic "emissions" wording without a clear metric_key match.
- Keep each field short (<= 80 chars), max 3 items.
- If no contradiction, return [].
- Output JSON only.
"""

_UP_WORDS = {"涓婂崌", "澧為暱", "澧炲姞", "鎻愬崌", "improve", "increase", "upward", "rise"}
_DOWN_WORDS = {"涓嬮檷", "鍑忓皯", "闄嶄綆", "涓嬫粦", "decrease", "decline", "downward", "drop", "fall"}
_FLAT_WORDS = {"鎸佸钩", "绋冲畾", "娉㈠姩涓嶅ぇ", "flat", "stable", "unchanged"}


def _parse_json_array_response(raw_text: str) -> list[dict]:
    """Parse model output into a JSON array with tolerant extraction."""
    txt = (raw_text or "").strip()
    txt = re.sub(r"^```json\s*", "", txt)
    txt = re.sub(r"^```\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)

    candidates: list[str] = [txt]
    for opener, closer in (("[", "]"), ("{", "}")):
        start = txt.find(opener)
        end = txt.rfind(closer)
        if start != -1 and end != -1 and end > start:
            candidates.append(txt[start:end + 1])

    parsed = None
    last_exc: Exception | None = None
    for candidate in candidates:
        for patched in (
            candidate,
            # Light repair for truncated endings when model hits token cap.
            candidate
            + ("]" * max(0, candidate.count("[") - candidate.count("]")))
            + ("}" * max(0, candidate.count("{") - candidate.count("}"))),
        ):
            try:
                parsed = json.loads(patched)
                break
            except Exception as exc:
                last_exc = exc
        if parsed is not None:
            break

    if parsed is None:
        raise ValueError(f"LLM JSON response parse failed: {last_exc}")

    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        items = parsed.get("items")
        if isinstance(items, list):
            return items
    raise ValueError("LLM JSON response is not an array")


def _parse_direction_claims_response(raw_text: str) -> list[dict]:
    """Parse stage-1 claim extraction output."""
    txt = (raw_text or "").strip()
    txt = re.sub(r"^```json\s*", "", txt)
    txt = re.sub(r"^```\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)

    candidates: list[str] = [txt]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = txt.find(opener)
        end = txt.rfind(closer)
        if start != -1 and end != -1 and end > start:
            candidates.append(txt[start:end + 1])

    parsed = None
    last_exc: Exception | None = None
    for candidate in candidates:
        for patched in (
            candidate,
            candidate
            + ("]" * max(0, candidate.count("[") - candidate.count("]")))
            + ("}" * max(0, candidate.count("{") - candidate.count("}"))),
        ):
            try:
                parsed = json.loads(patched)
                break
            except Exception as exc:
                last_exc = exc
        if parsed is not None:
            break

    if parsed is None:
        raise ValueError(f"direction claims parse failed: {last_exc}")

    if isinstance(parsed, list):
        claims = parsed
    elif isinstance(parsed, dict):
        claims = parsed.get("claims", [])
    else:
        raise ValueError("direction claims payload must be list/dict")

    if not isinstance(claims, list):
        raise ValueError("direction claims must be a list")

    normalized: list[dict] = []
    for item in claims:
        if not isinstance(item, dict):
            continue
        metric_key = str(item.get("metric_key", "")).strip()
        metric_name = str(item.get("metric_name", item.get("metric", ""))).strip()
        claim_type = str(item.get("claim_type", "unknown")).strip().lower()
        time_window = str(item.get("time_window", "")).strip()
        direction = str(item.get("direction", "")).strip().lower()
        report_quote = str(item.get("report_quote", "")).strip()
        if claim_type not in {"trend", "causal", "benchmark", "risk", "unknown"}:
            claim_type = "unknown"
        if direction not in {"up", "down", "flat", "mixed", "unknown"}:
            direction = "unknown"
        if not metric_key and not metric_name and not report_quote:
            continue
        normalized.append({
            "metric_key": metric_key,
            "metric_name": metric_name,
            "claim_type": claim_type,
            "time_window": time_window,
            "direction": direction,
            "report_quote": report_quote,
        })
    return normalized


def _quick_direction_check(sql_result: object, analysis: str) -> list[dict] | None:
    """
    Fast path for simple trend statements. Returns:
    - [] / [errors] when a quick decision can be made
    - None when ambiguous (fallback to LLM check)
    """
    if sql_result is None or not analysis:
        return []
    try:
        import pandas as pd
        df = sql_result.copy() if hasattr(sql_result, "copy") else pd.DataFrame(sql_result)
    except Exception:
        return None

    if getattr(df, "empty", True):
        return []

    if "year" in df.columns:
        try:
            df = df.sort_values("year")
        except Exception:
            pass

    skip_cols = {"year", "quality", "confidence", "company_name", "company", "metric"}
    numeric_cols: list[str] = []
    for col in df.columns:
        if col in skip_cols:
            continue
        try:
            series = pd.to_numeric(df[col], errors="coerce").dropna()
        except Exception:
            continue
        if len(series) >= 2:
            numeric_cols.append(col)

    if not numeric_cols:
        return []
    if len(numeric_cols) > 2:
        # Too many metrics: let LLM do a semantic check.
        return None

    text_lower = analysis.lower()
    has_up = any(w in analysis for w in _UP_WORDS) or any(w in text_lower for w in _UP_WORDS)
    has_down = any(w in analysis for w in _DOWN_WORDS) or any(w in text_lower for w in _DOWN_WORDS)
    has_flat = any(w in analysis for w in _FLAT_WORDS) or any(w in text_lower for w in _FLAT_WORDS)

    # No explicit direction claim -> no need for expensive semantic LLM check.
    if not has_up and not has_down and not has_flat:
        return []

    contradictions: list[dict] = []
    for col in numeric_cols:
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        values = [float(v) for v in series.tolist()]
        if len(values) < 2:
            continue
        first_v, last_v = values[0], values[-1]
        if abs(first_v) <= 1e-12:
            continue
        trend = (last_v - first_v) / abs(first_v)

        if trend > 0.02 and has_down and not has_up:
            contradictions.append({
                "type": "DIRECTION_CONTRADICTION",
                "detail": f"Trend appears up in {col}, but report states decline.",
                "priority": 1,
            })
        elif trend < -0.02 and has_up and not has_down:
            contradictions.append({
                "type": "DIRECTION_CONTRADICTION",
                "detail": f"Trend appears down in {col}, but report states increase.",
                "priority": 1,
            })
        elif abs(trend) <= 0.01 and (has_up ^ has_down):
            contradictions.append({
                "type": "DIRECTION_CONTRADICTION",
                "detail": f"Trend appears flat in {col}, but report gives one-way direction.",
                "priority": 1,
            })

    return contradictions


def _check_direction_consistency(state: AgentState, trace_id: str) -> list[dict]:
    sql_result = get_sql_result_dataframe(state)
    analysis = state.get("analysis", "")
    checker_model = _resolve_checker_model()
    data_limit = int(os.getenv("DIRECTION_CHECK_DATA_CHARS", "600"))
    conclusion_limit = int(os.getenv("DIRECTION_CHECK_CONCLUSION_CHARS", "600"))
    max_judge_claims = int(os.getenv("DIRECTION_JUDGE_MAX_CLAIMS", "4"))

    if sql_result is None or len(sql_result) == 0:
        return []

    data_summary = sql_result.to_string(index=False, max_rows=15)
    if data_limit > 0:
        data_summary = data_summary[:data_limit]
    metric_catalog = _build_direction_metric_catalog(state, sql_result)
    allowed_metrics = json.dumps(metric_catalog, ensure_ascii=False)
    trend_ledger = _build_direction_trend_ledger(state, sql_result)

    l2_match = re.search(r"Layer-2.*?(?=Layer-3|$)", analysis, re.DOTALL)
    l3_match = re.search(r"Layer-3.*?(?=Layer-4|$)", analysis, re.DOTALL)
    conclusion_excerpt = ""
    if l2_match:
        conclusion_excerpt += l2_match.group()[:conclusion_limit]
    if l3_match:
        conclusion_excerpt += l3_match.group()[:conclusion_limit]

    if not conclusion_excerpt:
        return []

    timeout_s = float(os.getenv("DIRECTION_CHECK_TIMEOUT_SEC", "40"))
    extract_max_tokens = int(os.getenv("DIRECTION_EXTRACT_MAX_TOKENS", "420"))
    judge_max_tokens = _DIRECTION_CHECK_MAX_TOKENS

    try:
        extract_prompt = _DIRECTION_CLAIM_EXTRACT_PROMPT.format(
            allowed_metrics=allowed_metrics,
            trend_ledger=trend_ledger,
            conclusion_excerpt=conclusion_excerpt,
        )

        def _extract_claims_call():
            resp = llm_generate_content(
                model=checker_model,
                contents=extract_prompt,
                config=_build_checker_config(
                    temperature=0.0,
                    max_output_tokens=extract_max_tokens,
                    timeout_s=timeout_s,
                ),
            )
            return _parse_direction_claims_response(resp.text)

        claims = llm_call_with_retry(
            _extract_claims_call,
            max_retries=_DIRECTION_CHECKER_MAX_RETRIES,
            timeout_seconds=timeout_s,
            base_delay=0.5,
            caller_name="direction_claim_extractor",
            trace_id=trace_id,
        )
        claims = claims or []
        log.info(f"direction_checker stage1 claims: {len(claims)}")
        judgeable_claims = [
            claim
            for claim in claims
            if claim.get("claim_type") == "trend"
            and claim.get("metric_key")
            and claim.get("direction") in {"up", "down", "flat"}
        ]
        if max_judge_claims > 0:
            judgeable_claims = judgeable_claims[:max_judge_claims]
        log.info(f"direction_checker stage1 judgeable claims: {len(judgeable_claims)}")
        if not judgeable_claims:
            return []

        judge_prompt = _DIRECTION_CONTRADICTION_JUDGE_PROMPT.format(
            allowed_metrics=allowed_metrics,
            trend_ledger=trend_ledger,
            claims_json=json.dumps(judgeable_claims, ensure_ascii=False),
            conclusion_excerpt=conclusion_excerpt,
        )

        def _judge_call():
            resp = llm_generate_content(
                model=checker_model,
                contents=judge_prompt,
                config=_build_checker_config(
                    temperature=0.0,
                    max_output_tokens=judge_max_tokens,
                    timeout_s=timeout_s,
                ),
            )
            return _parse_json_array_response(resp.text)

        contradictions = llm_call_with_retry(
            _judge_call,
            max_retries=_DIRECTION_CHECKER_MAX_RETRIES,
            timeout_seconds=timeout_s,
            base_delay=0.5,
            caller_name="direction_checker",
            trace_id=trace_id,
        )
        log.info(f"direction_checker stage2 contradictions: {len(contradictions or [])}")

        return [
            {
                "type":     "DIRECTION_CONTRADICTION",
                "detail":   (
                    f"Contradiction: {item.get('contradiction','')}; "
                    f"data: {item.get('data_says','')}; "
                    f"report: {item.get('report_says','')}"
                ),
                "priority": 1,
            }
            for item in (contradictions or [])
        ]

    except Exception as e:
        log.warning(f"direction_checker LLM failed: {e}")
        return [{
            "type": "EVAL_API_ERROR",
            "detail": "璐ㄦ妯″瀷璋冪敤寮傚父锛屼负淇濊瘉鏁版嵁瀹夊叏瀹炴柦鎷︽埅",
            "priority": 1,
        }]


def _check_scope_annotation(state: AgentState) -> list[dict]:
    scope = state.get("scope_consistency", {})
    analysis = state.get("analysis", "")
    errors = []
    warning_window_chars = int(os.getenv("SCOPE_WARNING_WINDOW_CHARS", "300"))
    warning_keywords = [
        "口径", "范围", "不一致", "scope", "注意", "说明", "可比性",
        "boundary", "definition", "not directly comparable",
        "不可比", "不可直接比较", "非严格意义", "计算基准", "统计口径",
        "培训人次", "场次", "百分比", "布尔", "binary", "0/1", "是/否",
        "单位未转换", "单位不一致", "量纲", "边界不可比",
        "无法直接对比", "无法直接数值对比", "不可直接数值对比",
    ]

    def _build_metric_patterns(metric_key: str, metric_cn: str) -> list[re.Pattern[str]]:
        patterns: list[re.Pattern[str]] = []
        aliases = {
            metric_key.strip(),
            metric_key.replace("_", " ").strip(),
        }
        if metric_cn:
            aliases.add(metric_cn.strip())
            aliases.add(metric_cn.replace(" ", "").strip())
            if metric_cn.endswith("率"):
                aliases.add(metric_cn[:-1].strip())
            if metric_cn.endswith("情况"):
                aliases.add(metric_cn[:-2].strip())
            if "碳排放" in metric_cn:
                aliases.add(metric_cn.replace("碳排放", "碳排放量").strip())

        for alias in aliases:
            if not alias:
                continue
            token_pattern = re.escape(alias)
            token_pattern = token_pattern.replace(r"\ ", r"[\s\u3000]*")
            token_pattern = token_pattern.replace(r"_", r"[\s_\-]*")
            token_pattern = token_pattern.replace(r"\-", r"[\s_\-]*")
            patterns.append(re.compile(token_pattern, re.IGNORECASE))

        if metric_key.startswith("scope_"):
            parts = metric_key.split("_")
            scope_no = parts[1] if len(parts) > 1 else ""
            scope_cn = {"1": "一", "2": "二", "3": "三"}.get(scope_no, scope_no)
            patterns.extend(
                [
                    re.compile(
                        rf"范围[{scope_cn}{scope_no}][\s\u3000]*[（(]?\s*scope[\s\-]*{scope_no}\s*[)）]?[\s\u3000]*碳排放(?:量)?",
                        re.IGNORECASE,
                    ),
                    re.compile(
                        rf"范围[{scope_cn}{scope_no}][\s\u3000]*碳排放(?:量)?",
                        re.IGNORECASE,
                    ),
                    re.compile(
                        rf"scope[\s\-]*{scope_no}[\s\u3000]*(?:carbon[\s\-]*emissions?|emissions?|碳排放(?:量)?)",
                        re.IGNORECASE,
                    ),
                ]
            )
        return patterns

    if not scope.get("checked"):
        return []

    for mk, info in scope.get("per_metric", {}).items():
        if info.get("action") == "flagged":
            # NOTE: Scope warning must appear near the metric mention (local window),
            # not anywhere in the full report.
            metric_cn, _ = get_metric_display(mk)
            analysis_lower = analysis.lower()
            positions = sorted(
                {
                    match.start()
                    for pattern in _build_metric_patterns(mk, metric_cn)
                    for match in pattern.finditer(analysis)
                }
            )

            has_warning_near_metric = False
            for pos in positions:
                left = max(0, pos - warning_window_chars)
                right = min(len(analysis), pos + warning_window_chars)
                local_window = analysis_lower[left:right]
                if any(keyword.lower() in local_window for keyword in warning_keywords):
                    has_warning_near_metric = True
                    break

            if not (positions and has_warning_near_metric):
                errors.append({
                    "type":     "MISSING_SCOPE_ANNOTATION",
                    "detail":   f"Missing scope annotation for {mk}.",
                    "priority": 2,
                })

    return errors

_DEGRADED_DISCLAIMER = """
---
> **Output degraded due to repeated quality check failures**
> Retry count: {retry_count}
{error_list}
> The answer above is a best-effort fallback.
"""


def _append_disclaimer(analysis: str, errors: list[dict], retry_count: int) -> str:
    error_list = "\n".join(
        f"> - {e.get('type')}: {e.get('detail', '')[:100]}"
        for e in errors[:5]
    )
    disclaimer = _DEGRADED_DISCLAIMER.format(
        retry_count=retry_count,
        error_list=error_list,
    )
    return analysis + disclaimer


@trace_node("evaluator_o", tags=["evaluation"])
def evaluator_o_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log = TraceLogger("evaluator_o", trace_id)
    retry_count = state.get("eval_o_retry_count", 0)
    analysis = state.get("analysis", "")

    if not analysis:
        log.warning("empty analysis output")
        state["eval_o_status"] = "fail"
        state["eval_o_errors"] = [{"type": "EMPTY_OUTPUT", "detail": "Empty analysis.", "priority": 0}]
        return state

    log.info(
        f"Evaluator-O run {retry_count + 1}, analysis length={len(analysis)}"
    )

    all_errors: list[dict] = []

    structure_errors = _check_structure(analysis)
    all_errors.extend(structure_errors)
    log.info(f"Check-1 structure errors: {len(structure_errors)}")

    entity_errors = _check_entity_hallucination(state)
    all_errors.extend(entity_errors)
    log.info(f"Check-3 entity errors: {len(entity_errors)}")

    if not structure_errors:
        run_direction_check = (
            _DIRECTION_CHECK_ENABLED
            and not (_EVAL_O_SKIP_DIRECTION_IF_BLOCKING and _has_blocking_error(all_errors))
        )
        number_errors: list[dict] = []
        direction_errors: list[dict] = []

        if run_direction_check:
            # NOTE: Check-2 and Check-4 are independent LLM calls; run in parallel
            # to cap tail latency to max(check2, check4) instead of sum(check2+check4).
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="eval_o_checker") as pool:
                number_future = pool.submit(_check_numbers, state, trace_id)
                direction_future = pool.submit(_check_direction_consistency, state, trace_id)
                number_errors = number_future.result()
                direction_errors = direction_future.result()
        else:
            number_errors = _check_numbers(state, trace_id)

        all_errors.extend(number_errors)
        log.info(f"Check-2 number errors: {len(number_errors)}")

        if run_direction_check:
            all_errors.extend(direction_errors)
            log.info(f"Check-4 direction errors: {len(direction_errors)}")
            if direction_errors:
                log.info(
                    "Check-4 direction details: "
                    + "; ".join((e.get("detail", "") or "")[:200] for e in direction_errors[:3])
                )
        else:
            if not _DIRECTION_CHECK_ENABLED:
                log.info("Check-4 skipped by config (DIRECTION_CHECK_ENABLED=0)")
            else:
                log.info("Check-4 skipped due to existing blocking errors")
    else:
        log.info("Check-2 skipped due to structure errors")
        log.info("Check-4 skipped due to structure errors")

    scope_errors = _check_scope_annotation(state)
    all_errors.extend(scope_errors)
    log.info(f"Check-5 scope errors: {len(scope_errors)}")

    if _EVAL_O_DEBUG and all_errors:
        error_lines = "; ".join(
            f"{e.get('type')}: {e.get('detail', '')}" for e in all_errors
        )
        log.warning(f"Evaluator-O debug errors: {error_lines}")
        log.warning("Evaluator-O debug analysis excerpt:\n" + analysis[:_EVAL_O_DEBUG_CHARS])

    if not all_errors:
        log.info("Evaluator-O pass")
        state["eval_o_status"] = "pass"
        state["eval_o_errors"] = []
        return state

    all_errors.sort(key=lambda e: e.get("priority", 9))
    priority0_errors = [e for e in all_errors if e.get("priority") == all_errors[0].get("priority")]

    # If synthesis already used fallback mode and still failed strict checks,
    # do not keep looping rewrite->recheck. Degrade directly to cap latency.
    if (
        _EVAL_O_SHORT_CIRCUIT_ON_SYNTH_FALLBACK
        and state.get("synth_fallback_used", False)
        and priority0_errors
        and priority0_errors[0].get("type") != "EVAL_API_ERROR"
    ):
        log.warning(
            "Evaluator-O short-circuit: synth fallback already used; "
            "degrading directly to avoid repeated rewrite loops"
        )
        state["eval_o_errors"] = priority0_errors
        reason_detail = f"errors={ [e.get('type', 'UNKNOWN') for e in priority0_errors[:3]] }"
        partial = _append_disclaimer(analysis, priority0_errors, retry_count)
        return apply_degraded_state(
            state,
            reason_code="EVAL_O_FALLBACK_SHORT_CIRCUIT",
            reason_detail=reason_detail,
            partial_analysis=partial,
        )

    # Circuit breaker: infra failures should not trigger meaningless rewrite loops.
    if priority0_errors and priority0_errors[0].get("type") == "EVAL_API_ERROR":
        log.warning("Evaluator-O infra error detected; degrade directly without retry loop")
        state["eval_o_errors"] = priority0_errors
        degraded_notice = "⚠️ 质检基础设施异常，本报告未完成最终数字校验"
        partial = f"{analysis}\n\n{degraded_notice}\n"
        return apply_degraded_state(
            state,
            reason_code="EVAL_O_INFRA_ERROR",
            reason_detail="checker_api_unavailable",
            partial_analysis=partial,
        )

    if retry_count >= MAX_EVAL_O_RETRY:
        log.warning(f"Evaluator-O exceeded max retries ({MAX_EVAL_O_RETRY}), degraded output")
        state["eval_o_errors"] = all_errors
        reason_detail = f"errors={ [e['type'] for e in all_errors[:3]] }"
        partial = _append_disclaimer(analysis, all_errors, retry_count)
        return apply_degraded_state(
            state,
            reason_code="EVAL_O_MAX_RETRY",
            reason_detail=reason_detail,
            partial_analysis=partial,
        )

    if _EVAL_O_LOG_FAIL_ANALYSIS:
        log.warning(
            "Evaluator-O fail analysis excerpt:\n"
            + analysis[:_EVAL_O_FAIL_ANALYSIS_CHARS]
        )
    log.warning(
        f"Evaluator-O failed (retry {retry_count + 1}), errors="
        f"{[e['type'] for e in priority0_errors]}"
    )
    state["eval_o_status"] = "fail"
    state["eval_o_errors"] = priority0_errors
    state["eval_o_retry_count"] = retry_count + 1

    return state



