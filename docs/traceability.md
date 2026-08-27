# 项目可追溯留痕规范

> 目的：确保项目每一步都有记录，可以回溯“为什么做、做了什么、效果如何、下一步是什么”。  
> 当前版本：v0.2  
> 更新日期：2026-08-09

---

## 1. 留痕原则

本项目所有重要变化都遵循四个问题：

1. **Why**：为什么要做这个改动？对应哪个业务痛点或评测问题？
2. **What**：具体做了什么？涉及哪些文件？
3. **Impact**：带来了什么效果？指标是否变化？
4. **Next**：下一步如何继续验证或优化？

---

## 2. 文档体系

| 文档 | 作用 | 更新时机 |
|---|---|---|
| `docs/project-story.md` | 记录项目故事线和面试叙事 | 项目定位或主线变化时 |
| `docs/product-design.md` | 记录产品定位、用户、痛点、功能边界 | 功能范围变化时 |
| `docs/roadmap.md` | 记录阶段计划和验收标准 | 每个阶段开始 / 结束时 |
| `docs/eval-design.md` | 记录评测集、指标、实验设计 | 评测体系变化时 |
| `docs/decision-log.md` | 记录关键产品 / 技术决策 | 每次做重要取舍时 |
| `docs/iteration_logs/` | 记录每轮开发和评测迭代 | 每轮实验或优化后 |
| `docs/experiment-results.md` | 汇总最终实验结果 | 系统实验完成后 |
| `docs/interview-script.md` | 面试讲述材料 | 项目进入包装阶段时 |
| `docs/architecture.md` | 当前系统架构和节点职责 | LangGraph 节点或路由变化时 |
| `docs/code-map.md` | 功能到代码文件映射 | 新增核心模块时 |
| `docs/data-card.md` | 数据覆盖与缺失值边界 | 数据范围变化时 |
| `docs/model-risk-and-boundaries.md` | 模型风险和业务边界 | 风险定义或输出策略变化时 |
| `docs/resume-bullets.md` | 简历素材 | 面试包装阶段或项目亮点变化时 |
| `docs/demo-script.md` | 现场演示脚本 | Demo 流程或稳定 case 变化时 |

---

## 3. 目录留痕规范

建议目录：

```text
ESG-Anylist-agent-master/
  outputs/
    eval_runs/
      {run_id}/
        eval_report.md
        metrics.json
        bad_cases.jsonl
        trace_summary.csv
    traces/
      {trace_id}.jsonl
  eval/
    datasets/
    rubrics/
  docs/ 或 ../docs/
```

注意：当前 `docs/` 同时承担项目方法论、架构说明、评测设计和面试材料四类用途。后续如果文档继续增多，可以拆分为 `docs/product/`、`docs/architecture/`、`docs/evaluation/`、`docs/interview/`。

---

## 4. 每轮迭代记录模板

每次优化后，在 `docs/iteration_logs/iteration_xx.md` 中记录：

```markdown
# Iteration XX：标题

## 1. 本轮目标

本轮要解决什么问题？对应哪个评测指标或 bad case 类型？

## 2. 修改前问题

- 现象：
- 影响样本：
- 初始指标：

## 3. 根因分析

- primary_error：
- secondary_error：
- root_cause：

## 4. 修改内容

| 文件 | 修改点 | 原因 |
|---|---|---|

## 5. 评测结果

| 指标 | 修改前 | 修改后 | 变化 |
|---|---:|---:|---:|

## 6. Bad Case 变化

- 修复的 case：
- 新增失败 case：
- 仍未解决 case：

## 7. 结论与下一步

- 本轮结论：
- 下一步：
```

---

## 5. 实验运行留痕规范

每次运行评测时，使用唯一 `run_id`：

```text
run_id = YYYYMMDD_HHMM_{short_name}
```

示例：

```text
20260806_2100_react_baseline
20260807_1030_langgraph_evald
20260810_2230_full_system
```

每次评测至少保存：

- `metrics.json`：机器可读指标；
- `eval_report.md`：人类可读总结；
- `bad_cases.jsonl`：失败样本；
- `trace_summary.csv`：节点级耗时和状态。

---

## 6. 决策留痕规范

任何满足以下条件的变化，都必须写入 `docs/decision-log.md`：

- 改变项目主线；
- 改变核心架构；
- 引入或移除关键模块；
- 改变评测指标；
- 改变数据范围；
- 做出明显 trade-off，例如牺牲延迟换事实性。

---

## 7. Git 提交建议

每次提交建议遵循：

```text
[type] scope: summary
```

示例：

```text
docs: add product positioning and roadmap
feat(eval): add disclosure quality benchmark schema
feat(agent): add disclosure quality scorer
fix(rag): improve carbon emission query rewrite
exp: run ablation for evaluator-d and evaluator-o
```

---

## 8. 面试可回溯讲法

如果面试官追问“你是怎么一步步做出来的”，可以按这个留痕链路回答：

```text
project-story.md：为什么做 ESG Agent
  ↓
product-design.md：做给谁，解决什么问题
  ↓
decision-log.md：为什么从 ReAct 转 LangGraph，为什么 SQL+RAG，为什么双 Evaluator
  ↓
eval-design.md：怎么证明效果，不靠主观感觉
  ↓
iteration_logs：每轮 bad case 怎么归因、怎么修复、指标怎么变化
  ↓
experiment-results.md：最终实验结果和消融分析
```
