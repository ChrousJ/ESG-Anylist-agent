# Iteration 04：评测流水线升级与业务信号指标接入

## 1. 本轮目标

继续把项目从“讲得好”推进到“可评测、有证据”。本轮重点不是调用外部模型跑正式实验，而是先把评测脚本和数据集结构补齐，让后续一条命令能产出面试可展示的评测目录。

## 2. 修改前问题

- `generate_eval_dataset.py` 主要覆盖趋势、对比、缺失和知识类问题，缺少披露质量与绿漂风险 case；
- `run_evaluation.py` 只统计 completion、rescue、latency 等通用指标，无法证明新增业务节点是否稳定产出；
- 评测结果默认散落在当前目录，不符合 `outputs/eval_runs/{run_id}/` 的留痕规范；
- 轻量测试环境缺少 `python-dotenv` 时无法导入评测脚本。

## 3. 修改内容

| 文件 | 修改点 |
|---|---|
| `scripts/generate_eval_dataset.py` | 增加 5 条 `disclosure_quality` case 和 5 条 `greenwashing_risk` case，总计 50 条 |
| `scripts/run_evaluation.py` | 增加 evidence presence、SQL/RAG evidence presence、disclosure score presence、average disclosure score、greenwashing radar presence、greenwashing non-zero cases |
| `scripts/run_evaluation.py` | 增加 `--run-id` / `--run-dir`，自动输出 `eval_results.json`、`metrics.json`、`bad_cases.jsonl`、`trace_summary.csv`、`eval_report.md` |
| `scripts/run_evaluation.py` | 对 `dotenv` 做 optional fallback，避免轻量测试环境导入失败 |
| `tests/test_eval_metrics.py` | 新增业务信号指标单元测试 |
| `README.md` / `docs/eval-design.md` / `docs/experiment-results.md` / `docs/roadmap.md` | 同步更新评测命令、指标和下一步状态 |

## 4. 新增评测指标

| 指标 | 含义 | 面试价值 |
|---|---|---|
| `evidence_presence_rate` | 至少带有 SQL 或 RAG 证据的样本比例 | 证明不是裸生成 |
| `sql_evidence_presence_rate` | 带结构化 SQL 证据的比例 | 证明数字分析依赖结构化事实 |
| `rag_evidence_presence_rate` | 带 RAG 来源的比例 | 证明 PDF 原文证据链 |
| `disclosure_score_presence_rate` | 输出披露质量结构的比例 | 证明新增业务节点稳定接入 |
| `avg_disclosure_score` | 平均披露质量分 | 用于观察样本整体披露质量分布 |
| `greenwashing_radar_presence_rate` | 输出绿漂风险雷达结构的比例 | 证明风险扫描节点稳定接入 |
| `greenwashing_nonzero_count` | 触发至少一个人工核查点的 case 数 | 用于 demo 风险雷达 |

## 5. 验证

```bash
python3 scripts/generate_eval_dataset.py -o /tmp/esg_eval_test.jsonl
python3 -m unittest discover -s tests
python3 -m compileall agent api scripts tests
```

结果：

```text
Generated 50 eval cases
Ran 6 tests
OK
```

## 6. 后续正式评测命令

```bash
python scripts/generate_eval_dataset.py -o eval_dataset.jsonl
python scripts/run_evaluation.py --skip-baseline --concurrency 1 --run-id 20260809_smoke_v1
```

输出目录：

```text
outputs/eval_runs/20260809_smoke_v1/
  eval_results.json
  metrics.json
  bad_cases.jsonl
  trace_summary.csv
  eval_report.md
```

## 7. 面试讲法

> 我没有只新增功能，还把新增功能纳入评测。比如 disclosure scorer 和 greenwashing detector 不只是前端卡片，而是会在离线评测里统计 presence rate、平均披露分和风险触发数。这样面试官追问“怎么证明它稳定工作”时，我可以打开 metrics.json 和 eval_report.md，而不是只靠现场 demo。
