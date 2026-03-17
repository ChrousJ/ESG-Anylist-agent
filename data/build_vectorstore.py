"""
build_vectorstore.py  —  ESG 报告向量库 & BM25 索引构建脚本
=============================================================
一次性离线运行，把三个行业文件夹里的 PDF 全部切块、嵌入、存入 ChromaDB，
同时序列化 BM25 索引供混合检索使用。

依赖安装：
  pip install chromadb pdfplumber FlagEmbedding rank-bm25 jieba pickle5

目录结构（运行后）：
  ./vector_store/          ← ChromaDB 持久化目录（自动创建）
  ./bm25_index.pkl         ← BM25 序列化索引
  ./chunk_metadata.json    ← 所有 chunk 的元数据（供 BM25 结果溯源）
"""

# ── 标准库 ──────────────────────────────────────────────────────────────────
import os
import re
import json
import pickle
import logging
from pathlib import Path
from typing import Optional

# ── 第三方库 ─────────────────────────────────────────────────────────────────
import pdfplumber
import jieba
from FlagEmbedding import FlagModel
from rank_bm25 import BM25Okapi
import chromadb
from chromadb.config import Settings

# ══════════════════════════════════════════════════════════════════════════════
# 0.  全局配置
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("build_vectorstore.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── 路径 ─────────────────────────────────────────────────────────────────────
VECTOR_STORE_DIR  = "./vector_store"
BM25_INDEX_PATH   = "./bm25_index.pkl"
CHUNK_META_PATH   = "./chunk_metadata.json"
COLLECTION_NAME   = "esg_reports"

# ── Chunking 参数 ─────────────────────────────────────────────────────────────
CHUNK_SIZE        = 400    # 每块目标字符数
CHUNK_OVERLAP     = 80     # 相邻块重叠字符数
MIN_CHUNK_SIZE    = 50     # 低于此长度的块直接丢弃（噪声）
MAX_PDF_PAGES     = 300    # 单个 PDF 最多处理页数

# ── BGE-M3 模型（首次运行自动下载，约 2.2GB） ─────────────────────────────────
BGE_MODEL_NAME    = "BAAI/bge-small-zh-v1.5"

# ── 行业文件夹映射 ─────────────────────────────────────────────────────────────
INDUSTRY_FOLDERS  = {
    "finance":  "bank",
    "car":      "new_energy",
    "electric": "power",
}

# ══════════════════════════════════════════════════════════════════════════════
# 1.  文件名解析（复用 createDB.py 的同款逻辑）
# ══════════════════════════════════════════════════════════════════════════════

def parse_filename(filename: str) -> tuple[Optional[str], Optional[int]]:
    stem = Path(filename).stem
    year_match = re.search(r"(20\d{2})", stem)
    if not year_match:
        return None, None
    year = int(year_match.group(1))
    company_part = stem[: year_match.start()]
    company_name = re.sub(r"[_\-\s]+$", "", company_part).strip()
    return (company_name, year) if company_name else (None, None)


# ══════════════════════════════════════════════════════════════════════════════
# 2.  PDF 文本提取（带页码标注）
# ══════════════════════════════════════════════════════════════════════════════

def extract_pages(pdf_path: str) -> list[dict]:
    """
    返回每页信息：{page_num, text}
    同时把页内表格内容也拼入文本，确保表格数字对 RAG 可见。
    """
    pages = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages[: MAX_PDF_PAGES]):
                text = page.extract_text() or ""

                # 把表格内容也追加进来（补充向量库对数字的覆盖）
                for tbl in page.extract_tables() or []:
                    rows = []
                    for row in tbl:
                        cleaned = [str(c).strip() for c in row if c]
                        if cleaned:
                            rows.append(" | ".join(cleaned))
                    if rows:
                        text += "\n" + "\n".join(rows)

                text = text.strip()
                if len(text) >= MIN_CHUNK_SIZE:
                    pages.append({"page_num": i + 1, "text": text})
    except Exception as e:
        log.error(f"PDF 提取失败 [{pdf_path}]: {e}")
    return pages


# ══════════════════════════════════════════════════════════════════════════════
# 3.  文本切块
# ══════════════════════════════════════════════════════════════════════════════

def _split_by_paragraph(text: str) -> list[str]:
    """按自然段落（连续换行）切分，单段过长则按句号二次切分。"""
    paras = re.split(r"\n{2,}", text)
    result = []
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) <= CHUNK_SIZE:
            result.append(para)
        else:
            # 按句号/问号/感叹号切句子，再合并到目标大小
            sentences = re.split(r"(?<=[。！？\.\!\?])", para)
            buf = ""
            for sent in sentences:
                if len(buf) + len(sent) <= CHUNK_SIZE:
                    buf += sent
                else:
                    if buf:
                        result.append(buf.strip())
                    buf = sent
            if buf.strip():
                result.append(buf.strip())
    return result


def chunk_pages(
    pages: list[dict],
    company_name: str,
    year: int,
    industry: str,
    source_file: str,
) -> list[dict]:
    """
    把页列表切成 chunk 列表。
    每个 chunk：{text, chunk_id, company_name, year, industry, page_num,
                 source_file, chunk_index}
    采用滑动窗口保证跨页语义连续：把相邻两页文本拼在一起再切块，
    然后用 overlap 拼接相邻块的尾部。
    """
    chunks = []
    chunk_index = 0

    # 把所有页按顺序处理，每页独立切块（保留页码精确性）
    for page in pages:
        page_num = page["page_num"]
        paragraphs = _split_by_paragraph(page["text"])

        # 合并段落到目标 chunk 大小（贪心合并）
        buffer = ""
        buffer_paras = []

        for para in paragraphs:
            if len(buffer) + len(para) + 1 <= CHUNK_SIZE:
                buffer = (buffer + "\n" + para).strip()
                buffer_paras.append(para)
            else:
                # 输出当前 buffer
                if len(buffer) >= MIN_CHUNK_SIZE:
                    chunk_id = f"{company_name}_{year}_p{page_num}_c{chunk_index}"
                    chunks.append({
                        "text":         buffer,
                        "chunk_id":     chunk_id,
                        "company_name": company_name,
                        "year":         str(year),        # ChromaDB metadata 要求 str
                        "industry":     industry,
                        "page_num":     str(page_num),
                        "source_file":  source_file,
                        "chunk_index":  chunk_index,
                    })
                    chunk_index += 1

                    # Overlap：把最后 CHUNK_OVERLAP 个字符带入下一块
                    overlap_text = buffer[-CHUNK_OVERLAP:] if len(buffer) > CHUNK_OVERLAP else buffer
                    buffer = (overlap_text + "\n" + para).strip()
                else:
                    buffer = para

        # 页尾剩余
        if len(buffer) >= MIN_CHUNK_SIZE:
            chunk_id = f"{company_name}_{year}_p{page_num}_c{chunk_index}"
            chunks.append({
                "text":         buffer,
                "chunk_id":     chunk_id,
                "company_name": company_name,
                "year":         str(year),
                "industry":     industry,
                "page_num":     str(page_num),
                "source_file":  source_file,
                "chunk_index":  chunk_index,
            })
            chunk_index += 1

    return chunks


# ══════════════════════════════════════════════════════════════════════════════
# 4.  BGE 嵌入（批量，带进度）
# ══════════════════════════════════════════════════════════════════════════════

def load_bge_model() -> FlagModel:
    log.info(f"加载 BGE 模型：{BGE_MODEL_NAME}")
    model = FlagModel(
        BGE_MODEL_NAME,
        use_fp16=False,
        device="cpu",
    )
    log.info("BGE 模型加载完成")
    return model


def embed_chunks(
    model: FlagModel,
    texts: list[str],
    batch_size: int = 4,
) -> list[list[float]]:
    all_embeddings = []
    total = len(texts)

    for i in range(0, total, batch_size):
        batch = texts[i: i + batch_size]

        embeddings = model.encode(batch)
        all_embeddings.extend(embeddings.tolist())

        if (i // batch_size) % 5 == 0:
            log.info(f"  嵌入进度：{min(i + batch_size, total)}/{total}")

    return all_embeddings


# ══════════════════════════════════════════════════════════════════════════════
# 5.  ChromaDB 写入
# ══════════════════════════════════════════════════════════════════════════════

def get_or_create_collection(persist_dir: str) -> chromadb.Collection:
    """获取或创建 ChromaDB 集合（余弦相似度空间）。"""
    client = chromadb.PersistentClient(
        path=persist_dir,
        settings=Settings(anonymized_telemetry=False),
    )
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},   # 余弦相似度
    )
    log.info(f"ChromaDB 集合 '{COLLECTION_NAME}'，"
             f"当前已有 {collection.count()} 条记录")
    return collection


def upsert_to_chroma(
    collection: chromadb.Collection,
    chunks: list[dict],
    embeddings: list[list[float]],
    batch_size: int = 100,
):
    """分批写入 ChromaDB，自动跳过已存在的 chunk_id。"""
    total = len(chunks)
    for i in range(0, total, batch_size):
        batch_chunks = chunks[i : i + batch_size]
        batch_embeds = embeddings[i : i + batch_size]

        collection.upsert(
            ids=[c["chunk_id"] for c in batch_chunks],
            documents=[c["text"] for c in batch_chunks],
            embeddings=batch_embeds,
            metadatas=[
                {
                    "company_name": c["company_name"],
                    "year":         c["year"],
                    "industry":     c["industry"],
                    "page_num":     c["page_num"],
                    "source_file":  c["source_file"],
                    "chunk_index":  str(c["chunk_index"]),
                }
                for c in batch_chunks
            ],
        )
    log.info(f"  ChromaDB 写入完成：{total} 条")


# ══════════════════════════════════════════════════════════════════════════════
# 6.  BM25 索引构建
# ══════════════════════════════════════════════════════════════════════════════

def tokenize_zh(text: str) -> list[str]:
    """
    中文分词（jieba）+ 英文保留 + 去停用词。
    BM25 的效果高度依赖分词质量。
    """
    # 简单停用词
    stopwords = {
        "的", "了", "和", "是", "在", "我", "有", "就", "不", "人",
        "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
        "你", "会", "着", "没有", "看", "好", "自己", "这", "那",
        "中", "为", "以", "及", "与", "或", "等", "年", "月", "日",
    }
    tokens = jieba.lcut(text)
    return [t for t in tokens if t.strip() and t not in stopwords and len(t) > 1]


def build_bm25_index(all_chunks: list[dict]) -> BM25Okapi:
    """对全量 chunk 文本建 BM25 索引。"""
    log.info(f"构建 BM25 索引，共 {len(all_chunks)} 个 chunk...")
    tokenized_corpus = [tokenize_zh(c["text"]) for c in all_chunks]
    bm25 = BM25Okapi(tokenized_corpus)
    log.info("BM25 索引构建完成")
    return bm25


def save_bm25(bm25: BM25Okapi, all_chunks: list[dict]):
    """序列化保存 BM25 索引和元数据。"""
    with open(BM25_INDEX_PATH, "wb") as f:
        pickle.dump(bm25, f)
    log.info(f"BM25 索引已保存：{BM25_INDEX_PATH}")

    with open(CHUNK_META_PATH, "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=2)
    log.info(f"Chunk 元数据已保存：{CHUNK_META_PATH}  ({len(all_chunks)} 条)")


# ══════════════════════════════════════════════════════════════════════════════
# 7.  主流程
# ══════════════════════════════════════════════════════════════════════════════

def build_all(base_dir: str = "."):
    base = Path(base_dir)

    # ── 初始化 ───────────────────────────────────────────────────────────────
    bge_model  = load_bge_model()
    collection = get_or_create_collection(VECTOR_STORE_DIR)

    all_chunks: list[dict] = []   # 汇总，最终用于 BM25

    # ── 遍历三个行业文件夹 ────────────────────────────────────────────────────
    for folder, industry in INDUSTRY_FOLDERS.items():
        folder_path = base / folder
        if not folder_path.exists():
            log.warning(f"文件夹不存在，跳过：{folder_path}")
            continue

        pdfs = sorted(folder_path.glob("*.pdf"))
        log.info(f"\n{'='*60}")
        log.info(f"行业：{industry}  |  文件夹：{folder}  |  共 {len(pdfs)} 个 PDF")
        log.info(f"{'='*60}")

        for pdf_path in pdfs:
            filename = pdf_path.name
            company_name, year = parse_filename(filename)

            if not company_name or not year:
                log.warning(f"文件名解析失败，跳过：{filename}")
                continue

            log.info(f"▶  {company_name} {year}年")

            # Step 1: 提取页面文本
            pages = extract_pages(str(pdf_path))
            if not pages:
                log.error(f"  PDF 文本为空，跳过：{filename}")
                continue
            log.info(f"  提取页面：{len(pages)} 页")

            # Step 2: 切块
            chunks = chunk_pages(pages, company_name, year, industry, filename)
            log.info(f"  切块完成：{len(chunks)} 个 chunk")
            if not chunks:
                continue

            # Step 3: 嵌入
            texts = [c["text"] for c in chunks]
            embeddings = embed_chunks(bge_model, texts, batch_size=16)

            # Step 4: 写入 ChromaDB
            upsert_to_chroma(collection, chunks, embeddings)

            # Step 5: 汇总到全局列表（给 BM25 用）
            all_chunks.extend(chunks)

            log.info(f"  ✅ 完成：{company_name} {year}年  "
                     f"[累计 {len(all_chunks)} chunks]")

    # ── 构建并保存 BM25 索引 ─────────────────────────────────────────────────
    if all_chunks:
        bm25 = build_bm25_index(all_chunks)
        save_bm25(bm25, all_chunks)
    else:
        log.error("没有任何 chunk 被处理，请检查 PDF 文件路径")
        return

    # ── 最终统计 ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  向量库构建完成")
    print("=" * 60)
    print(f"  ChromaDB 总记录数：{collection.count()}")
    print(f"  BM25 索引 chunk 数：{len(all_chunks)}")
    print(f"  向量库目录：{VECTOR_STORE_DIR}")
    print(f"  BM25 索引：{BM25_INDEX_PATH}")
    print(f"  Chunk 元数据：{CHUNK_META_PATH}")

    # 按行业统计
    from collections import Counter
    industry_counts = Counter(c["industry"] for c in all_chunks)
    company_counts  = Counter(c["company_name"] for c in all_chunks)
    print(f"\n  按行业分布：{dict(industry_counts)}")
    print(f"  涉及公司数：{len(company_counts)}")
    print("=" * 60)


# ══════════════════════════════════════════════════════════════════════════════
# 8.  入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ESG 向量库 & BM25 索引构建")
    parser.add_argument(
        "--base-dir", default=".",
        help="包含 finance/car/electric 文件夹的根目录（默认当前目录）",
    )
    args = parser.parse_args()

    build_all(base_dir=args.base_dir)