# ESG-Insight Agent 面试讲述稿

> 用途：面试前直接按这个文档准备。你可以根据面试官时间选择 1 分钟、3 分钟或 8 分钟版本。

---

## 1 分钟版本

我做的是一个面向 ESG 报告分析的可评测多智能体系统。它不是普通 PDF 问答，而是用 LangGraph 把任务拆成 context、supervisor、SQL worker、RAG worker、Evaluator 和 Synthesizer 等节点。系统会并行查询结构化 ESG 指标库和 PDF 报告原文，再通过生成前 Evaluator-D 检查数据质量，通过生成后 Evaluator-O 检查数字幻觉、实体越界和趋势方向。

最近我重点增强了两个业务能力：第一是披露质量评分，从完整性、连续性、可比性、可验证性和具体性五个维度给出 0-100 分；第二是潜在绿漂风险雷达，用规则型 claim-evidence mismatch 检测“强 ESG 表述但附近缺少量化证据”的片段。工程上还有 SSE 节点流式进度、trace dashboard、capabilities 边界接口和离线评测脚本。

---

## 3 分钟版本

这个项目的背景是 ESG 报告和传统财报不一样，它长文本多、格式不统一、披露不完整，而且有很多“绿色低碳、持续推进”这类定性表述。普通 RAG 很容易只把原文总结出来，但无法判断证据质量，也很难保证数字不幻觉。

所以我没有做黑盒 ReAct，而是用 LangGraph 做了一个白盒 Agent 工作流：

1. `context` 负责解析公司、年份、指标和意图；
2. `supervisor` 负责规划用 SQL、RAG 还是二者并行；
3. `schema_injector` 给 Text2SQL 注入数据字典和 few-shot；
4. `sql_worker` 查询结构化 SQLite 指标；
5. `rag_worker` 从 ESG PDF 召回原文证据；
6. `worker_aggregator` 汇总双路结果；
7. `evaluator_d` 在生成前检查数据覆盖、缺失和口径问题，不合格会 re-plan；
8. `synthesizer` 生成四层分析报告；
9. `disclosure_scorer` 输出披露质量评分；
10. `greenwashing_detector` 输出潜在绿漂风险雷达；
11. `evaluator_o` 做最终输出忠实度检查。

我最新加的两个亮点是独立节点：`disclosure_scorer` 和 `greenwashing_detector`。披露评分不是让大模型主观打分，而是一个确定性 Rubric：完整性 30、连续性 20、可比性 20、可验证性 20、具体性 10。绿漂检测也先不用 LLM，而是规则型检测：如果 RAG 片段出现“绿色低碳、全面推进、碳中和、行业领先”等强表述，但附近没有数字、同比、年度进展或第三方鉴证，就标成需要人工核查的风险信号。

这个项目的核心价值是：它体现了 Agent 从 Demo 到可靠应用需要的质量工程能力，而不只是会调用一个向量库。

---

## 8 分钟深挖版本

### 1. 为什么做 ESG Agent

我本科有金融工程和计算机背景，也接触过企业披露文件。财报的数字比较结构化，但 ESG 报告大量是非结构化长文本，而且披露口径不统一。投资研究、ESG 评级和企业对标都需要快速判断：某个指标是否披露、是否连续披露、是否可比、是否有原文证据支撑。

### 2. 为什么不是普通 RAG

普通 RAG 只能回答“报告里写了什么”，但 ESG 分析还需要：

- 精确结构化数字；
- PDF 原文证据；
- 缺失值不能当 0；
- 横向对比时要提醒口径；
- 回答里的数字要能回到证据；
- 发现“口号多、数据少”的风险。

所以我把系统设计成 SQL + RAG 双通道，再用 Evaluator 做质量闭环。

### 3. 架构怎么设计

我用 LangGraph 而不是 ReAct。ReAct 的问题是不可控、不可调试、失败后不知道是哪一步错了。LangGraph 可以把 Agent 拆成可观测节点，每个节点有输入输出、耗时、状态和重试逻辑。

核心链路是：

```text
context → supervisor → schema_injector → sql_worker/rag_worker
→ worker_aggregator → evaluator_d → map_reduce → synthesizer
→ disclosure_scorer → greenwashing_detector → evaluator_o → memory_updater
```

其中 SQL 和 RAG 是并行 fan-out，后面 worker_aggregator fan-in。Evaluator-D 如果发现 SQL 空、RAG 低相关、缺失过多，会打回 supervisor re-plan。Evaluator-O 如果发现数字幻觉、实体越界、趋势方向错误，会打回 synthesizer 局部修正。

### 4. 最新改动怎么讲

#### 披露质量评分

我新增了 `agent/disclosure_quality.py` 和 `agent/nodes/disclosure_scorer.py`。它把 ESG 披露质量拆成五个维度：

| 维度 | 权重 | 含义 |
|---|---:|---|
| 完整性 | 30 | 目标公司/年份/指标是否有数据 |
| 连续性 | 20 | 多年问题是否连续披露 |
| 可比性 | 20 | 数据质量、置信度、口径是否一致 |
| 可验证性 | 20 | 是否有 SQL、页码、原文片段 |
| 具体性 | 10 | 是否有量化事实，而不是纯定性 |

输出总分、等级、分项说明和风险信号。

#### 潜在绿漂风险雷达

我新增了 `agent/greenwashing.py` 和 `agent/nodes/greenwashing_detector.py`。我没有声称判断企业真实绿漂，而是把问题收窄成 claim-evidence mismatch：强 ESG 表述附近如果缺少数字、年度进展、同比变化或第三方鉴证，就标成需要人工核查的风险点。

这种设计的优点是可解释、便宜、稳定，而且适合作为第一版。

### 5. 怎么验证

工程上我补了：

- `/api/capabilities`：展示系统覆盖哪些报告、年份、行业和指标；
- 前端节点流式进度：能看到每个节点执行；
- dashboard：看每个节点 p50/p95 延迟；
- tests：覆盖披露质量评分、绿漂检测和能力扫描；
- eval scripts：后续可跑 completion rate、faithfulness、degrade rate。

### 6. 项目亮点总结

这个项目最想体现的不是“我会调 API”，而是：

1. 我能把业务问题抽象成 Agent 工作流；
2. 我知道大模型应用的主要风险是幻觉和不可控；
3. 我用 Evaluator、评分 Rubric、capabilities 边界、trace dashboard 做质量工程；
4. 我能把 ESG 的“长、散、虚、难比”变成可执行的系统设计。

---

## 6. 最新评测结果（2026-08-13）

我用同一个 `DeepSeek-V4-Pro`、同一组 10 条 smoke case 和同一 SQL/RAG 工具条件，对比了 LangGraph 主 Agent 与 ReAct baseline。单纯 completion 两边都是 10/10，因此最终采用综合 **Case Pass Rate**，把 Golden Facts、目标实体/年份覆盖、no-data 安全行为和 clarify 行为纳入主指标：

- LangGraph 主 Agent：**Case Pass 10/10**，Golden Facts **13/13**，平均延迟约 **29.0 秒**，p95 **63.9 秒**；
- ReAct baseline：**Case Pass 6/10**，Golden Facts **6/13**，平均延迟约 **67.6 秒**，p95 **132.2 秒**；
- baseline 的 4 个失败不是 crash，而是两条未覆盖公司带入无关证据、两条结构化事实/目标年份覆盖缺失；
- 外部 Judge 有意关闭，所以我不会把这组小样本结果说成泛化准确率或独立质量分。

这组结果最能说明的不是“模型更聪明”，而是白盒 workflow、覆盖门禁和安全降级能把“完成回答”与“业务上通过”区分开。

## 面试官可能追问 & 回答

### Q1：为什么用 LangGraph，不用一个 ReAct Agent？

ReAct 更像黑盒，失败时很难判断是规划、检索、SQL 还是生成出错。LangGraph 可以把流程拆成节点，每个节点职责明确，方便做 trace、replan、降级和单点优化。

### Q2：披露质量评分为什么不用 LLM judge？

第一版我故意用确定性规则，因为面试项目要可复现、可解释、低成本。LLM judge 可以作为二期，用来判断更复杂的语义证据，但底层 Rubric 仍然应该稳定。

### Q3：绿漂检测会不会误判？

会，所以我不把它叫最终结论，而叫“潜在风险雷达”或“人工核查点”。它只判断强表述附近是否缺少证据，不判断企业真实表现。这个边界很重要。

### Q4：如果数据集没覆盖怎么办？

系统有 coverage gap 处理和 `/api/capabilities` 接口。对未覆盖公司或缺失指标，系统应该明确说当前语料没有证据，而不是编数字。

### Q5：你后续会怎么优化？

我会从三点继续做：第一扩充 greenwashing 评测集；第二引入 LLM judge 做 claim-evidence 语义匹配；第三把 bad case 自动归因到具体节点，形成评测驱动迭代闭环。
