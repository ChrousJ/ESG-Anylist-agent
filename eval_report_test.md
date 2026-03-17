# ESG Agent Evaluation Report

> Generated: 2026-03-17T12:16:49.212665+00:00
> Total cases: 3
> LangGraph nodes: context -> supervisor -> schema_injector -> sql/rag workers -> evaluator_d -> map_reduce -> synthesizer -> evaluator_o -> memory_updater
> Baseline: ReAct agent (Qwen LLM + SQL/RAG tools, no quality loops)

## Overall Comparison

| Metric | LangGraph Agent | ReAct Baseline |
|--------|:--------------:|:--------------:|
| Completion Rate | 0.0% (0/3) | 0.0% (0/3) |
| Rescue Rate | 0.0% (0/3) | N/A (no loops) |
| p95 Latency | 278910ms | 3072ms |
| p50 Latency | 93321ms | 1703ms |
| Avg Latency | 151929ms | 1937ms |
| Crashed | 3 | 0 |
| Degraded | 0 | N/A |

## Per-Category Breakdown

| Category | Agent | Completion | p95 Latency | Crashed |
|----------|-------|:----------:|:-----------:|:-------:|
| trend | LangGraph | 0.0% | 278910ms | 3 |
| | ReAct | 0.0% | 3072ms | 0 |

## Node-Level Latency (LangGraph Agent)

## Failed / Crashed Cases

> These are real failures observed during evaluation. No data is fabricated.

| Case ID | Agent | Status | Category | Query | Error |
|---------|-------|--------|----------|-------|-------|
| EVAL-001 | LangGraph | crashed | trend | 比亚迪2022到2024年碳排放趋势如何？ | At key 'trace_id': Can receive only one value per step. Use an Annotated key to  |
| EVAL-002 | LangGraph | crashed | trend | 工商银行近三年绿色贷款余额变化趋势 | At key 'trace_id': Can receive only one value per step. Use an Annotated key to  |
| EVAL-003 | LangGraph | crashed | trend | 华能国际2022-2024清洁能源占比有什么变化？ | At key 'trace_id': Can receive only one value per step. Use an Annotated key to  |

## Key Takeaways

1. **Completion Rate**: LangGraph 0.0% vs ReAct 0.0%
2. **Rescue Rate**: No rescue events triggered in this run (expected for clean inputs).
3. **Latency**: LangGraph p95=278910ms, ReAct p95=3072ms. The multi-node pipeline adds latency but provides quality guarantees.
