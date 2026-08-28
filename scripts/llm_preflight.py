#!/usr/bin/env python3
"""Validate the configured generation provider before an expensive online suite."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
except Exception:
    pass


def check(timeout: float=15.0) -> dict:
    from agent.llm_provider import get_default_model, llm_generate_content
    model=os.getenv('LLM_MAIN_MODEL',get_default_model())
    provider=os.getenv('LLM_PROVIDER','gemini')
    try:
        response=llm_generate_content(model=model,contents='Reply with exactly OK.',config={'temperature':0,'max_output_tokens':8,'timeout':timeout})
        text=str(getattr(response,'text','')).strip()
        return {'status':'pass','provider':provider,'model':model,'response_preview':text[:40]}
    except Exception as exc:
        message=str(exc)
        return {'status':'fail','provider':provider,'model':model,'error':message[:500],
                'likely_auth_error': any(x in message.lower() for x in ('401','unauthorized','invalid api key','authentication'))}


def main():
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument('--timeout',type=float,default=15.0); args=ap.parse_args()
    report=check(args.timeout); print(json.dumps(report,ensure_ascii=False,indent=2)); raise SystemExit(0 if report['status']=='pass' else 1)
if __name__=='__main__':main()
