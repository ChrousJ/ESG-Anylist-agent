# ESG-Insight Agent Demo Script

> 用途：面试、录屏、项目答辩时按步骤演示，避免现场临时发挥。  
> 更新日期：2026-08-09

---

## 1. Demo 前检查清单

```bash
make test
make dev
```

打开：

- Chat UI: <http://127.0.0.1:8000/static/index.html>
- Dashboard: <http://127.0.0.1:8000/static/dashboard.html>
- Capabilities: <http://127.0.0.1:8000/api/capabilities>

如果现场网络或模型 API 不稳定，优先展示：

1. README 项目定位；
2. `docs/architecture.md` 架构图；
3. `/api/capabilities` 数据边界；
4. `tests/` 单元测试结果；
5. 预先准备的 demo 输出截图或录屏。

---

## 2. 3 分钟 Demo

### 0:00 - 0:30 项目定位

打开 `README.md`，讲：

> 这个项目不是普通 PDF QA，而是一个可评测 ESG 分析 Agent。它用 LangGraph 拆出显式节点，用 SQL + RAG 双通道获取证据，并通过双层 Evaluator 降低幻觉。

### 0:30 - 1:00 数据边界

打开 `/api/capabilities`，讲：

> 我显式暴露系统能力边界，包括当前覆盖的 PDF、SQLite 数据和指标字典。这样系统遇到未覆盖公司或年份时会降级，而不是把缺失编成数字。

### 1:00 - 2:20 完整链路 Case

在 Chat UI 输入：

```text
比亚迪2022到2024年碳排放趋势如何？
```

讲：

> 这里可以看到节点按顺序执行：context 识别公司和年份，supervisor 规划 SQL 和 RAG，两个 worker 分别拿结构化数字和报告原文，Evaluator-D 检查证据，Synthesizer 生成分析，然后 disclosure_scorer 给披露质量评分，greenwashing_detector 输出潜在风险雷达，最后 Evaluator-O 检查输出。

### 2:20 - 3:00 Dashboard

打开 Dashboard，讲：

> Dashboard 展示了节点级 trace 和耗时。真实 Agent 项目很重要的一点是可观测性，否则回答错了不知道是检索错、SQL 错还是生成错。

---

## 3. 8 分钟 Demo

### 0:00 - 1:00 背景与痛点

讲 ESG 报告痛点：

- 报告长；
- 指标散；
- 披露不完整；
- 口径不一致；
- 定性宣传多，证据弱；
- 人工横向对比成本高。

### 1:00 - 2:00 架构

打开 `docs/architecture.md`，展示 Mermaid 图或节点表。

核心句：

> 我把 Agent 自主性限制在可控图结构里，用白盒 workflow 替代黑盒 ReAct。

### 2:00 - 4:00 Case 1：趋势分析

问题：

```text
比亚迪2022到2024年碳排放趋势如何？
```

重点看：

- 是否出现多年数据；
- 是否说明趋势；
- 是否有证据来源；
- 是否展示披露质量分；
- 是否展示绿漂风险雷达。

### 4:00 - 5:00 Case 4：覆盖边界

问题：

```text
蔚来2024年碳排放表现如何？
```

重点讲：

> 这个 case 不是为了展示答得多，而是展示系统知道什么时候不能答。缺失数据不能被当作 0，也不能编造。

### 5:00 - 6:00 Case 5：潜在绿漂风险

问题：

```text
华友钴业报告中有哪些缺少数据支撑的绿色承诺？
```

重点讲：

> 我不说企业真实绿漂，只识别 strong claim 但缺少数字、年度进展或鉴证的人工核查点。

### 6:00 - 7:00 评测设计

打开 `docs/eval-design.md` / `docs/experiment-results.md`，讲：

> 评测不是只看回答好不好，而是拆成完成率、证据覆盖、数字忠实度、实体一致性、绿漂风险召回等指标。

### 7:00 - 8:00 总结亮点

用四句话收尾：

1. 不是普通 RAG，是白盒 LangGraph Agent；
2. 不是只生成回答，有双层 Evaluator；
3. 不是泛 ESG 问答，聚焦披露质量和绿漂风险；
4. 不是黑盒 Demo，有 capabilities、trace、tests 和 eval 设计。

---

## 4. 现场翻车预案

| 问题 | 处理方式 | 讲法 |
|---|---|---|
| LLM API 超时 | 打开 README / architecture / code-map | “核心工程逻辑可本地查看，模型调用是外部依赖。” |
| RAG 召回慢 | 展示 Dashboard 和降级策略 | “真实系统需要把慢和失败纳入设计。” |
| 某个问题没数据 | 打开 `/api/capabilities` | “这是预期行为，系统不会编造未覆盖数据。” |
| 前端没显示卡片 | 打开 API JSON 或代码 | “后端 state 已输出，前端只是展示层。” |
| 面试官追问准确率 | 打开 eval-design / experiment-results | “第一阶段重点是建立可评测闭环，后续用人工标注扩大样本。” |

---

## 5. 推荐演示问题

优先使用 `docs/demo-cases.md` 中的 case，不建议现场随机问完全未知公司。

最稳三问：

```text
比亚迪2022到2024年碳排放趋势如何？
蔚来2024年碳排放表现如何？
华友钴业报告中有哪些缺少数据支撑的绿色承诺？
```
