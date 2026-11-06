#!/usr/bin/env python3
"""Checks fixed heldout membership, metrics, evidence reconciliation and support scope."""
from __future__ import annotations
import hashlib,json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import application_evaluation as E

def reject(fn):
    try: fn()
    except (E.EvaluationRefused,ValueError): return
    raise AssertionError("expected refusal")
def sha(s): return hashlib.sha256(s.encode()).hexdigest()
def ev(status,producer,model,oracle): return {"status":status,"evidence_sha256":sha(producer+status),"producer_id":producer,"model_id":model,"oracle_id":oracle,"observed_at":"2026-11-02T00:00:00Z"}
def task(tid,kind="inventory"): return {"task_id":tid,"business_type":kind,"language":"Python","framework":"stdlib","storage":"memory","interface":"CLI","deployment":"isolated process","content_sha256":sha(tid)}
def result(t,first="FAIL",repair="PASS",status="PASS"):
    return {"task_id":t["task_id"],"status":status,"first_attempt_status":first,"repair_status":repair,"heldout":False,"evidence_sha256":sha("result"+t["task_id"]),"elapsed_seconds":1.5,"cost_microunits":0,"cost_source":"measured-local-zero","calls":3,"input_tokens":10,"output_tokens":5,"manual_touches":1,"change_count":2,"refusals":0,"budgets":{"seconds":60,"calls":4},"proposal_evidence":ev("PASS","builder","model-a","oracle-a"),"validator_evidence":ev("PASS","validator","model-b","oracle-b")}
def main():
    tasks=[task("a"),task("b","order"),task("c","ticket"),task("d","content")]
    inv=E.build_inventory(tasks,heldout_fraction=30); assert E.validate_inventory(inv)==inv
    changed=json.loads(json.dumps(inv)); changed["tasks"][0]["partition"]="heldout" if changed["tasks"][0]["partition"]=="development" else "development"; reject(lambda:E.validate_inventory(changed))
    partition={x["task_id"]:x["partition"] for x in inv["tasks"]}; assert partition=={x["task_id"]:x["partition"] for x in E.build_inventory(list(reversed(tasks)),heldout_fraction=30)["tasks"]}
    t=inv["tasks"][0]; r=result(t); r["heldout"]=t["partition"]=="heldout"
    report=E.evaluate(inv,[r]); metric=report["metrics"][t["partition"]]; assert metric["task_count"]==1 and metric["repair_success_count"]==1 and metric["manual_touches_total"]==1 and metric["change_count_total"]==2
    reused=E.reconcile_evidence(ev("PASS","same","same-model","same-oracle"),ev("PASS","same","same-model","same-oracle")); assert reused["outcome"]=="HOLD" and not reused["independent"]
    conflict=E.reconcile_evidence(ev("PASS","builder","m1","o1"),ev("FAIL","validator","m2","o2")); assert conflict["outcome"]=="HOLD" and conflict["conflict"]
    hold=E.reconcile_evidence(ev("PASS","builder","m1","o1"),ev("HOLD","validator","m2","o2")); assert hold["outcome"]=="HOLD"
    def iev(status,model,oracle,oracle_sha): return {"status":status,"evidence_sha256":sha(model+oracle),"producer_id":"harness","model_id":model,"oracle_id":oracle,"oracle_sha256":oracle_sha,"observed_at":"2026-11-06T00:00:00Z"}
    impl=E.reconcile_independent_implementation(iev("PASS","model-a","oracle-a","a"*64),iev("PASS","model-b","oracle-b","b"*64))
    assert impl["outcome"]=="PASS" and impl["authority"]=="independent_implementation" and impl["external_signoff"]=="NOT_RUN" and impl["independent"]
    assert E.reconcile_independent_implementation(iev("PASS","model-a","oracle-a","a"*64),iev("PASS","model-a","oracle-b","b"*64))["outcome"]=="HOLD"
    assert E.reconcile_independent_implementation(iev("PASS","model-a","oracle-a","a"*64),iev("PASS","model-b","oracle-a","b"*64))["outcome"]=="HOLD"
    assert E.reconcile_independent_implementation(iev("PASS","model-a","oracle-a","a"*64),iev("PASS","model-b","oracle-b","a"*64))["outcome"]=="HOLD"
    assert E.reconcile_independent_implementation(iev("PASS","model-a","oracle-a","a"*64),iev("HOLD","model-b","oracle-b","b"*64))["outcome"]=="HOLD"
    bad_lane=iev("PASS","model-b","oracle-b","b"*64); del bad_lane["oracle_sha256"]; reject(lambda:E.reconcile_independent_implementation(iev("PASS","model-a","oracle-a","a"*64),bad_lane))
    bad=result(t); bad["heldout"]=not r["heldout"]; reject(lambda:E.evaluate(inv,[bad]))
    support=E.support_matrix([{"business_type":"inventory","language":"Python","framework":"stdlib","storage":"memory","interface":"CLI","deployment":"isolated","status":"unsupported","evidence_sha256":None,"scope":"No verified path"}]); assert support["capabilities"][0]["status"]=="unsupported"
    reject(lambda:E.support_matrix([{"business_type":"inventory","language":"Python","framework":"x","storage":"x","interface":"x","deployment":"x","status":"verified_support","evidence_sha256":None,"scope":"all"}]))
    manifest=E.run_manifest(task_id="a",task_sha256=sha("task"),reference_sha256=sha("ref"),budgets={"seconds":5,"calls":2},tool_sha256=sha("tool"),acceptance_sha256=sha("accept"),proposal_argv=[sys.executable,"-c","print('adapter')"],replay_argv=[sys.executable,"-c","print('adapter')"],image_id="sha256:"+"a"*64)
    refused=E.execute_lane(manifest,"proposal",timeout_seconds=3,adapter_authorized=False); assert refused["status"]=="REFUSED"
    held=E.execute_lane(manifest,"proposal",timeout_seconds=3,adapter_authorized=True); assert held["status"]=="HOLD"
    held=E.execute_lane(manifest,"proposal",timeout_seconds=3,adapter_authorized=True,container_report={"expected_report_sha256":sha("container")}); assert held["status"]=="HOLD" and held["reason"]=="adapter result envelope invalid or context mismatch"
    replay={"task_id":"a","task_sha256":sha("task"),"reference_sha256":sha("ref"),"budgets":{"seconds":5},"tool_sha256":sha("tool"),"acceptance_sha256":sha("accept"),**ev("PASS","replayer","model-b","oracle-b")}
    proposal={**replay,"producer_id":"proposer","model_id":"model-a","oracle_id":"oracle-a","evidence_sha256":sha("proposal")}
    assert E.reconcile_lanes(proposal,replay)["outcome"]=="PASS"
    mismatch={**replay,"task_sha256":sha("other")}; assert E.reconcile_lanes(proposal,mismatch)["outcome"]=="HOLD"
    assert E.gate_status("G0",{})["status"]=="HOLD"
    g0={"preflight_evidence":{"status":"PASS","evidence_sha256":"a"*64}}
    assert E.gate_status("G0",g0)["status"]=="READY_FOR_REVIEW"
    bad={"preflight_evidence":{"status":"PASS","evidence_sha256":"g"*64}}
    assert E.gate_status("G0",bad)["status"]=="HOLD"
    empty=E.benchmark_report(inv,[]); assert all(empty["metrics"][p]["measurements_status"]=="NOT_MEASURED" for p in ("development","heldout"))
    with tempfile.TemporaryDirectory() as d:
        path=Path(d)/"report.json"; E.write_report(path,report); assert json.loads(path.read_text())==report
    print("application_evaluation_check: PASS (fixed heldout, actual-run metrics, argv refusal and container provenance, replay context, conservative HOLD, gates, unsupported matrix, atomic report)")
if __name__=="__main__": main()
