#!/usr/bin/env python3
"""Generate a provenance-rich ESG fact evaluation set from reviewed annotations.

The dataset is deterministic and versioned. It does not call an LLM. Gold facts
and source pages are copied from the primary-source annotation layer so that
changes to either layer are auditable in git.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ANNOTATIONS = ROOT / "data/annotations/verified_metrics_v1.jsonl"
DEFAULT_OUTPUT = ROOT / "eval/datasets/esg_eval_gold_v1.jsonl"


def load_annotations(path: Path) -> dict[tuple[str, int], dict[str, dict[str, Any]]]:
    out: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["metric_key"] not in {"scope_1_emissions", "scope_2_emissions"}:
            continue
        out[(r["company_name"], int(r["year"]))][r["metric_key"]] = r
    return dict(out)


def gold_fact(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "company": r["company_name"], "year": r["year"], "metric": r["metric_key"],
        "value": r["normalized_value"], "tolerance": max(0.01, abs(float(r["normalized_value"])) * 1e-6),
        "quality": "normal",
    }


def gold_source(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "company": r["company_name"], "year": r["year"], "metric": r["metric_key"],
        "source_file": r["source_file"], "pdf_page": r["pdf_page"],
        "organizational_boundary": r["organizational_boundary"],
    }


def records_for(data: dict, companies: list[str], years: list[int], metrics: list[str]) -> list[dict[str, Any]]:
    rows=[]
    for company in companies:
        for year in years:
            for metric in metrics:
                r=data.get((company,year),{}).get(metric)
                if r: rows.append(r)
    return rows


def make_case(case_id: str, category: str, query: str, companies: list[str], years: list[int], rows: list[dict[str, Any]], *, description: str, comparability_warning: bool=False) -> dict[str, Any]:
    metrics=sorted({r["metric_key"] for r in rows})
    sources=[]; seen=set()
    for r in rows:
        src=gold_source(r); key=(src["source_file"],src["pdf_page"],src["company"])
        if key not in seen: seen.add(key); sources.append(src)
    return {
        "id": case_id,
        "category": category,
        "query": query,
        "expected_class": "complex",
        "expected_entities": {"companies": companies, "years": years, "metrics": metrics, "intent": "compare" if category=="compare" else ("trend" if category=="trend" else "qa"), "industry": "new_energy"},
        "description": description,
        "expected_evidence": True,
        "golden_facts": [gold_fact(r) for r in rows],
        "gold_evidence": sources,
        "require_gold_evidence": False,
        "comparability_warning_expected": comparability_warning,
        "annotation_version": "verified_metrics_v1",
        "label_status": "primary_source_machine_assisted_pending_second_review",
    }


def build(data: dict) -> list[dict[str, Any]]:
    cases=[]
    latest={"比亚迪":2024,"长城汽车":2024,"广汽集团":2024,"赛力斯":2024,"长安汽车":2024,"宁德时代":2024,"亿纬锂能":2024,"上汽集团":2024,"国轩高科":2024,"华友钴业":2024}
    for idx,(company,year) in enumerate(latest.items(),1):
        rows=records_for(data,[company],[year],["scope_1_emissions","scope_2_emissions"])
        cases.append(make_case(f"GOLD-F-{idx:02d}","fact",f"{company}{year}年范围一和范围二温室气体排放量分别是多少？",[company],[year],rows,description="Single-company two-metric fact extraction with source-page gold."))

    trend_companies=["比亚迪","长城汽车","广汽集团","赛力斯","长安汽车","宁德时代"]
    for idx,company in enumerate(trend_companies,1):
        years=[2022,2023,2024]; rows=records_for(data,[company],years,["scope_1_emissions","scope_2_emissions"])
        warning=any(r.get("warnings") for r in rows)
        cases.append(make_case(f"GOLD-T-{idx:02d}","trend",f"分析{company}2022到2024年范围一和范围二排放趋势，并说明数据口径限制。",[company],years,rows,description="Three-year trend with six structured gold facts.",comparability_warning=warning))

    pairs=[("比亚迪","长城汽车"),("广汽集团","赛力斯"),("长安汽车","宁德时代"),("亿纬锂能","上汽集团"),("国轩高科","华友钴业"),("比亚迪","广汽集团")]
    for idx,(a,b) in enumerate(pairs,1):
        rows=records_for(data,[a,b],[2024],["scope_1_emissions","scope_2_emissions"])
        warning=any(r.get("organizational_boundary") != "group operational control" or r.get("warnings") for r in rows)
        cases.append(make_case(f"GOLD-C-{idx:02d}","compare",f"对比{a}和{b}2024年范围一、范围二排放，并判断口径是否可以直接比较。",[a,b],[2024],rows,description="Pair comparison requiring numeric facts and a comparability caveat.",comparability_warning=warning))

    group_specs=[
      (["比亚迪","长城汽车","广汽集团"],2024),
      (["赛力斯","长安汽车","宁德时代"],2024),
      (["比亚迪","广汽集团","赛力斯"],2023),
      (["长城汽车","长安汽车","宁德时代"],2022),
    ]
    for idx,(companies,year) in enumerate(group_specs,1):
        rows=records_for(data,companies,[year],["scope_1_emissions","scope_2_emissions"])
        cases.append(make_case(f"GOLD-G-{idx:02d}","compare",f"比较{'、'.join(companies)}{year}年的范围一和范围二排放，按指标列出并提示统计边界。",companies,[year],rows,description="Three-company comparison with complete entity coverage.",comparability_warning=True))

    # Explicit partial/missing cases are kept separate from factual accuracy rates.
    cases.extend([
      {"id":"GOLD-M-01","category":"partial_missing","query":"亿纬锂能2022年范围一和范围二排放量是多少？","expected_class":"complex","expected_entities":{"companies":["亿纬锂能"],"years":[2022],"metrics":["scope_1_emissions","scope_2_emissions"],"intent":"qa","industry":"new_energy"},"description":"Source table explicitly shows 2022 as unavailable.","expected_evidence":True,"golden_facts":[],"gold_evidence":[],"annotation_version":"verified_metrics_v1","label_status":"primary_source_machine_assisted_pending_second_review"},
      {"id":"GOLD-M-02","category":"partial_missing","query":"国轩高科2023年范围一和范围二排放量是多少？","expected_class":"complex","expected_entities":{"companies":["国轩高科"],"years":[2023],"metrics":["scope_1_emissions","scope_2_emissions"],"intent":"qa","industry":"new_energy"},"description":"No verified split Scope 1/2 fact in the annotation subset for 2023.","expected_evidence":True,"golden_facts":[],"gold_evidence":[],"annotation_version":"verified_metrics_v1","label_status":"primary_source_machine_assisted_pending_second_review"},
      {"id":"GOLD-M-03","category":"partial_missing","query":"华友钴业2022年范围一和范围二分别是多少？","expected_class":"complex","expected_entities":{"companies":["华友钴业"],"years":[2022],"metrics":["scope_1_emissions","scope_2_emissions"],"intent":"qa","industry":"new_energy"},"description":"2022 report discloses only combined Scope 1+2, so a split must not be fabricated.","expected_evidence":True,"golden_facts":[],"gold_evidence":[],"annotation_version":"verified_metrics_v1","label_status":"primary_source_machine_assisted_pending_second_review"},
      {"id":"GOLD-M-04","category":"partial_missing","query":"上汽集团2022年范围一和范围二排放量是多少？","expected_class":"complex","expected_entities":{"companies":["上汽集团"],"years":[2022],"metrics":["scope_1_emissions","scope_2_emissions"],"intent":"qa","industry":"new_energy"},"description":"No verified 2022 split value in the structured annotation subset.","expected_evidence":True,"golden_facts":[],"gold_evidence":[],"annotation_version":"verified_metrics_v1","label_status":"primary_source_machine_assisted_pending_second_review"}
    ])
    return cases


def main() -> None:
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('--annotations',type=Path,default=DEFAULT_ANNOTATIONS); ap.add_argument('-o','--output',type=Path,default=DEFAULT_OUTPUT); args=ap.parse_args()
    cases=build(load_annotations(args.annotations)); args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('w',encoding='utf-8') as f:
        for case in cases:f.write(json.dumps(case,ensure_ascii=False)+'\n')
    print(json.dumps({'output':str(args.output),'cases':len(cases),'golden_facts':sum(len(c.get('golden_facts',[])) for c in cases),'gold_evidence':sum(len(c.get('gold_evidence',[])) for c in cases)},ensure_ascii=False,indent=2))

if __name__=='__main__': main()
