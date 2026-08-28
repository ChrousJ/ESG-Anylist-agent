"""
agent/nodes/synthesizer.py  鈥? 鍥涘眰鍒嗘瀽鍚堟垚鑺傜偣锛堟祦绋嬬殑绗?鈶?姝ワ級
================================================================

銆愬湪娴佺▼涓殑浣嶇疆銆憁ap_reduce 鈫?鈽卻ynthesizer鈽?鈫?evaluator_o

銆愯繖涓妭鐐瑰共浠€涔堬紵銆?

Synthesizer 鏄暣涓郴缁熺殑"鍐欎綔鑰?鈥斺€斿畠鎶婂墠闈㈡敹闆嗗埌鐨勬墍鏈夋暟鎹?
锛圫QL 鏌ヨ缁撴灉 + RAG 鏂囨湰娈佃惤 + 缂哄け鎶ュ憡锛夎浆鍖栦负涓€浠戒笓涓氱殑 ESG 鍒嗘瀽鎶ュ憡銆?

鎶ュ憡涓ユ牸閬靛惊 MSCI ESG 鍒嗘瀽妗嗘灦鐨勫洓灞傜粨鏋勶細

  Layer-1 鈹€鈹€ 鍩虹鏁版嵁灞?
    鏍囧噯鍖栨暟鎹?+ 鍚屾瘮鍙樺寲 + 琛屼笟鍒嗕綅鏁?+ 鍥捐〃
    渚嬪锛?姣斾簹杩?2023 骞?Scope 1 鎺掓斁 12.3 涓囧惃锛屽悓姣斾笅闄?15%"

  Layer-2 鈹€鈹€ 椹卞姩鍥犵礌鍒嗘瀽灞傦紙鍥炵瓟"涓轰粈涔?锛?
    绾靛悜鍒嗘瀽鍙樺寲鍘熷洜
    渚嬪锛?涓嬮檷涓昏鍥犱负鍏変紡鍙戠數鑷敤姣斾緥鎻愬崌鍒?65%"

  Layer-3 鈹€鈹€ 琛屼笟瀵规爣灞傦紙鍥炵瓟"鍦ㄨ涓氶噷浠€涔堟按骞?锛?
    妯悜瀵规瘮鍚岃
    渚嬪锛?鍦ㄦ柊鑳芥簮杞︿紒涓浜庣 2 浣嶏紝浣庝簬瀹佸痉鏃朵唬"

  Layer-4 鈹€鈹€ 椋庨櫓涓庡悎瑙勫眰
    椋庨櫓鎻愮ず + 鏀跨瓥閿氬畾
    渚嬪锛?闇€鍏虫敞 EU CBAM 纰冲叧绋庡鍑哄彛褰卞搷"

姝ゅ杩樹細锛?
  - 鑷姩鐢熸垚 Plotly 鍥捐〃 JSON Spec锛堝彲瑙嗗寲鏁版嵁瓒嬪娍锛?
  - 鎻愮偧 3~5 鏉?key_findings锛堟憳瑕佽鐐癸級
  - 澶勭悊鍙ｅ緞宸紓鏍囨敞锛坒lagged 鈫?绂佹瀵规瘮锛宎djusted 鈫?娉ㄦ槑鏂规硶锛?

銆愬啓鍏?State 鐨勫叧閿瓧娈点€?
  - analysis: 鍥涘眰 Markdown 鎶ュ憡姝ｆ枃
  - chart_spec: Plotly 鍥捐〃 JSON
  - key_findings: 鍏抽敭鍙戠幇鍒楄〃
  - sources: 寮曠敤鏉ユ簮婧簮
  - normalization_applied: 褰掍竴鍖栨柟妗堣褰?
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from google.genai import types
from dotenv import load_dotenv

from agent.state import AgentState, NormalizationRecord, get_sql_result_dataframe
from agent.tracing import trace_node, TraceLogger, llm_call_with_retry
from agent.llm_provider import get_default_model, llm_generate_content
from agent.materiality import get_materiality_context_for_synthesizer
from agent.data_dictionary import get_metric_display, METRIC_DISPLAY_NAMES

load_dotenv()
log     = logging.getLogger(__name__)


def _bounded_int_env(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None and str(raw).strip() != "" else default
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _bounded_float_env(name: str, default: float, min_value: float, max_value: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None and str(raw).strip() != "" else default
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


_MODEL = os.getenv("LLM_MAIN_MODEL", get_default_model())
_SYNTH_TEMPERATURE = float(os.getenv("SYNTH_TEMPERATURE", "0.0"))
_SYNTH_MAX_OUTPUT_TOKENS = _bounded_int_env(
    "SYNTH_MAX_OUTPUT_TOKENS", default=4096, min_value=256, max_value=8192
)
_SYNTH_TIMEOUT_SEC = _bounded_float_env(
    "SYNTH_TIMEOUT_SEC", default=120.0, min_value=10.0, max_value=120.0
)
_SYNTH_MAX_RETRIES = int(os.getenv("SYNTH_MAX_RETRIES", "0"))
_SYNTH_CONTEXT_MAX_CHARS = int(os.getenv("SYNTH_CONTEXT_MAX_CHARS", "5000"))
_SYNTH_FAST_MAX_OUTPUT_TOKENS = _bounded_int_env(
    "SYNTH_FAST_MAX_OUTPUT_TOKENS", default=2048, min_value=128, max_value=4096
)
_SYNTH_FAST_TIMEOUT_SEC = _bounded_float_env(
    "SYNTH_FAST_TIMEOUT_SEC", default=60.0, min_value=10.0, max_value=120.0
)
_SYNTH_FAST_MAX_RETRIES = int(os.getenv("SYNTH_FAST_MAX_RETRIES", "0"))
_SYNTH_FALLBACK_MODEL = os.getenv("SYNTH_FALLBACK_MODEL", "").strip()
_SYNTH_FIX_MAX_ERRORS = int(os.getenv("SYNTH_FIX_MAX_ERRORS", "3"))
_SYNTH_FIX_DETAIL_CHARS = int(os.getenv("SYNTH_FIX_DETAIL_CHARS", "120"))
_SYNTH_PATCH_MAX_OUTPUT_TOKENS = _bounded_int_env(
    "SYNTH_PATCH_MAX_OUTPUT_TOKENS", default=4096, min_value=256, max_value=8192
)
_SYNTH_PATCH_TIMEOUT_SEC = _bounded_float_env(
    "SYNTH_PATCH_TIMEOUT_SEC", default=90.0, min_value=10.0, max_value=120.0
)
_SYNTH_PATCH_MAX_RETRIES = int(os.getenv("SYNTH_PATCH_MAX_RETRIES", "0"))
_SYNTH_SQL_FACT_ROWS = _bounded_int_env(
    "SYNTH_SQL_FACT_ROWS", default=12, min_value=3, max_value=30
)
_SYNTH_SQL_FACT_METRICS = _bounded_int_env(
    "SYNTH_SQL_FACT_METRICS", default=4, min_value=1, max_value=8
)
_SYNTH_RAG_NUMERIC_SENTENCES = _bounded_int_env(
    "SYNTH_RAG_NUMERIC_SENTENCES", default=6, min_value=0, max_value=20
)
_SYNTH_RAG_NUMERIC_SENTENCE_CHARS = _bounded_int_env(
    "SYNTH_RAG_NUMERIC_SENTENCE_CHARS", default=180, min_value=60, max_value=400
)
_PERCENT_METRIC_MAX = _bounded_float_env(
    "PERCENT_METRIC_MAX", default=100.0, min_value=1.0, max_value=1000.0
)


def _is_implausible_metric_value(metric_key: str, value: object) -> bool:
    if value is None:
        return False
    try:
        _, unit = get_metric_display(metric_key)
        numeric_value = float(value)
    except Exception:
        return False
    if unit == "%" and (numeric_value < 0 or numeric_value > _PERCENT_METRIC_MAX):
        return True
    return False
_LAYER_ORDER = ("Layer-1", "Layer-2", "Layer-3", "Layer-4")

# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 瑙勬ā褰掍竴鍖?
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

# 鍚勬寚鏍囩殑褰掍竴鍖栧垎姣嶄紭鍏堢骇
_NORM_DENOMINATORS = {
    "scope_1_emissions":        ["revenue", "output", "employee_count"],
    "scope_2_emissions":        ["revenue", "output"],
    "scope_3_emissions":        ["revenue"],
    "total_energy_consumption": ["revenue", "output"],
    "rd_investment_total":      ["revenue"],
    "charitable_donations":     ["revenue"],
    "green_finance_balance":    ["total_loan_balance"],
    "inclusive_finance_balance":["total_loan_balance"],
}


def _try_normalize(
    sql_result,
    metric_keys: list[str],
    compare_dimension: str,
) -> list[NormalizationRecord]:
    """
    灏濊瘯鍦?SQL 缁撴灉涓仛瑙勬ā褰掍竴鍖栥€?
    杩斿洖褰掍竴鍖栬褰曞垪琛ㄣ€?
    """
    import pandas as pd
    records: list[NormalizationRecord] = []

    if compare_dimension not in ("horizontal", "both"):
        return records
    if sql_result is None or len(sql_result) == 0:
        return records

    for metric_key in metric_keys:
        if metric_key not in _NORM_DENOMINATORS:
            continue
        denom_options = _NORM_DENOMINATORS[metric_key]

        # 鎵惧埌鍙敤鐨勫垎姣嶅垪锛堝鏋?SQL 缁撴灉閲屾湁锛?
        used_denom = None
        for denom in denom_options:
            # 绠€鍖栵細妫€鏌ユ槸鍚︽湁 energy_intensity 绛夊凡褰掍竴鍖栧瓧娈?
            if metric_key == "total_energy_consumption" and "energy_intensity" in sql_result.columns:
                used_denom = "energy_intensity(builtin)"
                break

        records.append(NormalizationRecord(
            metric=metric_key,
            denominator=used_denom or denom_options[0],
            denom_source="sql" if used_denom else "not_found",
            formula=(
                f"{metric_key} 梅 {used_denom or denom_options[0]}"
            ),
            applied=used_denom is not None,
            fallback_note=(
                "" if used_denom
                else f"鏈壘鍒板垎姣嶆暟鎹紙{denom_options[0]}锛夛紝鎶ュ憡涓娇鐢ㄧ粷瀵瑰€煎苟鏍囨敞椋庨櫓"
            ),
        ))

    return records


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 涓婁笅鏂囩粍瑁?
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _is_missing_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        return bool(value != value)
    except Exception:
        return False


def _select_sql_fact_metrics(state: AgentState, sql_result) -> list[str]:
    if sql_result is None or len(sql_result) == 0:
        return []

    requested_metrics = [
        metric
        for metric in (state.get("entities", {}) or {}).get("metrics", [])
        if metric in sql_result.columns
    ]
    if requested_metrics:
        return requested_metrics[:_SYNTH_SQL_FACT_METRICS]

    skip_cols = {"year", "quality", "confidence", "company_name", "company", "metric"}
    selected: list[str] = []
    for col in sql_result.columns:
        if col in skip_cols:
            continue
        series = sql_result[col]
        try:
            has_value = bool(series.notna().any())
        except Exception:
            has_value = True
        if not has_value:
            continue
        selected.append(col)
        if len(selected) >= _SYNTH_SQL_FACT_METRICS:
            break
    return selected


def _build_approved_numeric_facts(state: AgentState) -> list[str]:
    facts: list[str] = []
    sql_result = get_sql_result_dataframe(state)
    metric_cols = _select_sql_fact_metrics(state, sql_result)

    if sql_result is not None and len(sql_result) > 0 and metric_cols:
        shown_rows = 0
        for _, row in sql_result.iterrows():
            company = str(row.get("company_name", row.get("company", ""))).strip()
            year = str(row.get("year", "")).strip()
            metric_parts: list[str] = []
            for metric in metric_cols:
                value = row.get(metric)
                if _is_missing_value(value) or _is_implausible_metric_value(metric, value):
                    continue
                metric_name, unit = get_metric_display(metric)
                value_text = str(value).strip()
                if unit:
                    metric_parts.append(f"{metric_name}({metric})={value_text} {unit}")
                else:
                    metric_parts.append(f"{metric_name}({metric})={value_text}")
            if not metric_parts:
                continue
            prefix_bits = ["SQL"]
            if company:
                prefix_bits.append(f"company={company}")
            if year:
                prefix_bits.append(f"year={year}")
            quality = str(row.get("quality", "")).strip()
            if quality:
                prefix_bits.append(f"quality={quality}")
            facts.append(f"- {' | '.join(prefix_bits)} | " + "; ".join(metric_parts))
            shown_rows += 1
            if shown_rows >= _SYNTH_SQL_FACT_ROWS:
                break

    if _SYNTH_RAG_NUMERIC_SENTENCES <= 0:
        return facts

    rag_result = state.get("rag_result") or {}
    chunks = rag_result.get("chunks", []) or []
    seen_sentences: set[str] = set()
    rag_facts_added = 0
    for chunk in chunks:
        source_label = " ".join(
            part
            for part in [
                str(chunk.get("company_name", "")).strip(),
                str(chunk.get("year", "")).strip(),
                f"p{chunk.get('page_num')}" if chunk.get("page_num") is not None else "",
            ]
            if part
        ).strip()
        text = str(chunk.get("text", "")).strip()
        if not text:
            continue
        sentences = re.split(r"(?<=[。！？；;.!?\n])", text)
        for sentence in sentences:
            normalized = _normalize_ws(sentence)
            if len(normalized) < 8 or not re.search(r"\d", normalized):
                continue
            dedupe_key = re.sub(r"\s+", "", normalized)
            if dedupe_key in seen_sentences:
                continue
            seen_sentences.add(dedupe_key)
            if len(normalized) > _SYNTH_RAG_NUMERIC_SENTENCE_CHARS:
                normalized = normalized[:_SYNTH_RAG_NUMERIC_SENTENCE_CHARS].rstrip() + "..."
            facts.append(f"- RAG | source={source_label or 'unknown'} | sentence={normalized}")
            rag_facts_added += 1
            if rag_facts_added >= _SYNTH_RAG_NUMERIC_SENTENCES:
                return facts

    return facts


def _build_synthesis_context(state: AgentState) -> str:
    """Build synthesis context from SQL, RAG, and quality-control signals."""
    parts: list[str] = []

    approved_numeric_facts = _build_approved_numeric_facts(state)
    if approved_numeric_facts:
        parts.append("=== Approved Numeric Facts (copy exact values only) ===")
        parts.extend(approved_numeric_facts)
        parts.append("")

    sql_result = get_sql_result_dataframe(state)
    if sql_result is not None and len(sql_result) > 0:
        sql_display = sql_result.copy()
        for col in sql_display.columns:
            if col in {"company_name", "company", "year", "quality", "confidence", "metric"}:
                continue
            sql_display[col] = sql_display[col].apply(
                lambda value, metric_key=col: None
                if _is_implausible_metric_value(metric_key, value)
                else value
            )
        parts.append("=== Structured Data (SQL) ===")
        parts.append(sql_display.to_string(index=False, max_rows=25))
        parts.append("")

    if state.get("map_reduce_applied") and state.get("compressed_context"):
        parts.append("=== Retrieved Evidence (Compressed) ===")
        parts.append((state.get("compressed_context") or "")[:5000])
        parts.append("")
    else:
        rag_result = state.get("rag_result") or {}
        chunks = rag_result.get("chunks", [])
        if chunks:
            from data.rag_retriever import format_chunks_for_llm
            parts.append("=== Retrieved Evidence ===")
            parts.append(format_chunks_for_llm(chunks, max_chars=3500))
            parts.append("")

    missing = state.get("missing_data_report", {}) or {}
    if missing.get("L2") or missing.get("L3"):
        parts.append("=== Missing Data Notes ===")
        for item in missing.get("L2", []):
            parts.append(
                f"- {item.get('company', '')} {item.get('year', '')} {item.get('metric', '')}: 数据缺失/未披露"
            )
        for item in missing.get("L3", []):
            parts.append(f"- {item.get('metric', '')}: {item.get('detail', '')}")
        parts.append("")

    scope = state.get("scope_consistency", {}) or {}
    if scope.get("checked") and not scope.get("consistent"):
        parts.append("=== Scope Consistency Notes ===")
        for mk, info in scope.get("per_metric", {}).items():
            action = info.get("action", "")
            if action == "flagged":
                parts.append(f"- {mk}: scope not comparable")
            elif action == "adjusted":
                parts.append(
                    f"- {mk}: scope normalization required via "
                    f"{info.get('adjustment_method', 'method')}; "
                    "do not claim direct comparability without annotation."
                )
        parts.append("")

    norm_records = state.get("normalization_applied", [])
    if norm_records:
        parts.append("=== Normalization Plan ===")
        for r in norm_records:
            if r.get("applied"):
                parts.append(f"- {r['metric']}: {r.get('formula', '')}")
            else:
                parts.append(f"- {r['metric']}: {r.get('fallback_note', '')}")
        parts.append("")

    return "\n".join(parts)


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# Synthesizer Prompt
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

_SYSTEM_PROMPT = """\
You are a professional ESG analyst.
Produce a structured report with exactly 4 layers:
1) Layer-1: Data
2) Layer-2: Drivers
3) Layer-3: Benchmark
4) Layer-4: Risk & Compliance

Hard constraints:
- Every numeric claim must come from provided SQL/RAG context.
- Copy numeric values exactly from the provided evidence whenever possible.
- Do not append extra scale markers such as "万" or "亿" unless the same scale marker appears in the same evidence line.
- Do not invent data, do not estimate, do not project.
- Do not convert units unless the converted value appears explicitly in context.
- Do not compute new ratios, shares, CAGR, or growth percentages unless that computed result already appears explicitly in context.
- If evidence has scope/definition conflicts or missing yearly values, do NOT force a trend conclusion.
- In those conflict/uncertain cases, explicitly output "趋势不确定" or "数据缺失/未披露".
- If a source only provides a raw number in an incompatible unit / scope / definition, do not repeat that raw number in the report. Summarize it qualitatively as "单位不一致、不可直接比较" and treat the metric as unavailable for direct comparison.
- If "Scope Consistency Notes" says a metric is not comparable or requires normalization, add a nearby comparability note and avoid direct winner/loser claims.
- Do not introduce years beyond the requested year range.
- If data is missing, explicitly write "数据缺失/未披露".
- Layer-2, Layer-3, and Layer-4 should stay primarily qualitative. Avoid introducing new numeric claims there unless the exact number already appears in Layer-1 or in the approved numeric evidence.
- If a number cannot be directly grounded, delete that number and rewrite the sentence qualitatively instead of guessing a replacement.
- Keep the report concise and evidence-grounded.
- Keep total output within about 900 Chinese characters.
"""

_USER_PROMPT_TEMPLATE = """\
## Query
{query}

## Entities
companies={companies}
years={years}
intent={intent}
compare_dimension={compare_dimension}

## Materiality Context
{materiality_context}

## Evidence Context
{synthesis_context}

## Writing Checklist (required)
1. Read the "Approved Numeric Facts" section first, and prefer those exact numbers.
2. Layer-1 may contain concise numeric facts; Layer-2/3/4 should stay mostly qualitative.
3. If a number or direction is not directly provable from evidence, omit the number and write "趋势不确定" or "数据缺失/未披露".
4. Do not repair a missing number by replacing it with another inferred number.
5. If a source number is only mentioned to explain "单位不一致 / 不可比", do not repeat that raw number in the report body.

## Output Format (required)
Write a markdown report with exactly these headings:
### Layer-1: Data
### Layer-2: Drivers
### Layer-3: Benchmark
### Layer-4: Risk & Compliance
Each layer should contain at most 3 bullet points.
Do not include long paragraphs.

Then append one JSON code block:
```json
{{
  "key_findings": ["finding1", "finding2", "finding3"],
  "chart_spec": null
}}
```
"""

_PATCH_SYSTEM_PROMPT = """\
You are an ESG report patch editor.
Your job is to patch ONLY specified layers in a 4-layer report.

Hard constraints:
- Keep all numbers evidence-grounded; do not invent.
- Remove unsupported numbers instead of replacing them with newly inferred numbers.
- If a flagged number only appears in conflicting / incompatible-unit evidence, remove the raw number entirely and replace it with a qualitative note about non-comparability.
- Do not add years outside requested range.
- If data is missing, write "数据缺失/未披露".
- If direction cannot be reliably derived or sources conflict, write "趋势不确定" or "数据缺失/未披露".
- Keep Layer-2/Layer-3 primarily qualitative unless an exact number is directly grounded.
- Return JSON only.
"""


def _normalize_layer_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    s_lower = s.lower().replace("_", "").replace(" ", "")
    mapping = {
        "layer1": "Layer-1",
        "layer2": "Layer-2",
        "layer3": "Layer-3",
        "layer4": "Layer-4",
    }
    if s_lower in mapping:
        return mapping[s_lower]
    m = re.search(r"layer[-\s]?([1-4])", s_lower)
    if m:
        return f"Layer-{m.group(1)}"
    return s


def _extract_layer_blocks(report_text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    if not report_text:
        return blocks
    pattern = re.compile(
        r"(?ms)^###\s+(Layer-[1-4]):[^\n]*\n.*?(?=^###\s+Layer-[1-4]:|\Z)"
    )
    for m in pattern.finditer(report_text):
        layer = _normalize_layer_name(m.group(1))
        if layer:
            blocks[layer] = m.group(0).strip()
    return blocks


def _resolve_patch_layers(eval_o_errors: list[dict]) -> list[str]:
    if not eval_o_errors:
        return list(_LAYER_ORDER)

    mapping: dict[str, list[str]] = {
        "NUMBER_HALLUCINATION": ["Layer-1", "Layer-2", "Layer-3"],
        "DIRECTION_CONTRADICTION": ["Layer-2", "Layer-3"],
        "ENTITY_HALLUCINATION": list(_LAYER_ORDER),
        "MISSING_SCOPE_ANNOTATION": ["Layer-1", "Layer-4"],
    }

    layers: list[str] = []
    for err in eval_o_errors:
        err_type = str(err.get("type", "")).strip()
        if err_type == "MISSING_LAYER":
            raw_layer = str(err.get("layer", "")).strip()
            layer = _normalize_layer_name(raw_layer)
            if layer in _LAYER_ORDER:
                layers.append(layer)
                continue
            detail = str(err.get("detail", ""))
            m = re.search(r"Layer[-\s]?([1-4])", detail, flags=re.IGNORECASE)
            if m:
                layers.append(f"Layer-{m.group(1)}")
            continue

        layers.extend(mapping.get(err_type, list(_LAYER_ORDER)))

    deduped: list[str] = []
    for layer in layers:
        if layer in _LAYER_ORDER and layer not in deduped:
            deduped.append(layer)
    return deduped or list(_LAYER_ORDER)


def _build_patch_prompt(
    *,
    query: str,
    target_layers: list[str],
    base_report: str,
    synthesis_context: str,
    eval_o_errors: list[dict],
) -> str:
    def _extract_flagged_numbers(errors: list[dict]) -> list[str]:
        numbers: list[str] = []
        seen: set[str] = set()
        for err in errors:
            detail = str(err.get("detail", "") or "")
            match = re.search(r"Number '([^']+)'", detail)
            if not match:
                continue
            token = match.group(1).strip()
            if token and token not in seen:
                seen.add(token)
                numbers.append(token)
        return numbers

    layer_blocks = _extract_layer_blocks(base_report)
    target_preview_parts: list[str] = []
    for layer in target_layers:
        block = layer_blocks.get(layer, f"### {layer}: (missing)\n- No existing content.")
        target_preview_parts.append(f"[{layer}]\n{block}")

    error_lines = "\n".join(
        f"- {e.get('type')}: {(e.get('detail', '') or '')[:220]}"
        for e in eval_o_errors[:_SYNTH_FIX_MAX_ERRORS]
    )
    error_types = {str(e.get("type", "")).strip() for e in eval_o_errors}
    flagged_numbers = _extract_flagged_numbers(eval_o_errors)
    repair_rules: list[str] = []
    if "NUMBER_HALLUCINATION" in error_types:
        repair_rules.append(
            "- For NUMBER_HALLUCINATION: delete unsupported numbers; do not swap in another guessed value."
        )
        repair_rules.append(
            "- If the unsupported number comes from incompatible-unit / incomparable evidence, remove the raw numeric token entirely and keep only a qualitative non-comparability note."
        )
    if flagged_numbers:
        repair_rules.append(
            "- Forbidden numeric tokens: "
            + ", ".join(json.dumps(token, ensure_ascii=False) for token in flagged_numbers)
            + ". These tokens must not appear verbatim in the patched layers unless they appear exactly in Approved Numeric Facts."
        )
    if "DIRECTION_CONTRADICTION" in error_types:
        repair_rules.append(
            "- For DIRECTION_CONTRADICTION: only keep a trend statement when the same metric and time window are explicit in evidence; otherwise write '趋势不确定'."
        )
    if "MISSING_SCOPE_ANNOTATION" in error_types:
        repair_rules.append(
            "- For MISSING_SCOPE_ANNOTATION: add a brief scope/comparability note near the affected metric."
        )

    target_preview = "\n\n".join(target_preview_parts)
    repair_rules_text = os.linesep.join(repair_rules) if repair_rules else "- No extra repair rule."

    return f"""\
## Query
{query}

## Evaluator-O Errors To Fix
{error_lines or "- (none)"}

## Target Layers To Patch
{json.dumps(target_layers, ensure_ascii=False)}

## Current Report (full)
{base_report}

## Current Content Of Target Layers
{target_preview}

## Evidence Context
{synthesis_context}

## Output
Return JSON only:
{{
  "patches": [
    {{
      "layer": "Layer-2",
      "content": "### Layer-2: Drivers\\n- ...\\n- ..."
    }}
  ]
}}

Rules:
- Output patches for target layers only.
- Do not modify non-target layers.
- Each patch content must start with exact heading: "### Layer-X:"
- Keep each layer <= 3 bullet points.
- If evidence conflicts or direction is not provable, use "趋势不确定" or "数据缺失/未披露".
- If a target layer should stay unchanged, still return that layer with corrected/confirmed content.
{repair_rules_text}
"""


def _parse_patch_response(raw_text: str, allowed_layers: list[str]) -> dict[str, str]:
    txt = (raw_text or "").strip()
    txt = re.sub(r"^```json\s*", "", txt)
    txt = re.sub(r"^```\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)
    parsed = json.loads(txt)

    if isinstance(parsed, dict):
        patch_items = parsed.get("patches", [])
    elif isinstance(parsed, list):
        patch_items = parsed
    else:
        raise ValueError("patch response must be dict/list")

    if not isinstance(patch_items, list):
        raise ValueError("patches must be a list")

    allowed = set(allowed_layers)
    patches: dict[str, str] = {}
    for item in patch_items:
        if not isinstance(item, dict):
            continue
        layer = _normalize_layer_name(str(item.get("layer", "")).strip())
        content = str(item.get("content", "")).strip()
        if not layer or layer not in allowed or not content:
            continue
        if not content.startswith(f"### {layer}:"):
            content = f"### {layer}: Updated\n{content}"
        patches[layer] = content

    return patches


def _apply_layer_patches(base_report: str, patches: dict[str, str]) -> str:
    if not patches:
        return base_report

    text = (base_report or "").strip()
    if not text:
        return "\n\n".join(patches[layer] for layer in _LAYER_ORDER if layer in patches).strip()

    for layer in _LAYER_ORDER:
        if layer not in patches:
            continue
        pattern = re.compile(
            rf"(?ms)^###\s+{re.escape(layer)}:[^\n]*\n.*?(?=^###\s+Layer-[1-4]:|\Z)"
        )
        new_block = patches[layer].strip()
        if pattern.search(text):
            text = pattern.sub(new_block + "\n", text, count=1)
        else:
            text = text.rstrip() + "\n\n" + new_block + "\n"

    return text.strip()


# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# 涓昏妭鐐瑰嚱鏁?
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

def _build_timeout_fallback_report(
    query: str,
    sql_result,
    rag_chunks: list[dict],
    timeout_reason: str,
) -> str:
    """
    Deterministic fallback report used when synthesizer LLM repeatedly times out.
    Keeps a 4-layer structure to avoid downstream crashes.
    """
    lines: list[str] = []
    lines.append("### Layer-1: Data")
    lines.append("- Timeout fallback mode was used; full narrative synthesis was not completed.")
    if sql_result is not None and len(sql_result) > 0:
        preview = sql_result.head(8)
        lines.append("- Structured data preview (top 8 rows):")
        lines.append("```text")
        lines.append(preview.to_string(index=False))
        lines.append("```")
    else:
        lines.append("- Structured data is not available.")

    lines.append("")
    lines.append("### Layer-2: Drivers")
    lines.append("- Causal attribution was skipped due to synthesis timeout.")
    lines.append("- Please cross-check SQL output and retrieved evidence.")

    lines.append("")
    lines.append("### Layer-3: Benchmark")
    lines.append("- High-confidence horizontal ranking is omitted in fallback mode.")
    if rag_chunks:
        lines.append("- Evidence excerpts (top 2):")
        for idx, chunk in enumerate(rag_chunks[:2], start=1):
            excerpt = (chunk.get("text", "") or "")[:220].replace("\n", " ")
            lines.append(f"  - Evidence {idx}: {excerpt}")

    lines.append("")
    lines.append("### Layer-4: Risk & Compliance")
    lines.append("- Risk: synthesis timeout may reduce report completeness.")
    lines.append("- Recommendation: reduce output length or switch to fallback model and retry.")
    lines.append(f"- Timeout detail: {timeout_reason}")

    lines.append("")
    lines.append("### Note")
    lines.append(f"- Query: {query}")
    lines.append("- This is a reliability fallback and should not replace the full version.")
    return "\n".join(lines)


@trace_node("synthesizer", tags=["synthesis"])
def synthesizer_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("synthesizer", trace_id)

    entities  = state.get("entities", {})
    companies = entities.get("companies", [])
    years     = entities.get("years", [])
    metrics   = entities.get("metrics", [])
    intent    = entities.get("intent", "qa")
    compare_dimension = entities.get("compare_dimension", "none")
    industry  = entities.get("industry", "")
    query     = state.get("resolved_query", state.get("user_query", ""))

    # 鈹€鈹€ 瑙勬ā褰掍竴鍖?鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    norm_records = _try_normalize(
        get_sql_result_dataframe(state),
        metrics,
        compare_dimension,
    )
    state["normalization_applied"] = norm_records

    # 鈹€鈹€ 缁勮涓婁笅鏂?鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    synthesis_context   = _build_synthesis_context(state)
    if _SYNTH_CONTEXT_MAX_CHARS > 0 and len(synthesis_context) > _SYNTH_CONTEXT_MAX_CHARS:
        log.warning(
            f"鍚堟垚涓婁笅鏂囪繃闀匡紝瑁佸壀 {len(synthesis_context)} -> {_SYNTH_CONTEXT_MAX_CHARS} 瀛楃"
        )
        synthesis_context = synthesis_context[:_SYNTH_CONTEXT_MAX_CHARS]
    state["synthesis_context"] = synthesis_context
    materiality_context = get_materiality_context_for_synthesizer(industry, metrics)

    # 鈹€鈹€ 澶勭悊 Evaluator-O 鎵撳洖鐨勫眬閮ㄤ慨姝?鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    eval_o_errors = state.get("eval_o_errors", [])
    retry_count   = state.get("eval_o_retry_count", 0)
    fix_instruction = ""
    if eval_o_errors and retry_count > 0:
        compact_errors = eval_o_errors[:max(1, _SYNTH_FIX_MAX_ERRORS)]
        fix_instruction = (
            "\n\n## Fix Instruction From Evaluator-O\n"
            + "\n".join(
                f"- {e.get('type')}: {(e.get('detail', '') or '')[:_SYNTH_FIX_DETAIL_CHARS]}"
                for e in compact_errors
            )
            + "\n- Only fix the listed issues. Keep all other grounded numbers unchanged."
        )
        log.info(f"fix mode: retry={retry_count}, errors={[e['type'] for e in compact_errors]}")

    partial_missing_instruction = ""
    sql_df_for_prompt = get_sql_result_dataframe(state)
    requested_metric_columns = [m for m in metrics if sql_df_for_prompt is not None and m in sql_df_for_prompt.columns]
    no_separate_structured_values = (
        len(metrics) >= 2
        and (
            sql_df_for_prompt is None
            or getattr(sql_df_for_prompt, "empty", True)
            or not requested_metric_columns
            or all(sql_df_for_prompt[m].isna().all() for m in requested_metric_columns)
        )
    )
    if no_separate_structured_values:
        partial_missing_instruction = (
            "\n\n## Partial-Missing Safety Requirement\n"
            "- If the evidence provides only a combined total for multiple requested metrics, explicitly state that separate values are unavailable and cannot be split.\n"
            "- Do not substitute the combined total for any individual metric and do not infer a split."
        )

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        query=query,
        companies=companies,
        years=years,
        intent=intent,
        compare_dimension=compare_dimension,
        materiality_context=materiality_context,
        synthesis_context=synthesis_context,
    ) + partial_missing_instruction + fix_instruction

    log.info(f"寮€濮嬬敓鎴愬洓灞傚垎鏋愭姤鍛婏紝context={len(synthesis_context)}瀛楃")

    # 鈹€鈹€ LLM 鐢熸垚 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    rag_result = state.get("rag_result") or {}

    if os.getenv("OFFLINE_DETERMINISTIC_MODE", "false").strip().lower() in {"1", "true", "yes", "y"}:
        state["analysis"] = _build_timeout_fallback_report(
            query=query,
            sql_result=get_sql_result_dataframe(state),
            rag_chunks=rag_result.get("chunks", []),
            timeout_reason="offline deterministic mode: external LLM disabled",
        )
        state["synth_fallback_used"] = True
        state["key_findings"] = [
            "当前输出来自本地检索证据的确定性降级报告。",
            "未调用外部 LLM，不应将该结果解释为完整语义分析质量。",
        ]
        state["chart_spec"] = None
        sources = list(state.get("sources", []))
        sources.extend(state.get("sql_provenance_sources", []) or [])
        for chunk in rag_result.get("chunks", [])[:5]:
            sources.append({
                "type": "rag",
                "company": chunk.get("company_name", ""),
                "year": chunk.get("year", ""),
                "page": chunk.get("page_num", ""),
                "file": chunk.get("source_file", ""),
                "score": chunk.get("rerank_score", 0),
                "excerpt": chunk.get("text", "")[:80],
            })
        state["sources"] = sources
        return state

    def _run_synth(
        prompt_text: str,
        model_name: str,
        max_tokens: int,
        timeout_seconds: float,
        max_retries: int,
        caller_name: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        def _call() -> str:
            provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
            sys_prompt = system_prompt or _SYSTEM_PROMPT
            if provider in {"qwen", "openai", "openai_compatible"}:
                cfg: object = {
                    "system_instruction": sys_prompt,
                    "temperature": _SYNTH_TEMPERATURE,
                    "max_output_tokens": max_tokens,
                    "timeout": timeout_seconds,
                }
            else:
                cfg = types.GenerateContentConfig(
                    system_instruction=sys_prompt,
                    temperature=_SYNTH_TEMPERATURE,
                    max_output_tokens=max_tokens,
                )
            resp = llm_generate_content(
                model=model_name,
                contents=prompt_text,
                config=cfg,
            )
            return resp.text

        return llm_call_with_retry(
            _call,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds,
            caller_name=caller_name,
            trace_id=trace_id,
        )

    fallback_used = False
    patch_mode_used = False

    if eval_o_errors and retry_count > 0 and (state.get("analysis") or "").strip():
        compact_errors = eval_o_errors[:max(1, _SYNTH_FIX_MAX_ERRORS)]
        target_layers = _resolve_patch_layers(compact_errors)
        patch_prompt = _build_patch_prompt(
            query=query,
            target_layers=target_layers,
            base_report=state.get("analysis", ""),
            synthesis_context=synthesis_context,
            eval_o_errors=compact_errors,
        )
        try:
            raw_patch = _run_synth(
                prompt_text=patch_prompt,
                model_name=_MODEL,
                max_tokens=_SYNTH_PATCH_MAX_OUTPUT_TOKENS,
                timeout_seconds=_SYNTH_PATCH_TIMEOUT_SEC,
                max_retries=_SYNTH_PATCH_MAX_RETRIES,
                caller_name="synthesizer_patch_llm",
                system_prompt=_PATCH_SYSTEM_PROMPT,
            )
            patches = _parse_patch_response(raw_patch, target_layers)
            if not patches:
                raise ValueError("patch response contains no valid layer patches")
            raw_output = _apply_layer_patches(state.get("analysis", ""), patches)
            patch_mode_used = True
            log.info(
                "patch mode success: "
                f"retry={retry_count}, target_layers={target_layers}, patched={list(patches.keys())}"
            )
        except Exception as patch_exc:
            log.warning(
                "patch mode failed; fallback to full rewrite. "
                f"retry={retry_count}, err={str(patch_exc)[:200]}"
            )

    if not patch_mode_used:
        try:
            raw_output = _run_synth(
                prompt_text=user_prompt,
                model_name=_MODEL,
                max_tokens=_SYNTH_MAX_OUTPUT_TOKENS,
                timeout_seconds=_SYNTH_TIMEOUT_SEC,
                max_retries=_SYNTH_MAX_RETRIES,
                caller_name="synthesizer_llm",
            )
        except Exception as primary_exc:
            fallback_used = True
            fallback_model = _SYNTH_FALLBACK_MODEL or _MODEL
            log.warning(
                "Primary synthesis failed; switching to fast fallback."
                f" primary_model={_MODEL}, fallback_model={fallback_model}, err={str(primary_exc)[:180]}"
            )
            fast_prompt = (
                user_prompt
                + "\n\n## Fast Mode Constraints\n"
                + "- Keep factual consistency with provided evidence.\n"
                + "- Keep total response concise (about 1200 Chinese characters).\n"
                + "- Keep each layer within at most 3 bullet points."
            )
            try:
                raw_output = _run_synth(
                    prompt_text=fast_prompt,
                    model_name=fallback_model,
                    max_tokens=_SYNTH_FAST_MAX_OUTPUT_TOKENS,
                    timeout_seconds=_SYNTH_FAST_TIMEOUT_SEC,
                    max_retries=_SYNTH_FAST_MAX_RETRIES,
                    caller_name="synthesizer_llm_fast",
                )
            except Exception as fast_exc:
                log.error(
                    "Fast fallback failed; returning deterministic 4-layer report. "
                    f"err={str(fast_exc)[:240]}"
                )
                state["analysis"] = _build_timeout_fallback_report(
                    query=query,
                    sql_result=get_sql_result_dataframe(state),
                    rag_chunks=rag_result.get("chunks", []),
                    timeout_reason=str(fast_exc)[:240],
                )
                state["synth_fallback_used"] = True
                state["key_findings"] = []
                state["chart_spec"] = None
                sources = list(state.get("sources", []))
                sources.extend(state.get("sql_provenance_sources", []) or [])
                for chunk in rag_result.get("chunks", [])[:5]:
                    sources.append({
                        "type": "rag", "company": chunk.get("company_name", ""),
                        "year": chunk.get("year", ""), "page": chunk.get("page_num", ""),
                        "file": chunk.get("source_file", ""), "score": chunk.get("rerank_score", 0),
                        "excerpt": chunk.get("text", "")[:80],
                    })
                state["sources"] = sources
                return state

    # 鈹€鈹€ 瑙ｆ瀽 JSON 灏鹃儴锛坘ey_findings + chart_spec锛?鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    analysis_text = raw_output
    key_findings  = []
    chart_spec    = None

    json_match = re.search(r"```json\s*([\s\S]+?)\s*```", raw_output)
    if json_match:
        try:
            parsed      = json.loads(json_match.group(1))
            key_findings = parsed.get("key_findings", [])
            chart_spec   = parsed.get("chart_spec")
            # 浠庢鏂囦腑绉婚櫎 JSON 鍧?
            analysis_text = raw_output[:json_match.start()].strip()
        except json.JSONDecodeError as e:
            log.warning(f"JSON parse failed: {e}")

    # 鈹€鈹€ 鏋勫缓鏉ユ簮婧簮鍒楄〃 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    sources: list[dict] = list(state.get("sources", []))
    sources.extend(state.get("sql_provenance_sources", []) or [])
    rag_result = state.get("rag_result") or {}
    for chunk in rag_result.get("chunks", [])[:5]:
        sources.append({
            "type":        "rag",
            "company":     chunk.get("company_name", ""),
            "year":        chunk.get("year", ""),
            "page":        chunk.get("page_num", ""),
            "file":        chunk.get("source_file", ""),
            "score":       chunk.get("rerank_score", 0),
            "excerpt":     chunk.get("text", "")[:80],
        })
    if state.get("sql_query_executed"):
        sources.append({
            "type":  "sql",
            "query": state["sql_query_executed"][:200],
        })

    log.info(
        f"鎶ュ憡鐢熸垚瀹屾垚",
        {
            "analysis_chars": len(analysis_text),
            "findings":       len(key_findings),
            "has_chart":      chart_spec is not None,
        },
    )

    state["analysis"]      = analysis_text
    state["key_findings"]  = key_findings
    state["chart_spec"]    = chart_spec
    state["sources"]       = sources
    state["synth_fallback_used"] = fallback_used

    return state






