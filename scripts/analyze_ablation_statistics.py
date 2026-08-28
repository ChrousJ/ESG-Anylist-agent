#!/usr/bin/env python3
"""Compute paired bootstrap intervals and exact McNemar tests for ablation runs.

For repeated suites, every case is paired within the same repetition so that
model variability is retained while profile comparisons remain matched.
"""
from __future__ import annotations
import argparse,json,math,random
from pathlib import Path
from typing import Any


def exact_mcnemar(b:int,c:int)->float:
    n=b+c
    if n==0:return 1.0
    k=min(b,c)
    tail=sum(math.comb(n,i) for i in range(k+1))/(2**n)
    return min(1.0,2*tail)


def percentile(xs:list[float],q:float)->float:
    ys=sorted(xs); idx=(len(ys)-1)*q; lo=int(idx); hi=min(len(ys)-1,lo+1); frac=idx-lo
    return ys[lo]*(1-frac)+ys[hi]*frac


def load_results(run_dir:Path)->dict[str,dict[str,Any]]:
    d=json.loads((run_dir/'eval_results.json').read_text())
    return {r['case_id']:r for r in d['langgraph']['results']}


def compare(a_runs:list[dict], b_runs:list[dict], seed:int=20260827, reps:int=10000)->dict:
    pairs=[]
    for a,b in zip(a_runs,b_runs):
        for case_id in sorted(set(a)&set(b)):
            pairs.append((bool(a[case_id].get('case_pass')),bool(b[case_id].get('case_pass'))))
    n=len(pairs); b_only=sum(x and not y for x,y in pairs); c_only=sum(y and not x for x,y in pairs)
    rng=random.Random(seed); diffs=[]
    for _ in range(reps):
        sample=[pairs[rng.randrange(n)] for _ in pairs] if n else []
        diffs.append((sum(y for _,y in sample)-sum(x for x,_ in sample))/len(sample)*100 if sample else 0.0)
    observed=(sum(y for _,y in pairs)-sum(x for x,_ in pairs))/n*100 if n else 0.0
    return {
        'paired_case_runs': n,
        'a_pass': sum(x for x,_ in pairs), 'b_pass': sum(y for _,y in pairs),
        'difference_percentage_points': round(observed,2),
        'bootstrap_95_ci': [round(percentile(diffs,.025),2),round(percentile(diffs,.975),2)],
        'discordant_a_only': b_only, 'discordant_b_only': c_only,
        'mcnemar_exact_p': round(exact_mcnemar(b_only,c_only),6),
    }


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('suite_dir',type=Path); ap.add_argument('--reference',default='no_evaluators'); ap.add_argument('--bootstrap',type=int,default=10000)
    args=ap.parse_args(); suite=json.loads((args.suite_dir/'suite_results.json').read_text())
    by_profile={}
    for r in suite['runs']:
        by_profile.setdefault(r['profile'],{})[int(r['repetition'])]=load_results(Path(r['run_dir']))
    ref=by_profile[args.reference]
    out={'suite_id':suite['suite_id'],'reference':args.reference,'comparisons':{}}
    for name,runs in by_profile.items():
        if name==args.reference: continue
        reps=sorted(set(ref)&set(runs)); out['comparisons'][name]=compare([ref[i] for i in reps],[runs[i] for i in reps],reps=args.bootstrap)
        out['comparisons'][name]['repetitions_used']=reps
    path=args.suite_dir/'paired_statistics.json'; path.write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
