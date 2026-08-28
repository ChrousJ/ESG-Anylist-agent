#!/usr/bin/env python3
"""Independent machine-assisted second-pass audit of annotation pages.

This is deliberately not labelled as human inter-rater review. It uses a second
PDF extraction configuration and checks metric terms, numeric values, units,
and excerpt overlap. Human reviewer fields remain separate.
"""
from __future__ import annotations
import argparse, csv, json, re
from pathlib import Path
import pdfplumber

ROOT=Path(__file__).resolve().parents[1]
TERMS={
'scope_1_emissions':('范围1','范围一','范畴1','范畴一','直接温室','直接溫室','类别一'),
'scope_2_emissions':('范围2','范围二','范畴2','范畴二','间接温室','間接溫室','类别二'),
'scope_3_emissions':('范围3','范围三','范畴3','范畴三','类别三'),
'green_finance_balance':('绿色贷款','绿色信贷','绿色金融')}
def compact(x): return re.sub(r'[\s,，。；;:：()]','',str(x or '')).lower()
def candidates(r):
    v=float(r['normalized_value']); raw=compact(r['raw_value'])
    return {raw,compact(f'{v:g}'),compact(f'{v:,.2f}'),compact(str(int(v)))}
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--input',type=Path,default=ROOT/'data/annotations/verified_metrics_v1.jsonl'); ap.add_argument('--output',type=Path,default=ROOT/'data/annotations/second_pass_machine_audit.json'); ap.add_argument('--csv',type=Path,default=ROOT/'data/annotations/second_review_machine_pass.csv'); args=ap.parse_args()
    rows=[json.loads(x) for x in args.input.read_text(encoding='utf8').splitlines() if x.strip()]
    results=[]; cache={}
    for r in rows:
        path=next(iter(ROOT.joinpath('data').glob(f'*/{r["source_file"]}')),None)
        checks=[]; text=''
        if path is None: checks.append('source_not_found')
        else:
            key=str(path)
            if key not in cache: cache[key]=pdfplumber.open(path)
            pdf=cache[key]; page=int(r['pdf_page'])
            if page<1 or page>len(pdf.pages): checks.append('page_out_of_range')
            else:
                pg=pdf.pages[page-1]
                text=(pg.extract_text(layout=True) or '')+'\n'+(pg.extract_text(x_tolerance=1,y_tolerance=3) or '')
                c=compact(text)
                if not any(compact(t) in c for t in TERMS[r['metric_key']]): checks.append('metric_term_not_found')
                if not any(x and x in c for x in candidates(r)): checks.append('numeric_value_not_found')
                # Require at least two meaningful excerpt fragments to survive alternate extraction.
                frags=[compact(f) for f in re.split('[，。；;]',r.get('excerpt','')) if len(compact(f))>=6]
                if frags and sum(f in c for f in frags)<max(1,min(2,len(frags))): checks.append('excerpt_overlap_weak')
        core_failures=[c for c in checks if c in {'source_not_found','page_out_of_range','metric_term_not_found','numeric_value_not_found'}]
        results.append({'annotation_id':r['annotation_id'],'source_file':r['source_file'],'pdf_page':r['pdf_page'],'decision':'accept_machine_pass' if not core_failures else 'review_required','checks':checks,'reviewer_id':'machine_second_pass_v1','review_date':'2026-08-28','note':'Independent machine-assisted extraction pass; not a human inter-rater review.'})
    for p in cache.values(): p.close()
    summary={'audit':'machine_second_pass_v1','date':'2026-08-28','records':len(results),'accepted':sum(x['decision']=='accept_machine_pass' for x in results),'review_required':sum(x['decision']!='accept_machine_pass' for x in results),'human_review_completed':False,'results':results}
    args.output.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8')
    fields=['annotation_id','source_file','pdf_page','decision','checks','reviewer_id','review_date','note']
    with args.csv.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for x in results: x=dict(x); x['checks']=';'.join(x['checks']); w.writerow(x)
    print(json.dumps({k:summary[k] for k in ['audit','date','records','accepted','review_required','human_review_completed']},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
