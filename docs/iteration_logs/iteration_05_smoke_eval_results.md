# Iteration 05 — Smoke Eval 运行与评测健壮性修复

> 日期：2026-08-09  
> 目标：按面试前 smoke eval 流程实际跑一次评测，确认输出目录、metrics、bad cases 和 report 能稳定生成。

## 1. 本轮做了什么

### 1.1 运行 smoke eval

执行命令：

```bash
/usr/bin/python3 scripts/run_evaluation.py \
  -i eval/datasets/esg_eval_smoke.jsonl \
  --skip-baseline \
  --skip-judge \
  --concurrency 1 \
  --delay 0 \
  --run-id 20260809_smoke_v1
```

生成目录：

```text
outputs/eval_runs/20260809_smoke_v1/
  eval_results.json
  metrics.json
  bad_cases.jsonl
  trace_summary.csv
  eval_report.md
```

### 1.2 修复评测脚本的 fail-fast 问题

原先 `scripts/run_evaluation.py` 在 `_run_langgraph_agent()` 顶层导入 `agent.graph`，如果本地缺少 `langgraph` 等运行依赖，整个评测会直接中断，无法沉淀可复盘结果。

本轮将导入移动到 `try` 内部，使依赖缺失也会被记录为单条 case 的 `crashed` 结果，并继续生成：

- `metrics.json`
- `bad_cases.jsonl`
- `eval_report.md`

这体现的是评测工程能力：**评测任务不能因为一个环境问题就丢失全部上下文**。

### 1.3 环境记录

本机尝试创建过两个虚拟环境：

- `.venv`：由 Homebrew `python3` 创建，版本为 Python 3.14；部分依赖暂无兼容轮子或解析失败。
- `.venv39`：由 `/usr/bin/python3` 创建，版本为 Python 3.9.6；网络下载速度过慢，完整安装未完成。

已将 `.venv39/` 加入 `.gitignore`，避免误提交本地虚拟环境。

## 2. 本次 smoke eval 真实结果

由于运行环境缺少核心依赖 `langgraph`，10 条 smoke case 均被评测脚本捕获为 `crashed`，不是模型能力结果。

| 指标 | 结果 |
|---|---:|
| Total cases | 10 |
| Completion Rate | 0.0% |
| Crashed | 10 |
| p95 Latency | 3ms |
| Primary error | `No module named 'langgraph'` |

bad case 样例：

```json
{"case_id":"SMOKE-001","status":"crashed","error":"No module named 'langgraph'"}
```

## 3. 如何向面试官解释这次结果

这次结果不能用于证明 Agent 业务效果，但可以证明两点工程意识：

1. **评测闭环已跑通**：即使环境依赖缺失，系统仍然产出结构化 metrics、bad cases 和 report。
2. **失败可归因**：失败原因集中为 runtime dependency，而不是 RAG 召回、SQL 生成或 evaluator 逻辑错误。

推荐表述：

> 我把 eval runner 做成了“失败也可观测”的模式。本地这次 smoke eval 因为机器缺少 langgraph 依赖，10 条 case 都被标记为 crashed，但评测脚本没有直接中断，而是生成了 metrics、bad_cases 和 report。正式环境装好依赖和 API key 后，同一条命令即可得到业务指标。

## 4. 后续动作

- [ ] 使用 Python 3.10/3.11 重建虚拟环境，避免 Python 3.14 依赖兼容问题；
- [ ] 完成 `pip install -r requirements.txt`；
- [ ] 配置 `.env` 中的 LLM key；
- [ ] 重新运行 `20260809_smoke_v2`；
- [ ] 将真实业务指标回填到 `docs/experiment-results.md`。
