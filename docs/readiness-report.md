# ESG-Insight Agent Readiness Report

> Profile: `runtime`  
> Status: **ready**  
> Generated: 2026-08-13T09:18:27.921794+00:00

| Check | Status | Detail |
|---|:---:|---|
| `python_version` | ✅ | Python 3.11.15; supported range is >=3.10,<3.13 |
| `repository_structure` | ✅ | required project entrypoints are present |
| `pdf_corpus` | ✅ | 90 reports, 30 companies, years=[2022, 2023, 2024] |
| `smoke_dataset` | ✅ | 10 valid cases, 0 invalid lines |
| `runtime_dependencies` | ✅ | core runtime modules importable |
| `structured_database` | ✅ | /Users/nanyu/ntu/ESG-Anylist-agent-master/data/esg_data.db; rows=90; companies=30; verified_metric_values=19 |
| `retrieval_index` | ✅ | mode=hybrid/vector; vector=/Users/nanyu/ntu/ESG-Anylist-agent-master/data/vector_store; bm25=/Users/nanyu/ntu/ESG-Anylist-agent-master/data/bm25_index.pkl |
| `llm_configuration` | ✅ | provider=openai_compatible; OPENAI_API_KEY configured=True; offline_mode=False |

## Interpretation

- `source` profile verifies repository structure, corpus presence, Python compatibility, and smoke dataset integrity.
- `runtime` profile additionally verifies dependencies, SQLite/vector artifacts, and LLM configuration.
- The report never prints API-key values.
