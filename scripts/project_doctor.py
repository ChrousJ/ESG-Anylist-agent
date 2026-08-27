#!/usr/bin/env python3
"""Deterministic project-readiness checks for local development and interviews.

The doctor intentionally uses only the Python standard library, so it can explain
why the full Agent cannot start even when third-party dependencies are missing.
It never prints secret values; it only reports whether a key is configured.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PYTHON_MIN = (3, 10)
SUPPORTED_PYTHON_MAX_EXCLUSIVE = (3, 13)

REQUIRED_PATHS = (
    "README.md",
    "agent/graph.py",
    "agent/state.py",
    "api/main.py",
    "static/index.html",
    "eval/datasets/esg_eval_smoke.jsonl",
    "docs/interview-guide.md",
)

RUNTIME_MODULES = {
    "fastapi": "fastapi",
    "pydantic": "pydantic",
    "dotenv": "python-dotenv",
    "langgraph": "langgraph",
    "google.genai": "google-genai",
    "openai": "openai",
    "pandas": "pandas",
}


def _check(name: str, status: str, detail: str, *, blocking: bool = False) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "blocking": blocking}


def _load_dotenv_values(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without requiring python-dotenv."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _effective_env(root: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    values = _load_dotenv_values(root / ".env")
    values.update(dict(environ or os.environ))
    return values


def _scan_reports(root: Path) -> tuple[int, int, list[int]]:
    pattern = re.compile(r"(.+?)(20\d{2})年ESG报告\.pdf$", re.IGNORECASE)
    companies: set[str] = set()
    years: set[int] = set()
    count = 0
    for industry_dir in ("car", "electric", "finance"):
        for pdf in (root / "data" / industry_dir).glob("*.pdf"):
            count += 1
            match = pattern.search(pdf.name)
            if match:
                companies.add(match.group(1))
                years.add(int(match.group(2)))
    return count, len(companies), sorted(years)


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def collect_readiness(
    root: Path | str = PROJECT_ROOT,
    *,
    profile: str = "runtime",
    environ: Mapping[str, str] | None = None,
    python_version: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    """Collect source/runtime readiness without mutating the project."""
    root = Path(root).resolve()
    env = _effective_env(root, environ)
    version = python_version or sys.version_info[:3]
    checks: list[dict[str, Any]] = []

    supported = SUPPORTED_PYTHON_MIN <= version[:2] < SUPPORTED_PYTHON_MAX_EXCLUSIVE
    checks.append(_check(
        "python_version",
        "pass" if supported else "fail",
        f"Python {version[0]}.{version[1]}.{version[2]}; supported range is >=3.10,<3.13",
        blocking=not supported,
    ))

    missing_paths = [path for path in REQUIRED_PATHS if not (root / path).exists()]
    checks.append(_check(
        "repository_structure",
        "pass" if not missing_paths else "fail",
        "required project entrypoints are present" if not missing_paths else f"missing: {', '.join(missing_paths)}",
        blocking=bool(missing_paths),
    ))

    report_count, company_count, years = _scan_reports(root)
    checks.append(_check(
        "pdf_corpus",
        "pass" if report_count else "fail",
        f"{report_count} reports, {company_count} companies, years={years or 'none'}",
        blocking=report_count == 0,
    ))

    smoke_path = root / "eval" / "datasets" / "esg_eval_smoke.jsonl"
    smoke_cases = 0
    invalid_lines = 0
    if smoke_path.exists():
        for line in smoke_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                smoke_cases += int(bool(item.get("id") and item.get("query")))
            except Exception:
                invalid_lines += 1
    checks.append(_check(
        "smoke_dataset",
        "pass" if smoke_cases and not invalid_lines else "fail",
        f"{smoke_cases} valid cases, {invalid_lines} invalid lines",
        blocking=not smoke_cases or bool(invalid_lines),
    ))

    if profile == "runtime":
        missing_modules = [package for module, package in RUNTIME_MODULES.items() if not _module_available(module)]
        checks.append(_check(
            "runtime_dependencies",
            "pass" if not missing_modules else "fail",
            "core runtime modules importable" if not missing_modules else f"missing packages: {', '.join(missing_modules)}",
            blocking=bool(missing_modules),
        ))

        db_path = Path(env.get("DB_PATH", "./data/esg_data.db"))
        if not db_path.is_absolute():
            db_path = root / db_path
        db_ready = db_path.is_file()
        db_detail = str(db_path)
        if db_ready:
            try:
                conn = sqlite3.connect(str(db_path), timeout=2)
                rows = conn.execute("SELECT COUNT(*) FROM esg_universal_metrics").fetchone()[0]
                companies = conn.execute("SELECT COUNT(DISTINCT company_name) FROM esg_universal_metrics").fetchone()[0]
                verified = 0
                for table in ("esg_universal_metrics", "esg_banking_metrics", "esg_auto_metrics", "esg_power_metrics"):
                    for quality_row in conn.execute(f"SELECT data_quality FROM {table}"):
                        quality = json.loads(quality_row[0] or "{}")
                        verified += sum(1 for value in quality.values() if value not in {"missing", "parse_failed"})
                conn.close()
                db_ready = rows > 0 and companies > 0 and verified > 0
                db_detail = f"{db_path}; rows={rows}; companies={companies}; verified_metric_values={verified}"
            except Exception as exc:
                db_ready = False
                db_detail = f"{db_path}; inspection failed: {str(exc)[:120]}"
        checks.append(_check(
            "structured_database",
            "pass" if db_ready else "fail",
            db_detail,
            blocking=not db_ready,
        ))

        vector_path = Path(env.get("VECTOR_STORE_DIR", "./data/vector_store"))
        if not vector_path.is_absolute():
            vector_path = root / vector_path
        vector_ready = vector_path.is_dir() and any(vector_path.iterdir())
        bm25_path = root / env.get("BM25_INDEX_PATH", "./data/bm25_index.pkl")
        metadata_path = root / env.get("CHUNK_META_PATH", "./data/chunk_metadata.json")
        bm25_ready = bm25_path.is_file() and metadata_path.is_file()
        retrieval_ready = vector_ready or bm25_ready
        retrieval_mode = "hybrid/vector" if vector_ready else "bm25_only" if bm25_ready else "missing"
        checks.append(_check(
            "retrieval_index",
            "pass" if retrieval_ready else "fail",
            f"mode={retrieval_mode}; vector={vector_path}; bm25={bm25_path}",
            blocking=not retrieval_ready,
        ))

        provider = env.get("LLM_PROVIDER", "gemini").strip().lower()
        key_name = (
            "QWEN_API_KEY" if provider == "qwen"
            else "OPENAI_API_KEY" if provider in {"openai", "openai_compatible"}
            else "GEMINI_API_KEY"
        )
        has_key = bool(env.get(key_name, "").strip())
        offline_mode = env.get("OFFLINE_DETERMINISTIC_MODE", "false").strip().lower() in {"1", "true", "yes", "y"}
        llm_ready = has_key or offline_mode
        checks.append(_check(
            "llm_configuration",
            "pass" if llm_ready else "fail",
            f"provider={provider}; {key_name} configured={has_key}; offline_mode={offline_mode}",
            blocking=not llm_ready,
        ))

    failed = [item for item in checks if item["status"] == "fail"]
    blocking = [item for item in failed if item["blocking"]]
    status = "ready" if not failed else "blocked" if blocking else "degraded"
    return {
        "project": "ESG-Insight Agent",
        "profile": profile,
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "passed": sum(item["status"] == "pass" for item in checks),
            "failed": len(failed),
            "blocking": len(blocking),
        },
        "checks": checks,
        "next_actions": [item["detail"] for item in blocking],
    }


def render_markdown(report: dict[str, Any]) -> str:
    icon = {"pass": "✅", "fail": "❌", "warn": "⚠️"}
    lines = [
        "# ESG-Insight Agent Readiness Report",
        "",
        f"> Profile: `{report['profile']}`  ",
        f"> Status: **{report['status']}**  ",
        f"> Generated: {report['generated_at']}",
        "",
        "| Check | Status | Detail |",
        "|---|:---:|---|",
    ]
    for item in report["checks"]:
        detail = str(item["detail"]).replace("|", "/")
        lines.append(f"| `{item['name']}` | {icon.get(item['status'], item['status'])} | {detail} |")
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- `source` profile verifies repository structure, corpus presence, Python compatibility, and smoke dataset integrity.",
        "- `runtime` profile additionally verifies dependencies, SQLite/vector artifacts, and LLM configuration.",
        "- The report never prints API-key values.",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Check ESG-Insight Agent project readiness")
    parser.add_argument("--profile", choices=("source", "runtime"), default="runtime")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    parser.add_argument("--write-report", default="", help="Optional Markdown output path")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when status is not ready")
    args = parser.parse_args()

    report = collect_readiness(profile=args.profile)
    output = json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_markdown(report)
    print(output)
    if args.write_report:
        path = Path(args.write_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(report), encoding="utf-8")
    if args.strict and report["status"] != "ready":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
