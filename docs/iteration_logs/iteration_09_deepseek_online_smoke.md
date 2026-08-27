# Iteration 09 — DeepSeek-V4-Pro 在线 Smoke Eval 与 Bad-case 审计

> 日期：2026-08-13  
> 历史 Run ID：`20260813_deepseek_smoke_v1`；最终对照 Run ID：`20260813_deepseek_ab_smoke_v1`

## 1. 运行目标与配置

本轮在单条在线 probe 通过后，以相同配置运行完整 10 条 smoke dataset，验证真实在线 completion、Text2SQL、re-plan、RAG 证据、业务节点、Evaluator-O 和延迟指标。

核心配置：

```text
LLM provider: AntChat OpenAI-compatible
Main model: DeepSeek-V4-Pro
Retrieval: local vector + BM25 + RRF
Reranker: disabled
Offline deterministic mode: false
Concurrency: 1
Baseline: skipped
External LLM judge: disabled
```

Judge 关闭是有意的：本轮主要验证链路行为，并避免同一个模型同时生成和自评。Evaluator-O 属于 Agent 内部质量门禁，仍保持开启。

原始产物：

```text
outputs/eval_runs/20260813_deepseek_smoke_v1/
  eval_results.json
  metrics.json
  bad_cases.jsonl
  trace_summary.csv
  eval_report.md
```

## 2. 核心结果

| 指标 | 结果 |
|---|---:|
| Completion Rate | 100.0% (10/10) |
| Strict non-degraded success | 90.0% (9/10) |
| Crashed / Degraded | 0 / 1 |
| Repair-loop Activation | 60.0% (6/10) |
| Tool Error Rate | 0.0 |
| Evidence Presence | 60.0% (6/10) |
| Evidence Presence among evidence-analysis cases | 100.0% (6/6) |
| SQL Evidence Presence | 0.0% |
| RAG Citation Presence | 60.0% (6/10) |
| Disclosure / GW structure presence | 60.0% / 60.0% |
| Avg / p50 / p95 latency | 30.897s / 32.921s / 81.208s |
| Judge | N/A（关闭） |

其中 4 条无证据输出是预期路径，不应计作 evidence miss：

- `SMOKE-003`、`SMOKE-010`：数据集外公司，安全拒绝具体数值；
- `SMOKE-004`：知识类快速出口；
- `SMOKE-009`：信息不足，返回澄清问题。

因此，全量 evidence presence 为 60%，而 6 条需要报告证据的分析 case 实际为 6/6。

## 3. 链路结论

### 已验证通过

- DeepSeek-V4-Pro 能完成在线 Text2SQL、报告生成和 Evaluator-O 数字检查；
- 本地 `vector + BM25 + RRF` 可与在线生成链路组合运行；
- 10 条均无 crash，tool error rate 为 0；
- `SMOKE-003`、`SMOKE-010` 对未覆盖实体未编造数值，no-data safe response 为 2/2；
- `SMOKE-009` 正确进入 clarify 快速出口；
- 6 条触发 re-plan 或输出修正，说明 repair loop 在真实在线调用中被实际执行。

### 必须保留的边界

本轮是 **online workflow smoke / behavior eval**，不是准确率基准：

1. Baseline 被跳过，不能声称优于 ReAct；
2. Judge 被关闭，`Evaluator-O=pass` 不能替代独立模型或人工评分；
3. SQLite 业务表当前均为 0 行，故 SQL evidence presence 为 0%，所谓“SQL + RAG 双证据”尚未在本轮得到业务数据层面的验证；
4. 目前只有 evidence presence，没有 golden evidence relevance、numeric faithfulness 或 entity consistency 的独立标注；
5. 平均披露分 24.75 主要反映当前证据/结构化数据覆盖，不适合解释为企业真实 ESG 水平。

因此可对外引用的是：**10-case 在线链路 100% 完成、0 crash、1 degraded、p95 81.208s、预期证据分析 case 6/6 有 RAG 证据**。不能引用“准确率 100%”或“优于 baseline”。

## 4. Bad Case：SMOKE-005

问题：

```text
对比比亚迪和长城汽车2024年碳排放披露质量
```

结果：

```text
status=degraded
latency=81.208s
replan_count=1
eval_o_retry=2
node_count=26
```

### 直接现象

- 比亚迪命中了 2024 年报告，但部分 top chunk 与碳披露比较不相关（客户服务、产品质量、废铝回收）；
- 长城汽车只命中 2023 年报告，没有命中请求所需的 2024 年报告；
- SQL 结果为空，无法补充结构化碳指标；
- Synthesizer 生成后连续经过 2 次 Evaluator-O 修正仍未通过，最终返回安全 degraded response；
- 该 case 同时贡献了全局 p95 / max latency 81.208s。

### 根因归因

主要根因是 **目标年份/实体证据覆盖不足 + 检索相关性不足**，不是 crash：

1. multi-company compare 没有执行“每个目标公司 × 目标年份至少一条有效证据”的硬覆盖门禁；
2. 检索结果允许以长城汽车 2023 年证据替代 2024 年目标；
3. query variants 对“披露质量”扩展过宽，召回客户服务、产品质量等低相关 chunk；
4. SQL 表为空，使结构化 worker 无法提供第二证据通道；
5. Evaluator-O 能阻止不可靠答案直接通过，但重复 synthesis 使尾延迟升高。

### 建议修复优先级

- **P0**：Evaluator-D 增加 compare coverage matrix，逐项检查 `company × year × metric/topic`；缺项时先定向补检索，仍缺失则明确 partial compare；
- **P0**：RAG worker 对显式年份启用严格 year filter，禁止静默使用相邻年份替代；
- **P1**：为“碳排放披露质量”增加专用 query rewrite 和页码/表格关键词，降低客户服务、产品质量等噪声；
- **P1**：在 eval 结果中保存 evaluator_o 的具体失败 reason，避免 bad case 只能标成泛化的 `Runtime/Degradation`；
- **P1**：填充结构化 SQLite 指标表，再验证 SQL evidence 与跨年/跨公司比较；
- **P2**：对 correction loop 设置基于失败类型的早停；证据缺失时不要重复纯文本改写。

## 5. 性能观察

节点均值显示主要成本来自：

| 节点 | 平均延迟 |
|---|---:|
| synthesizer | 27.075s |
| sql_worker | 6.870s |
| evaluator_d | 5.941s（仅 2 次在线检查） |
| context | 4.965s（LLM 快速出口场景） |
| evaluator_o | 1.557s |
| rag_worker | 0.686s |

本轮关闭本地 reranker 后，RAG 本身不再是主要瓶颈；在线 Synthesizer 和失败后的重复 correction 是尾延迟重点。

## 6. 下一步

- [x] 完成 10 条在线 Smoke Eval；
- [x] 人工审计 metrics、逐条路径和唯一 degraded case；
- [x] 修正报告中 baseline/Judge 跳过时显示 0 的误导表述；
- [ ] 为 compare case 增加 company-year coverage gate；
- [ ] 将 Evaluator-O failure reasons 写入 eval result / bad_cases；
- [ ] 填充 SQLite 结构化指标，重跑 SQL+RAG 在线 smoke；
- [ ] 使用独立 Judge 或人工 rubric 复核 numeric faithfulness、entity consistency 和 evidence relevance；
- [ ] 再进行 baseline / evaluator ablation，之后才形成效果对比结论。


## 7. 最终对照更新（2026-08-13）

随后完成同一 `DeepSeek-V4-Pro` 下的主 Agent / ReAct baseline 10×2 对照，使用 `outputs/eval_runs/20260813_deepseek_ab_smoke_v1/` 产物。为避免 completion 掩盖业务失败，最终主指标升级为 Case Pass Rate：主 Agent **10/10（100%）**，baseline **6/10（60%）**；Golden Facts **13/13 vs 6/13**；平均延迟 **29.007s vs 67.586s**，p95 **63.870s vs 132.226s**。Judge 仍关闭，结论仅限该 10-case smoke。

本文件前述 `20260813_deepseek_smoke_v1` 的 1 degraded 结果属于未补齐结构化评测前的历史链路 smoke；对外展示和最终报告以 `experiment-results.md` 第 2.4 节为准。
