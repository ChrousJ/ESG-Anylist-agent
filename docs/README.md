# Docs Index

> 这是 ESG-Insight Agent 的文档入口。面试前建议从这里开始看。  
> 更新日期：2026-08-13

---

## 1. 如果你是面试官，推荐阅读顺序

1. `interview-guide.md`：秋招主讲文档、项目亮点、架构深挖与追问；
2. `../README.md`：项目定位、快速开始、核心亮点；
3. `project-story.md`：为什么做这个项目、从 ReAct 到 LangGraph 的演进；
4. `architecture.md`：当前系统架构和节点职责；
5. `demo-cases.md`：推荐演示问题；
6. `experiment-results.md`：评测结果入口和后续实验计划。

---

## 2. 如果你是开发者，推荐阅读顺序

1. `code-map.md`：功能与代码文件映射；
2. `architecture.md`：LangGraph 流程；
3. `data-card.md`：数据覆盖边界；
4. `eval-design.md`：评测体系；
5. `roadmap.md`：后续开发计划；
6. `decision-log.md`：关键技术和产品决策。

---

## 3. 如果你是我自己准备秋招，推荐阅读顺序

1. `resume-bullets.md`：简历 bullet；
2. `interview-script.md`：1/3/8 分钟讲述稿；
3. `demo-script.md`：现场演示流程；
4. `demo-cases.md`：稳定 demo 问题；
5. `model-risk-and-boundaries.md`：回答绿漂、合规、幻觉风险追问。

---

## 4. 文档地图

| 文档 | 用途 |
|---|---|
| `interview-guide.md` | 秋招项目主讲手册：30 秒/3 分钟讲法、架构深挖、高频追问、简历 Bullet |
| `project-audit-20260812.md` | 当前项目的客观评分、关键欠缺和面试前验收线 |
| `project-story.md` | 项目故事线：为什么做 ESG Agent，如何从 ReAct 演进到可评测 Agent |
| `product-design.md` | 产品定位、目标用户、业务问题、功能边界 |
| `architecture.md` | 当前 LangGraph 架构、节点职责、降级路径 |
| `code-map.md` | 能力到代码文件的映射 |
| `data-card.md` | 数据来源、覆盖边界、缺失值处理原则 |
| `eval-design.md` | 评测体系设计 |
| `../eval/README.md` | 可版本化评测样本和 Rubric 入口 |
| `experiment-results.md` | 实验结果、消融实验和 bad case 汇总入口 |
| `decision-log.md` | 关键设计决策 |
| `roadmap.md` | 阶段计划和验收标准 |
| `traceability.md` | 留痕规范 |
| `interview-script.md` | 面试讲述稿 |
| `demo-script.md` | Demo 演示脚本 |
| `demo-cases.md` | 稳定演示问题 |
| `resume-bullets.md` | 简历表达素材 |
| `model-risk-and-boundaries.md` | 模型风险、业务边界、合规表达 |
| `iteration_logs/` | 每轮迭代记录 |


## 5. 最新验收口径

- 当前最终在线对照：`outputs/eval_runs/20260813_deepseek_ab_smoke_v1/`。主 Agent Case Pass 10/10，ReAct baseline 6/10；Judge 关闭。
- 当前离线完整 regression：`outputs/eval_runs/20260813_offline_full50_v5/`。50/50 completion、Case Pass 39/50、0 crash；clarify success 100%，结果用于暴露缺失与澄清边界，不作为在线准确率。
- 当前运行自检：`docs/readiness-report.md` 为 runtime **ready**；全量单测 21/21 passed；API `/health`、`/api/capabilities` 和 OpenAPI 已通过 TestClient smoke。
- 详细实验边界和可引用数字统一以 `docs/experiment-results.md` 第 2.4 节为准。

## 6. 最新迭代记录

- `iteration_logs/iteration_09_deepseek_online_smoke.md`：记录 DeepSeek-V4-Pro 10-case 在线 smoke、指标边界和 SMOKE-005 bad case。
- `iteration_logs/iteration_08_antchat_and_local_bge.md`：记录 AntChat provider、本地 BGE 模型与 Hybrid RAG。
- `iteration_logs/iteration_05_smoke_eval_results.md`：记录早期环境失败归因，以及 eval runner 的健壮性修复。
