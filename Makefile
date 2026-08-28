PYTHON ?= python

.PHONY: install dev test eval eval-offline dataset seed-db bm25-index health doctor doctor-offline doctor-source quality vector-index validate-annotations gold-dataset ablation-offline

install:
	$(PYTHON) -m pip install -r requirements.txt

dev:
	$(PYTHON) -m uvicorn api.main:app --reload --port $${PORT:-8000}

test:
	$(PYTHON) -m unittest discover -s tests

dataset:
	$(PYTHON) scripts/generate_eval_dataset.py -o eval_dataset.jsonl

eval:
	$(PYTHON) scripts/run_evaluation.py --skip-baseline --concurrency 1

health:
	curl -s http://127.0.0.1:$${PORT:-8000}/health | $(PYTHON) -m json.tool


doctor:
	$(PYTHON) scripts/project_doctor.py --profile runtime

doctor-source:
	$(PYTHON) scripts/project_doctor.py --profile source

quality:
	$(PYTHON) -m compileall agent api scripts tests
	$(PYTHON) -m unittest discover -s tests
	$(PYTHON) scripts/project_doctor.py --profile source --strict


seed-db:
	$(PYTHON) data/seed_structured_db.py --reset

bm25-index:
	$(PYTHON) scripts/build_bm25_index.py --base-dir data --output-dir data

doctor-offline:
	OFFLINE_DETERMINISTIC_MODE=true $(PYTHON) scripts/project_doctor.py --profile runtime

eval-offline:
	OFFLINE_DETERMINISTIC_MODE=true DISABLE_VECTOR_SEARCH=true DISABLE_RERANK=true \
	$(PYTHON) scripts/run_evaluation.py -i eval/datasets/esg_eval_smoke.jsonl \
	--skip-baseline --skip-judge --concurrency 1 --delay 0 --run-id $${RUN_ID:-offline_smoke}


vector-index:
	$(PYTHON) scripts/build_chroma_from_chunks.py

validate-annotations:
	$(PYTHON) scripts/validate_metric_annotations.py

gold-dataset:
	$(PYTHON) scripts/generate_gold_eval_dataset.py

ablation-offline:
	$(PYTHON) scripts/run_ablation_suite.py --mode offline --include-baseline
