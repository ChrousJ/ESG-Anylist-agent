#!/usr/bin/env python3
"""Validate primary-source metric annotations against bundled PDF pages.

The validator is intentionally conservative. It checks schema-like invariants,
duplicate keys, source existence, page range, metric terminology, and whether the
raw numeric token can be found in text extracted from the cited PDF page.
It produces a machine-readable review queue rather than silently accepting weak
records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "data/annotations/verified_metrics_v1.jsonl"
METRIC_TERMS = {
    "scope_1_emissions": ("范围1", "范围一", "范畴1", "范畴一", "範圍1", "範疇一", "直接温室", "直接溫室", "类别一"),
    "scope_2_emissions": ("范围2", "范围二", "范畴2", "范畴二", "範圍2", "範疇二", "间接温室", "間接溫室", "类别二"),
    "scope_3_emissions": ("范围3", "范围三", "范畴3", "范畴三", "範圍3", "範疇三", "类别三"),
    "green_finance_balance": ("绿色贷款", "绿色信贷", "绿色金融"),
}


def _compact(text: str) -> str:
    return re.sub(r"[\s,，]", "", text or "").lower()


def _numeric_candidates(raw: str, normalized: float) -> list[str]:
    values = {_compact(raw), _compact(f"{normalized:g}"), _compact(f"{normalized:,.2f}")}
    # PDF table extraction may split decimals or preserve a converted ten-thousand-unit value only.
    return sorted(v for v in values if v)


def _load(path: Path) -> list[dict[str, Any]]:
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        row["_line"] = lineno
        rows.append(row)
    return rows


def validate(path: Path, *, data_dir: Path) -> dict[str, Any]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pdfplumber is required for PDF verification") from exc

    rows = _load(path)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    keys = Counter((r.get("company_name"), r.get("year"), r.get("metric_key")) for r in rows)
    for key, count in keys.items():
        if count > 1:
            errors.append({"type": "duplicate_key", "key": key, "count": count})

    pdf_cache: dict[Path, Any] = {}
    hash_cache: dict[Path, str] = {}
    checked = 0
    try:
        for row in rows:
            rid = row.get("annotation_id", f"line-{row['_line']}")
            required = [
                "company_name", "year", "metric_key", "normalized_value", "normalized_unit",
                "raw_value", "raw_unit", "source_file", "pdf_page", "excerpt",
                "organizational_boundary", "reporting_basis", "quality", "confidence",
                "review_status", "needs_second_reviewer",
            ]
            missing = [k for k in required if row.get(k) in (None, "")]
            if missing:
                errors.append({"annotation_id": rid, "type": "missing_fields", "fields": missing})
                continue
            if row["metric_key"] not in METRIC_TERMS:
                errors.append({"annotation_id": rid, "type": "unsupported_metric", "value": row["metric_key"]})
            expected_unit = "亿元" if row["metric_key"] == "green_finance_balance" else "tCO2e"
            if row["normalized_unit"] != expected_unit:
                errors.append({"annotation_id": rid, "type": "unexpected_unit", "value": row["normalized_unit"], "expected": expected_unit})
            if not (0 <= float(row["confidence"]) <= 1):
                errors.append({"annotation_id": rid, "type": "invalid_confidence"})

            candidates = list(data_dir.glob(f"*/{row['source_file']}"))
            if len(candidates) != 1:
                errors.append({"annotation_id": rid, "type": "source_resolution", "matches": [str(p) for p in candidates]})
                continue
            pdf_path = candidates[0]
            if pdf_path not in pdf_cache:
                pdf_cache[pdf_path] = pdfplumber.open(pdf_path)
                hash_cache[pdf_path] = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            pdf = pdf_cache[pdf_path]
            page_no = int(row["pdf_page"])
            if page_no < 1 or page_no > len(pdf.pages):
                errors.append({"annotation_id": rid, "type": "page_out_of_range", "page": page_no, "pages": len(pdf.pages)})
                continue
            text = pdf.pages[page_no - 1].extract_text() or ""
            compact = _compact(text)
            terms = METRIC_TERMS[row["metric_key"]]
            if not any(_compact(term) in compact for term in terms):
                errors.append({"annotation_id": rid, "type": "metric_term_not_found", "page": page_no})
            numeric_found = any(token in compact for token in _numeric_candidates(str(row["raw_value"]), float(row["normalized_value"])) if token)
            if not numeric_found:
                errors.append({
                    "annotation_id": rid,
                    "type": "raw_value_not_found",
                    "page": page_no,
                    "candidates": _numeric_candidates(str(row["raw_value"]), float(row["normalized_value"])),
                })
            if row.get("needs_second_reviewer"):
                warnings.append({"annotation_id": rid, "type": "second_review_pending"})
            checked += 1
    finally:
        for pdf in pdf_cache.values():
            pdf.close()

    by_company = Counter(r["company_name"] for r in rows)
    by_metric = Counter(r["metric_key"] for r in rows)
    return {
        "status": "pass" if not errors else "fail",
        "input": str(path),
        "record_count": len(rows),
        "checked_against_pdf": checked,
        "unique_company_year_metric": len(keys),
        "companies": dict(sorted(by_company.items())),
        "metrics": dict(sorted(by_metric.items())),
        "source_sha256": {p.name: digest for p, digest in sorted(hash_cache.items(), key=lambda x: x[0].name)},
        "errors": errors,
        "warnings": warnings,
        "second_review_pending_count": sum(1 for r in rows if r.get("needs_second_reviewer")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data/annotations/validation_report.json")
    args = parser.parse_args()
    report = validate(args.input, data_dir=args.data_dir)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
