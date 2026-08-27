# ESG-Insight Agent 代码地图

> 用途：快速把“项目能力”映射到“代码文件”，方便自己复盘和面试现场打开代码讲。  
> 更新日期：2026-08-09

---

## 1. 总入口

| 能力 | 文件 | 说明 |
|---|---|---|
| 后端 API | `api/main.py` | FastAPI 服务入口，提供 `/api/chat`、SSE、`/api/capabilities`、`/health` |
| Agent 图 | `agent/graph.py` | LangGraph 节点注册、边、条件路由、降级路径 |
| 状态结构 | `agent/state.py` | 定义 AgentState，包括 SQL/RAG 结果、评测状态、披露评分、绿漂风险 |
| 前端聊天页 | `static/index.html`, `static/app.js` | 展示聊天、节点执行进度、最终回答 |
| Dashboard | `static/dashboard.html` | 展示 trace 和节点级可观测信息 |

---

## 2. LangGraph 节点实现

| 节点 | 文件 | 主要职责 |
|---|---|---|
| `context` | `agent/nodes/context.py` | 解析用户 query，识别任务类型、公司、年份、指标 |
| `supervisor` | `agent/nodes/supervisor.py` | 制定执行计划，决定 SQL/RAG 策略，处理 re-plan |
| `schema_injector` | `agent/nodes/schema_injector.py` | 将数据库 schema 注入 SQL worker 上下文 |
| `sql_worker` | `agent/nodes/sql_worker.py` | Text2SQL、安全校验、执行 SQLite 查询 |
| `rag_worker` | `agent/nodes/rag_worker.py` | PDF 报告检索，召回原文 chunks |
| `worker_aggregator` | `agent/nodes/worker_aggregator.py` | 汇总 SQL 和 RAG 结果，判断证据覆盖状态 |
| `evaluator_d` | `agent/nodes/evaluator_d.py` | 生成前数据质量评估，不足时触发 re-plan 或降级 |
| `map_reduce` | `agent/nodes/map_reduce.py` | 长文本压缩，控制上下文长度 |
| `synthesizer` | `agent/nodes/synthesizer.py` | 综合 SQL/RAG 证据生成分析报告 |
| `disclosure_scorer` | `agent/nodes/disclosure_scorer.py` | 调用披露质量评分逻辑并写入 state |
| `greenwashing_detector` | `agent/nodes/greenwashing_detector.py` | 识别 strong claim / weak evidence 风险点 |
| `evaluator_o` | `agent/nodes/evaluator_o.py` | 生成后质量检查，必要时触发修正 |
| `memory_updater` | `agent/nodes/memory_updater.py` | 更新对话记忆 |

---

## 3. 本轮秋招强化新增模块

| 模块 | 文件 | 面试时怎么讲 |
|---|---|---|
| 能力边界扫描 | `agent/capabilities.py` | 系统显式告诉用户当前数据覆盖边界，不把缺失当作 0 |
| Capabilities API | `api/main.py` | `/api/capabilities` 返回 PDF、SQLite、指标字典、已知边界 |
| 披露质量评分 | `agent/disclosure_quality.py` | 确定性 Rubric，五维评分，可测试、可复现 |
| 披露质量节点 | `agent/nodes/disclosure_scorer.py` | 把评分变成 LangGraph 显式节点，支持 trace |
| 绿漂风险雷达 | `agent/greenwashing.py` | 识别 claim-evidence mismatch，不武断定性 |
| 绿漂检测节点 | `agent/nodes/greenwashing_detector.py` | 把绿漂风险写入 state/API/前端 |
| 前端卡片 | `static/app.js`, `static/style.css` | 展示 disclosure score 和 greenwashing radar |
| 单元测试 | `tests/test_*.py` | 保证核心规则逻辑可回归 |

---

## 4. 数据与检索相关

| 文件 / 目录 | 作用 |
|---|---|
| `data/` | ESG PDF、SQLite 数据库、向量库构建脚本等 |
| `data/createDB.py` | 构建结构化 ESG 指标库 |
| `data/build_vectorstore.py` | 构建 PDF 向量检索库 |
| `agent/data_dictionary.py` | 指标名、展示名、业务含义映射 |
| `agent/retriever.py` | RAG 检索能力封装 |

---

## 5. 评测与验证

| 文件 | 作用 |
|---|---|
| `scripts/generate_eval_dataset.py` | 生成离线评测样本 |
| `scripts/run_evaluation.py` | 执行 Agent 评测，生成指标和 bad cases |
| `tests/test_capabilities.py` | 测试能力边界扫描结构 |
| `tests/test_disclosure_quality.py` | 测试披露评分逻辑 |
| `tests/test_greenwashing.py` | 测试绿漂风险规则 |
| `docs/eval-design.md` | 评测体系设计 |
| `docs/experiment-results.md` | 实验结果汇总入口 |

---

## 6. 面试时打开代码的推荐顺序

1. `README.md`：先讲项目定位；
2. `agent/graph.py`：讲 LangGraph 白盒工作流；
3. `agent/state.py`：讲状态如何在节点间流动；
4. `agent/disclosure_quality.py`：讲确定性 Rubric；
5. `agent/greenwashing.py`：讲 claim-evidence mismatch；
6. `api/main.py`：讲 API、SSE、capabilities；
7. `static/app.js`：讲前端如何展示节点进度和评分卡片；
8. `tests/`：讲可回归验证。
