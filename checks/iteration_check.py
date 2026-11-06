#!/usr/bin/env python3
"""Focused real regression for checkpoint/resume orchestration (offline Python+Julia+Rust)."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse
import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RUNNER=ROOT / "autoforge" / "iterate.py"
EXE=ROOT/"target/release/autoforge.exe"
EXTERNAL_FIT=ROOT / "autoforge" / "external_fit.py"

def run(*args, expected=0):
    env=os.environ.copy()
    for key in tuple(env):
        if key.startswith("AUTOFORGE_"):env.pop(key)
    env.update(AUTOFORGE_PROJECT=str(ROOT),AUTOFORGE_PROVIDER="offline",AUTOFORGE_PYTHON=sys.executable)
    p=subprocess.run([sys.executable,str(RUNNER),*map(str,args)],cwd=ROOT,env=env,capture_output=True,timeout=1200,check=False)
    out=p.stdout.decode("utf-8","replace"); err=p.stderr.decode("utf-8","replace")
    if p.returncode!=expected:raise RuntimeError(f"expected exit {expected}, got {p.returncode}: {args}\n{out}\n{err}")
    return out,err

def require(ok,msg):
    if not ok:raise AssertionError(msg)

def main():
    if not EXE.is_file():raise FileNotFoundError(f"release executable missing: {EXE}")
    with tempfile.TemporaryDirectory(prefix="autoforge-iteration-") as td:
        run_dir=Path(td)/"four-rounds"
        fit=Path(td)/"fit.json"; regression=Path(td)/"regression.json"
        fit.write_text(json.dumps([{"input":[-8,3],"expected":-5},{"input":[7,9],"expected":16}]),encoding="utf-8")
        regression.write_text(fit.read_text(encoding="utf-8"),encoding="utf-8")
        contract={"schema":1,"id":"sum-contract","goal":"Return the signed 64-bit wrapping sum x+y",
                  "public_cases":[{"input":[-8,3],"expected":-5},{"input":[7,9],"expected":16}],
                  "holdout_cases":[{"input":[4,-4],"expected":0}],"max_rounds":2}
        task_list=Path(td)/"tasks.json"; task_list.write_text(json.dumps([contract]),encoding="utf-8")
        evaluator=[sys.executable,str(EXTERNAL_FIT),"--fit",str(fit),"--regression",str(regression),"--max-mae","0"]
        common=("--tasks",task_list,"--evaluator-command",json.dumps(evaluator),"--evaluation-timeout",120)
        out,_=run("--rounds",4,"--checkpoint-every",1,"--proposal-rounds",2,"--timeout",180,
                  "--output",run_dir,"--max-new-rounds",2,*common)
        checkpoints=sorted((run_dir/"checkpoints").glob("*.json"))
        first=json.loads(checkpoints[-1].read_text(encoding="utf-8"))
        require(first["status"]=="PAUSED" and first["target_rounds"]==4 and first["completed_rounds"]==2,
                "first invocation did not pause at two completed of four target rounds")
        require(len(first["rows"])==2 and all(r["line_gate_evidence"].keys()=={"python","julia"} for r in first["rows"]),
                "checkpoint missing per-line gate evidence")
        require(all(r.get("external_evaluation",{}).get("status")=="PASS" for r in first["rows"]),"external evaluator receipt missing")
        state=run_dir/"state"
        status=subprocess.run([str(EXE),"status",str(state)],cwd=ROOT,capture_output=True,timeout=30)
        status_obj=json.loads(status.stdout.decode())
        require(status_obj.get("state")=="READY","Rust state not READY after first segment")
        cp_path=checkpoints[-1]; original=cp_path.read_bytes()
        cp_path.write_bytes(original+b" ")
        _,err=run("--rounds",4,"--checkpoint-every",1,"--proposal-rounds",2,"--timeout",180,"--resume",run_dir,*common,expected=1)
        require("checkpoint" in err.lower() or "json" in err.lower(),"tampered checkpoint was not refused")
        cp_path.write_bytes(original)
        _,err=run("--rounds",5,"--checkpoint-every",1,"--proposal-rounds",2,"--timeout",180,"--resume",run_dir,*common,expected=1)
        require("refused" in err.lower() and ("config" in err.lower() or "parameter" in err.lower()),"changed total target was not refused")
        out,_=run("--rounds",4,"--checkpoint-every",1,"--proposal-rounds",2,"--timeout",180,"--resume",run_dir,"--max-new-rounds",2,*common)
        checkpoints=sorted((run_dir/"checkpoints").glob("*.json")); final=json.loads(checkpoints[-1].read_text(encoding="utf-8"))
        require(final["status"]=="COMPLETE" and final["target_rounds"]==4 and final["completed_rounds"]==4,
                "resume did not complete exact remaining rounds")
        require(len(final["rows"])==4 and len({r["task_id"] for r in final["rows"]})==4,
                "round tasks are not uniquely identified")
        require(final["ledger_count"]>=5,"Rust signed ledger does not contain four promotions")
        require(all(r["signed_gates"].get("oracle") and r["signed_gates"].get("dual_line") and r["fresh_probes"] for r in final["rows"]),
                "completed rounds lack real promotion gate/probe evidence")
        plan,_=run("--rounds",1000,"--proposal-rounds",8,"--checkpoint-every",17,"--timeout",180,
                   "--output",Path(td)/"plan-only-never-created","--plan-only")
        planned=json.loads(plan)
        require(planned["total_tasks"]==1000 and planned["config"]["rounds"]==1000 and planned["config"]["proposal_rounds"]==8,
                "1000-round plan parameters were not represented exactly")
        require(not (Path(td)/"plan-only-never-created").exists(),"plan-only created runner state/output")
        report={"status":"PASS","scope":"real offline Rust promotions with Python and Julia providers; 4 rounds total, executed 2+2",
                "completed_rounds":final["completed_rounds"],"target_rounds":final["target_rounds"],
                "ledger_count":final["ledger_count"],"checkpoint_files":len(checkpoints),
                "negative_controls":{"checkpoint_tamper":"REFUSED","changed_config":"REFUSED"},
                "plan_only":{"target_rounds":planned["total_tasks"],"proposal_rounds":planned["config"]["proposal_rounds"]},
                "model_weight_learning_claimed":False}
        out_path=ROOT/"bootstrap-runs"/"iteration-check-latest.json"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
        print(json.dumps(report,indent=2,ensure_ascii=False))
    return 0
if __name__=="__main__":raise SystemExit(main())
