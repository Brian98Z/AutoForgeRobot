#!/usr/bin/env python3
"""Deterministic benchmark inventory, trusted lane protocol, and evidence reporting."""
from __future__ import annotations
import argparse, hashlib, json, sys
from pathlib import Path
from typing import Any
import application_checkpoint as checkpoint

import os, subprocess, time, tempfile
import applications

GATES={
    "G0":{"deps":[],"entry":"scope and authorization are declared","exit":"read-only/configuration checks have a recorded result","evidence":["preflight_evidence"]},
    "G1":{"deps":["G0"],"entry":"G0 exit evidence; explicit provider configuration","exit":"schema, permissions, stop behavior and a real tool interaction are evidenced","evidence":["real_tool_loop_evidence"]},
    "G2":{"deps":["G1"],"entry":"G1 exit evidence; approved isolated environment","exit":"candidate changes and execution are isolated and evidenced","evidence":["candidate_evidence","isolated_execution_evidence"]},
    "G3":{"deps":["G2"],"entry":"G2 exit evidence; independent acceptance criteria","exit":"real correction feedback and independent acceptance are evidenced","evidence":["correction_evidence","independent_acceptance_evidence"]},
    "G4":{"deps":["G3"],"entry":"G3 exit evidence; deployment authorization where relevant","exit":"persistent multi-round operation and deployment expansion are separately verified","evidence":["persistent_operation_evidence","deployment_evidence"]}}

def run_manifest(*,task_id:str,task_sha256:str,reference_sha256:str,budgets:dict,tool_sha256:str,acceptance_sha256:str,proposal_argv:list[str],replay_argv:list[str],image_id:str)->dict:
    """Freeze identical task context and exact trusted argv for two independently owned lanes."""
    _text(task_id,"task_id")
    for k,v in (("task_sha256",task_sha256),("reference_sha256",reference_sha256),("tool_sha256",tool_sha256),("acceptance_sha256",acceptance_sha256),("image_id",image_id)):
        if k=="image_id":
            if not isinstance(v,str) or not v.startswith("sha256:") or len(v)!=71: raise EvaluationRefused("image_id must be exact sha256 image ID")
        else: _digest(v,k)
    if not isinstance(budgets,dict) or not budgets or any(type(v) not in (int,float) or v<0 for v in budgets.values()): raise EvaluationRefused("budgets must be configured nonnegative limits")
    lanes={}
    for name,argv in (("proposal",proposal_argv),("replay",replay_argv)):
        if not isinstance(argv,list) or not argv or any(not isinstance(x,str) or not x for x in argv): raise EvaluationRefused(name+" exact argv required")
        lanes[name]={"argv":argv,"argv_sha256":hashlib.sha256(checkpoint.canonical(argv)).hexdigest()}
    payload={"schema":1,"task_id":task_id,"task_sha256":task_sha256,"reference_sha256":reference_sha256,"budgets":budgets,"tool_sha256":tool_sha256,"acceptance_sha256":acceptance_sha256,"image_id":image_id,"lanes":lanes,"limitations":["argv lanes execute trusted adapters only; candidate execution must be delegated to application_build/application_builder container path.","lane identity declarations are not authenticated by this manifest."]}
    return {**payload,"sha256":_sha(payload)}

def _validate_run_manifest(m):
    if not isinstance(m,dict) or set(m)!={"schema","task_id","task_sha256","reference_sha256","budgets","tool_sha256","acceptance_sha256","image_id","lanes","limitations","sha256"} or m.get("schema")!=1: raise EvaluationRefused("run manifest envelope invalid")
    args={k:m[k] for k in ("task_id","task_sha256","reference_sha256","budgets","tool_sha256","acceptance_sha256","image_id")}
    lanes=m["lanes"]
    if not isinstance(lanes,dict) or set(lanes)!={"proposal","replay"}: raise EvaluationRefused("two lanes required")
    rebuilt=run_manifest(**args,proposal_argv=lanes["proposal"].get("argv"),replay_argv=lanes["replay"].get("argv"))
    if rebuilt!=m: raise EvaluationRefused("run manifest digest mismatch")
    return rebuilt

def execute_lane(manifest:dict,lane:str,*,timeout_seconds:int,adapter_authorized:bool,container_report:dict|None=None)->dict:
    """Execute exact trusted adapter argv. Candidate code is never launched on host."""
    m=_validate_run_manifest(manifest)
    if lane not in ("proposal","replay"): raise EvaluationRefused("unknown lane")
    if not adapter_authorized: return {"status":"REFUSED","reason":"trusted adapter authorization absent","lane":lane,"argv_sha256":m["lanes"][lane]["argv_sha256"]}
    if type(timeout_seconds)is not int or not 1<=timeout_seconds<=3600: raise EvaluationRefused("bounded timeout_seconds required")
    # Caller-provided report is only a handoff expectation. The subprocess must return the actual bound report envelope.
    if not isinstance(container_report,dict) or not isinstance(container_report.get("expected_report_sha256"),str):
        return {"status":"HOLD","reason":"expected builder report digest missing","lane":lane,"argv_sha256":m["lanes"][lane]["argv_sha256"]}
    _digest(container_report["expected_report_sha256"],"expected_report_sha256")
    started=time.monotonic()
    try:
        with tempfile.TemporaryFile() as stdout,tempfile.TemporaryFile() as stderr:
            proc=subprocess.Popen(m["lanes"][lane]["argv"],stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,shell=False,start_new_session=os.name!="nt",close_fds=True)
            try:
                while True:
                    if os.fstat(stdout.fileno()).st_size>applications.MAX_STDOUT_BYTES or os.fstat(stderr.fileno()).st_size>applications.MAX_STDERR_BYTES: raise EvaluationRefused("trusted adapter output exceeds bounded limit")
                    remaining=timeout_seconds-(time.monotonic()-started)
                    if remaining<=0: raise EvaluationRefused("trusted adapter deadline exceeded")
                    try: proc.wait(timeout=min(0.05,remaining)); break
                    except subprocess.TimeoutExpired: continue
                stdout.seek(0); stderr.seek(0); out=stdout.read(applications.MAX_STDOUT_BYTES+1); err=stderr.read(applications.MAX_STDERR_BYTES+1)
                if len(out)>applications.MAX_STDOUT_BYTES or len(err)>applications.MAX_STDERR_BYTES: raise EvaluationRefused("trusted adapter output exceeds bounded limit")
                elapsed=time.monotonic()-started
                try: observed=json.loads(out.decode("utf-8"))
                except (UnicodeDecodeError,json.JSONDecodeError): observed=None
                valid=(proc.returncode==0 and isinstance(observed,dict) and observed.get("schema")==1 and observed.get("task_id")==m["task_id"] and observed.get("task_sha256")==m["task_sha256"] and observed.get("reference_sha256")==m["reference_sha256"] and observed.get("budgets")==m["budgets"] and observed.get("tool_sha256")==m["tool_sha256"] and observed.get("acceptance_sha256")==m["acceptance_sha256"] and observed.get("status") in STATUSES and isinstance(observed.get("evidence_sha256"),str) and len(observed["evidence_sha256"])==64 and all(c in "0123456789abcdef" for c in observed["evidence_sha256"]) and observed.get("container_report_sha256")==container_report["expected_report_sha256"])
                return {"status":observed["status"] if valid else ("FAIL" if proc.returncode else "HOLD"),"reason":None if valid else "adapter result envelope invalid or context mismatch","returncode":proc.returncode,"elapsed_seconds":elapsed,"stdout_sha256":hashlib.sha256(out).hexdigest(),"stderr_sha256":hashlib.sha256(err).hexdigest(),"argv_sha256":m["lanes"][lane]["argv_sha256"],"lane":lane,"observed_result":observed if valid else None,"authority":"bounded adapter subprocess returned context-bound result; identity and supplied container digest remain caller-declared"}
            finally: applications._terminate_process(proc)
    except (OSError,EvaluationRefused) as exc:
        return {"status":"HOLD","reason":str(exc),"elapsed_seconds":time.monotonic()-started,"argv_sha256":m["lanes"][lane]["argv_sha256"],"lane":lane}

def gate_status(gate:str,evidence:dict)->dict:
    if gate not in GATES: raise EvaluationRefused("unknown gate")
    if not isinstance(evidence,dict): raise EvaluationRefused("gate evidence must be object")
    definition=GATES[gate]; missing=[]; invalid=[]
    def has_digest(item):
        digest=item.get('evidence_sha256') if isinstance(item,dict) else None
        return isinstance(digest,str) and len(digest)==64 and all(ch in '0123456789abcdef' for ch in digest)
    for key in definition['evidence']:
        item=evidence.get(key)
        if not isinstance(item,dict) or item.get('status')!='PASS' or not has_digest(item):missing.append(key)
    prior=definition['deps']
    for dependency in prior:
        dep=evidence.get(dependency)
        if not isinstance(dep,dict) or dep.get('status')!='PASS' or not has_digest(dep):invalid.append(dependency)
    status='READY_FOR_REVIEW' if not missing and not invalid else 'HOLD'
    return {'gate':gate,'status':status,'dependencies':prior,'entry':definition['entry'],'exit':definition['exit'],'required_evidence':definition['evidence'],'missing_evidence':missing,'unpassed_dependencies':invalid,'authority':'machine-checkable evidence declaration presence and SHA-256 shape only; bytes/provenance/approval not verified'}
PLATFORM_MATRIX=[{"interface":"CLI","platform":"local host controller; candidate only via exact local Linux container","protocol":"JSON-lines or trusted argv adapter","adapter":"application_builder / application_build","proof":"implemented_unverified per benchmark task"},{"interface":"web","platform":"local container service via controlled loopback tunnel","protocol":"HTTP contract; optional authored browser checks","adapter":"application_service / container_http_tunnel / application_browser","proof":"implemented_unverified; no general generated-app claim"},{"interface":"background","platform":"local controller process","protocol":"durable job/session","adapter":"agent_runtime / application_plan","proof":"implemented_unverified per benchmark task"},{"interface":"desktop","platform":"unsupported","protocol":"unsupported","adapter":"unsupported","proof":"unsupported"},{"interface":"mobile","platform":"unsupported","protocol":"unsupported","adapter":"unsupported","proof":"unsupported"},{"interface":"realtime","platform":"unsupported","protocol":"unsupported","adapter":"unsupported","proof":"unsupported"}]
COMPONENT_MATRIX=[{"component":"file_upload","adapter":"application_service HTTP request handling only","scope":"no durable upload/object storage","proof":"unsupported"},{"component":"object_store","adapter":"none","scope":"no cloud or durable object-store adapter","proof":"unsupported"},{"component":"queue","adapter":"application_plan local dependency DAG","scope":"local in-process scheduling only; no external broker","proof":"implemented_unverified scoped-local"},{"component":"retry","adapter":"agent_runtime resume/uncertain-effect reconciliation","scope":"no generic external request retry policy","proof":"implemented_unverified scoped-runtime"},{"component":"scheduled_jobs","adapter":"none","scope":"no cron/scheduler integration","proof":"unsupported"},{"component":"search","adapter":"agent_tools reference search","scope":"local reference corpus only; no app search service","proof":"implemented_unverified scoped-reference"},{"component":"cache","adapter":"application_dependencies local artifact cache","scope":"offline dependency artifact cache only; no runtime/distributed cache","proof":"implemented_unverified scoped-artifact"},{"component":"notifications","adapter":"none","scope":"no email/SMS/push provider","proof":"unsupported"},{"component":"import_export","adapter":"none","scope":"no general data import/export adapter","proof":"unsupported"},{"component":"third_party_integrations","adapter":"none","scope":"no external SaaS/API integration adapters","proof":"unsupported"}]
def benchmark_report(inventory:dict,results:list[dict])->dict:
    report=evaluate(inventory,results)
    for part,metrics in report["metrics"].items():
        subset=[r for r in report["results"] if r["partition"]==part]
        def distribution(key):
            values=sorted(r[key] for r in subset if r[key] is not None)
            if not values:return None
            return {"count":len(values),"min":values[0],"median":values[len(values)//2],"max":values[-1]}
        metrics["distributions"]={key:distribution(key) for key in ("elapsed_seconds","cost_microunits","calls","input_tokens","output_tokens","manual_touches","change_count","refusals")}
        metrics["measurements_status"]="actual_runs_only" if subset else "NOT_MEASURED"
    report["platform_matrix"]=PLATFORM_MATRIX
    report["component_matrix"]=COMPONENT_MATRIX
    report["limitations"]=["Only supplied actual run records contribute measurements; missing records are not imputed.","Heldout assignment is deterministic inventory partitioning, not cryptographic lock or proof of concealment.","Lane process evidence and declared producer/model/oracle identities are not cryptographically authenticated.","Platform and component matrices are scoped declarations, not evidence that every product stack was executed.","No UI-wide, component-wide or third-party integration capability is claimed from module availability."]
    payload={k:v for k,v in report.items() if k!="sha256"}
    return {**payload,"sha256":_sha(payload)}

def reconcile_lanes(proposal:dict,replay:dict)->dict:
    required={"task_id","task_sha256","reference_sha256","budgets","tool_sha256","acceptance_sha256","status","evidence_sha256","producer_id","model_id","oracle_id","observed_at"}
    for name,row in (("proposal",proposal),("replay",replay)):
        if not isinstance(row,dict) or not required.issubset(row):raise EvaluationRefused(name+" lane record incomplete")
    keys=("task_id","task_sha256","reference_sha256","budgets","tool_sha256","acceptance_sha256")
    if any(proposal[k]!=replay[k] for k in keys):
        return {"outcome":"HOLD","reason":"lane context mismatch","compared":list(keys),"authority":"not an independent replay"}
    evidence=reconcile_evidence({k:proposal[k] for k in ("status","evidence_sha256","producer_id","model_id","oracle_id","observed_at")},{k:replay[k] for k in ("status","evidence_sha256","producer_id","model_id","oracle_id","observed_at")})
    return {**evidence,"same_task_context":True,"authority":"protocol-level replay claim only; declared identities and replay independence are unauthenticated"}
SCHEMA=1
KINDS={"inventory","order","ticket","content"}
STATUSES={"PASS","FAIL","HOLD","INCONCLUSIVE","REFUSED"}
class EvaluationRefused(ValueError): pass

def _digest(v,where):
    if not isinstance(v,str) or len(v)!=64 or any(c not in "0123456789abcdef" for c in v): raise EvaluationRefused(f"{where} must be SHA-256")
    return v
def _text(v,where):
    if not isinstance(v,str) or not v.strip(): raise EvaluationRefused(f"{where} required")
    return v.strip()
def _sha(v): return checkpoint.digest(checkpoint.canonical(v))
def _load_json(path,limit=16*1024*1024):
    with Path(path).open("rb") as f: raw=f.read(limit+1)
    if len(raw)>limit: raise EvaluationRefused("input exceeds size bound")
    return json.loads(raw.decode("utf-8"))


def benchmark_inventory(tasks:list[dict],*,heldout_fraction:int=20,partition_key:str="autoforge-benchmark-v1"):
    """Convert versioned public task manifest into the immutable evaluation inventory."""
    rows=[]
    for n,t in enumerate(tasks):
        fields={"task_id","task_version","business_type","language","framework","storage","interface","deployment","public_input","acceptance"}
        if not isinstance(t,dict) or set(t)!=fields: raise EvaluationRefused(f"benchmark tasks[{n}] schema invalid")
        if t["task_version"]!="2": raise EvaluationRefused("unsupported public task version")
        public={"task_version":t["task_version"],"public_input":_text(t["public_input"],"public_input"),"acceptance":_text(t["acceptance"],"acceptance")}
        meta={k:t[k] for k in ("task_id","business_type","language","framework","storage","interface","deployment")}
        rows.append({**meta,"content_sha256":_sha(public)})
    return build_inventory(rows,heldout_fraction=heldout_fraction,partition_key=partition_key)
def build_inventory(tasks: list[dict], *, heldout_fraction: int=20, partition_key: str="autoforge-eval-v1") -> dict:
    """Assign fixed heldout membership by stable task ID; never uses task outcomes."""
    if type(heldout_fraction) is not int or not 1<=heldout_fraction<=99: raise EvaluationRefused("heldout_fraction must be 1..99 percent")
    if not isinstance(tasks,list) or not tasks: raise EvaluationRefused("tasks must be nonempty")
    ids=set(); rows=[]
    for i,t in enumerate(tasks):
        if not isinstance(t,dict) or set(t)!={"task_id","business_type","language","framework","storage","interface","deployment","content_sha256"}: raise EvaluationRefused(f"tasks[{i}] shape invalid")
        tid=_text(t["task_id"],"task_id")
        if tid in ids: raise EvaluationRefused("duplicate task_id")
        ids.add(tid)
        if t["business_type"] not in KINDS: raise EvaluationRefused("unsupported business_type label")
        row={k:t[k] for k in t}
        for key in ("language","framework","storage","interface","deployment"): _text(row[key],key)
        _digest(row["content_sha256"],"content_sha256")
        bucket=int(hashlib.sha256((partition_key+"\0"+tid).encode()).hexdigest()[:8],16)%100
        row["partition"]="heldout" if bucket<heldout_fraction else "development"
        rows.append(row)
    rows.sort(key=lambda r:r["task_id"])
    payload={"schema":SCHEMA,"partition_key":_text(partition_key,"partition_key"),"heldout_fraction":heldout_fraction,"tasks":rows}
    return {**payload,"sha256":_sha(payload)}

def validate_inventory(inventory: Any) -> dict:
    if not isinstance(inventory,dict) or set(inventory)!={"schema","partition_key","heldout_fraction","tasks","sha256"} or inventory.get("schema")!=SCHEMA: raise EvaluationRefused("inventory envelope invalid")
    rebuilt=build_inventory([{k:v for k,v in r.items() if k!="partition"} for r in inventory["tasks"]],heldout_fraction=inventory["heldout_fraction"],partition_key=inventory["partition_key"])
    if rebuilt!=inventory: raise EvaluationRefused("inventory digest or fixed partition mismatch")
    return rebuilt

def reconcile_evidence(proposal: dict, validator: dict) -> dict:
    """Conservatively reconcile independently produced, content-bound evidence."""
    required={"status","evidence_sha256","producer_id","model_id","oracle_id","observed_at"}
    for name,obj in (("proposal",proposal),("validator",validator)):
        if not isinstance(obj,dict) or set(obj)!=required: raise EvaluationRefused(f"{name} evidence shape invalid")
        if obj["status"] not in STATUSES: raise EvaluationRefused(f"{name} status invalid")
        _digest(obj["evidence_sha256"],name+".evidence_sha256")
        for k in ("producer_id","model_id","oracle_id","observed_at"): _text(obj[k],name+"."+k)
    shared=[]
    if proposal["producer_id"]==validator["producer_id"]: shared.append("producer")
    if proposal["model_id"]==validator["model_id"]: shared.append("model")
    if proposal["oracle_id"]==validator["oracle_id"]: shared.append("oracle")
    conflict=proposal["status"]!=validator["status"]
    missing=proposal["status"] in {"HOLD","INCONCLUSIVE"} or validator["status"] in {"HOLD","INCONCLUSIVE"}
    independent=not shared
    if conflict or missing or not independent: outcome="HOLD"
    elif proposal["status"]=="PASS" and validator["status"]=="PASS": outcome="PASS"
    elif proposal["status"]=="REFUSED" and validator["status"]=="REFUSED": outcome="REFUSED"
    else: outcome="FAIL"
    payload={"outcome":outcome,"proposal":proposal,"validator":validator,"independent":independent,"shared_authorities":shared,"conflict":conflict,"limitations":["Evidence identity declarations are not cryptographically authenticated by this module.","Shared model or oracle prevents claiming independent confirmation."]}
    return {**payload,"sha256":_sha(payload)}

def reconcile_independent_implementation(proposal: dict, validator: dict) -> dict:
    """Reconcile an independently authored judgement implementation against the builder lane.

    Independence here is implementation independence, not external authority: the executing
    harness may be shared, while the authoring model and the judgement program must differ.
    Recorded hashes are content bindings, not signatures, and the result never claims
    external signoff.
    """
    required={"status","evidence_sha256","producer_id","model_id","oracle_id","oracle_sha256","observed_at"}
    for name,obj in (("proposal",proposal),("validator",validator)):
        if not isinstance(obj,dict) or set(obj)!=required: raise EvaluationRefused(f"{name} evidence shape invalid")
        if obj["status"] not in STATUSES: raise EvaluationRefused(f"{name} status invalid")
        _digest(obj["evidence_sha256"],name+".evidence_sha256")
        _digest(obj["oracle_sha256"],name+".oracle_sha256")
        for k in ("producer_id","model_id","oracle_id","observed_at"): _text(obj[k],name+"."+k)
    shared=[]
    if proposal["model_id"]==validator["model_id"]: shared.append("model")
    if proposal["oracle_id"]==validator["oracle_id"]: shared.append("oracle")
    if proposal["oracle_sha256"]==validator["oracle_sha256"]: shared.append("oracle_sha256")
    conflict=proposal["status"]!=validator["status"]
    missing=proposal["status"] in {"HOLD","INCONCLUSIVE"} or validator["status"] in {"HOLD","INCONCLUSIVE"}
    if conflict or missing or shared: outcome="HOLD"
    elif proposal["status"]=="PASS" and validator["status"]=="PASS": outcome="PASS"
    elif proposal["status"]=="REFUSED" and validator["status"]=="REFUSED": outcome="REFUSED"
    else: outcome="FAIL"
    payload={"criterion":"independent_implementation","outcome":outcome,"proposal":proposal,"validator":validator,"independent":not shared,"shared_authorities":shared,"conflict":conflict,"authority":"independent_implementation","external_signoff":"NOT_RUN","limitations":["Independence is implementation independence: a different authoring model and a different judgement program, not an external organizational approval.","Evidence identity declarations and hashes are content bindings, not cryptographic signatures.","A shared executing harness is allowed by this criterion and is not evidence of independence."]}
    return {**payload,"sha256":_sha(payload)}

def _count(v,where):
    if type(v) is not int or v<0: raise EvaluationRefused(f"{where} must be nonnegative integer")
    return v

def evaluate(inventory: dict, results: list[dict]) -> dict:
    inv=validate_inventory(inventory)
    if not isinstance(results,list): raise EvaluationRefused("results must be list")
    byid={}
    fields={"task_id","status","first_attempt_status","repair_status","heldout","evidence_sha256","elapsed_seconds","cost_microunits","cost_source","calls","input_tokens","output_tokens","manual_touches","change_count","refusals","budgets","proposal_evidence","validator_evidence"}
    for i,r in enumerate(results):
        if not isinstance(r,dict) or set(r)!=fields: raise EvaluationRefused(f"results[{i}] shape invalid")
        tid=_text(r["task_id"],"task_id")
        if tid in byid: raise EvaluationRefused("duplicate result task")
        byid[tid]=r
    known={t["task_id"]:t for t in inv["tasks"]}
    if set(byid)-known.keys(): raise EvaluationRefused("result references unknown task")
    rows=[]
    for tid,r in sorted(byid.items()):
        task=known[tid]
        if type(r["heldout"]) is not bool or r["heldout"]!=(task["partition"]=="heldout"): raise EvaluationRefused("result partition does not match fixed inventory")
        if r["status"] not in STATUSES or r["first_attempt_status"] not in STATUSES or r["repair_status"] not in STATUSES: raise EvaluationRefused("invalid status")
        _digest(r["evidence_sha256"],"result.evidence_sha256")
        if not isinstance(r["elapsed_seconds"],(int,float)) or isinstance(r["elapsed_seconds"],bool) or r["elapsed_seconds"]<0: raise EvaluationRefused("elapsed_seconds invalid")
        for k in ("cost_microunits","calls","manual_touches","change_count","refusals"): _count(r[k],k)
        for k in ("input_tokens","output_tokens"):
            if r[k] is not None: _count(r[k],k)
        if not isinstance(r["cost_source"],str) or not r["cost_source"].strip(): raise EvaluationRefused("cost_source required; use NOT_RECORDED only when no cost measurement exists")
        if not isinstance(r["budgets"],dict) or not r["budgets"] or any(not isinstance(k,str) or type(v) not in (int,float) or v<0 for k,v in r["budgets"].items()): raise EvaluationRefused("budgets must contain nonnegative configured limits")
        reconciled=reconcile_evidence(r["proposal_evidence"],r["validator_evidence"])
        # Report outcome cannot override evidence reconciliation.
        effective=reconciled["outcome"]
        if r["status"]!=effective: effective="HOLD" if "HOLD" in (r["status"],effective) else "INCONCLUSIVE"
        rows.append({"task_id":tid,"business_type":task["business_type"],"partition":task["partition"],"reported_status":r["status"],"evidence_status":effective,"first_attempt_status":r["first_attempt_status"],"repair_status":r["repair_status"],"elapsed_seconds":r["elapsed_seconds"],"cost_microunits":r["cost_microunits"],"cost_source":r["cost_source"],"calls":r["calls"],"input_tokens":r["input_tokens"],"output_tokens":r["output_tokens"],"manual_touches":r["manual_touches"],"change_count":r["change_count"],"refusals":r["refusals"],"budgets":r["budgets"],"reconciliation":reconciled})
    metrics={}
    for part in ("development","heldout"):
        subset=[r for r in rows if r["partition"]==part]
        metrics[part]={"task_count":len(subset),"first_success_count":sum(r["first_attempt_status"]=="PASS" and r["evidence_status"]=="PASS" for r in subset),"repair_success_count":sum(r["first_attempt_status"]!="PASS" and r["repair_status"]=="PASS" and r["evidence_status"]=="PASS" for r in subset),"pass_count":sum(r["evidence_status"]=="PASS" for r in subset),"hold_count":sum(r["evidence_status"]=="HOLD" for r in subset),"inconclusive_count":sum(r["evidence_status"]=="INCONCLUSIVE" for r in subset),"refusal_count":sum(r["refusals"]>0 or r["evidence_status"]=="REFUSED" for r in subset),"elapsed_seconds_total":sum(r["elapsed_seconds"] for r in subset),"cost_microunits_total":sum(r["cost_microunits"] for r in subset),"calls_total":sum(r["calls"] for r in subset),"input_tokens_total":sum(r["input_tokens"] for r in subset if r["input_tokens"] is not None),"input_tokens_recorded_count":sum(r["input_tokens"] is not None for r in subset),"output_tokens_total":sum(r["output_tokens"] for r in subset if r["output_tokens"] is not None),"output_tokens_recorded_count":sum(r["output_tokens"] is not None for r in subset),"manual_touches_total":sum(r["manual_touches"] for r in subset),"change_count_total":sum(r["change_count"] for r in subset),"cost_sources":sorted({r["cost_source"] for r in subset}),"budget_observations":[{"task_id":r["task_id"],"configured":r["budgets"]} for r in subset]}
    payload={"schema":SCHEMA,"inventory_sha256":inv["sha256"],"task_order":[r["task_id"] for r in rows],"task_count":len(inv["tasks"]),"reported_result_count":len(rows),"metrics":metrics,"results":rows,"interpretation":"Descriptive evidence report only; no improvement or general capability is inferred from these observations."}
    return {**payload,"sha256":_sha(payload)}

def support_matrix(rows: list[dict]) -> dict:
    if not isinstance(rows,list): raise EvaluationRefused("support rows must be list")
    clean=[]
    for row in rows:
        if not isinstance(row,dict) or set(row)!={"business_type","language","framework","storage","interface","deployment","status","evidence_sha256","scope"}: raise EvaluationRefused("support row shape invalid")
        if row["business_type"] not in KINDS or row["status"] not in {"verified_support","implemented_unverified","unsupported"}: raise EvaluationRefused("unsupported capability status or business type")
        for k in ("language","framework","storage","interface","deployment","scope"): _text(row[k],k)
        if row["status"]=="verified_support": _digest(row["evidence_sha256"],"support evidence")
        elif row["evidence_sha256"] is not None: _digest(row["evidence_sha256"],"support evidence")
        clean.append(row)
    clean.sort(key=lambda r:tuple(str(r[k]) for k in ("business_type","language","framework","storage","interface","deployment")))
    payload={"schema":SCHEMA,"capabilities":clean,"limitations":["Unsupported means no supported path is declared; it is not an unverified-success claim."]}
    return {**payload,"sha256":_sha(payload)}

def write_report(path: str|Path, report: dict): checkpoint._atomic(Path(path),checkpoint.canonical(report)+b"\n")
def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="cmd",required=True)
    a=sub.add_parser("inventory"); a.add_argument("tasks"); a.add_argument("output"); a.add_argument("--heldout-fraction",type=int,default=20); a.add_argument("--partition-key",default="autoforge-eval-v1")
    e=sub.add_parser("run"); e.add_argument("inventory"); e.add_argument("results"); e.add_argument("output")
    s=sub.add_parser("support"); s.add_argument("capabilities"); s.add_argument("output")
    args=p.parse_args(argv)
    try:
        if args.cmd=="inventory": result=build_inventory(_load_json(args.tasks),heldout_fraction=args.heldout_fraction,partition_key=args.partition_key)
        elif args.cmd=="run": result=evaluate(_load_json(args.inventory),_load_json(args.results))
        else: result=support_matrix(_load_json(args.capabilities))
        write_report(args.output,result); print(json.dumps(result,ensure_ascii=False,sort_keys=True)); return 0
    except (ValueError,OSError,KeyError,TypeError) as exc: print(f"HOLD: {exc}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
