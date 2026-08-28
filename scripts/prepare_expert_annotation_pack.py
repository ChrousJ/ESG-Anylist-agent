#!/usr/bin/env python3
import csv,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
cases=[json.loads(x) for x in (ROOT/'eval/datasets/esg_eval_gold_v1.jsonl').read_text().splitlines() if x.strip()]
# Use fact/trend/comparison cases to avoid making missing-data safety the only construct.
selected=( [c for c in cases if c['category']=='fact'][:5] + [c for c in cases if c['category']=='trend'][:5] + [c for c in cases if c['category']=='compare'][:5] )
ex=ROOT/'eval/expert'; ex.mkdir(parents=True,exist_ok=True)
with (ex/'disclosure_quality_template.csv').open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.writer(f); w.writerow(['item_id','case_id','category','query','evidence_reference','reviewer_a_overall_1_5','reviewer_a_completeness_1_5','reviewer_a_continuity_1_5','reviewer_a_comparability_1_5','reviewer_a_verifiability_1_5','reviewer_a_specificity_1_5','reviewer_b_overall_1_5','reviewer_b_completeness_1_5','reviewer_b_continuity_1_5','reviewer_b_comparability_1_5','reviewer_b_verifiability_1_5','reviewer_b_specificity_1_5','adjudicated_label','notes'])
 for i,c in enumerate(selected,1): w.writerow([f'DQ-{i:02d}',c['id'],c['category'],c['query'],json.dumps(c.get('gold_evidence',[]),ensure_ascii=False)]+['']*13)
with (ex/'claim_evidence_mismatch_template.csv').open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.writer(f); w.writerow(['item_id','case_id','claim_text','evidence_reference','reviewer_a_mismatch_0_1','reviewer_b_mismatch_0_1','adjudicated_label','mismatch_type','notes'])
 # 30 rows: each case query as a claim placeholder; reviewers must replace/inspect claim_text from recorded outputs.
 for i,c in enumerate(cases,1): w.writerow([f'CE-{i:02d}',c['id'],c['query'],json.dumps(c.get('gold_evidence',[]),ensure_ascii=False),'','','','','Replace claim_text with the exact recorded claim before annotation'])
print(f'prepared {len(selected)} disclosure and {len(cases)} claim/evidence rows')
