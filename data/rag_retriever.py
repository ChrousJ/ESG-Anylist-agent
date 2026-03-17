"""
data/rag_retriever.py  —  混合检索 + BGE Reranker 封装
======================================================

【这个模块是做什么的？】

rag_retriever 是 RAG Worker 的"后端引擎"——它负责从 ESG 报告的
文本片段库中找到与用户问题最相关的段落。

【检索流程（像搜索引擎一样）】

  用户问题 → Query 改写（生成 3 个变体，覆盖不同表述方式）
           → 双通道检索：
               ① 向量检索（语义相似度，ChromaDB）→ 找到 top-20
               ② BM25 检索（关键词匹配，jieba 分词）→ 找到 top-20
           → RRF 融合（合并两种结果，避免只靠一种方法）
           → BGE Reranker 精排（重新打分，选出真正最相关的 Top-K）

【为什么需要这么复杂？】

  - 向量检索擅长理解语义（"碳排放" ≈ "温室气体排放"）
  - BM25 擅长精确匹配关键词（"Scope 1" 就是 "Scope 1"）
  - 两者互补 → 检索质量更高
  - Reranker 做最终精排 → 过滤掉假阳性

【依赖】
  pip install chromadb FlagEmbedding rank-bm25 jieba google-genai
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
import jieba
import chromadb
from chromadb.config import Settings
from FlagEmbedding import FlagModel
from FlagEmbedding import FlagReranker
from rank_bm25 import BM25Okapi
from google import genai
from dotenv import load_dotenv

load_dotenv()

import transformers
if not hasattr(transformers.PreTrainedTokenizerBase, 'prepare_for_model'):
    def _prepare_for_model(self, ids, pair_ids=None, add_special_tokens=True, padding=False, truncation=False, max_length=None, **kwargs):
        is_roberta = 'roberta' in self.__class__.__name__.lower()
        if truncation == 'only_second' and pair_ids is not None and max_length is not None:
            num_special = 4 if is_roberta else 3
            max_pair_len = max_length - len(ids) - num_special
            if max_pair_len > 0 and len(pair_ids) > max_pair_len:
                pair_ids = pair_ids[:max_pair_len]
        
        if add_special_tokens:
            cls = [self.cls_token_id] if getattr(self, "cls_token_id", None) is not None else []
            sep = [self.sep_token_id] if getattr(self, "sep_token_id", None) is not None else []
            if is_roberta:
                input_ids = cls + ids + sep + sep + (pair_ids if pair_ids else []) + sep
            else:
                input_ids = cls + ids + sep + (pair_ids if pair_ids else []) + sep
        else:
            input_ids = ids + (pair_ids if pair_ids else [])
            
        res = {'input_ids': input_ids, 'attention_mask': [1] * len(input_ids)}
        if hasattr(self, 'model_input_names') and 'token_type_ids' in self.model_input_names:
            if is_roberta:
                res['token_type_ids'] = [0] * len(input_ids)
            else:
                res['token_type_ids'] = [0] * (len(cls) + len(ids) + len(sep)) + ([1] * (len(pair_ids) + len(sep)) if pair_ids else [])
        return res
        
    transformers.PreTrainedTokenizerBase.prepare_for_model = _prepare_for_model
    transformers.PreTrainedTokenizerFast.prepare_for_model = _prepare_for_model
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# 0.  配置常量
# ══════════════════════════════════════════════════════════════════════════════

VECTOR_STORE_DIR   = os.getenv("VECTOR_STORE_DIR", "./data/vector_store")
BM25_INDEX_PATH    = os.getenv("BM25_INDEX_PATH", "./data/bm25_index.pkl")
CHUNK_META_PATH    = os.getenv("CHUNK_META_PATH", "./data/chunk_metadata.json")
COLLECTION_NAME    = "esg_reports"

BGE_EMBED_MODEL    = "BAAI/bge-small-zh-v1.5"
BGE_RERANK_MODEL   = "BAAI/bge-reranker-v2-m3"

GEMINI_MODEL       = "gemini-2.5-flash-preview-05-20"
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")

# Evaluation / stability toggles
DISABLE_RERANK = os.getenv("DISABLE_RERANK", "false").strip().lower() in {"1", "true", "yes", "y"}
# 检索超参
VECTOR_TOP_K       = 20     # 向量检索召回数
BM25_TOP_K         = 20     # BM25 召回数
RERANK_TOP_K       = 5      # Reranker 最终保留数
RELEVANCE_THRESHOLD = 0.3   # Reranker 分数低于此值的 chunk 丢弃
RRF_K              = 60     # RRF 融合系数（标准值）

# ══════════════════════════════════════════════════════════════════════════════
# 1.  模型单例（延迟加载，全局共享，避免重复加载）
# ══════════════════════════════════════════════════════════════════════════════

# ── 延迟加载模型（全局单例） ──────────────────────────────────────────────────
_bge_embed_model:   Optional[FlagModel] = None
_bge_rerank_model:  Optional[FlagReranker]   = None
_bm25_index:        Optional[BM25Okapi]      = None
_chunk_metadata:    Optional[list[dict]]     = None
_chroma_collection: Optional[chromadb.Collection] = None
_gemini_client:     Optional[genai.Client]   = None


def _get_embed_model() -> FlagModel:
    global _bge_embed_model
    if _bge_embed_model is None:
        log.info(f"加载 BGE Embedding 模型 ({BGE_EMBED_MODEL})...")
        _bge_embed_model = FlagModel(
            BGE_EMBED_MODEL,
            use_fp16=False,
            device="cpu",
        )
    return _bge_embed_model


def _get_rerank_model() -> FlagReranker:
    global _bge_rerank_model
    if _bge_rerank_model is None:
        log.info("加载 BGE Reranker 模型...")
        _bge_rerank_model = FlagReranker(
            BGE_RERANK_MODEL,
            use_fp16=True,
            device="cpu",
        )
    return _bge_rerank_model


def _get_bm25() -> tuple[BM25Okapi, list[dict]]:
    global _bm25_index, _chunk_metadata
    if _bm25_index is None:
        if not Path(BM25_INDEX_PATH).exists():
            raise FileNotFoundError(
                f"BM25 索引不存在：{BM25_INDEX_PATH}，请先运行 build_vectorstore.py"
            )
        with open(BM25_INDEX_PATH, "rb") as f:
            _bm25_index = pickle.load(f)
        with open(CHUNK_META_PATH, "r", encoding="utf-8") as f:
            _chunk_metadata = json.load(f)
        log.info(f"BM25 索引加载完成，共 {len(_chunk_metadata)} 条")
    return _bm25_index, _chunk_metadata


def _get_chroma() -> chromadb.Collection:
    global _chroma_collection
    if _chroma_collection is None:
        if not Path(VECTOR_STORE_DIR).exists():
            raise FileNotFoundError(
                f"向量库目录不存在：{VECTOR_STORE_DIR}，请先运行 build_vectorstore.py"
            )
        client = chromadb.PersistentClient(
            path=VECTOR_STORE_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        _chroma_collection = client.get_collection(COLLECTION_NAME)
        log.info(f"ChromaDB 集合加载完成，共 {_chroma_collection.count()} 条")
    return _chroma_collection


def _get_gemini() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def get_bge_embedding(text: str) -> list[float]:
    model = _get_embed_model()
    out = model.encode([text])
    return out[0].tolist()


# ══════════════════════════════════════════════════════════════════════════════
# 2.  Query 改写（生成多变体提升召回率）
# ══════════════════════════════════════════════════════════════════════════════

_QUERY_REWRITE_PROMPT = """你是 ESG 报告检索专家。
将下面这个查询改写为 3 个语义等价但表达不同的检索变体，用于在中文 ESG 年报中检索相关段落。

要求：
1. 保留原始查询的核心意图
2. 变体之间词汇尽量不同（扩大召回覆盖）
3. 可以用更正式/更口语/指标英文缩写等不同风格
4. 每行一个变体，共 3 行，不加编号和解释

原始查询：{query}"""


def rewrite_query(query: str) -> list[str]:
    """
    用 Gemini 生成 3 个查询变体。
    失败时直接返回原始 query（不阻断流程）。
    """
    try:
        client = _get_gemini()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=_QUERY_REWRITE_PROMPT.format(query=query),
        )
        lines = [l.strip() for l in response.text.strip().split("\n") if l.strip()]
        variants = lines[:3]
        # 原始 query 也加进去，共最多 4 个
        all_queries = list(dict.fromkeys([query] + variants))  # 去重保序
        log.debug(f"Query 改写结果：{all_queries}")
        return all_queries
    except Exception as e:
        log.warning(f"Query 改写失败，使用原始 query：{e}")
        return [query]


# ══════════════════════════════════════════════════════════════════════════════
# 3.  分词工具（与 build_vectorstore.py 保持一致）
# ══════════════════════════════════════════════════════════════════════════════

_STOPWORDS = {
    "的", "了", "和", "是", "在", "我", "有", "就", "不", "人",
    "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
    "你", "会", "着", "没有", "看", "好", "自己", "这", "那",
    "中", "为", "以", "及", "与", "或", "等", "年", "月", "日",
}

def tokenize_zh(text: str) -> list[str]:
    tokens = jieba.lcut(text)
    return [t for t in tokens if t.strip() and t not in _STOPWORDS and len(t) > 1]


# ══════════════════════════════════════════════════════════════════════════════
# 4.  向量检索
# ══════════════════════════════════════════════════════════════════════════════

def _build_chroma_filter(
    companies: Optional[list[str]] = None,
    years: Optional[list[int]] = None,
    industries: Optional[list[str]] = None,
) -> Optional[dict]:
    """
    构造 ChromaDB where 过滤条件。
    多个条件用 $and 组合；单个条件直接用。
    """
    conditions = []

    if companies and len(companies) == 1:
        conditions.append({"company_name": {"$eq": companies[0]}})
    elif companies:
        conditions.append({"company_name": {"$in": companies}})

    if years:
        str_years = [str(y) for y in years]
        if len(str_years) == 1:
            conditions.append({"year": {"$eq": str_years[0]}})
        else:
            conditions.append({"year": {"$in": str_years}})

    if industries and len(industries) == 1:
        conditions.append({"industry": {"$eq": industries[0]}})
    elif industries:
        conditions.append({"industry": {"$in": industries}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def vector_search(
    queries: list[str],
    top_k: int = VECTOR_TOP_K,
    companies: Optional[list[str]] = None,
    years: Optional[list[int]] = None,
    industries: Optional[list[str]] = None,
) -> list[dict]:
    """
    对多个查询变体分别做向量检索，合并去重结果。
    返回 list[{text, chunk_id, score, metadata}]
    """
    model      = _get_embed_model()
    collection = _get_chroma()
    where      = _build_chroma_filter(companies, years, industries)

    seen_ids = set()
    results  = []

    for query in queries:
        # 嵌入查询
        output = model.encode(
            [query],
            max_length=512,
        )
        query_embedding = output[0].tolist()

        # ChromaDB 查询
        query_kwargs = dict(
            query_embeddings=[query_embedding],
            n_results=min(top_k, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
        if where:
            query_kwargs["where"] = where

        try:
            res = collection.query(**query_kwargs)
        except Exception as e:
            log.warning(f"向量检索异常（query='{query[:30]}'）: {e}")
            continue

        for doc, meta, dist in zip(
            res["documents"][0],
            res["metadatas"][0],
            res["distances"][0],
        ):
            chunk_id = meta.get("chunk_id") or f"{doc[:20]}_hash"
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)

            # ChromaDB cosine distance → similarity score
            similarity = 1.0 - dist

            results.append({
                "text":         doc,
                "chunk_id":     chunk_id,
                "score":        similarity,
                "source":       "vector",
                "company_name": meta.get("company_name", ""),
                "year":         meta.get("year", ""),
                "industry":     meta.get("industry", ""),
                "page_num":     meta.get("page_num", ""),
                "source_file":  meta.get("source_file", ""),
            })

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 5.  BM25 关键词检索
# ══════════════════════════════════════════════════════════════════════════════

def bm25_search(
    queries: list[str],
    top_k: int = BM25_TOP_K,
    companies: Optional[list[str]] = None,
    years: Optional[list[int]] = None,
    industries: Optional[list[str]] = None,
) -> list[dict]:
    """
    BM25 检索，支持与向量检索相同的元数据过滤。
    返回格式与 vector_search 保持一致。
    """
    bm25, all_chunks = _get_bm25()

    # 元数据过滤：先筛出符合条件的 chunk 索引
    def _match(chunk: dict) -> bool:
        if companies and chunk["company_name"] not in companies:
            return False
        if years and int(chunk["year"]) not in years:
            return False
        if industries and chunk["industry"] not in industries:
            return False
        return True

    if companies or years or industries:
        valid_indices = [i for i, c in enumerate(all_chunks) if _match(c)]
    else:
        valid_indices = list(range(len(all_chunks)))

    if not valid_indices:
        return []

    seen_ids = set()
    results  = []

    for query in queries:
        tokens = tokenize_zh(query)
        if not tokens:
            continue

        # 全量打分，取 valid_indices 子集
        scores = bm25.get_scores(tokens)
        valid_scores = [(i, scores[i]) for i in valid_indices]
        valid_scores.sort(key=lambda x: -x[1])
        top_items = valid_scores[:top_k]

        for idx, score in top_items:
            chunk = all_chunks[idx]
            chunk_id = chunk["chunk_id"]
            if chunk_id in seen_ids:
                continue
            seen_ids.add(chunk_id)

            results.append({
                "text":         chunk["text"],
                "chunk_id":     chunk_id,
                "score":        float(score),
                "source":       "bm25",
                "company_name": chunk["company_name"],
                "year":         chunk["year"],
                "industry":     chunk["industry"],
                "page_num":     chunk["page_num"],
                "source_file":  chunk["source_file"],
            })

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 6.  RRF 融合（Reciprocal Rank Fusion）
# ══════════════════════════════════════════════════════════════════════════════

def reciprocal_rank_fusion(
    vector_results: list[dict],
    bm25_results: list[dict],
    k: int = RRF_K,
) -> list[dict]:
    """
    RRF 公式：score(d) = Σ 1 / (k + rank(d))
    把向量检索和 BM25 检索结果融合，返回按 RRF 分数降序排列的去重列表。
    """
    rrf_scores: dict[str, float] = {}
    chunk_map:  dict[str, dict]  = {}

    # 向量检索排名打分
    for rank, item in enumerate(vector_results, start=1):
        cid = item["chunk_id"]
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (k + rank)
        chunk_map[cid]  = item

    # BM25 排名打分
    for rank, item in enumerate(bm25_results, start=1):
        cid = item["chunk_id"]
        rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (k + rank)
        if cid not in chunk_map:
            chunk_map[cid] = item

    # 排序并返回
    sorted_ids = sorted(rrf_scores, key=lambda x: -rrf_scores[x])
    fused = []
    for cid in sorted_ids:
        item = dict(chunk_map[cid])
        item["rrf_score"] = round(rrf_scores[cid], 6)
        item["source"]    = "hybrid"
        fused.append(item)

    return fused


# ══════════════════════════════════════════════════════════════════════════════
# 7.  BGE Reranker 重排序
# ══════════════════════════════════════════════════════════════════════════════

def rerank(
    query: str,
    candidates: list[dict],
    top_k: int = RERANK_TOP_K,
    threshold: float = RELEVANCE_THRESHOLD,
) -> list[dict]:
    """
    用 BGE Reranker 对候选 chunk 精排，保留 top_k 且分数 ≥ threshold 的结果。
    每个结果追加 rerank_score 字段。
    """
    if not candidates:
        return []

    if DISABLE_RERANK:
        # Fast path: keep fused order, attach a lightweight score
        results = []
        for idx, item in enumerate(candidates):
            scored = dict(item)
            if "rerank_score" not in scored:
                scored["rerank_score"] = round(max(0.0, 1.0 - idx * 0.01), 4)
            results.append(scored)
        return results[:top_k]

    reranker = _get_rerank_model()

    pairs = [(query, c["text"]) for c in candidates]
    raw_scores = reranker.compute_score(pairs, normalize=True)  # 归一化到 [0,1]

    # 打分并过滤
    scored = []
    for chunk, score in zip(candidates, raw_scores):
        item = dict(chunk)
        item["rerank_score"] = round(float(score), 4)
        if item["rerank_score"] >= threshold:
            scored.append(item)

    # 降序取 top_k
    scored.sort(key=lambda x: -x["rerank_score"])
    return scored[:top_k]


# ══════════════════════════════════════════════════════════════════════════════
# 8.  对外主接口：retrieve()
# ══════════════════════════════════════════════════════════════════════════════

def retrieve(
    query: str,
    companies:  Optional[list[str]] = None,
    years:      Optional[list[int]]  = None,
    industries: Optional[list[str]] = None,
    top_k:      int = RERANK_TOP_K,
    rewrite:    bool = True,
) -> dict:
    """
    混合检索主接口，供 Agent RAG Worker 节点调用。

    参数：
        query       - 用户原始查询（经 resolved_query 处理后）
        companies   - 过滤公司列表，如 ["比亚迪", "宁德时代"]
        years       - 过滤年份列表，如 [2022, 2023]
        industries  - 过滤行业列表，如 ["new_energy"]
        top_k       - 最终返回 chunk 数量（默认 5）
        rewrite     - 是否做 query 改写（默认开启）

    返回：
        {
            "chunks": [
                {
                    "text":          str,    # chunk 原文
                    "rerank_score":  float,  # 相关性分数 0~1
                    "company_name":  str,
                    "year":          str,
                    "industry":      str,
                    "page_num":      str,
                    "source_file":   str,
                    "chunk_id":      str,
                }
            ],
            "query_variants":   list[str],   # 实际使用的查询变体
            "vector_count":     int,         # 向量检索召回数
            "bm25_count":       int,         # BM25 召回数
            "fused_count":      int,         # 融合后候选数
            "final_count":      int,         # Reranker 后数量
            "low_relevance":    bool,        # 最高分 < threshold → 提示数据不足
        }
    """
    log.info(f"RAG 检索开始：query='{query[:50]}'  "
             f"companies={companies}  years={years}")

    # ── Step 1: Query 改写 ────────────────────────────────────────────────
    queries = rewrite_query(query) if rewrite else [query]

    # ── Step 2: 向量检索 ─────────────────────────────────────────────────
    vector_results = vector_search(
        queries, top_k=VECTOR_TOP_K,
        companies=companies, years=years, industries=industries,
    )
    log.info(f"  向量检索召回：{len(vector_results)} 条")

    # ── Step 3: BM25 检索 ─────────────────────────────────────────────────
    bm25_results = bm25_search(
        queries, top_k=BM25_TOP_K,
        companies=companies, years=years, industries=industries,
    )
    log.info(f"  BM25 检索召回：{len(bm25_results)} 条")

    # ── Step 4: RRF 融合 ─────────────────────────────────────────────────
    fused = reciprocal_rank_fusion(vector_results, bm25_results)
    log.info(f"  RRF 融合后：{len(fused)} 条候选")

    # ── Step 5: Reranker 精排 ─────────────────────────────────────────────
    final_chunks = rerank(query, fused, top_k=top_k, threshold=RELEVANCE_THRESHOLD)
    log.info(f"  Reranker 精排后：{len(final_chunks)} 条，"
             f"最高分：{final_chunks[0]['rerank_score'] if final_chunks else 'N/A'}")

    low_relevance = (
        not final_chunks
        or final_chunks[0]["rerank_score"] < RELEVANCE_THRESHOLD
    )
    if low_relevance:
        log.warning(f"  ⚠️  召回相关性偏低（最高分 < {RELEVANCE_THRESHOLD}），"
                    "建议触发 Supervisor Re-plan")

    return {
        "chunks":         final_chunks,
        "query_variants": queries,
        "vector_count":   len(vector_results),
        "bm25_count":     len(bm25_results),
        "fused_count":    len(fused),
        "final_count":    len(final_chunks),
        "low_relevance":  low_relevance,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 9.  辅助工具：格式化 chunks 为 LLM 上下文字符串
# ══════════════════════════════════════════════════════════════════════════════

def format_chunks_for_llm(chunks: list[dict], max_chars: int = 6000) -> str:
    """
    把 retrieve() 返回的 chunks 拼装成供 LLM 消费的上下文字符串。
    包含来源标注（公司/年份/页码），便于 Synthesizer 做溯源引用。
    """
    parts = []
    total = 0

    for i, chunk in enumerate(chunks, start=1):
        header = (
            f"[来源{i}] {chunk['company_name']} {chunk['year']}年 "
            f"第{chunk['page_num']}页｜{chunk['source_file']}"
            f"（相关性：{chunk['rerank_score']:.2f}）"
        )
        body = chunk["text"]
        section = f"{header}\n{body}\n"

        if total + len(section) > max_chars:
            parts.append(f"[来源{i}] ... [后续内容因长度限制截断]")
            break

        parts.append(section)
        total += len(section)

    return "\n---\n".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 10.  快速测试入口
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    # 简单 CLI：python rag_retriever.py "查询内容" [公司] [年份]
    query = sys.argv[1] if len(sys.argv) > 1 else "碳排放减排目标和具体措施"
    companies  = [sys.argv[2]] if len(sys.argv) > 2 else None
    years      = [int(sys.argv[3])] if len(sys.argv) > 3 else None

    print(f"\n查询：{query}")
    print(f"过滤：companies={companies}  years={years}")
    print("=" * 60)

    result = retrieve(query, companies=companies, years=years)

    print(f"\n检索统计：向量={result['vector_count']}  "
          f"BM25={result['bm25_count']}  "
          f"融合={result['fused_count']}  "
          f"最终={result['final_count']}")
    print(f"低相关性警告：{result['low_relevance']}")
    print(f"\n── 检索结果 ──\n")
    print(format_chunks_for_llm(result["chunks"]))
