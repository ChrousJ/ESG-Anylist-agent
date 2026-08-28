#!/usr/bin/env python3
"""Analyze agreement; refuses to compute results when either reviewer is incomplete."""
import argparse,csv,json,math
from pathlib import Path

def kappa(a,b):
 n=len(a); po=sum(x==y for x,y in zip(a,b))/n; cats=set(a)|set(b); pe=sum(a.count(c)*b.count(c) for c in cats)/(n*n); return (po-pe)/(1-pe) if pe<1 else 1.0

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--disclosure',type=Path,default=Path('eval/expert/disclosure_quality_template.csv')); ap.add_argument('--claims',type=Path,default=Path('eval/expert/claim_evidence_mismatch_template.csv')); ap.add_argument('--output',type=Path,default=Path('outputs/expert_agreement/agreement.json')); a=ap.parse_args()
 out={'status':'blocked','reason':'independent reviewer labels are required; no synthetic labels are accepted'}
 for p in (a.disclosure,a.claims):
  if not p.exists(): out['reason']=f'missing {p}'; a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False)); return
 with a.disclosure.open(encoding='utf-8-sig') as f: d=list(csv.DictReader(f))
 with a.claims.open(encoding='utf-8-sig') as f: c=list(csv.DictReader(f))
 def vals(rows, x,y): return [(r[x].strip(),r[y].strip()) for r in rows]
 pairs=vals(d,'reviewer_a_overall_1_5','reviewer_b_overall_1_5')
 cp=vals(c,'reviewer_a_mismatch_0_1','reviewer_b_mismatch_0_1')
 if any(not x or not y for x,y in pairs+cp):
  a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False)); return
 da=[x for x,y in pairs]; db=[y for x,y in pairs]; ca=[x for x,y in cp]; cb=[y for x,y in cp]
 mae=sum(abs(int(x)-int(y)) for x,y in pairs)/len(pairs)
 out={'status':'pass','disclosure':{'n':len(pairs),'cohen_kappa':kappa(da,db),'mae':mae},'claim_evidence_mismatch':{'n':len(cp),'cohen_kappa':kappa(ca,cb)}}
 a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
