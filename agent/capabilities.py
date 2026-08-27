"""Project capability introspection for demo/readiness endpoints.

This module is intentionally deterministic: it scans local data/docs and the optional
SQLite database, so recruiters can see the project has explicit coverage boundaries
instead of pretending to answer everything.
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from agent.data_dictionary import TABLE_DICT

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "esg_data.db"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

_INDUSTRY_DIR_LABELS = {
    "car": "new_energy",
    "electric": "power",
    "finance": "bank",
}


def _scan_pdf_coverage(data_dir: Path) -> dict[str, Any]:
    reports_by_industry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    companies: set[str] = set()
    years: set[int] = set()
    pattern = re.compile(r"(.+?)(20\d{2})年ESG报告\.pdf$", re.IGNORECASE)

    for subdir, industry in _INDUSTRY_DIR_LABELS.items():
        root = data_dir / subdir
        if not root.exists():
            continue
        for pdf in sorted(root.glob("*.pdf")):
            match = pattern.search(pdf.name)
            company = match.group(1) if match else pdf.stem
            year = int(match.group(2)) if match else None
            companies.add(company)
            if year:
                years.add(year)
            reports_by_industry[industry].append({
                "company": company,
                "year": year,
                "file": str(pdf.relative_to(PROJECT_ROOT)),
            })

    return {
        "report_count": sum(len(v) for v in reports_by_industry.values()),
        "company_count": len(companies),
        "years": sorted(years),
        "industries": sorted(reports_by_industry.keys()),
        "reports_by_industry": dict(reports_by_industry),
    }


def _scan_db_coverage(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"db_exists": False, "tables": {}, "companies": [], "years": []}

    tables: dict[str, Any] = {}
    companies: set[str] = set()
    years: set[int] = set()
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.row_factory = sqlite3.Row
        table_names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in sorted(table_names):
            row_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            table_info = {"row_count": int(row_count)}
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            table_info["columns"] = cols
            if "company_name" in cols:
                vals = [str(r[0]) for r in conn.execute(f"SELECT DISTINCT company_name FROM {table} WHERE company_name IS NOT NULL")]
                table_info["company_count"] = len(vals)
                companies.update(vals)
            if "year" in cols:
                yvals = [int(r[0]) for r in conn.execute(f"SELECT DISTINCT year FROM {table} WHERE year IS NOT NULL")]
                table_info["years"] = sorted(yvals)
                years.update(yvals)
            tables[table] = table_info
        try:
            verified_values = 0
            quality_rows = conn.execute("SELECT data_quality FROM esg_universal_metrics").fetchall()
            for quality_row in quality_rows:
                import json
                quality = json.loads(quality_row[0] or "{}")
                verified_values += sum(1 for value in quality.values() if value not in {"missing", "parse_failed"})
            for table in ("esg_banking_metrics", "esg_auto_metrics", "esg_power_metrics"):
                for quality_row in conn.execute(f"SELECT data_quality FROM {table}").fetchall():
                    quality = json.loads(quality_row[0] or "{}")
                    verified_values += sum(1 for value in quality.values() if value not in {"missing", "parse_failed"})
            tables["_quality_summary"] = {"verified_metric_values": verified_values}
        except Exception:
            pass
        conn.close()
    except Exception as exc:
        return {"db_exists": True, "error": str(exc)[:200], "tables": tables}

    return {
        "db_exists": True,
        "tables": tables,
        "companies": sorted(companies),
        "years": sorted(years),
    }


def get_capabilities(
    data_dir: str | os.PathLike[str] | None = None,
    db_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    data_root = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
    db = Path(db_path) if db_path else Path(os.getenv("DB_PATH", str(DEFAULT_DB_PATH)))
    pdf = _scan_pdf_coverage(data_root)
    db_cov = _scan_db_coverage(db)

    metric_tables = {k: v for k, v in TABLE_DICT.items() if not k.startswith("missing")}
    metric_count = sum(
        1 for table in metric_tables.values() for key in table.keys() if key != "_meta"
    )

    return {
        "project": "ESG-Insight Agent",
        "positioning": "Evaluable LangGraph ESG analysis agent with SQL + RAG + dual evaluators.",
        "coverage": {
            "reports": pdf,
            "database": db_cov,
            "metric_dictionary": {
                "table_count": len(metric_tables),
                "field_count": metric_count,
                "tables": sorted(metric_tables.keys()),
            },
        },
        "agent_capabilities": [
            "context understanding and entity normalization",
            "planner-supervisor with re-plan loop",
            "parallel SQL and RAG workers",
            "data-quality evaluator before generation",
            "output-faithfulness evaluator after generation",
            "deterministic disclosure-quality scoring",
            "rule-based claim-evidence mismatch risk radar",
            "SSE node-progress streaming and trace dashboard",
        ],
        "known_boundaries": [
            "current evidence corpus is limited to bundled 2022-2024 ESG reports",
            "financial/investment recommendations are out of scope",
            "missing values mean not disclosed or not verified in the structured seed, never zero",
            "structured coverage rows and verified metric values are reported separately",
        ],
    }
