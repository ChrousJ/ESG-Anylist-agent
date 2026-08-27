# ESG-Insight Agent｜秋招王牌项目讲解手册

> 版本：v1.1  
> 更新日期：2026-08-12  
> 用途：这是面试讲解的主文档。先背“一句话”和“3 分钟版本”，再按追问进入架构、评测、难点和边界。

---

## 0. 先记住项目的核心结论

### 一句话定位

**ESG-Insight Agent 是一个面向企业 ESG 报告研究的可评测 Agent：它用 LangGraph 编排 SQL 与 RAG 双路证据，在生成前后设置质量门禁，并把披露质量和潜在绿漂核查点做成可解释的结构化输出。**

### 它不是什么

- 不是上传 PDF 后直接问答的普通 RAG；
- 不是让一个 ReAct Agent 无限自主调用工具；
- 不是企业 ESG 评级系统，也不提供投资建议；
- 不宣称判断企业“真实绿漂”，只识别 claim-evidence mismatch 人工核查点。

### 最值得讲的四个亮点

1. **白盒工作流**：把上下文理解、规划、SQL、RAG、质量检查和生成拆成可追踪节点；
2. **双证据通道**：SQL 支撑数字与趋势，PDF RAG 支撑原因解释和原文引用；
3. **双层质量门禁**：Evaluator-D 在生成前检查证据，Evaluator-O 在生成后检查忠实度；
4. **ESG 业务建模**：用确定性 Rubric 评价披露质量，用规则识别强表述弱证据。

---

## 1. 面试官为什么会对这个项目感兴趣

普通 AI 项目常见问题是“套壳、不可控、没评测、讲不清业务价值”。这个项目对应展示四种能力：

| 面试官关注点 | 项目证据 | 你要表达的能力 |
|---|---|---|
| 是否只会调用 API | LangGraph 条件路由、fan-out/fan-in、修正循环 | Agent 架构设计 |
| 是否理解 RAG 局限 | SQL + RAG 双通道、证据覆盖、缺失降级 | 检索与数据工程 |
| 是否考虑可靠性 | Evaluator-D/O、no-data、trace、project doctor | AI 质量工程 |
| 是否理解业务 | 披露五维评分、绿漂风险边界、ESG data card | 业务抽象能力 |
| 是否可复现 | Makefile、CI、Python 版本约束、单元测试、eval assets | 软件工程能力 |

---

## 2. 30 秒开场

> 我做的是 ESG-Insight Agent，一个面向 ESG 报告分析的可评测智能体。ESG 数据同时包含结构化指标和大量非结构化报告文本，普通 RAG 很难稳定处理跨年数字、缺失值和口径问题。所以我用 LangGraph 把系统拆成 context、supervisor、SQL/RAG worker、双层 evaluator 和 synthesizer 等显式节点。SQL 提供可计算事实，RAG 提供 PDF 原文证据；生成前检查证据质量，生成后检查数字和实体忠实度。除此之外，我还实现了五维披露质量评分和“强表述、弱证据”的潜在绿漂核查雷达。

---

## 3. 3 分钟主讲稿

### 3.1 背景和问题

ESG 报告通常几十到上百页，存在三个难点：

1. 数字分散且跨年、跨公司比较困难；
2. 披露可能缺失或口径不一致，缺失不能当成 0；
3. 报告中有大量“绿色低碳、持续推进”之类定性表达，但不一定有量化进展支撑。

普通 PDF RAG 可以总结“报告写了什么”，但很难可靠回答“数据是多少、能否比较、证据是否充分”。

### 3.2 我的方案

我采用 FastAPI + LangGraph 构建白盒工作流：

```text
context → supervisor → schema_injector
        → SQL worker / RAG worker（并行）
        → worker_aggregator → Evaluator-D
        → map_reduce → synthesizer
        → disclosure_scorer → greenwashing_detector
        → Evaluator-O → memory_updater
```

- `context` 抽取公司、年份、指标和意图；
- `supervisor` 决定走 SQL、RAG 或并行；
- SQL 查询结构化指标，RAG 召回 PDF 原文；
- Evaluator-D 检查数据覆盖、缺失和口径，失败时 re-plan；
- Evaluator-O 检查最终回答中的数字、实体和趋势，失败时打回生成节点修正。

### 3.3 两个业务亮点

第一个是披露质量评分。我没有让 LLM 随意打分，而是设计确定性 Rubric：完整性 30、连续性 20、可比性 20、可验证性 20、具体性 10，输出总分、等级、分项原因和风险标记。

第二个是潜在绿漂风险雷达。它不判断企业是否真的绿漂，而是识别 PDF 片段中的 claim-evidence mismatch：出现“碳中和、行业领先、全面推进”等强表述，但附近没有数字、同比、年度进展或第三方鉴证，就输出人工核查点。

### 3.4 工程闭环

项目还有 SSE 节点进度、trace dashboard、`/api/capabilities` 数据边界接口、离线 eval runner、bad case 产物、`project_doctor.py` 环境自检和 GitHub Actions。我的目标不是让模型看起来无所不能，而是让系统的能力边界、失败原因和质量指标可观察、可复现、可迭代。

---

## 4. 架构深挖：为什么这么设计

### 4.1 为什么不用纯 ReAct

纯 ReAct 的优势是灵活，但在这个场景中有三类风险：

- 工具调用路径不可预测；
- 出错后难定位是规划、SQL、召回还是生成；
- 无法稳定保证“证据检查一定发生在生成前”。

LangGraph 的价值不是“节点越多越高级”，而是把关键质量约束固化为图结构。自主性放在 supervisor 的策略选择里，安全性放在条件边和重试上限里。

### 4.2 为什么 SQL 和 RAG 都需要

| 查询类型 | SQL 的作用 | RAG 的作用 |
|---|---|---|
| 跨年趋势 | 提供连续数值、同比计算 | 提供变化原因和报告解释 |
| 同行对比 | 排序、过滤、归一化 | 检查口径和边界描述 |
| 披露质量 | 判断字段是否连续存在 | 判断是否有页码、原文和鉴证 |
| 绿色承诺 | 辅助核对量化结果 | 定位承诺、目标和进展文本 |

一句话：**SQL 解决“算得准”，RAG 解决“说得有依据”。**

### 4.3 为什么要两个 Evaluator

- Evaluator-D 是输入侧门禁：证据不够就 re-plan 或降级，避免空证据生成；
- Evaluator-O 是输出侧门禁：检查回答有没有超出证据，必要时局部修正。

两者不是重复，而是分别处理“原料是否合格”和“成品是否合格”。

### 4.4 为什么业务评分采用确定性规则

- 可复现：同一输入得到同一分数；
- 可解释：能说明每一分从哪里来；
- 可测试：不调用外部模型也能做单元测试；
- 成本低：适合作为在线链路中的稳定基线。

后续可以引入 LLM judge 处理语义层面的复杂判断，但不能替代底层可解释 Rubric。

---

## 5. 我真正解决过的工程难点

### 难点一：并行 Worker 的状态合并

SQL 和 RAG fan-out 后会同时写 `worker_status`。如果普通字典直接覆盖，会丢失另一条分支结果。项目在 `AgentState` 中为并行字段定义 reducer，将两路状态合并，再由 aggregator 做 fan-in 判断。

### 难点二：缺失数据语义

ESG 场景中以下三件事完全不同：

```text
数值为 0 ≠ 企业未披露 ≠ 当前语料未覆盖
```

系统通过 coverage、missing report、no-data/degraded response 显式区分，避免把缺失解释成表现差或编造数字。

### 难点三：控制修正循环

Evaluator 能提升质量，但也可能造成无限循环。系统对 re-plan 和 synthesis correction 设置独立计数和上限，超限后进入降级路径，保证流程可终止。

### 难点四：评测环境失败不能吞掉结果

评测脚本将依赖导入和运行异常捕获为 case-level `crashed`，仍生成 `metrics.json`、`bad_cases.jsonl` 和 report。新增 `scripts/project_doctor.py` 后，可以在真正跑 Agent 前检查 Python 版本、依赖、数据库、向量库、语料和 LLM 配置。

---

## 6. 如何讲评测，不夸大结果

### 已完成的评测工程

- 10 条 smoke dataset 和 50 条生成式 eval dataset；
- completion、latency、evidence presence、disclosure score presence、greenwashing radar presence；
- case-level result、bad cases、trace summary、Markdown report；
- 确定性模块单元测试；
- CI source-quality 检查。

### 当前必须诚实说明的状态

截至 **2026-08-13**，仓库已在 Python 3.11、完整本地 Hybrid 索引和有效 AntChat 配置下完成 10-case DeepSeek-V4-Pro 在线 smoke：10/10 completion、0 crash、1 degraded、p95 81.208s；6 条需要报告证据的分析 case 均有 RAG evidence，2 条数据集外公司均安全拒绝数值。

但该 run 跳过了 ReAct baseline 和外部 Judge，且 SQLite 业务表仍为空，因此它证明的是 **在线链路稳定性、修正/降级行为和 RAG 证据覆盖**，不是 100% 准确率，也不能证明优于 baseline。

面试推荐表达：

> 我已经跑完 10 条 DeepSeek-V4-Pro 在线 smoke，链路 10/10 完成、没有 crash，6 条证据分析 case 都返回了 RAG 证据；其中一条跨公司 2024 年披露对比因为目标年份证据不足，在两次输出修正后安全降级。这个结果证明 workflow 和质量门禁真实工作，但 baseline、独立 Judge 和结构化 SQL 数据尚未补齐，所以我不会把 completion 包装成准确率。

### 有正式结果后再写入简历的数字

只有 `docs/experiment-results.md` 有可复核 run id 和原始产物后，才写：

- completion rate；
- evidence coverage；
- no-data safe response rate；
- p95 latency；
- evaluator rescue/correction rate；
- baseline 对比提升。

---

## 7. Demo 顺序

### 最稳的 5 分钟版本

1. 打开 README，用 30 秒讲定位；
2. 打开 `/api/capabilities`，讲当前覆盖边界；
3. 输入“比亚迪 2022 到 2024 年碳排放趋势如何”；
4. 指着前端节点进度解释 SQL/RAG 和双 Evaluator；
5. 展示披露评分与风险雷达；
6. 打开 dashboard 或 eval report 收尾。

### 三个推荐问题

```text
比亚迪2022到2024年碳排放趋势如何？
蔚来2024年碳排放表现如何？
华友钴业报告中有哪些缺少数据支撑的绿色承诺？
```

它们分别展示：正常复杂链路、覆盖外安全降级、业务差异化能力。

### Demo 前先执行

```bash
make doctor
make quality
make dev
```

如果 `make doctor` 不是 ready，不要现场硬跑外部 API，改用预录视频、截图、架构和测试产物。

---

## 8. 高频追问和回答

### Q1：这算多智能体还是工作流？

> 更准确地说，它是一个多角色、图编排的 Agent workflow。SQL worker、RAG worker、Evaluator 和 Synthesizer 有独立职责，但共享结构化状态。我不会为了营销强行说是完全自治的 multi-agent；它的重点是可控协作。

### Q2：Evaluator 也用 LLM，怎么保证它不幻觉？

> 我把检查分层：能确定性完成的部分，例如数据覆盖、数字集合、实体边界和披露评分，尽量用程序规则；只有复杂语义判断才使用 LLM。同时 evaluator 有证据输入、结构化输出、重试上限，最终仍通过离线标注集评估，而不是相信模型自评。

### Q3：为什么不用一个大模型直接读整份报告？

> 报告多、上下文长、成本高，而且跨公司计算不稳定。预先结构化高频指标，用 SQL 做精确计算；RAG 只取相关原文，可以降低上下文和幻觉风险。

### Q4：绿漂规则会不会误判？

> 会，所以输出名称是风险雷达和人工核查点。当前规则只说明附近证据弱，不说明公司主观故意或真实表现。后续会通过人工标注集评估 precision/recall，并加入跨段证据匹配。

### Q5：项目最大不足是什么？

> 当前最大不足不是缺少节点，而是正式业务评测和部署复现证据还不够强。我已经补了 eval runner、project doctor 和 CI，下一步优先完成依赖锁定、索引构建、golden evidence 标注和 baseline 消融，而不是继续堆功能。

### Q6：如果重做一次会怎么简化？

> 我会先做 20 条高质量 golden set，再围绕错误类型逐步增加节点。Agent 系统最容易过度设计，所以每个节点都应该对应一个可测量的失败模式。

---

## 9. STAR 讲法

### Situation

ESG 报告长、指标散、缺失和口径问题多，普通 RAG 对数字趋势和证据质量处理不稳定。

### Task

设计一个既能查数字、又能引用原文，并能在证据不足时安全降级的 ESG 研究辅助 Agent。

### Action

用 LangGraph 构建白盒工作流，引入 SQL + RAG 双通道、生成前后双 Evaluator、确定性披露评分、claim-evidence mismatch 风险雷达，并补充 trace、capabilities、eval runner、project doctor 和 CI。

### Result

完成了一个从数据覆盖、Agent 执行、质量门禁到在线评测的端到端原型。10-case 在线 smoke 已验证 100% completion、0 crash 和安全降级行为；但准确率、baseline 提升与 SQL+RAG 双证据效果仍需独立 Judge、人工标注和结构化数据补齐后再对外引用。

---

## 10. 简历建议

### 推荐标题

**ESG-Insight Agent｜基于 LangGraph 的可评测 ESG 报告分析智能体**

### 推荐四条 Bullet

- 基于 FastAPI + LangGraph 构建 ESG 分析 Agent，将 Context、Supervisor、SQL/RAG Worker、Evaluator 和 Synthesizer 拆为显式节点，支持条件路由、并行执行、re-plan、修正循环与安全降级。
- 设计 SQL + PDF RAG 双证据通道：结构化指标用于跨年趋势与同行比较，报告原文用于解释、口径和页码引用，并通过生成前后双 Evaluator 降低证据不足与数字幻觉风险。
- 实现确定性 ESG 披露质量 Rubric，从完整性、连续性、可比性、可验证性和具体性五维输出 0-100 分；构建 claim-evidence mismatch 风险雷达，识别强 ESG 表述的人工核查点。
- 搭建 SSE 节点进度、trace dashboard、capabilities 边界接口、离线 eval/bad-case pipeline、环境自检和 CI，使 Agent 的覆盖范围、失败原因与工程质量可观测、可复现。

**不要在没有正式 run 结果前填写准确率提升百分比。**

---

## 11. 接下来让项目真正成为“王牌”的优先级

### P0：必须完成

1. 用 Python 3.10/3.11 安装完整依赖；
2. 构建 `data/esg_data.db` 和 `data/vector_store/`；
3. [已完成] 配置测试 API key 并运行 10-case 在线 smoke；
4. [已完成] 人工复核 10 条结果并记录唯一 degraded case；
5. 准备 2-3 张截图或 3 分钟录屏。

### P1：拉开差距

1. 标注 30-50 条 golden evidence；
2. 跑 Simple RAG / SQL+RAG / +Evaluator / Full System 消融；
3. 增加 numeric faithfulness 和 no-data safe response 的确定性判分；
4. 固定依赖版本并提供 Docker 启动方式。

### P2：有时间再做

1. 跨段 claim-evidence 匹配；
2. 人工反馈回流和 bad-case clustering；
3. token/cost 统计；
4. 更完善的鉴权、限流和生产部署。

---

## 12. 收尾金句

> 这个项目最重要的不是让 Agent 回答更多，而是让它知道证据从哪里来、什么时候不该回答、失败发生在哪一步，以及如何用评测推动下一轮优化。
