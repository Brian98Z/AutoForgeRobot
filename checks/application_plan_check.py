#!/usr/bin/env python3
"""Checks for durable plans through real trusted JSON adapter processes."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
import sys
import tempfile
import subprocess
import time
from pathlib import Path
import application_plan as plan
import application_spec


def spec_fixture():
    raw={"schema":1,"spec_id":"plan-test","version":1,"goal":"run trusted adapter","requirements":[{"id":"R","statement":"Produce recorded adapter evidence","provenance":"user","source":"request","inputs":["task"],"outputs":["evidence"],"failures":["invalid request held"],"invariants":["task and acceptance are bound"],"state_transitions":[],"non_goals":[],"acceptance_ids":["A"],"ambiguity_ids":[],"conflict_ids":[]}],"entities":[],"tasks":[{"id":"T1","title":"first","depends_on":[],"requirement_ids":["R"],"acceptance_ids":["A"],"kind":"implementation"},{"id":"T2","title":"second","depends_on":["T1"],"requirement_ids":["R"],"acceptance_ids":["A"],"kind":"verification"}],"oracle":{"public_acceptance_ids":["A"],"hidden_oracle_ids":["H"],"separation":"separate"},"ambiguities":[],"conflicts":[],"non_goals":[]}
    return application_spec.approve_spec(application_spec.propose_spec(raw),operator="operator",authorization_source="check fixture approval")

ADAPTER='import json,sys; p=json.load(sys.stdin); print(json.dumps({"status":"SUCCEEDED","evidence_id":"fixture-"+p["task_id"],"task_id":p["task_id"],"attempt_id":p["attempt_id"],"requirement_ids":p["requirement_ids"],"acceptance_ids":p["acceptance_ids"]}))'
def make_adapter():return [sys.executable,"-c",ADAPTER]
def cli(*args,timeout=10):
    return subprocess.run([sys.executable,str(Path(plan.__file__)),*map(str,args)],capture_output=True,text=True,timeout=timeout,check=False)
def create_cli(spec_path,adapters_path,output_path,timeout=10):
    return subprocess.run([sys.executable,str(Path(application_spec.__file__)),"plan",str(spec_path),"--adapters",str(adapters_path),"--output",str(output_path)],capture_output=True,text=True,timeout=timeout,check=False)

def main():
  with tempfile.TemporaryDirectory() as td:
    spec=spec_fixture();argv={"implementation":make_adapter(),"verification":make_adapter()}
    p=Path(td)/"plan.json";assert plan.create(spec,argv,p)["status"]=="READY"
    assert plan.pause(p)["status"]=="PAUSED";assert plan.resume(p)["status"]=="READY"
    done=plan.run(p);assert done["status"]=="SUCCEEDED"
    assert done["tasks"][0]["evidence"]["evidence_id"]=="fixture-T1"
    assert done["tasks"][1]["evidence"]["evidence_id"]=="fixture-T2"
    assert plan.run(p)["status"]=="SUCCEEDED"
    p2=Path(td)/"unknown.json";record=plan.create(spec,argv,p2);record["tasks"][0]["state"]="RUNNING";record["tasks"][0]["attempt_id"]="orphaned-attempt";plan._save(p2,record)
    assert plan._recover_orphaned_inflight(p2)["status"]=="UNKNOWN"
    recovered=plan.reconcile(p2,"T1",outcome="SUCCEEDED",evidence={"evidence_id":"observed-external-evidence"},operator="operator",source="manual execution ledger")
    assert recovered["tasks"][0]["state"]=="SUCCEEDED" and recovered["tasks"][1]["state"]=="PENDING"
    p3=Path(td)/"blocked.json";plan.create(spec,argv,p3);record=plan._load(p3);record["tasks"][0]["state"]="FAILED";record["tasks"][0]["error"]={"reason":"fixture failure"};plan._refresh(record);plan._save(p3,record)
    assert plan._load(p3)["tasks"][1]["state"]=="BLOCKED"
    p4=Path(td)/"batch.json";plan.create(spec,argv,p4);partial=plan.run(p4,max_tasks=1)
    assert partial["status"]=="PAUSED" and partial["tasks"][0]["state"]=="SUCCEEDED"
    assert plan.resume(p4)["status"]=="READY";assert plan.run(p4)["status"]=="SUCCEEDED"
    p0=Path(td)/"zero.json";plan.create(spec,argv,p0);zero=plan.run(p0,max_tasks=0)
    assert zero["status"]=="PAUSED" and all(t["state"]=="PENDING" for t in zero["tasks"])
    for invalid_timeout in (0,-1,float("inf"),float("nan"),True):
        try:plan.run(p0,timeout=invalid_timeout)
        except plan.PlanRefused:pass
        else:raise AssertionError(f"invalid timeout accepted: {invalid_timeout!r}")

    # Actual CLI path consumes approved spec + explicit trusted adapters, never candidate source.
    cli_spec=Path(td)/"approved-spec.json";cli_spec.write_text(json.dumps(spec),encoding="utf-8")
    good=make_adapter();adapters=Path(td)/"adapters.json";adapters.write_text(json.dumps({"implementation":good,"verification":good}),encoding="utf-8")
    cp=Path(td)/"cli-plan.json"
    created=create_cli(cli_spec,adapters,cp);assert created.returncode==0,created.stderr
    assert json.loads(created.stdout)["status"]=="READY"
    ran=cli("run",cp);assert ran.returncode==0,ran.stdout+ran.stderr
    result=json.loads(ran.stdout);assert result["status"]=="SUCCEEDED"
    assert all(t["evidence"]["task_id"]==t["id"] and t["evidence"]["attempt_id"]==t["attempt_id"] for t in result["tasks"])
    wrong='import json,sys; p=json.load(sys.stdin); print(json.dumps({"status":"SUCCEEDED","evidence_id":"wrong-attempt","task_id":p["task_id"],"attempt_id":"not-"+p["attempt_id"],"requirement_ids":p["requirement_ids"],"acceptance_ids":p["acceptance_ids"]}))'
    bad_adapters=Path(td)/"wrong-adapters.json";bad_adapters.write_text(json.dumps({"implementation":[sys.executable,"-c",wrong],"verification":good}),encoding="utf-8")
    bad_plan=Path(td)/"wrong-plan.json";assert create_cli(cli_spec,bad_adapters,bad_plan).returncode==0
    bad_run=cli("run",bad_plan);assert bad_run.returncode==3
    bad_state=json.loads(bad_run.stdout);assert bad_state["status"]=="UNKNOWN" and bad_state["tasks"][0]["state"]=="UNKNOWN"
    assert cli("resume",bad_plan).returncode==3 and json.loads(cli("status",bad_plan).stdout)["tasks"][0]["state"]=="UNKNOWN"

    marker=Path(td)/"adapter-started"
    long='import json,sys,time,pathlib; pathlib.Path('+repr(str(marker))+').write_text("started"); time.sleep(30)'
    long_adapters=Path(td)/"long-adapters.json";long_argv=[sys.executable,"-c",long];long_adapters.write_text(json.dumps({"implementation":long_argv,"verification":good}),encoding="utf-8")
    long_plan=Path(td)/"long-plan.json";assert create_cli(cli_spec,long_adapters,long_plan).returncode==0
    runner=subprocess.Popen([sys.executable,str(Path(plan.__file__)),"run",str(long_plan),"--timeout","20"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+5
        while not marker.exists() and runner.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists(),"long trusted adapter did not start"
        paused=cli("pause",long_plan);assert paused.returncode==3 and json.loads(paused.stdout)["status"]=="PAUSED"
        out,err=runner.communicate(timeout=8);assert runner.returncode==3,(out,err)
        long_state=json.loads(cli("status",long_plan).stdout)
        assert long_state["status"]=="UNKNOWN" and long_state["tasks"][0]["state"]=="UNKNOWN"
        assert cli("resume",long_plan).returncode==3
        assert json.loads(cli("status",long_plan).stdout)["tasks"][0]["state"]=="UNKNOWN"
    finally:
        if runner.poll() is None:
            runner.kill();runner.wait(timeout=5)
    # Deadline of an already launched adapter is unknown, never a reusable pending task.
    timed_plan=Path(td)/'timed-plan.json';assert create_cli(cli_spec,long_adapters,timed_plan).returncode==0
    timed=cli('run',timed_plan,'--timeout','0.3',timeout=8)
    assert timed.returncode==3 and json.loads(timed.stdout)['tasks'][0]['state']=='UNKNOWN'
    assert cli('resume',timed_plan).returncode==3
  print("application_plan_check: PASS (trusted adapter CLI DAG, task-limit pause/resume, exact attempt binding, live pause termination, unknown non-replay, idempotency and dependency refusal; not generated-app/container evidence)")
if __name__=="__main__":main()
