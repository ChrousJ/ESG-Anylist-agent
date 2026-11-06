# ESG-Insight Agent

> 对外展示名：**ESG-Insight Agent**；仓库名保留 ESG-Anylist-agent-master。  
> 面向投资研究 / ESG 分析场景的 **可评测多智能体系统**：用 LangGraph 将 SQL、RAG、质量评估、披露质量评分、绿漂风险雷达、降级响应和可观测性串成一条可解释工作流。

## 核心功能

- **结构化查询**：Text2SQL 查询 SQLite ESG 指标库。
- **非结构化检索**：PDF 报告 RAG 检索，结合向量召回、BM25、RRF、重排。
- **多智能体工作流**：Context → Supervisor → Schema Injector → SQL/RAG Workers → Evaluators → Synthesizer → Disclosure Scorer → Greenwashing Detector。
- **披露质量评分**：确定性 Rubric，输出 0-100 分与 A/B/C/D 等级：完整性、连续性、可比性、可验证性、具体性。
- **可观测性**：SSE 实时展示节点进度，`/static/dashboard.html` 查看节点延迟统计。
- **评测闭环**：`scripts/generate_eval_dataset.py` 和 `scripts/run_evaluation.py` 支持离线评测与 bad case 归因。

## 架构图

```text
User Query
  │
  ▼
context ── knowledge/clarify ───────────────┐
  │ complex                                  │
  ▼                                          │
supervisor ── re-plan ◀── evaluator_d        │
  │                                          │
  ▼                                          │
schema_injector                              │
  ├──────────────┬──────────────┐            │
  ▼              ▼              │            │
sql_worker    rag_worker        │            │
  └──────┬───────┘              │            │
         ▼                      │            │
worker_aggregator ── degraded/no_data ────────┤
         ▼                                   │
    evaluator_d ── re-plan ─────────────────┘
         ▼
map_reduce → synthesizer → disclosure_scorer
         │              │
         │              ▼
         │       greenwashing_detector
         │              ▼
         └────────→ evaluator_o ── retry patch → synthesizer
                        ▼
                 memory_updater → response
```

## 快速开始

> 建议使用 Python 3.10 / 3.11。不要优先用过新的 Python 3.14 跑依赖安装；部分 AI/RAG 生态依赖可能还没有完整兼容。

```bash
python3.11 -m venv .venv  # 如果本机没有 python3.11，可用 python3.10
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env

# 如本地没有 data/esg_data.db，可先构建结构化库和向量库
python data/createDB.py
python data/build_vectorstore.py

uvicorn api.main:app --reload --port 8000
```

打开：

- 聊天终端：<http://127.0.0.1:8000/static/index.html>
- 可观测面板：<http://127.0.0.1:8000/static/dashboard.html>
- 健康检查：<http://127.0.0.1:8000/health>
- 项目覆盖边界：<http://127.0.0.1:8000/api/capabilities>

## 示例问题

- `比亚迪2022到2024年碳排放趋势如何？`
- `工商银行近三年绿色贷款余额变化趋势`
- `对比新能源行业所有公司2023年范围一碳排放`
- `华友钴业报告中有哪些缺少数据支撑的绿色承诺？`

## 本地 BGE 模型

如果 Hugging Face 不可访问，可从 ModelScope + Git LFS 下载模型到 `data/models/`，并在 `.env` 配置本地路径。项目提供：

```bash
make PYTHON=.venv311/bin/python vector-index
```

它会从已有 25,041 个 PDF chunk 构建/续建 ChromaDB，不重复解析 PDF。

## 受限网络下的离线评测

如果无法访问 Hugging Face 或暂时没有 LLM key，可以验证本地 PDF/BM25/Agent 路由链路：

```bash
make PYTHON=.venv311/bin/python bm25-index
make PYTHON=.venv311/bin/python doctor-offline
make PYTHON=.venv311/bin/python eval-offline RUN_ID=offline_smoke_v1
```

离线指标只能说明链路韧性，不代表完整模型语义质量。

## 评测

```bash
# 快速 smoke eval；如果依赖缺失，runner 会把每条 case 记录为 crashed 并生成 bad_cases，方便排查
python scripts/run_evaluation.py -i eval/datasets/esg_eval_smoke.jsonl --skip-baseline --skip-judge --concurrency 1 --run-id <run_id>

# 生成完整 50 条 eval dataset
python scripts/generate_eval_dataset.py -o eval_dataset.jsonl

python -m unittest discover -s tests
```

评测关注：**Case Pass Rate（最终主指标）**、Golden Facts、目标实体/年份覆盖、no-data 安全行为、clarify 行为、证据覆盖、数字支持率、披露评分/绿漂雷达输出率、p95 延迟和降级率。单纯 Completion Rate 不能代表业务质量。

最近一次同模型在线对照（2026-08-13，10 条 smoke，Judge 关闭）结果：LangGraph 主 Agent Case Pass **10/10**、ReAct baseline **6/10**；Golden Facts **13/13 vs 6/13**；平均延迟约 **29.0s vs 67.6s**。这是小样本工程对照，不是泛化准确率 benchmark。详见 `docs/experiment-results.md`。

## 环境变量

见 `.env.example`。默认支持 Gemini，也可通过 OpenAI-compatible 接口切换到 Qwen。

## 文档入口

- `docs/project-audit-20260812.md`：项目客观审查与剩余短板
- `docs/README.md`：文档索引
- `docs/architecture.md`：当前 LangGraph 架构
- `docs/code-map.md`：代码地图
- `docs/resume-bullets.md`：简历素材
- `docs/demo-script.md`：演示脚本
- `docs/model-risk-and-boundaries.md`：模型风险和业务边界
