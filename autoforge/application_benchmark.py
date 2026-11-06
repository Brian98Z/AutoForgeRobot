#!/usr/bin/env python3
"""Finite benchmark inventory, exact trusted adapter lanes, and actual-run reporting."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
import application_evaluation as E

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); s=p.add_subparsers(dest="cmd",required=True)
    m=s.add_parser("manifest"); m.add_argument("input"); m.add_argument("output")
    r=s.add_parser("reconcile"); r.add_argument("proposal"); r.add_argument("replay"); r.add_argument("output")
    q=s.add_parser("report"); q.add_argument("inventory"); q.add_argument("results"); q.add_argument("output")
    b=s.add_parser("inventory"); b.add_argument("tasks"); b.add_argument("output"); b.add_argument("--heldout-fraction",type=int,default=20); b.add_argument("--partition-key",default="autoforge-benchmark-v1")
    g=s.add_parser("gate"); g.add_argument("gate",choices=E.GATES); g.add_argument("evidence"); g.add_argument("output")
    x=s.add_parser("lane"); x.add_argument("manifest"); x.add_argument("lane",choices=("proposal","replay")); x.add_argument("output"); x.add_argument("--timeout",type=int,required=True); x.add_argument("--authorize-trusted-adapter",action="store_true"); x.add_argument("--container-report")
    a=p.parse_args(argv)
    try:
        if a.cmd=="inventory":
            source=E._load_json(a.tasks)
            if isinstance(source,dict) and set(source)=={"schema","clarification_source","original_request_sha256","tasks"} and source["schema"]==2:
                digest=source["original_request_sha256"]
                if not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest): raise E.EvaluationRefused("parent must bind original request sha256 before inventory lock")
                out=E.benchmark_inventory(source["tasks"],heldout_fraction=a.heldout_fraction,partition_key=a.partition_key)
            else: out=E.build_inventory(source,heldout_fraction=a.heldout_fraction,partition_key=a.partition_key)
        elif a.cmd=="manifest": out=E.run_manifest(**E._load_json(a.input))
        elif a.cmd=="reconcile": out=E.reconcile_lanes(E._load_json(a.proposal),E._load_json(a.replay))
        elif a.cmd=="report": out=E.benchmark_report(E._load_json(a.inventory),E._load_json(a.results))
        elif a.cmd=="gate": out=E.gate_status(a.gate,E._load_json(a.evidence))
        else: out=E.execute_lane(E._load_json(a.manifest),a.lane,timeout_seconds=a.timeout,adapter_authorized=a.authorize_trusted_adapter,container_report=E._load_json(a.container_report) if a.container_report else None)
        E.write_report(a.output,out); print(json.dumps(out,ensure_ascii=False,sort_keys=True)); return 0
    except (ValueError,OSError,KeyError,TypeError) as exc: print(f"HOLD: {exc}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
