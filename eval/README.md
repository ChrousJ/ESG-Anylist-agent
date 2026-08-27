# ESG-Insight Agent Eval Assets

> 这里存放可版本化的评测样本和 Rubric。临时运行输出请放到 `outputs/eval_runs/{run_id}/`，不要提交大体积运行产物。

## 目录

```text
eval/
  datasets/
    esg_eval_smoke.jsonl      # 面试前快速 smoke eval，小样本、可人工复核
    esg_eval_full_50.jsonl    # 50-case 离线 regression 集
  rubrics/
    disclosure_quality_rubric.yaml
    greenwashing_rubric.yaml
```

## 生成完整数据集

```bash
python scripts/generate_eval_dataset.py -o eval_dataset.jsonl
```

## 跑一次 smoke eval

```bash
python scripts/run_evaluation.py \
  -i eval/datasets/esg_eval_smoke.jsonl \
  --skip-baseline \
  --skip-judge \
  --concurrency 1 \
  --run-id <run_id>
```

输出：

```text
outputs/eval_runs/20260809_smoke_v1/
  eval_results.json
  metrics.json
  bad_cases.jsonl
  trace_summary.csv
  eval_report.md
```


## 环境失败也会产出报告

`scripts/run_evaluation.py` 会把依赖导入失败、运行时异常等记录成 case-level `crashed`，继续生成 `metrics.json` 和 `bad_cases.jsonl`。

例如本地缺少 `langgraph` 时，smoke eval 不会直接中断，而是输出：

```text
status=crashed
error=No module named 'langgraph'
```

这类结果只能说明运行环境未准备好，不能作为模型业务效果指标。安装依赖和配置 `.env` 后请使用新的 `run_id` 重跑。

## 当前 50-case 离线 regression

```bash
OFFLINE_DETERMINISTIC_MODE=true \
DISABLE_VECTOR_SEARCH=true DISABLE_RERANK=true \
RAG_TIMEOUT_SEC=0 RAG_SCOPE_TIMEOUT_SEC=0 \
.venv311/bin/python scripts/run_evaluation.py \
  -i eval/datasets/esg_eval_full_50.jsonl \
  --skip-baseline --skip-judge --concurrency 1 --delay 0 \
  --run-id 20260813_offline_full50_v3
```

本次结果：50/50 completion、Case Pass 34/50、0 crash；详情见 `docs/experiment-results.md` 第 2.6 节。
