# Iteration 07 — Python 3.11 环境、BM25 离线索引与真实 Smoke 指标

> 日期：2026-08-13

## 1. 环境安装

已通过 Homebrew 安装 Python 3.11.15，并创建：

```text
.venv311/
```

安装了 `requirements.txt` 的完整依赖，包括 LangGraph、FastAPI、ChromaDB、FlagEmbedding、Transformers、PyTorch、Pandas 和 PDF 工具链。验证结果：

```text
pip check: No broken requirements found
unit tests: Ran 8 tests, OK
compileall: passed
```

## 2. 数据资产构建

Hugging Face 在当前网络中持续超时，无法下载 `BAAI/bge-small-zh-v1.5`，因此未构建向量索引。为保证项目在受限网络下仍可评测，新增：

```text
scripts/build_bm25_index.py
```

从本地 PDF 构建结果：

| 资产 | 结果 |
|---|---:|
| PDF | 90 |
| 公司 | 30 |
| Chunk | 25,041 |
| 银行 Chunk | 10,450 |
| 新能源 Chunk | 8,270 |
| 电力 Chunk | 6,321 |
| PDF 解析失败 | 0 |

输出：

```text
data/bm25_index.pkl
data/chunk_metadata.json
data/bm25_build_summary.json
```

同时初始化了 SQLite schema，但由于没有有效 DashScope/Gemini key，结构化指标表目前是空表，不能引用 SQL 业务数据。

## 3. 发现并修复的问题

- 修复 `agent/nodes/synthesizer.py` 中 Python 3.11 可见的 f-string 语法错误；
- RAG 新增 `DISABLE_VECTOR_SEARCH`，支持 BM25-only；
- SQL worker 新增仅针对已知实体/指标的安全 SELECT fallback，不猜测数值；
- 新增 `OFFLINE_DETERMINISTIC_MODE`，外部 LLM 不可用时不做无意义重试；
- 离线模式保留 knowledge / clarify 快速出口；
- 增加“特斯拉”实体识别，未覆盖公司会命中 coverage-gap，而不是跨公司召回；
- Project Doctor 现在识别 hybrid/vector 或 BM25-only 检索模式。

## 4. 离线 Smoke Eval v2

Run ID：

```text
20260813_offline_bm25_v2
```

配置：

```text
OFFLINE_DETERMINISTIC_MODE=true
DISABLE_VECTOR_SEARCH=true
DISABLE_RERANK=true
skip_baseline=true
skip_judge=true
```

结果：

| 指标 | 数值 |
|---|---:|
| Completion Rate | 100.0% (10/10) |
| Crashed | 0 |
| Degraded | 0 |
| Rescue Rate | 50.0% (5/10) |
| Evidence Presence | 60.0% (6/10) |
| RAG Citation Presence | 60.0% |
| SQL Evidence Presence | 0.0% |
| Disclosure Score Presence | 60.0% |
| Greenwashing Radar Presence | 60.0% |
| p50 Latency | 67ms |
| p95 Latency | 7048ms |
| Avg Latency | 1356ms |

### 指标解释

- 10 条包含 knowledge、clarify 和 coverage-gap，因此这 4 条不应强制输出 RAG 证据/披露评分；60% presence 对应 6 条证据分析 case。
- 这是 **离线链路韧性指标**：证明本地 PDF → BM25 → LangGraph → Evaluator → 业务节点 → 报告产物可运行。
- 它不是完整模型质量指标：没有向量召回、BGE reranker、LLM synthesis、LLM judge，也没有结构化指标数据。
- 不能用平均披露分 24.73 判断企业表现；低分主要由 SQL 空表和离线降级证据造成。

## 5. 仍需补齐

- 配置有效 Gemini 或 Qwen/DashScope key；
- 构建结构化 SQLite 指标数据；
- 在可访问 Hugging Face 的网络下载 embedding/reranker；
- 重跑 full online smoke 和 baseline 消融；
- 人工复核 numeric faithfulness 与 evidence relevance。
