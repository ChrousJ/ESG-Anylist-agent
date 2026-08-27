# Iteration 08 — OpenAI-compatible Provider、本地 BGE 模型与 Hybrid RAG

> 日期：2026-08-13

## 1. OpenAI-compatible Provider

新增通用 provider：

```env
LLM_PROVIDER=openai_compatible
OPENAI_BASE_URL=https://antchat.alipay.com/v1
OPENAI_MAIN_MODEL=DeepSeek-V4-Pro
```

API key 只保存在本地 `.env`：

- `.env` 已由 `.gitignore` 排除；
- 文件权限为 `600`；
- key 未写入源码、文档、评测产物或日志。

本次连通性测试能够到达 AntChat 网关，但返回 `401 API key not found`。说明 provider 适配和网络地址可用，但当前 key 未被服务端识别，需要重新确认 key 是否完整、是否已启用、是否属于该环境。

> 后续状态更新（2026-08-13 15:28，Asia/Shanghai）：credential/configuration 问题已解决，单条在线 probe 与 10-case `20260813_deepseek_smoke_v1` 均已通过网关执行；详见 `iteration_09_deepseek_online_smoke.md`。

## 2. 本地模型下载

Hugging Face 官方源和 hf-mirror 在当前网络不可达，改用 ModelScope + Git LFS 下载：

| 模型 | 本地目录 | 大小 | 验证 |
|---|---|---:|---|
| BAAI/bge-small-zh-v1.5 | `data/models/bge-small-zh-v1.5` | 376MB | embedding `(1, 512)` 通过 |
| BAAI/bge-reranker-v2-m3 | `data/models/bge-reranker-v2-m3` | 4.3GB | 项目兼容补丁下 score 计算通过 |

模型目录已加入 `.gitignore`，不提交大权重。

## 3. 向量索引

新增可恢复构建脚本：

```text
scripts/build_chroma_from_chunks.py
```

它直接消费已有 `data/chunk_metadata.json`，无需重复解析 90 份 PDF，支持中断后按 chunk id 续建。

构建结果：

```text
ChromaDB collection: esg_reports
chunk count: 25,041
embedding model: ./data/models/bge-small-zh-v1.5
vector store: data/vector_store
```

## 4. Hybrid Retrieval 验证

真实检索链路已通过：

```text
vector_count=20
bm25_count=20
fused_count=32
final_count=5
highest rerank score=0.9813
```

检索结果均按公司和年份过滤到比亚迪报告，证明本地：

```text
BGE embedding + ChromaDB + BM25 + RRF + BGE reranker
```

可以完整运行。

## 5. Hybrid Offline Smoke Eval

Run ID：

```text
20260813_offline_hybrid_bge_v1
```

结果：

| 指标 | 数值 |
|---|---:|
| Completion | 100% |
| Crash / Degraded | 0 / 0 |
| Evidence Presence | 60%（6 条证据分析 case 全覆盖） |
| Rescue Rate | 50% |
| p50 Latency | 10.249s |
| p95 Latency | 97.795s |

该 run 证明 hybrid retrieval 可用，但本地 2.1B reranker 在 CPU 上对多个 materiality query 逐个重排，尾延迟明显偏高。面试 Demo 默认可选：

- 快速模式：vector + BM25 + RRF，关闭 reranker；
- 质量模式：开启 reranker，但减少 query variants / candidates；
- 生产优化：使用更小 reranker、ONNX/MPS、缓存或批量重排。

该 run 仍是 offline deterministic synthesis，不是 DeepSeek 在线生成质量指标。
