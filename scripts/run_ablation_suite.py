#!/usr/bin/env python3
"""Run controlled ESG Agent ablations and aggregate reproducible results."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ("no_evaluators", "eval_d_only", "eval_o_only", "full")
METRICS = (
    "completion_rate", "case_pass_rate", "golden_fact_accuracy", "gold_evidence_recall",
    "evidence_required_coverage_rate", "avg_entity_evidence_precision",
    "avg_packaged_numeric_support_rate", "no_data_safe_response_rate",
    "avg_target_coverage_rate", "partial_missing_safe_rate", "comparability_caveat_rate",
    "avg_latency_ms", "p95_latency_ms", "rescue_rate",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unavailable"


def aggregate(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "stdev": 0.0, "min": 0.0, "max": 0.0}
    return {
        "mean": round(statistics.mean(values), 3),
        "stdev": round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
        "min": round(min(values), 3), "max": round(max(values), 3),
    }


def run_suite(args: argparse.Namespace) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suite_id = args.suite_id or f"{timestamp}_{args.mode}_ablation"
    suite_dir = ROOT / "outputs" / "ablation_runs" / suite_id
    suite_dir.mkdir(parents=True, exist_ok=True)
    dataset = (ROOT / args.input).resolve() if not Path(args.input).is_absolute() else Path(args.input)
    env_base = os.environ.copy()
    if args.mode == "online" and not args.skip_preflight:
        preflight = subprocess.run([sys.executable, "scripts/llm_preflight.py"], cwd=ROOT, env=env_base, text=True, capture_output=True)
        (suite_dir / "llm_preflight.json").write_text(preflight.stdout or json.dumps({"status":"fail","stderr":preflight.stderr[:500]}), encoding="utf-8")
        if preflight.returncode:
            raise RuntimeError(f"LLM preflight failed; inspect {suite_dir/'llm_preflight.json'}")
    if args.mode == "offline":
        env_base.update({"OFFLINE_DETERMINISTIC_MODE": "true", "DISABLE_VECTOR_SEARCH": "true", "DISABLE_RERANK": "true"})
    if args.deterministic_sql:
        env_base["DETERMINISTIC_SQL_ONLY"] = "true"
    if args.disable_rerank:
        env_base["DISABLE_RERANK"] = "true"
    if args.model:
        env_base["LLM_MAIN_MODEL"] = args.model
        env_base["OPENAI_MAIN_MODEL"] = args.model
    if args.llm_min_interval > 0:
        env_base["LLM_MIN_INTERVAL_SEC"] = str(args.llm_min_interval)
    runs=[]
    for profile in args.profiles:
        for repetition in range(1, args.repetitions + 1):
            run_name=f"{profile}_r{repetition}"
            run_dir=suite_dir/run_name
            cmd=[sys.executable,"scripts/run_evaluation.py","-i",str(dataset),"--run-dir",str(run_dir),"--ablation-profile",profile,"--skip-judge","--concurrency",str(args.concurrency),"--delay",str(args.delay)]
            include_baseline = args.include_baseline and args.mode == "online" and profile == "full" and repetition == 1
            if not include_baseline: cmd.append("--skip-baseline")
            if args.max_cases: cmd += ["--max-cases",str(args.max_cases)]
            print("RUN", " ".join(cmd), flush=True)
            started=datetime.now(timezone.utc).isoformat()
            proc=subprocess.run(cmd,cwd=ROOT,env=env_base,text=True,capture_output=True)
            (run_dir).mkdir(parents=True,exist_ok=True)
            (run_dir/"stdout.log").write_text(proc.stdout,encoding="utf-8")
            (run_dir/"stderr.log").write_text(proc.stderr,encoding="utf-8")
            if proc.returncode:
                raise RuntimeError(f"Ablation run failed: {run_name}; inspect {run_dir/'stderr.log'}")
            metrics=json.loads((run_dir/"metrics.json").read_text(encoding="utf-8"))
            runs.append({"profile":profile,"repetition":repetition,"run_dir":str(run_dir.relative_to(ROOT)),"started_at":started,"langgraph":metrics["langgraph"],"baseline":metrics.get("baseline",{}) if include_baseline else {}})

    summary={}
    for profile in args.profiles:
        subset=[r for r in runs if r["profile"]==profile]
        summary[profile]={m:aggregate([float(r["langgraph"].get(m) or 0) for r in subset]) for m in METRICS}
    baseline=next((r["baseline"] for r in runs if r.get("baseline") and r["baseline"].get("total")),{})
    manifest={
        "suite_id":suite_id,"created_at":datetime.now(timezone.utc).isoformat(),"mode":args.mode,
        "dataset":str(dataset.relative_to(ROOT)),"dataset_sha256":sha256(dataset),"git_commit":git_commit(),
        "python":sys.version,"profiles":args.profiles,"repetitions":args.repetitions,
        "environment_flags":{k:env_base.get(k,"") for k in ("OFFLINE_DETERMINISTIC_MODE","DETERMINISTIC_SQL_ONLY","DISABLE_VECTOR_SEARCH","DISABLE_RERANK","LLM_PROVIDER","LLM_MAIN_MODEL","LLM_MIN_INTERVAL_SEC")},
        "annotation_sha256":sha256(ROOT/"data/annotations/verified_metrics_v1.jsonl"),
        "runs":runs,"summary":summary,"baseline":baseline,
    }
    (suite_dir/"suite_results.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    with (suite_dir/"summary.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(["profile","metric","mean","stdev","min","max"])
        for profile,data in summary.items():
            for metric,stats in data.items(): w.writerow([profile,metric,stats["mean"],stats["stdev"],stats["min"],stats["max"]])
    lines=[f"# Ablation Suite: {suite_id}","",f"- Mode: `{args.mode}`",f"- Dataset: `{manifest['dataset']}`",f"- Repetitions: {args.repetitions}",f"- Git commit: `{manifest['git_commit']}`","", "## Aggregate Results","", "| Profile | Case Pass | Golden Facts | Gold Evidence | Numeric Support | Comparability Caveat | Partial Missing Safe | Avg Latency | p95 |","|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for p in args.profiles:
        d=summary[p]; lines.append(f"| {p} | {d['case_pass_rate']['mean']:.1f}% | {d['golden_fact_accuracy']['mean']:.1f}% | {d['gold_evidence_recall']['mean']:.1f}% | {d['avg_packaged_numeric_support_rate']['mean']:.1f}% | {d['comparability_caveat_rate']['mean']:.1f}% | {d['partial_missing_safe_rate']['mean']:.1f}% | {d['avg_latency_ms']['mean']:.0f} ms | {d['p95_latency_ms']['mean']:.0f} ms |")
    if baseline:
        lines += ["","## ReAct Baseline (single matched run)","",f"- Case pass: {baseline.get('case_pass_rate','N/A')}%",f"- Golden fact accuracy: {baseline.get('golden_fact_accuracy','N/A')}%",f"- Average latency: {baseline.get('avg_latency_ms','N/A')} ms"]
    lines += ["","## Interpretation Guardrails","",f"- {'Offline deterministic runs are workflow regressions, not semantic LLM benchmarks.' if args.mode == 'offline' else 'Online results are valid only when llm_preflight.json reports pass and no provider errors occur.'}","- Records remain pending second-review confirmation and must not yet be described as independently human annotated.","- Causal Evaluator claims should be based on matched online repetitions after the offline suite is stable."]
    (suite_dir/"summary.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return suite_dir


def main() -> None:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-i","--input",default="eval/datasets/esg_eval_gold_v1.jsonl")
    ap.add_argument("--suite-id",default="")
    ap.add_argument("--mode",choices=["offline","online"],default="offline")
    ap.add_argument("--profiles",nargs="+",choices=PROFILES,default=list(PROFILES))
    ap.add_argument("--repetitions",type=int,default=1)
    ap.add_argument("--include-baseline",action="store_true")
    ap.add_argument("--skip-preflight",action="store_true",help="Skip online provider authentication preflight")
    ap.add_argument("--model",default="",help="Override main model for all matched profiles")
    ap.add_argument("--deterministic-sql",action="store_true",help="Use deterministic SQL while keeping online synthesis/evaluation")
    ap.add_argument("--disable-rerank",action="store_true",help="Disable local cross-encoder reranking")
    ap.add_argument("--llm-min-interval",type=float,default=0.0,help="Process-level minimum interval between LLM calls")
    ap.add_argument("--concurrency",type=int,default=1); ap.add_argument("--delay",type=float,default=0.0); ap.add_argument("--max-cases",type=int,default=0)
    args=ap.parse_args(); out=run_suite(args); print(out)

if __name__=="__main__": main()
