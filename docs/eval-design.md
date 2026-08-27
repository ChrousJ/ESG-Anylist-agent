# ESG-Insight Agent 评测体系设计

> 目的：将项目核心能力转化为可量化、可回归、可归因的评测体系。  
> 当前版本：v0.2  
> 更新日期：2026-08-09

---

## 1. 评测定位

本项目的评测目标不是简单判断“回答好不好”，而是回答四个问题：

1. Agent 有没有完成任务？
2. 关键结论有没有证据？
3. 数字、实体、趋势是否可信？
4. 如果失败，失败原因出在哪个环节？

对应 AI Evaluation 能力：

- 长文本信息提取评测；
- Agentic Workflow 多步任务评测；
- LLM-as-a-Judge；
- Bad Case 归因；
- 评测发现问题 -> 数据 / 系统修复 -> 回归验证的数据飞轮。

---

## 2. 评测集设计

### 2.1 数据范围

第一版聚焦新能源汽车产业链，原因是 ESG 相关性强，横向可比价值高，且现有数据较充分。

候选公司：

- 比亚迪；
- 宁德时代；
- 长城汽车；
- 长安汽车；
- 广汽集团；
- 亿纬锂能；
- 国轩高科；
- 华友钴业。

年份范围：2022-2024。

### 2.2 任务类型

| task_type | 说明 | 示例 |
|---|---|---|
| fact_extraction | 单指标事实查询 | 比亚迪 2024 年温室气体排放量是多少？ |
| trend_analysis | 跨年趋势分析 | 宁德时代 2022-2024 年能源消耗趋势如何？ |
| peer_comparison | 同行横向对比 | 对比比亚迪和长城汽车 2024 年碳排放披露情况。 |
| disclosure_quality | 披露质量判断 | 广汽集团是否连续披露员工培训指标？ |
| greenwashing_risk | 绿漂风险识别 | 华友钴业报告中有哪些缺少数据支撑的绿色承诺？ |

### 2.3 JSONL Schema

```json
{
  "id": "car_disclosure_001",
  "task_type": "disclosure_quality",
  "query": "对比比亚迪和长城汽车2024年碳排放披露质量",
  "companies": ["比亚迪", "长城汽车"],
  "years": [2024],
  "metrics": ["carbon_emission"],
  "expected_evidence": [
    {
      "company": "比亚迪",
      "year": 2024,
      "metric": "carbon_emission",
      "value": "待标注",
      "unit": "待标注",
      "page": "待标注"
    }
  ],
  "judge_rubric": {
    "must_include": ["是否披露", "口径是否一致", "是否可比", "原文证据"],
    "must_not_include": ["无依据投资建议", "未出现来源的数字"]
  },
  "difficulty": "medium"
}
```

---

## 3. 核心评测指标

### 3.1 Completion Rate

任务是否成功完成。

```text
completion_rate = 成功返回结构化答案的样本数 / 总样本数
```

失败包括：异常报错、空回答、无关回答、提前降级。

### 3.2 Evidence Coverage

关键结论是否有证据支持。

```text
evidence_coverage = 有来源支撑的关键结论数 / 关键结论总数
```

证据可以来自：

- SQL 结构化查询结果；
- RAG 检索段落；
- 报告页码和原文引用。

### 3.3 Numeric Faithfulness

回答中出现的数字是否能在证据中找到。

```text
numeric_faithfulness = 可追溯数字数 / 回答中数字总数
```

需要支持单位归一化，例如：

- 万元 / 亿元；
- tCO2e / 万吨二氧化碳当量；
- 百分比 / 小数。

### 3.4 Entity Consistency

回答是否只讨论用户请求的公司、年份、指标。

```text
entity_consistency = 实体一致样本数 / 总样本数
```

需要注意：政策目标年份如“2030 年碳达峰”不应被误判为越界年份。

### 3.5 Disclosure Judgment Accuracy

披露质量判断是否正确。

判断标签包括：

- disclosed：已披露；
- not_disclosed：未披露；
- partially_disclosed：部分披露；
- incomparable：口径不可比。

### 3.6 Greenwashing Risk Recall

绿漂风险识别是否覆盖标注风险点。

```text
greenwashing_risk_recall = 识别出的有效风险点 / 标注风险点
```

第一版可先人工抽样复核，后续再引入 LLM-as-a-Judge。

### 3.7 Business Signal Presence

用于确认新增业务节点是否稳定产出结构化结果。

```text
disclosure_score_presence = 输出 disclosure_quality 的样本数 / 总样本数
greenwashing_radar_presence = 输出 greenwashing_risks 的样本数 / 总样本数
evidence_presence = 至少带有 SQL 或 RAG 证据的样本数 / 总样本数
```

这些指标不等价于准确率，但能帮助判断：

- 新增节点是否被正确接入主流程；
- 前端和 API 是否能稳定消费结构化字段；
- 哪些 task_type 没有产出业务信号，需要进一步定位。

---

## 4. 工程指标

| 指标 | 说明 | 价值 |
|---|---|---|
| average latency | 平均端到端耗时 | 用户体验 |
| p95 latency | 95 分位耗时 | 稳定性 |
| average token cost | 平均 token 消耗 | 成本控制 |
| replan rate | evaluator_d 打回 supervisor 的比例 | 数据获取质量 |
| correction rate | evaluator_o 打回 synthesizer 的比例 | 输出质量 |
| degrade rate | 降级输出比例 | 可靠性 |
| SQL success rate | SQL worker 成功率 | 结构化查询能力 |
| RAG recall success rate | RAG worker 成功召回率 | 长文本检索能力 |

---

## 5. Bad Case 归因体系

| 错误类型 | 判定线索 | 修复方向 |
|---|---|---|
| Intent Error | context 解析意图与 query 不一致 | 优化 context prompt / 意图标签 |
| Entity Error | 公司、年份、指标抽取错误 | 实体词典、别名表、规则校验 |
| SQL Error | SQL 执行失败或返回空 | schema 注入、SQL validator |
| RAG Recall Error | top chunks 无关或缺少目标页 | query rewrite、chunking、rerank |
| Evidence Missing | 结论无 citation | synthesizer 强制引用 |
| Numeric Hallucination | 数字无法在证据中匹配 | evaluator_o 数字校验 |
| Trend Error | 趋势判断与数据方向不一致 | 趋势计算工具 |
| Judge Error | 评测器误判 | 调整 rubric / 人工复核 |

---

## 6. 评测输出

每次运行评测生成：

```text
outputs/eval_runs/{run_id}/
  eval_report.md       # 人类可读报告
  eval_results.json    # 完整原始结果
  metrics.json         # 聚合指标与分类型指标
  bad_cases.jsonl      # 失败 / 降级样本和归因
  trace_summary.csv    # 节点耗时、状态、重试次数
```

`bad_cases.jsonl` 示例：

```json
{
  "case_id": "car_trend_012",
  "pass": false,
  "primary_error": "RAG Recall Error",
  "secondary_error": "Evidence Missing",
  "root_cause": "query rewrite 未包含'温室气体排放'同义词，导致召回绿色项目案例而非排放数据页",
  "suggested_fix": "为 carbon_emission 指标加入同义词：温室气体、碳排放、GHG、Scope 1、Scope 2",
  "hard_case": true
}
```

---

## 7. 实验设计

### 7.1 Baseline 与消融实验

| 实验组 | 说明 |
|---|---|
| ReAct Baseline | 原始黑盒 ReAct Agent |
| LangGraph without Evaluator | 白盒 workflow，但关闭 evaluator |
| LangGraph + Evaluator-D | 加数据质量评估 |
| LangGraph + Evaluator-D + Evaluator-O | 加输出质量评估 |
| Full System | 加披露质量评分和绿漂识别 |

### 7.2 预期证明点

不是证明“我的 Agent 最强”，而是证明：

> 在 ESG 报告分析这种长文本、非结构化、口径不一致的任务中，白盒 workflow + 双层 evaluator 能提升事实性、证据覆盖和披露判断准确率；代价是一定的延迟和 token 成本增加。

---

## 8. 评测驱动迭代闭环

```text
运行评测
  ↓
生成指标和 bad_cases
  ↓
自动 / 半自动归因
  ↓
提出修复方案
  ↓
修改 Agent 节点、检索策略、词典或 rubric
  ↓
回归评测
  ↓
记录 iteration log
```

每一轮迭代必须留下：

- 本轮目标；
- 修改内容；
- 指标变化；
- bad case 变化；
- 未解决问题；
- 下一步计划。
