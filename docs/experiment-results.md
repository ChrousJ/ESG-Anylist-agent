# ESG-Insight Agent 实验结果

> 用途：集中记录评测运行结果、消融实验、bad case 和后续优化。  
> 更新日期：2026-08-28

---

## 1. 当前状态

当前项目已经具备评测设计和评测脚本。2026-08-09 已完成评测脚本结构升级，并实际跑过一次 smoke eval。早期该次运行由于本机缺少 `langgraph` 依赖，结果不能代表模型业务效果；它验证了评测脚本即使遇到依赖缺失也会生成 metrics、bad cases 和 report，便于复盘。当前验收以 Python 3.11 完整依赖环境的结果为准。

已有能力：

- `scripts/generate_eval_dataset.py`：生成 50 条离线评测样本，覆盖 disclosure_quality / greenwashing_risk；
- `scripts/run_evaluation.py`：运行 Agent 评测，输出 metrics、bad cases、trace summary 和 report；
- `tests/`：覆盖 capabilities、披露评分、绿漂风险等确定性模块；
- `docs/eval-design.md`：定义评测指标和 bad case 归因体系。

当前已验证的轻量测试：

```bash
python3 -m compileall agent api scripts tests
python3 -m unittest discover -s tests
```

实际结果：

```text
Ran 6 tests in 0.002s
OK
```

---

## 2. 正式评测命令

建议在模型 API 和数据都准备好后执行：

```bash
# 快速 smoke eval：10 条，适合面试前验证链路
python scripts/run_evaluation.py -i eval/datasets/esg_eval_smoke.jsonl --skip-baseline --skip-judge --concurrency 1 --run-id <run_id>

# 完整 eval：50 条
python scripts/generate_eval_dataset.py -o eval_dataset.jsonl
python scripts/run_evaluation.py -i eval_dataset.jsonl --skip-baseline --concurrency 1 --run-id 20260809_full_v1
```

如果要做 baseline 对比：

```bash
python scripts/run_evaluation.py --concurrency 1
```

建议把结果保存到：

```text
outputs/eval_runs/{run_id}/
  eval_report.md
  metrics.json
  bad_cases.jsonl
  trace_summary.csv
```

run_id 示例：

```text
20260809_2100_full_system_v1
```

---

## 2.1 历史 smoke eval：20260809_smoke_v1（环境失败样本）

运行命令：

```bash
/usr/bin/python3 scripts/run_evaluation.py \
  -i eval/datasets/esg_eval_smoke.jsonl \
  --skip-baseline \
  --skip-judge \
  --concurrency 1 \
  --delay 0 \
  --run-id <run_id>
```

输出目录：

```text
outputs/eval_runs/<run_id>/
  eval_results.json
  metrics.json
  bad_cases.jsonl
  trace_summary.csv
  eval_report.md
```

真实结果摘要：

| 指标 | 值 | 解释 |
|---|---:|---|
| Total cases | 10 | smoke dataset 全量运行 |
| Completion Rate | 0.0% | 不是业务能力结果，原因是运行环境缺少依赖 |
| Crashed | 10 | 所有 case 均为同一个 runtime dependency 错误 |
| p95 Latency | 3ms | 依赖导入阶段即失败 |
| Primary error | `No module named 'langgraph'` | 需要安装项目运行依赖后重跑 |

这次运行的意义：

- 验证了 `outputs/eval_runs/{run_id}/` 目录规范；
- 验证了 `metrics.json` / `bad_cases.jsonl` / `eval_report.md` 能稳定产出；
- 暴露出环境依赖问题，并推动 `scripts/run_evaluation.py` 修复为“依赖缺失也不中断整个评测”的健壮模式。

面试中不要把这组 0% completion 当作模型效果指标引用；它只适合作为 **evaluation robustness / bad case audit** 的工程例子。正式展示请使用已完成的 `20260813_deepseek_ab_smoke_v1` 对照结果或对应离线结构化评测。

---

## 2.2 离线链路评测：20260813_offline_bm25_v2

2026-08-13，在 Python 3.11 完整依赖环境中，从 90 份本地 PDF 构建了 25,041 个 chunk 的 BM25 索引，并运行 10 条 smoke eval。

| 指标 | 结果 |
|---|---:|
| Completion Rate | 100.0% (10/10) |
| Crashed / Degraded | 0 / 0 |
| Rescue Rate | 50.0% |
| Evidence Presence | 60.0% |
| Disclosure Score Presence | 60.0% |
| Greenwashing Radar Presence | 60.0% |
| p50 / p95 Latency | 67ms / 7048ms |
| SQL Evidence Presence | 0.0% |

运行模式：BM25-only、无向量模型、无外部 LLM、无 judge。该结果证明离线工作流和安全降级可运行，**不能作为完整模型准确率或语义质量结论**。其中 6 条属于本地证据分析 case，另外 4 条为 knowledge / clarify / coverage-gap，因此 60% presence 符合该 smoke 集的路径结构。

原始产物位于：

```text
outputs/eval_runs/20260813_offline_bm25_v2/
```

---

## 2.3 Hybrid RAG 离线评测：20260813_offline_hybrid_bge_v1

在 ModelScope 下载本地 BGE embedding/reranker 后，构建了包含 25,041 个 chunk 的 ChromaDB，并验证 `vector + BM25 + RRF + reranker` 完整检索链路。

| 指标 | 结果 |
|---|---:|
| Completion / Crash | 100% / 0 |
| Evidence Presence | 60%（6 条证据分析 case 全覆盖） |
| Rescue Rate | 50% |
| p50 / p95 Latency | 10.249s / 97.795s |

Hybrid 检索质量链路可用，但 `bge-reranker-v2-m3` 在 CPU 上对多个 query variants 逐个重排，尾延迟过高。该结果用于定位性能瓶颈，不应作为在线 DeepSeek 生成质量指标。

---

## 2.4 DeepSeek-V4-Pro 在线 10×2 对照：`20260813_deepseek_ab_smoke_v1`

2026-08-13 在 AntChat OpenAI-compatible provider 上，使用同一 `DeepSeek-V4-Pro`、同一 10 条 smoke dataset、同一 SQL/RAG 工具条件，完成主 Agent 与 ReAct baseline 的对照。主 Agent 保留 Evaluator-D/O、re-plan、披露质量评分和绿漂风险雷达；baseline 不含这些质量环路。外部 Judge 两边均关闭，避免同一生成模型自评。

| 指标 | LangGraph 主 Agent | ReAct baseline |
|---|---:|---:|
| Completion Rate | 100.0% (10/10) | 100.0% (10/10) |
| **Case Pass Rate** | **100.0% (10/10)** | **60.0% (6/10)** |
| Golden Structured Facts | **13/13 (100.0%)** | **6/13 (46.2%)** |
| Required Evidence Coverage | 100.0% (6/6) | 100.0% (6/6) |
| No-data Safe Response Rate | **100.0%** | **0.0%** |
| Clarify Success Rate | 100.0% | 100.0% |
| Avg Entity Evidence Precision | **100.0%** | **30.9%** |
| Packaged Numeric Support | **96.7%** | **65.4%** |
| Avg / p50 / p95 Latency | **29.007s / 25.922s / 63.870s** | 67.586s / 61.218s / 132.226s |
| Disclosure Score Presence | 60.0% (6/10) | N/A |
| Greenwashing Radar Presence | 60.0% (6/10) | N/A |
| Crash / Degraded | 0 / 0 | 0 / N/A |
| External Judge | N/A（关闭） | N/A（关闭） |

**Case Pass Rate 是本项目最终主指标**：它综合 expected behavior、Golden Facts、目标实体/年份覆盖、no-data 安全行为和 clarify 行为；单纯 completion 不能掩盖“完成但答错/污染证据”的质量失败。主 Agent 10/10，baseline 6/10。该结论仅适用于这组 10-case smoke，不是泛化 benchmark 或统计显著性结论。

### 可复核结论与边界

- 同模型、同工具条件下，主 Agent 的 case pass rate 高于 baseline（10/10 vs 6/10），且平均延迟约低 57.1%。这是小样本工程对照，不声称普适性能提升。
- 主 Agent 命中 13/13 条结构化 Golden Facts；baseline 命中 6/13。Golden Facts 是项目本地标注事实，不等同于独立人工质量评分。
- 主 Agent 对 2 条数据集外公司均安全拒绝具体数值；baseline 在这些 case 中带入无关公司证据，因此 no-data safe response 为 0%。
- 证据分析 case 为 6/6 覆盖；全量 evidence presence 为 60% 是因为 knowledge、clarify 和 coverage-gap 路径按设计不需要证据。
- Judge 关闭，因此 numeric faithfulness / evidence relevance 的独立 Judge 分数为 N/A；对外应引用 Golden Facts、coverage 和安全行为，并明确这一限制。

原始产物：

```text
outputs/eval_runs/20260813_deepseek_ab_smoke_v1/
  eval_results.json  metrics.json  bad_cases.jsonl
  trace_summary.csv  eval_report.md
```

逐条 bad case 审计：baseline 的 4 个失败均为非 crash 质量失败：2 条 no-data 证据污染，2 条 SQL Golden Facts/目标覆盖缺失。主 Agent 10 条均通过。此前 `20260813_deepseek_smoke_v1` 是未包含 baseline/结构化 Golden Facts 对照的历史链路 smoke，应以本节作为当前对外口径。

## 2.5 Python 3.11 离线完整 50-case：`20260813_offline_full50_v3`

为避免把未覆盖公司和网络失败混入质量结论，先生成固定的 50-case 数据集，并在 `OFFLINE_DETERMINISTIC_MODE=true`、BM25-only、无 reranker、无 baseline、无 Judge 条件下运行。期间修正了两个评测基础问题：

- “近三年”固定解释为项目语料窗口 **2022、2023、2024**，不再依赖运行机器的当前年份；
- 离线模式下知识问题优先于指标关键词，且 Evaluator-D 口径检查不再尝试外部 LLM。
- 完整集中的公司名统一为当前 90 份 PDF 的 canonical names；Tesla、蔚来、小鹏等仍保留为覆盖边界/缺失场景。

| 指标 | 结果 |
|---|---:|
| Completion Rate | **100.0% (50/50)** |
| Strict Success Rate | 94.0% |
| Case Pass Rate | 68.0% (34/50) |
| Expected Class Accuracy | 90.0% |
| Required Evidence Coverage | 96.7% (29/30) |
| No-data Safe Response Rate | 20.0% |
| Clarify Success Rate | 50.0% |
| Avg Target Entity-Year Coverage | 98.0% |
| Evidence / SQL / RAG Presence | 74.0% / 32.0% / 72.0% |
| Disclosure / Greenwashing Output Presence | 72.0% / 72.0% |
| Rescue / Degraded / Crash | 32.0% / 3 cases / 0 cases |
| Avg / p50 / p95 Latency | 359ms / 200ms / 756ms |
| Judge | N/A（关闭） |

**解读**：这轮是离线 workflow regression，不是在线模型质量 benchmark。Case Pass 受缺失/降级和 clarify 负向样本影响；trend、disclosure_quality、greenwashing_risk 三类的 case pass 均为 100%，compare 为 70%，missing_degradation 为 20%，clarify 为 50%。该结果暴露出下一轮最值得修复的是 no-data response 污染、跨公司目标覆盖和 clarify/knowledge 边界，而不是继续堆检索组件。

原始产物：

```text
eval/datasets/esg_eval_full_50.jsonl
outputs/eval_runs/20260813_offline_full50_v3/
  eval_results.json  metrics.json  bad_cases.jsonl
  trace_summary.csv  eval_report.md
```

## 2.6 离线 50-case v5：澄清与 no-data 安全回归

在 v4 基础上补充了安全回归：

- 业务实体不足时，离线 context 现在显式写入 `query_class=clarify`，不再保留 `complex + need_clarify=true` 的不一致状态；
- knowledge case（包括范围一/二/三定义问题）继续走知识快速出口；
- terminal no-data 响应会清空遗留的 SQL/RAG sources，避免把其他公司的证据随 coverage-gap 文案返回；
- 新增 context routing 与 no-data evidence 清理单测。

| 指标 | v4 | v5 |
|---|---:|---:|
| Completion Rate | 100.0% | **100.0%** |
| Case Pass Rate | 78.0% | **78.0%** |
| Expected Class Accuracy | 98.0% | **98.0%** |
| Clarify Success Rate | 100.0% | **100.0%** |
| No-data Safe Response Rate | 20.0% | 20.0% |
| Degraded | 2 | 2 |
| Crashed | 0 | **0** |
| p95 Latency | 681ms | 733ms |

结果表明 clarify/knowledge 路由问题已关闭；剩余 no-data 低分来自评测集中部分“已覆盖公司但指标缺失”和行业宽查询，它们仍会生成带部分 RAG 的分析结果，尚未统一进入终态 no-data response。下一步应将“全量目标无有效指标值”与“部分缺失但有可用数据”分开判定：前者清空证据并安全拒答，后者保留证据并返回 partial disclosure。

原始产物：`outputs/eval_runs/20260813_offline_full50_v5/`。

## 3. 已完成评测汇总

| 评测 | 状态 | 可引用结论 |
|---|---|---|
| 离线确定性 structured smoke | 已完成 | 10/10 completion，Golden/路径指标可复现 |
| 本地 BM25 / Hybrid RAG | 已完成 | 本地检索与降级链路可运行；CPU reranker 尾延迟较高 |
| DeepSeek 在线单条 probe | 已完成 | 主 Agent 与 baseline 均可完成在线调用 |
| DeepSeek 10×2 对照 | 已完成 | 主 Agent Case Pass 10/10，baseline 6/10；详见 2.4 |
| API/readiness/全量单测 | 已完成 | runtime readiness ready；21 tests passed |
| 独立 LLM Judge | 未运行 | 当前结论明确标记 N/A，不冒充独立质量分 |
| 60–100 条正式 benchmark | 未运行 | smoke 结果不外推到大样本准确率 |

## 4. 当前业务信号（主 Agent，10-case smoke）

| 指标 | 当前值 | 解释 |
|---|---:|---|
| Case Pass Rate | 100.0% (10/10) | 最终综合质量指标，仅限本 smoke |
| Golden Fact Accuracy | 100.0% (13/13) | 本地结构化标注事实命中 |
| Required Evidence Coverage | 100.0% (6/6) | 需要证据的分析 case 全覆盖 |
| No-data Safe Response | 100.0% | 未覆盖公司不编造具体数值 |
| Disclosure Score Presence | 60.0% (6/10) | 仅对证据分析路径输出 |
| Greenwashing Radar Presence | 60.0% (6/10) | 仅对报告分析路径输出 |
| p95 Latency | 63.870s | 在线 smoke，串行 concurrency=1 |

## 5. Bad Case 记录

失败样例和建议修复会写入每次运行目录的 `bad_cases.jsonl`。当前对照中 baseline 的失败归因包括：`Unsafe No-data Behavior`、`Structured Evidence Missing`、`Evidence Target Coverage`；主 Agent 没有 case-level failure。

## 6. 面试中如何讲当前实验状态

> 我用同一个 DeepSeek-V4-Pro 和同一组 10 条 ESG smoke case 做了主 Agent 与 ReAct baseline 对照。单纯 completion 两边都是 10/10，所以我把主指标升级为综合 Golden Facts、目标覆盖、no-data 安全和 clarify 行为的 Case Pass Rate：主 Agent 10/10，baseline 6/10；主 Agent 命中 13/13 条本地 Golden Facts，平均延迟约 29 秒，baseline 约 68 秒。Judge 没有运行，因此我不会把这个小样本结果包装成泛化准确率，而是把它作为 workflow 质量工程的可复核证据。

## 7. 后续路线（非本次验收阻塞项）

- [ ] 扩展到 60–100 条并补充人工标注 evidence relevance / numeric faithfulness；
- [ ] 独立 Judge 或人工 rubric 复核，避免生成模型自评；
- [ ] 增加 evaluator / retrieval 消融，分别量化各质量门禁贡献；
- [ ] 继续优化多公司跨年份检索的召回和 correction-loop 尾延迟。

## 2.7 Dissertation Gold v1 and controlled ablation infrastructure (2026-08-27)

A provenance-rich structured subset and a controlled graph-ablation runner were added for dissertation experiments.

### Data layer

- `data/annotations/verified_metrics_v1.jsonl`: 56 primary-source metric records;
- 52 automotive/new-energy Scope 1/2 facts across 10 companies;
- 104 golden facts in `eval/datasets/esg_eval_gold_v1.jsonl`;
- 42 gold source-page requirements;
- every annotation records raw value/unit, normalized value/unit, source PDF, physical PDF page, excerpt, organizational boundary, reporting basis, quality status and warnings;
- PDF-page validation passed for all 56 records;
- all records remain `needs_second_reviewer=true`, so this is not yet described as independently human-labelled data.

The structured database now contains 56 verified values instead of 19. Historical values explicitly restated by a later report use the latest restated series. Examples include CATL's 2022/2023 battery-production-base emissions as restated in its 2024 report and Seres's adjusted 2022 Scope 1 value.

### Controlled profiles

The graph supports `no_evaluators`, `eval_d_only`, `eval_o_only` and `full`. Only Evaluator-D/O are bypassed; all other nodes are held constant.

### Offline deterministic regression

Suite: `outputs/ablation_runs/20260827_gold_v1_offline_ablation_v3/`

| Profile | Case pass | Golden facts | Gold page recall | Numeric support | Comparability caveat | Partial-missing safety |
|---|---:|---:|---:|---:|---:|---:|
| No evaluators | 100% | 100% | 100% | 94.0% | 100% | 100% |
| Evaluator-D only | 100% | 100% | 100% | 94.5% | 100% | 100% |
| Evaluator-O only | 100% | 100% | 100% | 94.5% | 100% | 100% |
| Full | 100% | 100% | 100% | 95.0% | 100% | 100% |

This result is a workflow regression, not evidence of Evaluator causal benefit. The deterministic mode makes all four profiles succeed on the structured facts. Its value is that it verifies profile isolation, data ingestion, SQL/RAG packaging, missing-data behaviour and reproducible metrics.

During this iteration, gold source-page recall initially measured about 50%. Root-cause analysis showed that SQL outputs retained values but not the annotation's source PDF/page, especially when a 2024 report restated 2022/2023 values. Adding `sql_provenance_sources` raised gold page recall to 100% in the fixed suite.

### Online probe status

A two-case online probe reached the configured endpoint but received HTTP 401 responses from `https://antchat.alipay.com/v1/chat/completions`. The deterministic fallback preserved facts and provenance, but runs with Evaluator-O degraded because semantic checker calls also received 401. These outputs are infrastructure-failure artefacts and must not be reported as online model-quality results. A valid credential must pass `scripts/llm_preflight.py` before the matched online ablation is run.

## 2.9 Post-fix final full-profile regression（2026-08-27）

The first complete online ablation exposed a false-positive scope gate: valid answers such as `范围一排放口径说明` were not recognised by the Evaluator-O matcher. A second false-negative was found in the partial-missing predicate, which did not recognise `单独排放量数据未在证据中披露，仅提供合并总量`. Both rules were repaired and covered by regression tests.

The repaired full profile was rerun with the same controlled configuration:

```text
model: DeepSeek-V4-Pro
SQL: deterministic entity/metric SELECT fallback
RAG: local vector + BM25 + RRF, reranker disabled
LLM_MIN_INTERVAL_SEC: 5
concurrency: 1
Judge: disabled
```

Run directory: `outputs/ablation_runs/20260827_gold_v1_deepseek_full_final_r1/`

| Metric | Result |
|---|---:|
| Completion rate | 100.0% (30/30) |
| Case pass rate | 100.0% (30/30) |
| Golden fact accuracy | 100.0% (104/104) |
| Gold evidence page recall | 100.0% (42/42) |
| Packaged numeric support | 94.6% |
| Comparability caveat rate | 100.0% |
| Partial-missing safe rate | 100.0% |
| Rescue rate | 13.3% (4/30) |
| Degraded / crashed | 0 / 0 |
| Average latency | 40.599 s |
| p95 latency | 51.622 s |

This is a valid single online full-profile regression: there were no provider errors, no timeouts, no deterministic synthesis fallback, no degraded terminal states and no crashes. It is not yet a causal evaluator result because the other profiles have not been rerun after the same fixes. The earlier 20/30 full-profile run is retained as a bug-discovery artefact, not as the final quality result.

## 2.7 修复后在线四 profile × 三次重复消融：`20260828_gold_v1_deepseek_postfix_r3`

2026-08-28 完成修复后的正式在线消融。四种 profile 在相同 30-case Gold 集上各运行 3 次，共 360 次 case execution；SQL deterministic，RAG 使用本地 vector/BM25 + RRF，reranker 关闭，模型为 DeepSeek-V4-Pro，串行请求，LLM 最小间隔 5 秒，Judge 关闭。

| Profile | Case pass | Golden facts | Gold pages | Numeric support | Mean latency (s) | p95 (s) |
|---|---:|---:|---:|---:|---:|---:|
| no_evaluators | 100.0% | 100.0% | 100.0% | 94.57±0.15% | 30.26±0.31 | 38.16±1.55 |
| eval_d_only | 100.0% | 100.0% | 100.0% | 94.97±0.67% | 34.72±0.56 | 48.24±1.36 |
| eval_o_only | 100.0% | 100.0% | 100.0% | 94.77±0.42% | 41.21±0.66 | 59.91±11.40 |
| full | 100.0% | 100.0% | 100.0% | 94.87±0.59% | 41.38±2.20 | 57.10±8.19 |

相对于 no_evaluators，每个对比均有 90 个 paired case runs；两边均 90/90 通过，discordant pairs 为 0，McNemar exact p=1.0，paired bootstrap 95% CI=[0,0] 个百分点。结论是当前 Gold 集上没有观察到 Evaluator 对 case pass rate 的增益；可观察到的代价主要是延迟增加，尤其是 Evaluator-O。

原始产物：`outputs/ablation_runs/20260828_gold_v1_deepseek_postfix_r3/`；统计：`paired_statistics.json`。

## 2.8 第二遍数据复核状态

对 56 条记录执行了独立参数的 machine-assisted second-pass PDF audit，检查了来源解析、页码范围、指标术语、数值 token 和 excerpt overlap。56/56 条通过核心来源与数值检查；`data/annotations/second_pass_machine_audit.json` 保存了逐条结果。

这不是第二位人类复核，因此没有把 `needs_second_reviewer` 改为 `false`，也没有把数据集描述为 independently human-labelled benchmark。需要真实第二位复核者在 `data/annotations/second_review_template.csv` 中填写 reviewer ID、日期和 accept/correct/reject 决策后，才能升级该表述。

## 2.9 专家一致性实验状态

已准备专家标注包：

- `eval/expert/disclosure_quality_template.csv`：15 个 disclosure-quality items；
- `eval/expert/claim_evidence_mismatch_template.csv`：30 个 claim/evidence items；
- `scripts/analyze_expert_agreement.py`：在两位专家均完成盲标后计算 Cohen's kappa、MAE 等指标。

脚本当前会在标签为空时安全阻断，避免用模型自评或合成标签伪造专家一致性。由于工作目录没有第二位真实 ESG 专家提交的独立标签，截至 2026-08-28 该实验尚不能产生合法的 kappa、MAE、Spearman 或 F1 结果。
