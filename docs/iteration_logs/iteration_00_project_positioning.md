# Iteration 00：项目定位与路线收敛

> 日期：2026-08-06  
> 类型：项目定位 / 文档沉淀  
> 状态：completed

---

## 1. 本轮目标

将项目从“ESG 报告分析 Agent 雏形”收敛为一个能用于秋招展示和结业论文的实战项目方向。

核心目标：

- 找到真实落地痛点；
- 避免功能堆砌；
- 形成有故事、有内核、有技术链路的项目主线；
- 明确后续开发计划；
- 建立可追溯文档体系。

---

## 2. 修改前问题

当前项目已有 ReAct baseline、LangGraph workflow、SQL/RAG worker、Evaluator-D/O、评测脚本等雏形，但存在三个问题：

1. **主线不够聚焦**：如果只说“分析 ESG 报告”，容易显得泛；
2. **亮点不够业务化**：已有功能容易被理解为常见 Agent 技术组合；
3. **缺少完整留痕**：项目思考、技术取舍、后续路线没有被系统记录。

---

## 3. 根因分析

- 项目还停留在“功能实现”层，没有完全上升到“解决真实问题的方法论”；
- ESG 的业务价值需要进一步具体化到用户、任务和指标；
- AI 应用和 AI 评测方向需要通过可量化评测和 bad case 归因体现。

---

## 4. 本轮决策

本轮做出以下关键决策：

1. 项目主线定为：**ESG 披露质量评估与绿漂风险识别**；
2. 目标用户定为：**投资研究员 / ESG 分析师 / 券商研究助理**；
3. 技术主线定为：**评测驱动的白盒 Agent Workflow**；
4. 能力展示重点定为：**AI 应用 + AI 评测 + 效果优化闭环**；
5. 后续优先级定为：先做评测集，再做披露质量评分和绿漂风险识别。

---

## 5. 产出文档

| 文件 | 内容 |
|---|---|
| `docs/product-design.md` | 产品定位、用户、痛点、功能边界 |
| `docs/roadmap.md` | 8 周落地计划和验收标准 |
| `docs/eval-design.md` | 评测集、指标、bad case 归因和实验设计 |
| `docs/decision-log.md` | 关键产品 / 技术决策 |
| `docs/traceability.md` | 后续所有迭代如何留痕 |
| `docs/iteration_logs/README.md` | 迭代日志规范 |

---

## 6. 指标变化

本轮是规划和文档沉淀，没有运行代码评测，因此暂无量化指标。

后续 Phase 1/2 将建立 baseline 指标。

---

## 7. 下一步计划

下一步进入 Phase 1：构建最小可评测数据集。

优先任务：

1. 在代码目录下新增 `eval/datasets/esg_eval_v1.jsonl`；
2. 先人工设计 60-100 条高质量 case；
3. 按 fact_extraction、trend_analysis、peer_comparison、disclosure_quality、greenwashing_risk 五类组织；
4. 改造评测脚本，使其输出 metrics、bad cases 和 trace summary。
