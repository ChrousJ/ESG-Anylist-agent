"""Deterministic ESG disclosure-quality scoring.

The score is not a replacement for a full ESG rating. It evaluates whether the
agent has enough disclosed, comparable, and verifiable evidence to support an
answer. This is a strong interview-facing feature because it turns a vague RAG
answer into a measurable quality artifact.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from agent.state import get_sql_result_dataframe

WEIGHTS = {
    "completeness": 30,
    "continuity": 20,
    "comparability": 20,
    "verifiability": 20,
    "specificity": 10,
}

_ID_COLUMNS = {"company_name", "year", "industry", "source_file", "data_quality", "confidence_scores"}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        if isinstance(value, float) and math.isnan(value):
            return True
    except Exception:
        pass
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _records_from_sql(sql_result: Any) -> list[dict[str, Any]]:
    if sql_result is None:
        return []
    if hasattr(sql_result, "to_dict"):
        try:
            return list(sql_result.to_dict(orient="records"))
        except Exception:
            return []
    if isinstance(sql_result, dict) and sql_result.get("_type") == "dataframe":
        return list(sql_result.get("data", []))
    if isinstance(sql_result, list):
        return [r for r in sql_result if isinstance(r, dict)]
    return []


def _requested_metrics(state: dict, records: list[dict[str, Any]]) -> list[str]:
    entities = state.get("entities", {}) or {}
    metrics = [m for m in entities.get("metrics", []) if isinstance(m, str)]
    if metrics:
        return metrics
    candidates: list[str] = []
    for row in records:
        for key, value in row.items():
            if key not in _ID_COLUMNS and not _is_missing(value):
                candidates.append(key)
    return sorted(set(candidates))


def _parse_jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _score_completeness(records: list[dict[str, Any]], metrics: list[str]) -> tuple[float, list[str]]:
    if not records or not metrics:
        return 0.0, ["结构化结果为空或未识别目标指标"]
    expected = len(records) * len(metrics)
    disclosed = 0
    missing_examples: list[str] = []
    for row in records:
        company = row.get("company_name", "")
        year = row.get("year", "")
        for metric in metrics:
            if not _is_missing(row.get(metric)):
                disclosed += 1
            elif len(missing_examples) < 3:
                missing_examples.append(f"{company}{year}-{metric} 未披露")
    score = disclosed / expected if expected else 0.0
    return score, missing_examples


def _score_continuity(records: list[dict[str, Any]], metrics: list[str], years: list[int]) -> tuple[float, list[str]]:
    if not records or not metrics:
        return 0.0, []
    requested_years = sorted(set(years or [int(r["year"]) for r in records if not _is_missing(r.get("year"))]))
    if len(requested_years) <= 1:
        return 1.0, ["单年份问题默认连续性满分"]
    by_company: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        by_company.setdefault(str(row.get("company_name", "")), []).append(row)
    ratios: list[float] = []
    notes: list[str] = []
    for company, rows in by_company.items():
        row_by_year = {int(r["year"]): r for r in rows if not _is_missing(r.get("year"))}
        for metric in metrics:
            hit = sum(1 for y in requested_years if y in row_by_year and not _is_missing(row_by_year[y].get(metric)))
            ratios.append(hit / len(requested_years))
            if hit < len(requested_years) and len(notes) < 3:
                notes.append(f"{company}-{metric} 覆盖 {hit}/{len(requested_years)} 年")
    return (sum(ratios) / len(ratios) if ratios else 0.0), notes


def _score_comparability(state: dict, records: list[dict[str, Any]], metrics: list[str]) -> tuple[float, list[str]]:
    if not records or not metrics:
        return 0.0, []
    penalties = 0
    checks = 0
    notes: list[str] = []
    scope = state.get("scope_consistency", {}) or {}
    if scope.get("checked") and not scope.get("consistent", True):
        penalties += 1
        notes.append("Evaluator-D 标记存在口径不一致")
    for row in records:
        quality = _parse_jsonish(row.get("data_quality"))
        conf = _parse_jsonish(row.get("confidence_scores"))
        for metric in metrics:
            if _is_missing(row.get(metric)):
                continue
            checks += 1
            q = str(quality.get(metric, "normal"))
            c = conf.get(metric)
            if q in {"estimated", "parse_failed"}:
                penalties += 0.5
                if len(notes) < 3:
                    notes.append(f"{row.get('company_name','')}{row.get('year','')}-{metric} 数据质量={q}")
            if isinstance(c, (int, float)) and c < 0.7:
                penalties += 0.5
                if len(notes) < 3:
                    notes.append(f"{row.get('company_name','')}{row.get('year','')}-{metric} 置信度较低({c:.2f})")
    if checks == 0:
        return 0.3 if state.get("rag_result") else 0.0, notes
    return max(0.0, 1.0 - penalties / max(checks, 1)), notes


def _score_verifiability(state: dict) -> tuple[float, list[str]]:
    sources = state.get("sources", []) or []
    rag = state.get("rag_result", {}) or {}
    chunks = rag.get("chunks", []) if isinstance(rag, dict) else []
    has_sql = bool(state.get("sql_query_executed"))
    cited = [s for s in sources if s.get("type") == "rag" and (s.get("page") or s.get("excerpt"))]
    evidence_count = len(cited) + min(len(chunks), 5) + (1 if has_sql else 0)
    score = min(1.0, evidence_count / 4)
    notes = []
    if not has_sql:
        notes.append("未保留 SQL 结构化溯源")
    if not cited and not chunks:
        notes.append("缺少 PDF 页码/原文片段证据")
    return score, notes


def _score_specificity(state: dict, records: list[dict[str, Any]]) -> tuple[float, list[str]]:
    numeric_cells = 0
    for row in records:
        for key, value in row.items():
            if key not in _ID_COLUMNS and isinstance(value, (int, float)) and not _is_missing(value):
                numeric_cells += 1
    chunks = (state.get("rag_result", {}) or {}).get("chunks", []) if isinstance(state.get("rag_result"), dict) else []
    quantified_chunks = sum(1 for c in chunks[:8] if re.search(r"\d+(?:\.\d+)?\s*(?:%|万吨|吨|亿元|万元|GJ|千瓦时|人|次)", str(c.get("text", ""))))
    score = min(1.0, (numeric_cells + quantified_chunks) / 6)
    notes = [] if score >= 0.5 else ["可量化事实较少，回答更偏定性"]
    return score, notes


def score_disclosure_quality(state: dict) -> dict[str, Any]:
    sql_df = get_sql_result_dataframe(state) if isinstance(state, dict) else None
    records = _records_from_sql(sql_df)
    entities = state.get("entities", {}) or {}
    metrics = _requested_metrics(state, records)
    years = [int(y) for y in entities.get("years", []) if str(y).isdigit()]

    components: dict[str, dict[str, Any]] = {}
    calculators = {
        "completeness": lambda: _score_completeness(records, metrics),
        "continuity": lambda: _score_continuity(records, metrics, years),
        "comparability": lambda: _score_comparability(state, records, metrics),
        "verifiability": lambda: _score_verifiability(state),
        "specificity": lambda: _score_specificity(state, records),
    }
    total = 0.0
    all_notes: list[str] = []
    for name, calc in calculators.items():
        ratio, notes = calc()
        points = round(ratio * WEIGHTS[name], 1)
        components[name] = {"weight": WEIGHTS[name], "score": points, "ratio": round(ratio, 3), "notes": notes}
        total += points
        all_notes.extend(notes)

    risk_flags: list[dict[str, str]] = []
    if components["completeness"]["ratio"] < 0.6:
        risk_flags.append({"level": "high", "type": "incomplete_disclosure", "detail": "目标指标披露覆盖不足"})
    if components["verifiability"]["ratio"] < 0.5:
        risk_flags.append({"level": "medium", "type": "weak_evidence", "detail": "页码/原文/SQL 溯源不足"})
    if components["specificity"]["ratio"] < 0.4:
        risk_flags.append({"level": "medium", "type": "claim_evidence_mismatch", "detail": "定性表述多于量化证据，存在潜在绿漂核查点"})
    if components["comparability"]["ratio"] < 0.7:
        risk_flags.append({"level": "medium", "type": "low_comparability", "detail": "数据质量、置信度或口径一致性不足"})

    band = "A" if total >= 85 else "B" if total >= 70 else "C" if total >= 55 else "D"
    return {
        "score": round(total, 1),
        "band": band,
        "components": components,
        "risk_flags": risk_flags,
        "notes": all_notes[:6],
        "method": "weighted deterministic rubric: completeness 30, continuity 20, comparability 20, verifiability 20, specificity 10",
    }


def render_disclosure_quality_markdown(result: dict[str, Any]) -> str:
    rows = []
    labels = {
        "completeness": "完整性",
        "continuity": "连续性",
        "comparability": "可比性",
        "verifiability": "可验证性",
        "specificity": "具体性",
    }
    for key, item in result.get("components", {}).items():
        notes = "；".join(item.get("notes", [])[:2]) or "-"
        rows.append(f"| {labels.get(key, key)} | {item.get('score')}/{item.get('weight')} | {notes} |")
    risk_lines = [f"- **{r.get('level')} / {r.get('type')}**：{r.get('detail')}" for r in result.get("risk_flags", [])]
    if not risk_lines:
        risk_lines = ["- 未触发明显披露质量风险；仍建议结合原始报告人工复核。"]
    return "\n".join([
        "",
        "---",
        "### Disclosure Quality Score（披露质量评分）",
        f"**总分：{result.get('score')}/100，等级：{result.get('band')}**",
        "",
        "| 维度 | 得分 | 说明 |",
        "|---|---:|---|",
        *rows,
        "",
        "**潜在绿漂 / 披露风险信号**",
        *risk_lines,
        "",
        f"> 评分方法：{result.get('method')}。该分数评价“披露证据质量”，不构成投资建议或完整 ESG 评级。",
    ])
