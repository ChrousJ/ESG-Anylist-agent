# Dissertation Experiment Protocol v1

> Date established: 2026-08-27  
> Purpose: separate engineering regression, causal ablation, and domain-validity evaluation.

## 1. Research hypotheses

- **H1:** A controlled LangGraph workflow achieves higher case-pass and golden-fact accuracy than a ReAct baseline under matched model and tool conditions.
- **H2:** Evaluator-D primarily improves evidence coverage, target coverage, and safe no-data behaviour.
- **H3:** Evaluator-O primarily improves packaged numeric support, entity consistency, and trend-direction consistency.
- **H4:** Quality gates add bounded node cost but may reduce uncontrolled tool loops relative to ReAct.

## 2. Controlled profiles

| Profile | Evaluator-D | Evaluator-O | Other nodes |
|---|:---:|:---:|---|
| `no_evaluators` | bypass | bypass | unchanged |
| `eval_d_only` | enabled | bypass | unchanged |
| `eval_o_only` | bypass | enabled | unchanged |
| `full` | enabled | enabled | unchanged |
| ReAct baseline | N/A | N/A | same SQL/RAG tools where supported |

The profile implementation changes only the evaluator node implementation. Context, supervisor, schema injection, SQL/RAG workers, aggregation, map-reduce, synthesis, disclosure scoring, risk radar, and memory remain in the graph.

## 3. Evaluation layers

### 3.1 Offline deterministic regression

Purpose: verify routing, SQL/RAG packaging, missing-data behaviour, output contracts, and profile reproducibility without claiming LLM semantic quality.

Command:

```bash
make PYTHON=.venv311/bin/python ablation-offline
```

### 3.2 Matched online ablation

Purpose: estimate the contribution of Evaluator-D and Evaluator-O with the same model, dataset, retrieval configuration, temperature, and concurrency.

Minimum recommended design:

- 30 gold cases;
- three repetitions per profile;
- concurrency 1;
- fixed model and temperature;
- Judge disabled for primary metrics;
- optional independent judge reported separately;
- paired case-level output retained for McNemar and bootstrap analysis.

### 3.3 Domain validity

Purpose: test whether disclosure-quality and claim--evidence mismatch outputs agree with human reviewers. This is separate from workflow factuality and must not be inferred from output-presence metrics.

## 4. Gold dataset

`eval/datasets/esg_eval_gold_v1.jsonl` contains:

- 30 cases;
- 104 structured golden facts;
- 42 gold source-page requirements;
- fact, trend, comparison, and explicit missing-data tasks.

The facts originate from `data/annotations/verified_metrics_v1.jsonl`. At present the annotations are machine-assisted primary-source transcriptions and remain pending a second human reviewer. Until second review is complete, results must be described as **auditable primary-source regression**, not independently human-labelled accuracy.

## 5. Primary metrics

1. Case pass rate
2. Golden fact accuracy
3. Gold evidence page recall
4. Required evidence coverage
5. Target entity-year coverage
6. Packaged numeric support
7. Entity evidence precision
8. No-data safe response rate
9. Average and p95 latency
10. Re-plan and correction rates

Completion rate is a health metric, not the headline quality metric.

## 6. Statistical analysis for online runs

- Report mean, standard deviation, minimum and maximum across repetitions.
- Use paired bootstrap confidence intervals for rate differences.
- Use McNemar's exact test for paired case pass/fail comparisons.
- Report per-task results because aggregate rates can hide missing-data failures.
- Do not run significance tests on the deterministic offline suite as if it were an independent sample of model behaviour.

## 7. Annotation review workflow

A second reviewer should inspect each cited page and record:

- reviewer identifier;
- review date;
- accept / correct / reject;
- corrected value or page where applicable;
- boundary agreement;
- notes.

Only accepted records should set `needs_second_reviewer=false`. Corrections must preserve the original annotation history rather than overwriting it without a log.

## 8. Claim guardrails

Allowed after offline regression:

> The workflow and ablation infrastructure are reproducible on the local corpus.

Allowed after matched online repetitions:

> Under the tested model, tools and cases, the enabled quality gates changed specified reliability metrics by the reported amount.

Not allowed without domain review:

- the disclosure score agrees with ESG experts;
- the risk radar detects real-world greenwashing;
- the benchmark represents all ESG reports or companies.

## 10. Post-fix full-profile result（2026-08-27）

The repaired full profile completed 30/30 online gold cases. It recovered 104/104 golden facts and 42/42 gold source pages, with 94.6% packaged numeric support, 100% comparability caveat rate, 100% partial-missing safe rate, 0 degraded cases and 0 crashes. The result is a post-fix regression result. A matched four-profile post-fix run remains necessary before making causal claims about Evaluator-D or Evaluator-O.
