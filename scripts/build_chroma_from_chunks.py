#!/usr/bin/env python3
"""Build/resume ChromaDB embeddings from existing chunk_metadata.json.

Unlike data/build_vectorstore.py, this script does not re-parse PDFs or rebuild
BM25. It is resumable and uses a local embedding model path from the environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import chromadb
from chromadb.config import Settings
from dotenv import load_dotenv
from FlagEmbedding import FlagModel

load_dotenv(".env")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build ChromaDB from existing ESG chunks")
    parser.add_argument("--chunks", default=os.getenv("CHUNK_META_PATH", "data/chunk_metadata.json"))
    parser.add_argument("--persist-dir", default=os.getenv("VECTOR_STORE_DIR", "data/vector_store"))
    parser.add_argument("--model", default=os.getenv("BGE_EMBED_MODEL", "BAAI/bge-small-zh-v1.5"))
    parser.add_argument("--encode-batch", type=int, default=64)
    parser.add_argument("--upsert-batch", type=int, default=256)
    parser.add_argument("--max-chunks", type=int, default=0)
    args = parser.parse_args()

    chunks = json.loads(Path(args.chunks).read_text(encoding="utf-8"))
    if args.max_chunks:
        chunks = chunks[: args.max_chunks]

    model = FlagModel(args.model, use_fp16=False, device="cpu")
    client = chromadb.PersistentClient(
        path=args.persist_dir,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_or_create_collection(
        name="esg_reports",
        metadata={"hnsw:space": "cosine"},
    )

    existing: set[str] = set()
    if collection.count():
        # IDs are lightweight; reading them makes the build safely resumable.
        page_size = 5000
        for offset in range(0, collection.count(), page_size):
            existing.update(collection.get(limit=page_size, offset=offset, include=[])["ids"])

    pending = [chunk for chunk in chunks if chunk["chunk_id"] not in existing]
    print(
        f"model={args.model} total={len(chunks)} existing={len(existing)} pending={len(pending)}",
        flush=True,
    )

    completed = 0
    for start in range(0, len(pending), args.upsert_batch):
        batch = pending[start : start + args.upsert_batch]
        texts = [item["text"] for item in batch]
        embeddings = model.encode(
            texts,
            batch_size=args.encode_batch,
            max_length=512,
        ).tolist()
        collection.upsert(
            ids=[item["chunk_id"] for item in batch],
            documents=texts,
            embeddings=embeddings,
            metadatas=[{
                "chunk_id": item["chunk_id"],
                "company_name": item["company_name"],
                "year": str(item["year"]),
                "industry": item["industry"],
                "page_num": str(item["page_num"]),
                "source_file": item["source_file"],
                "chunk_index": str(item["chunk_index"]),
            } for item in batch],
        )
        completed += len(batch)
        print(f"embedded={completed}/{len(pending)} collection={collection.count()}", flush=True)

    summary = {
        "model": args.model,
        "chunk_count": len(chunks),
        "collection_count": collection.count(),
        "persist_dir": args.persist_dir,
    }
    output = Path(args.persist_dir) / "build_summary.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
