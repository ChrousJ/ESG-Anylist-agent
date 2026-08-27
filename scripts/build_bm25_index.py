#!/usr/bin/env python3
"""Build the local PDF chunk metadata and BM25 index without embedding models.

This is the deterministic/offline fallback for environments that cannot reach
Hugging Face. It reuses the project's page extraction, chunking and tokenization
logic, but deliberately skips ChromaDB embeddings.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from data.build_vectorstore import (  # noqa: E402
    INDUSTRY_FOLDERS,
    build_bm25_index,
    chunk_pages,
    extract_pages,
    parse_filename,
)


def build_bm25_only(base_dir: Path, output_dir: Path, max_pdfs: int = 0) -> dict:
    all_chunks: list[dict] = []
    pdf_count = 0
    failures: list[str] = []

    for folder, industry in INDUSTRY_FOLDERS.items():
        for pdf_path in sorted((base_dir / folder).glob("*.pdf")):
            if max_pdfs and pdf_count >= max_pdfs:
                break
            company, year = parse_filename(pdf_path.name)
            if not company or not year:
                failures.append(f"filename:{pdf_path.name}")
                continue
            pages = extract_pages(str(pdf_path))
            if not pages:
                failures.append(f"empty:{pdf_path.name}")
                continue
            chunks = chunk_pages(pages, company, year, industry, pdf_path.name)
            all_chunks.extend(chunks)
            pdf_count += 1
            print(f"[{pdf_count:02d}] {company} {year}: pages={len(pages)}, chunks={len(chunks)}", flush=True)
        if max_pdfs and pdf_count >= max_pdfs:
            break

    if not all_chunks:
        raise RuntimeError("No chunks were produced from the PDF corpus")

    output_dir.mkdir(parents=True, exist_ok=True)
    bm25 = build_bm25_index(all_chunks)
    with (output_dir / "bm25_index.pkl").open("wb") as f:
        pickle.dump(bm25, f)
    (output_dir / "chunk_metadata.json").write_text(
        json.dumps(all_chunks, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = {
        "mode": "bm25_only",
        "pdf_count": pdf_count,
        "chunk_count": len(all_chunks),
        "company_count": len({c["company_name"] for c in all_chunks}),
        "industry_counts": dict(Counter(c["industry"] for c in all_chunks)),
        "failures": failures,
        "bm25_index": str(output_dir / "bm25_index.pkl"),
        "chunk_metadata": str(output_dir / "chunk_metadata.json"),
    }
    (output_dir / "bm25_build_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build BM25-only ESG PDF index")
    parser.add_argument("--base-dir", default="data")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--max-pdfs", type=int, default=0)
    args = parser.parse_args()
    summary = build_bm25_only(
        Path(args.base_dir).resolve(), Path(args.output_dir).resolve(), args.max_pdfs
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
