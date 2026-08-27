# ESG-Insight Agent 简历素材

> 用途：把项目压缩成简历和自我介绍能直接使用的表达。  
> 更新日期：2026-08-13

---

## 1. 推荐项目标题

**ESG-Insight Agent：基于 LangGraph 的可评测 ESG 报告分析智能体**

如果简历空间较短，可以写：

**ESG-Insight Agent｜可评测 ESG 报告分析 Agent**

---

## 2. 一句话项目简介

基于 FastAPI + LangGraph 构建面向 ESG 报告分析的多节点 Agent，将企业可持续发展报告中的结构化指标和 PDF 原文证据结合起来，支持事实抽取、趋势分析、披露质量评分、潜在绿漂风险识别和节点级可观测评测。

---

## 3. 简历 Bullet 版本 A：AI Agent / 应用开发方向

- 基于 **FastAPI + LangGraph** 构建 ESG 报告分析 Agent，将黑盒 ReAct 流程重构为 `context / supervisor / SQL worker / RAG worker / evaluator / synthesizer` 等显式节点，支持节点级 trace、条件路由、re-plan 和降级响应。
- 设计 **SQL + RAG 双通道证据获取机制**：通过 Text2SQL 查询 SQLite ESG 指标库，并从企业 PDF 报告中检索原文证据，用结构化数据支撑数字计算，用非结构化文本支撑原因解释和页码引用。
- 实现 **Evaluator-D / Evaluator-O 双层质量保障**：生成前检查数据覆盖、证据相关性和缺失情况，生成后检查数字事实性、实体一致性和趋势方向，降低长文本 ESG 分析中的幻觉风险。
- 新增 **披露质量评分与绿漂风险雷达**：基于完整性、连续性、可比性、可验证性、具体性五维 Rubric 输出可解释评分，并识别“强 ESG 表述但缺少量化证据”的人工核查点。
- 搭建前端 Demo 与可观测 Dashboard，通过 SSE 展示 Agent 节点执行进度，并提供 `/api/capabilities` 暴露数据覆盖边界，避免系统在未覆盖场景下编造答案。

---

## 4.1 有数据支撑的评测结果 Bullet（按需使用）

- 在同一 `DeepSeek-V4-Pro`、同一 SQL/RAG 工具条件下完成 10-case 主 Agent / ReAct 对照：以 Golden Facts、目标覆盖、no-data 安全和 clarify 行为组成 Case Pass Rate，主 Agent **10/10 vs baseline 6/10**，结构化 Golden Facts **13/13 vs 6/13**，平均延迟约 **29s vs 68s**。

> 这条只适合在简历允许写实验条件和样本量时使用；不要省略“10-case smoke”或把它写成泛化准确率。

## 4. 简历 Bullet 版本 B：AI Evaluation / 数据质量方向

- 设计 ESG Agent 评测体系，覆盖事实抽取、趋势分析、同行对比、披露质量判断和潜在绿漂风险识别等任务类型，并沉淀 Completion、Evidence Coverage、Numeric Faithfulness、Entity Consistency 等指标。
- 将 Agent 失败归因拆解为 Intent Error、Entity Error、SQL Error、RAG Recall Error、Evidence Missing、Numeric Hallucination、Trend Error 等类型，用于驱动后续 bad case 修复。
- 引入生成前 Evaluator-D 和生成后 Evaluator-O，将质量控制前置到证据检查阶段，并在最终回答前进行忠实度和一致性校验。
- 对披露质量评分采用确定性 Rubric 而不是纯 LLM 主观打分，使评分逻辑可解释、可复现、可单元测试，并能接入离线评测统计。
- 对绿漂风险识别采用 claim-evidence mismatch 定义，输出人工核查点而非企业定性判断，兼顾可解释性、合规边界和第一版工程可落地性。

---

## 5. 简历 Bullet 版本 C：金融科技 / ESG 场景方向

- 面向投资研究和 ESG 分析场景，构建企业 ESG 报告智能分析系统，解决报告篇幅长、披露不完整、口径不一致、潜在绿漂风险难以人工批量识别的问题。
- 将 ESG 分析任务拆解为结构化指标查询、PDF 原文证据检索、跨年趋势分析、横向可比性判断、披露质量评分和风险提示等子能力。
- 针对 ESG 缺失数据设计安全边界：区分“未披露”“当前数据未覆盖”和“数值为 0”，并在证据不足时触发降级响应，避免误导性结论。
- 在前端展示 disclosure score 和 greenwashing radar，使系统输出从单纯问答升级为可解释的研究辅助报告。

---

## 6. 面试开场 30 秒

> 我做的是一个 ESG 报告分析 Agent，不是普通 PDF 问答。因为 ESG 报告里既有结构化数字，比如碳排放、绿色贷款，也有大量非结构化描述，比如减排措施和绿色承诺。我用 LangGraph 把 Agent 拆成 SQL worker、RAG worker、双层 evaluator、synthesizer、披露评分和绿漂风险识别等节点。这样系统既能回答趋势和对比问题，也能告诉用户证据是否充分、披露质量如何、哪些强 ESG 表述可能需要人工核查。

---

## 7. STAR 讲法

### Situation

ESG 报告通常有几十到上百页，指标分散、口径不一致、官话多，人工分析效率低。普通 RAG 可以总结文本，但很难稳定处理数字事实、跨年趋势和证据缺失。

### Task

我希望做一个能用于投资研究辅助的 ESG Agent，不只回答问题，还要能解释证据来源、识别披露短板，并且可以被评测和迭代。

### Action

我用 LangGraph 重构工作流，引入 SQL + RAG 双通道证据获取，增加 Evaluator-D / Evaluator-O 双层质量保障，并实现披露质量评分和潜在绿漂风险雷达。同时补充 capabilities endpoint、前端可视化和单元测试。

### Result

项目从一个普通 ESG 问答 Demo 升级为可观测、可降级、可评测的 ESG 分析 Agent。面试展示时可以清楚讲出架构设计、业务抽象、质量保障、风险边界和后续评测路线。

---

## 8. 不建议在简历中夸大的点

不要写：

- “准确识别企业绿漂”；
- “达到生产级 ESG 评级”；
- “完全替代 ESG 分析师”；
- “覆盖全市场所有公司”；
- “给出投资建议”。

建议写：

- “识别潜在绿漂人工核查点”；
- “辅助 ESG 报告分析”；
- “基于当前数据覆盖范围回答”；
- “在证据不足时降级而非编造”。
