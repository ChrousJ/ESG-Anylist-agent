#!/usr/bin/env python3
"""Build an auditable local ESG SQLite seed database without external LLM calls.

The seed has two layers:
1. coverage rows for every bundled company/report year, with unknown metrics as NULL;
2. manually verified core metrics used by smoke/demo cases, each carrying page/excerpt provenance.

This is intentionally conservative. A missing value is never converted to zero.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "esg_data.db"

INDUSTRY_DIRS = {"finance": "bank", "car": "new_energy", "electric": "power"}

UNIVERSAL_METRICS = [
    "scope_1_emissions", "scope_2_emissions", "total_energy_consumption",
    "energy_intensity", "employee_training_hours", "safety_accidents_count",
    "customer_complaint_res", "charitable_donations", "independent_director_ratio",
    "female_director_ratio", "anti_corruption_coverage", "regulatory_penalties",
    "esg_committee_setup", "external_esg_rating",
]
INDUSTRY_METRICS = {
    "bank": ["green_finance_balance", "inclusive_finance_balance"],
    "new_energy": ["scope_3_emissions", "rd_investment_total", "supplier_esg_audit_ratio"],
    "power": ["scope_3_emissions", "clean_energy_ratio", "rd_investment_total"],
}

DDL = """
CREATE TABLE IF NOT EXISTS esg_universal_metrics (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL, year INTEGER NOT NULL,
 industry TEXT NOT NULL, scope_1_emissions REAL, scope_2_emissions REAL,
 total_energy_consumption REAL, energy_intensity REAL, employee_training_hours REAL,
 safety_accidents_count REAL, customer_complaint_res REAL, charitable_donations REAL,
 independent_director_ratio REAL, female_director_ratio REAL, anti_corruption_coverage REAL,
 regulatory_penalties REAL, esg_committee_setup REAL, external_esg_rating TEXT,
 raw_scope_1 TEXT, raw_scope_2 TEXT, raw_energy_total TEXT, raw_energy_intensity TEXT,
 raw_training_hours TEXT, raw_safety_accidents TEXT, raw_complaint_res TEXT,
 raw_donations TEXT, raw_ind_dir_ratio TEXT, raw_female_dir_ratio TEXT,
 raw_anti_corruption TEXT, raw_penalties TEXT, raw_esg_committee TEXT, raw_esg_rating TEXT,
 data_quality TEXT DEFAULT '{}', confidence_scores TEXT DEFAULT '{}',
 validation_warnings TEXT DEFAULT '{}', source_file TEXT,
 extraction_method TEXT DEFAULT 'verified_seed', created_at TEXT DEFAULT (datetime('now')),
 updated_at TEXT DEFAULT (datetime('now')), UNIQUE(company_name, year)
);
CREATE TABLE IF NOT EXISTS esg_banking_metrics (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL, year INTEGER NOT NULL,
 green_finance_balance REAL, inclusive_finance_balance REAL,
 raw_green_finance TEXT, raw_inclusive_finance TEXT, data_quality TEXT DEFAULT '{}',
 confidence_scores TEXT DEFAULT '{}', validation_warnings TEXT DEFAULT '{}', source_file TEXT,
 created_at TEXT DEFAULT (datetime('now')), UNIQUE(company_name, year)
);
CREATE TABLE IF NOT EXISTS esg_auto_metrics (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL, year INTEGER NOT NULL,
 scope_3_emissions REAL, rd_investment_total REAL, supplier_esg_audit_ratio REAL,
 raw_scope_3 TEXT, raw_rd_investment TEXT, raw_supplier_audit TEXT,
 data_quality TEXT DEFAULT '{}', confidence_scores TEXT DEFAULT '{}',
 validation_warnings TEXT DEFAULT '{}', source_file TEXT,
 created_at TEXT DEFAULT (datetime('now')), UNIQUE(company_name, year)
);
CREATE TABLE IF NOT EXISTS esg_power_metrics (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL, year INTEGER NOT NULL,
 scope_3_emissions REAL, clean_energy_ratio REAL, rd_investment_total REAL,
 raw_scope_3 TEXT, raw_clean_energy TEXT, raw_rd_investment TEXT,
 data_quality TEXT DEFAULT '{}', confidence_scores TEXT DEFAULT '{}',
 validation_warnings TEXT DEFAULT '{}', source_file TEXT,
 created_at TEXT DEFAULT (datetime('now')), UNIQUE(company_name, year)
);
CREATE TABLE IF NOT EXISTS missing_data_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, company_name TEXT NOT NULL, year INTEGER NOT NULL,
 metric_key TEXT NOT NULL, industry TEXT, missing_reason TEXT, source_file TEXT,
 logged_at TEXT DEFAULT (datetime('now')), UNIQUE(company_name, year, metric_key)
);
"""


def metric(value: float, raw_value: str, unit: str, page: int, excerpt: str,
           *, quality: str = "verified", confidence: float = 0.99,
           warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "value": value,
        "raw": {"raw_value": raw_value, "raw_unit": unit, "page": str(page), "excerpt": excerpt},
        "quality": quality,
        "confidence": confidence,
        "warnings": warnings or [],
    }


# The primary-source annotation file is the authoritative structured seed.
DEFAULT_ANNOTATION_PATH = DEFAULT_DATA_DIR / "annotations/verified_metrics_v1.jsonl"


def load_verified_annotations(path: Path = DEFAULT_ANNOTATION_PATH) -> dict[tuple[str, int], dict[str, dict[str, Any]]]:
    """Load one-record-per-metric annotations into the database seed mapping."""
    verified: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    if not path.exists():
        raise FileNotFoundError(f"Verified annotation file not found: {path}")
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        required = {
            "company_name", "year", "metric_key", "normalized_value",
            "raw_value", "raw_unit", "pdf_page", "excerpt", "source_file",
            "quality", "confidence",
        }
        missing = sorted(required - record.keys())
        if missing:
            raise ValueError(f"{path}:{lineno}: missing fields {missing}")
        key = (str(record["company_name"]), int(record["year"]))
        metric_key = str(record["metric_key"])
        metrics = verified.setdefault(key, {})
        if metric_key in metrics:
            raise ValueError(f"{path}:{lineno}: duplicate annotation {key}/{metric_key}")
        metrics[metric_key] = {
            "value": float(record["normalized_value"]),
            "raw": {
                "raw_value": str(record["raw_value"]),
                "raw_unit": str(record["raw_unit"]),
                "normalized_unit": str(record.get("normalized_unit", "")),
                "page": str(record["pdf_page"]),
                "excerpt": str(record["excerpt"]),
                "source_file": str(record["source_file"]),
                "organizational_boundary": str(record.get("organizational_boundary", "")),
                "reporting_basis": str(record.get("reporting_basis", "")),
                "review_status": str(record.get("review_status", "")),
                "needs_second_reviewer": bool(record.get("needs_second_reviewer", True)),
            },
            "quality": str(record["quality"]),
            "confidence": float(record["confidence"]),
            "warnings": list(record.get("warnings", [])),
        }
    return verified


VERIFIED = load_verified_annotations()

RAW_COLUMNS = {
    "scope_1_emissions": "raw_scope_1", "scope_2_emissions": "raw_scope_2",
    "total_energy_consumption": "raw_energy_total", "energy_intensity": "raw_energy_intensity",
    "employee_training_hours": "raw_training_hours", "safety_accidents_count": "raw_safety_accidents",
    "customer_complaint_res": "raw_complaint_res", "charitable_donations": "raw_donations",
    "independent_director_ratio": "raw_ind_dir_ratio", "female_director_ratio": "raw_female_dir_ratio",
    "anti_corruption_coverage": "raw_anti_corruption", "regulatory_penalties": "raw_penalties",
    "esg_committee_setup": "raw_esg_committee", "external_esg_rating": "raw_esg_rating",
}
INDUSTRY_TABLES = {
    "bank": ("esg_banking_metrics", {"green_finance_balance": "raw_green_finance", "inclusive_finance_balance": "raw_inclusive_finance"}),
    "new_energy": ("esg_auto_metrics", {"scope_3_emissions": "raw_scope_3", "rd_investment_total": "raw_rd_investment", "supplier_esg_audit_ratio": "raw_supplier_audit"}),
    "power": ("esg_power_metrics", {"scope_3_emissions": "raw_scope_3", "clean_energy_ratio": "raw_clean_energy", "rd_investment_total": "raw_rd_investment"}),
}


def scan_reports(data_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for folder, industry in INDUSTRY_DIRS.items():
        for pdf in sorted((data_dir / folder).glob("*.pdf")):
            match = re.match(r"(.+?)(20\d{2})年ESG报告\.pdf$", pdf.name)
            if match:
                rows.append({"company": match.group(1), "year": int(match.group(2)), "industry": industry, "source_file": pdf.name})
    return rows


def _payload(metrics: dict[str, dict[str, Any]], keys: list[str]) -> tuple[dict, dict, dict]:
    quality, confidence, warnings = {}, {}, {}
    for key in keys:
        if key in metrics:
            quality[key] = metrics[key]["quality"]
            confidence[key] = metrics[key]["confidence"]
            if metrics[key]["warnings"]:
                warnings[key] = metrics[key]["warnings"]
        else:
            quality[key] = "missing"
            confidence[key] = 0.0
    return quality, confidence, warnings


def build_database(db_path: Path, data_dir: Path, reset: bool = False) -> dict[str, Any]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    reports = scan_reports(data_dir)

    for row in reports:
        key = (row["company"], row["year"])
        metrics = VERIFIED.get(key, {})
        uq, uc, uw = _payload(metrics, UNIVERSAL_METRICS)
        values = {m: metrics[m]["value"] if m in metrics else None for m in UNIVERSAL_METRICS}
        raw = {RAW_COLUMNS[m]: json.dumps(metrics[m]["raw"], ensure_ascii=False) if m in metrics else None for m in UNIVERSAL_METRICS}
        columns = ["company_name", "year", "industry"] + UNIVERSAL_METRICS + list(RAW_COLUMNS.values()) + ["data_quality", "confidence_scores", "validation_warnings", "source_file", "extraction_method"]
        params = [row["company"], row["year"], row["industry"]] + [values[m] for m in UNIVERSAL_METRICS] + [raw[c] for c in RAW_COLUMNS.values()] + [json.dumps(uq, ensure_ascii=False), json.dumps(uc, ensure_ascii=False), json.dumps(uw, ensure_ascii=False), row["source_file"], "verified_seed"]
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(f"{c}=excluded.{c}" for c in columns[2:])
        conn.execute(f"INSERT INTO esg_universal_metrics ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT(company_name,year) DO UPDATE SET {updates}", params)

        table, raw_map = INDUSTRY_TABLES[row["industry"]]
        metric_keys = INDUSTRY_METRICS[row["industry"]]
        iq, ic, iw = _payload(metrics, metric_keys)
        cols = ["company_name", "year"] + metric_keys + list(raw_map.values()) + ["data_quality", "confidence_scores", "validation_warnings", "source_file"]
        vals = [row["company"], row["year"]] + [metrics[m]["value"] if m in metrics else None for m in metric_keys] + [json.dumps(metrics[m]["raw"], ensure_ascii=False) if m in metrics else None for m in metric_keys] + [json.dumps(iq, ensure_ascii=False), json.dumps(ic, ensure_ascii=False), json.dumps(iw, ensure_ascii=False), row["source_file"]]
        updates = ",".join(f"{c}=excluded.{c}" for c in cols[2:])
        conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)}) ON CONFLICT(company_name,year) DO UPDATE SET {updates}", vals)

        all_metrics = UNIVERSAL_METRICS + metric_keys
        for metric_key in all_metrics:
            if metric_key not in metrics:
                conn.execute("INSERT OR REPLACE INTO missing_data_log (company_name,year,metric_key,industry,missing_reason,source_file) VALUES (?,?,?,?,?,?)", (row["company"], row["year"], metric_key, row["industry"], "not_verified_in_structured_seed", row["source_file"]))
            else:
                conn.execute("DELETE FROM missing_data_log WHERE company_name=? AND year=? AND metric_key=?", (row["company"], row["year"], metric_key))

    conn.commit()
    summary = {
        "database": str(db_path), "report_rows": len(reports),
        "companies": conn.execute("SELECT COUNT(DISTINCT company_name) FROM esg_universal_metrics").fetchone()[0],
        "verified_metric_values": sum(len(v) for v in VERIFIED.values()),
        "universal_rows": conn.execute("SELECT COUNT(*) FROM esg_universal_metrics").fetchone()[0],
        "bank_rows": conn.execute("SELECT COUNT(*) FROM esg_banking_metrics").fetchone()[0],
        "auto_rows": conn.execute("SELECT COUNT(*) FROM esg_auto_metrics").fetchone()[0],
        "power_rows": conn.execute("SELECT COUNT(*) FROM esg_power_metrics").fetchone()[0],
        "missing_log_rows": conn.execute("SELECT COUNT(*) FROM missing_data_log").fetchone()[0],
    }
    conn.close()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--summary", type=Path, default=DEFAULT_DATA_DIR / "structured_seed_summary.json")
    args = parser.parse_args()
    summary = build_database(args.db, args.data_dir, reset=args.reset)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
