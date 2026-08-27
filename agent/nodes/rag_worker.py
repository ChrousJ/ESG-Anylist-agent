"""
agent/nodes/rag_worker.py  —  RAG Worker（流程的第 ⑤ 步，与 SQL Worker 并行）
==============================================================================

【在流程中的位置】schema_injector → ★rag_worker★ → worker_aggregator
                                     ↕ （与 sql_worker 并行执行）

【什么是 RAG？】

RAG = Retrieval-Augmented Generation（检索增强生成）。
简单说就是：先从文档库中找到相关的段落（Retrieval），
然后把这些段落作为"参考资料"交给 LLM 生成回答（Generation）。

在本项目中，RAG Worker 的"文档库"是各公司的 ESG 年度报告（PDF）。
这些 PDF 已经被预处理成文本片段（chunk），存储在 ChromaDB 向量数据库中。

【这个节点的工作流程】

  1. 从 Supervisor 获取实质性议题变体（如"碳排放强度与脱碳承诺"）
  2. 调用 rag_retriever.retrieve() 做混合检索：
     - 向量检索（语义相似度）+ BM25 检索（关键词匹配）
     - RRF 融合两种结果
     - BGE Reranker 精排，选出最相关的 top-5 段落
  3. 补充检索"口径说明" chunk（供 Evaluator-D 校验数据一致性）

【写入 State 的关键字段】
  - rag_result: 检索结果（包含 chunks、相关性分数等）
  - scope_adjustment_chunks: 口径说明文本片段
  - worker_status["rag"]: 执行状态记录
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from agent.state import AgentState, WorkerStatusDict
from agent.tracing import trace_node, TraceLogger, run_with_timeout

log = logging.getLogger(__name__)

# 口径说明检索的关键词模板
_SCOPE_KEYWORDS = ["计算方法", "统计口径", "包含范围", "披露依据", "核算边界", "计量方式"]
_VALID_INDUSTRIES = {"bank", "power", "new_energy"}


def _build_scope_query(metric_key: str) -> str:
    """为某指标构造口径说明检索 query。"""
    metric_cn = {
        "scope_1_emissions":        "碳排放范围一",
        "scope_2_emissions":        "碳排放范围二",
        "scope_3_emissions":        "碳排放范围三",
        "total_energy_consumption": "综合能耗",
        "energy_intensity":         "能耗强度",
        "green_finance_balance":    "绿色贷款",
        "clean_energy_ratio":       "清洁能源占比",
        "rd_investment_total":      "研发投入",
    }.get(metric_key, metric_key)
    return f"{metric_cn} 计算方法 统计口径 包含范围 核算边界"


_METRIC_RETRIEVAL_TERMS = {
    "scope_1_emissions": "范围一 范围1 直接温室气体排放量 tCO2e",
    "scope_2_emissions": "范围二 范围2 间接温室气体排放量 tCO2e",
    "scope_3_emissions": "范围三 范围3 价值链温室气体排放量",
    "green_finance_balance": "绿色贷款余额 绿色信贷余额 亿元 万亿元",
    "clean_energy_ratio": "清洁能源占比 可再生能源装机比例",
    "rd_investment_total": "研发投入总额 研发费用 亿元",
}

def _build_targeted_evidence_query(base_query: str, metrics: list[str]) -> str:
    terms = " ".join(_METRIC_RETRIEVAL_TERMS.get(m, m) for m in metrics[:3])
    return f"{base_query} {terms} 定量绩效 指标 单位 数据表".strip()

def _coverage_targets(companies: list[str], years: list[int]) -> list[tuple[str, int]]:
    # Prevent combinatorial expansion on malformed requests. Smoke/demo ranges are <= 3 years.
    return [(c, int(y)) for c in companies[:10] for y in years[:5]]

def _chunk_target(chunk: dict) -> tuple[str, int] | None:
    company = str(chunk.get("company_name", ""))
    try:
        year = int(chunk.get("year"))
    except (TypeError, ValueError):
        return None
    return company, year

def _select_with_target_coverage(chunks: list[dict], targets: list[tuple[str, int]], top_k: int) -> list[dict]:
    """Keep the best chunk for every explicit company-year before global score fill."""
    ordered = sorted(chunks, key=lambda x: -float(x.get("rerank_score", 0)))
    selected: list[dict] = []
    seen: set[str] = set()
    for target in targets:
        match = next((c for c in ordered if _chunk_target(c) == target), None)
        if match is not None:
            cid = str(match.get("chunk_id", ""))
            if cid not in seen:
                selected.append(match)
                seen.add(cid)
    for chunk in ordered:
        cid = str(chunk.get("chunk_id", ""))
        if cid in seen:
            continue
        selected.append(chunk)
        seen.add(cid)
        if len(selected) >= max(top_k, len(targets)):
            break
    return selected[:max(top_k, len(targets))]


@trace_node("rag_worker", tags=["worker", "rag"])
def rag_worker_node(state: AgentState) -> AgentState:
    trace_id = state.get("trace_id", "")
    log      = TraceLogger("rag_worker", trace_id)

    plan     = state.get("plan", {})
    entities = state.get("entities", {})

    # 未被调度时跳过
    if "rag" not in plan.get("workers", []):
        log.info("RAG Worker 未被调度，跳过")
        return {
            "worker_status": {
                "rag": WorkerStatusDict(
                    status="skipped", latency_ms=0,
                    error_type="", error_detail="", retried=False,
                ),
            },
        }

    # 延迟导入（避免模型在不需要时加载）
    from data.rag_retriever import retrieve, format_chunks_for_llm

    companies  = entities.get("companies", [])
    years      = entities.get("years", [])
    industry = entities.get("industry", "")
    industries = [industry] if industry in _VALID_INDUSTRIES else []
    metrics    = entities.get("metrics", [])

    # Supervisor 注入的实质性议题变体
    materiality_variants: list[str] = plan.get("_materiality_variants", [])
    rag_strategy = plan.get("rag_strategy", "standard")
    base_query   = state.get("resolved_query", state.get("user_query", ""))

    # NOTE: All retrieval thresholds are env-configurable for interview-time tuning.
    threshold_standard = float(os.getenv("RAG_RERANK_THRESHOLD_STANDARD", "0.3"))
    threshold_relaxed = float(os.getenv("RAG_RERANK_THRESHOLD_RELAXED", "0.2"))
    threshold_recall_fallback = float(os.getenv("RAG_RERANK_THRESHOLD_RECALL_FALLBACK", "0.1"))
    threshold = (
        threshold_recall_fallback if rag_strategy == "recall_fallback"
        else threshold_relaxed if rag_strategy == "relaxed"
        else threshold_standard
    )
    top_k_standard = int(os.getenv("RAG_TOP_K_STANDARD", "5"))
    top_k_recall_fallback = int(os.getenv("RAG_TOP_K_RECALL_FALLBACK", "8"))
    first_pass_top_k = top_k_recall_fallback if rag_strategy == "recall_fallback" else top_k_standard
    fallback_min_chunks = int(os.getenv("RAG_RECALL_FALLBACK_MIN_CHUNKS", "4"))
    final_top_k_standard = int(os.getenv("RAG_FINAL_TOP_K", "10"))
    final_top_k_recall_fallback = int(os.getenv("RAG_FINAL_TOP_K_RECALL_FALLBACK", "12"))
    final_top_k = final_top_k_recall_fallback if rag_strategy == "recall_fallback" else final_top_k_standard

    log.info(
        f"开始检索，strategy={rag_strategy}",
        {"variants": len(materiality_variants), "threshold": threshold},
    )

    # ── 主检索：逐个议题变体检索，合并结果 ──────────────────────────────────
    rag_timeout = float(os.getenv("RAG_TIMEOUT_SEC", "400"))
    rag_scope_timeout = float(os.getenv("RAG_SCOPE_TIMEOUT_SEC", "30"))

    def _run_retrieve():
        from data.rag_retriever import retrieve as _retrieve
        all_chunks   = []
        seen_ids     = set()
        query_list   = materiality_variants if materiality_variants else [base_query]

        def _collect_chunks(
            *,
            companies_filter,
            years_filter,
            industries_filter,
            top_k: int,
        ) -> None:
            for variant_query in query_list:
                res = _retrieve(
                    query=variant_query,
                    companies=companies_filter,
                    years=years_filter,
                    industries=industries_filter,
                    top_k=top_k,
                    rewrite=False,   # 变体已由 Supervisor 生成，不再二次改写
                )
                for chunk in res.get("chunks", []):
                    cid = chunk.get("chunk_id", "")
                    if cid not in seen_ids and chunk.get("rerank_score", 0) >= threshold:
                        seen_ids.add(cid)
                        all_chunks.append(chunk)

        # First pass: strict filters from extracted entities.
        _collect_chunks(
            companies_filter=companies if companies else None,
            years_filter=years if years else None,
            industries_filter=industries if industries else None,
            top_k=first_pass_top_k,
        )

        # Explicit multi-company/year requests require at least one candidate per target.
        # A single global top-k can otherwise be monopolized by one company.
        targets = _coverage_targets(companies, years) if companies and years else []
        targeted_query = _build_targeted_evidence_query(base_query, metrics)
        present_targets = {_chunk_target(c) for c in all_chunks}
        for company, year in targets:
            if (company, year) in present_targets:
                continue
            res = _retrieve(
                query=targeted_query, companies=[company], years=[year],
                industries=industries if industries else None,
                top_k=max(3, first_pass_top_k), rewrite=False,
            )
            for chunk in res.get("chunks", []):
                cid = chunk.get("chunk_id", "")
                if cid not in seen_ids and chunk.get("rerank_score", 0) >= threshold:
                    seen_ids.add(cid)
                    all_chunks.append(chunk)
            present_targets = {_chunk_target(c) for c in all_chunks}

        if rag_strategy == "recall_fallback" and len(all_chunks) < fallback_min_chunks:
            # NOTE: Recall fallback broadens filters progressively to reduce degraded cases.
            _collect_chunks(
                companies_filter=companies if companies else None,
                years_filter=None,
                industries_filter=industries if industries else None,
                top_k=top_k_recall_fallback,
            )

        if rag_strategy == "recall_fallback" and len(all_chunks) < fallback_min_chunks:
            _collect_chunks(
                companies_filter=None,
                years_filter=None,
                industries_filter=industries if industries else None,
                top_k=top_k_recall_fallback,
            )

        # Preserve explicit company-year coverage before filling by global relevance.
        all_chunks.sort(key=lambda x: -x.get("rerank_score", 0))
        final_chunks = _select_with_target_coverage(all_chunks, targets, final_top_k)
        covered_targets = sorted({t for c in final_chunks if (t := _chunk_target(c)) in set(targets)})
        missing_targets = [list(t) for t in targets if t not in set(covered_targets)]
        return {
            "chunks": final_chunks,
            "final_count": len(final_chunks),
            "low_relevance": (not all_chunks or all_chunks[0].get("rerank_score", 0) < threshold),
            "coverage": {
                "required": [list(t) for t in targets],
                "covered": [list(t) for t in covered_targets],
                "missing": missing_targets,
                "complete": not missing_targets,
            },
        }

    if rag_timeout <= 0:
        rag_result_wrap = {
            "status": "success",
            "result": _run_retrieve(),
            "error_type": "",
            "error_detail": "",
            "latency_ms": 0,
        }
    else:
        rag_result_wrap = run_with_timeout(
            _run_retrieve,
            timeout_seconds=rag_timeout,
            worker_name="rag_retriever",
            trace_id=trace_id,
        )

    if rag_result_wrap["status"] != "success":
        log.error(f"RAG 检索失败：{rag_result_wrap['error_type']}")
        return {
            "worker_status": {
                "rag": WorkerStatusDict(
                    status=rag_result_wrap["status"],
                    error_type=rag_result_wrap["error_type"],
                    error_detail=rag_result_wrap["error_detail"],
                    latency_ms=rag_result_wrap["latency_ms"],
                    retried=False,
                ),
            },
            "rag_result": None,
        }

    rag_result = rag_result_wrap["result"]
    log.info(
        f"主检索完成",
        {
            "chunks":       rag_result["final_count"],
            "low_relevance": rag_result["low_relevance"],
            "latency_ms":   rag_result_wrap["latency_ms"],
        },
    )

    # ── 口径说明补充检索 ─────────────────────────────────────────────────────
    # 仅对参与横向对比的定量指标补充检索口径 chunk
    scope_chunks = []
    compare_dim  = entities.get("compare_dimension", "none")
    if compare_dim in ("horizontal", "both") and metrics:
        log.info(f"触发口径说明补充检索，指标：{metrics}")

        def _run_scope_retrieve():
            from data.rag_retriever import retrieve as _retrieve
            chunks = []
            seen   = set()
            for mk in metrics[:3]:  # 最多3个指标，避免过长
                sq = _build_scope_query(mk)
                res = _retrieve(
                    query=sq,
                    companies=companies if companies else None,
                    years=[years[-1]] if years else None,  # 只取最近年
                    top_k=3,
                    rewrite=False,
                )
                for c in res.get("chunks", []):
                    cid = c.get("chunk_id", "")
                    if cid not in seen:
                        seen.add(cid)
                        c["_scope_metric"] = mk   # 标记来源指标
                        chunks.append(c)
            return chunks

        if rag_scope_timeout <= 0:
            scope_wrap = {
                "status": "success",
                "result": _run_scope_retrieve(),
                "error_type": "",
                "error_detail": "",
                "latency_ms": 0,
            }
        else:
            scope_wrap = run_with_timeout(
                _run_scope_retrieve,
                timeout_seconds=rag_scope_timeout,
                worker_name="rag_scope",
                trace_id=trace_id,
            )
        if scope_wrap["status"] == "success":
            scope_chunks = scope_wrap["result"]
            log.info(f"口径说明检索完成，{len(scope_chunks)} 个 chunk")

    total_latency = rag_result_wrap["latency_ms"]

    return {
        "rag_result": rag_result,
        "scope_adjustment_chunks": scope_chunks,
        "worker_status": {
            "rag": WorkerStatusDict(
                status="success",
                error_type="",
                error_detail="",
                latency_ms=total_latency,
                retried=False,
            ),
        },
    }
