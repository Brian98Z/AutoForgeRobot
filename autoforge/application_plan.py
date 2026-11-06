#!/usr/bin/env python3
"""Durable DAG scheduler for trusted JSON task adapters; never executes candidate code."""
from __future__ import annotations
import argparse
import math
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import applications
import application_checkpoint

SCHEMA=1
TERMINAL={"SUCCEEDED","FAILED","BLOCKED","UNKNOWN"}
class PlanRefused(ValueError): pass

def _fail(message): raise PlanRefused(message)
def _canonical(value): return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode("utf-8")
def _sha(value): return hashlib.sha256(_canonical(value)).hexdigest()
def _json(path):
    try:return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError,UnicodeError,json.JSONDecodeError) as exc: raise PlanRefused(f"cannot read plan JSON: {type(exc).__name__}") from exc

def _lock(path: Path):
    return application_checkpoint.ExclusiveLock(path.with_suffix(path.suffix+".lock"))

@contextmanager
def _execution_lock(path:Path):
    with application_checkpoint.ExclusiveLock(path.with_suffix(path.suffix+".runner.lock")):
        yield

def _atomic(path:Path,data:dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        with temp.open("xb") as f:
            f.write(_canonical(data));f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        try:temp.unlink()
        except FileNotFoundError:pass

def _clean(plan:Any)->dict:
    if not isinstance(plan,dict) or set(plan)!={"schema","plan_id","spec_sha256","tasks","status","events"}:_fail("invalid plan shape")
    if plan["schema"]!=SCHEMA or not isinstance(plan["plan_id"],str) or not plan["plan_id"]:_fail("invalid plan identity/schema")
    if not isinstance(plan["spec_sha256"],str) or len(plan["spec_sha256"])!=64:_fail("invalid spec digest")
    if plan["status"] not in {"READY","PAUSED","BLOCKED","SUCCEEDED","FAILED","UNKNOWN"}:_fail("invalid plan status")
    if not isinstance(plan["events"],list) or not isinstance(plan["tasks"],list) or not plan["tasks"]:_fail("tasks/events invalid")
    ids=set();graph={}
    for task in plan["tasks"]:
        required={"id","depends_on","requirement_ids","acceptance_ids","adapter","argv","state","evidence","attempt_id","error"}
        if not isinstance(task,dict) or set(task)!=required:_fail("invalid task record")
        tid=task["id"]
        if not isinstance(tid,str) or not tid or tid in ids:_fail("duplicate/invalid task id")
        ids.add(tid);graph[tid]=task["depends_on"]
        if task["state"] not in {"PENDING","RUNNING","SUCCEEDED","FAILED","BLOCKED","UNKNOWN"}:_fail(f"invalid task state {tid}")
        if not isinstance(task["argv"],list) or not task["argv"] or not all(isinstance(x,str) and x for x in task["argv"]):_fail(f"invalid trusted adapter argv for {tid}")
        if task["state"]=="SUCCEEDED":
            ev=task["evidence"]
            if not isinstance(ev,dict) or ev.get("task_id")!=tid or ev.get("adapter_argv_sha256")!=_sha(task["argv"]) or not ev.get("evidence_id") or not ev.get("result_sha256"):_fail(f"missing or unbound completion evidence for {tid}")
            if not isinstance(ev.get("spec_sha256"),str) or ev.get("spec_sha256")!=plan["spec_sha256"] or ev.get("requirement_ids")!=task["requirement_ids"] or ev.get("acceptance_ids")!=task["acceptance_ids"]:_fail(f"completion evidence is not bound to spec/requirement/acceptance for {tid}")
    for tid,deps in graph.items():
        if not isinstance(deps,list) or len(deps)!=len(set(deps)) or tid in deps or not set(deps)<=ids:_fail(f"invalid dependencies for {tid}")
    visited=set();visiting=set()
    def visit(tid):
        if tid in visiting:_fail("task graph cycle")
        if tid in visited:return
        visiting.add(tid)
        for d in graph[tid]:visit(d)
        visiting.remove(tid);visited.add(tid)
    for tid in ids:visit(tid)
    return plan

def create(spec:dict, adapter_argv:dict[str,list[str]], path:Path|str, plan_id:str|None=None)->dict:
    """Create absent durable plan from an approved governed spec and explicit trusted argv map."""
    import application_spec
    governed=application_spec.validate_spec(spec,require_approved=True)
    if not isinstance(adapter_argv,dict):_fail("adapter_argv must map task kind to trusted argv")
    tasks=[]
    for task in governed["tasks"]:
        argv=adapter_argv.get(task["kind"])
        if not isinstance(argv,list) or not argv or not all(isinstance(x,str) and x for x in argv):_fail(f"no trusted adapter argv for task kind {task['kind']}")
        tasks.append({"id":task["id"],"depends_on":task["depends_on"],"requirement_ids":task["requirement_ids"],"acceptance_ids":task["acceptance_ids"],"adapter":task["kind"],"argv":argv,"state":"PENDING","evidence":None,"attempt_id":None,"error":None})
    record={"schema":SCHEMA,"plan_id":plan_id or uuid.uuid4().hex,"spec_sha256":governed["sha256"],"tasks":tasks,"status":"READY","events":[]}
    _clean(record); target=Path(path)
    with _lock(target):
        if target.exists():_fail("plan output already exists")
        _atomic(target,record)
    return record

def _event(plan,event):
    plan["events"].append({"seq":len(plan["events"])+1,"time_ns":time.time_ns(),**event})

def _refresh(plan):
    states={t["id"]:t["state"] for t in plan["tasks"]}
    for t in plan["tasks"]:
        if t["state"]!="PENDING":continue
        failed=[d for d in t["depends_on"] if states[d] in {"FAILED","BLOCKED","UNKNOWN"}]
        if failed:
            t["state"]="BLOCKED";t["error"]={"reason":"dependency failed/blocked/unknown","dependencies":failed};_event(plan,{"type":"task_blocked","task_id":t["id"],"dependencies":failed})
    if any(t["state"]=="UNKNOWN" for t in plan["tasks"]):plan["status"]="UNKNOWN"
    elif any(t["state"]=="FAILED" for t in plan["tasks"]):plan["status"]="FAILED"
    elif any(t["state"]=="BLOCKED" for t in plan["tasks"]):plan["status"]="BLOCKED"
    elif all(t["state"]=="SUCCEEDED" for t in plan["tasks"]):plan["status"]="SUCCEEDED"
    elif plan["status"]!="PAUSED":plan["status"]="READY"

def _load(path):
    return _clean(_json(path))

def _recover_orphaned_inflight(path:Path):
    """Mark abandoned tasks UNKNOWN while holding exclusive runner ownership."""
    with _lock(path):
        plan=_load(path);changed=False
        for t in plan["tasks"]:
            if t["state"]=="RUNNING":
                t["state"]="UNKNOWN";t["error"]={"reason":"prior scheduler stopped with inflight task; reconcile before resume"};_event(plan,{"type":"task_unknown","task_id":t["id"],"attempt_id":t["attempt_id"]});changed=True
        if changed:
            plan["status"]="UNKNOWN";_save(path,plan)
        return plan

def _save(path,plan):_clean(plan);_atomic(Path(path),plan)
def _plan_paused(target:Path)->bool:
    try:
        with _lock(target):return _load(target)["status"]!="READY"
    except (OSError,PlanRefused):return True

def run(path:Path|str, *, max_tasks:int|None=None, timeout:int=applications.COMMAND_TIMEOUT, on_event=None)->dict:
    """Run trusted adapters under a finite wall-clock bound; uncertain attempts are never replayed."""
    if max_tasks is not None and (type(max_tasks) is not int or max_tasks < 0):_fail("max_tasks must be a nonnegative integer")
    if type(timeout) not in {int,float} or not math.isfinite(timeout) or timeout<=0:_fail("timeout must be a positive finite number")
    target=Path(path)
    with _execution_lock(target):
        plan=_recover_orphaned_inflight(target)
        if plan["status"]!="READY":return plan
        return _run_owned(target,max_tasks=max_tasks,deadline=time.monotonic()+timeout,on_event=on_event)

def _run_owned(target:Path, *, max_tasks:int|None=None, deadline:float, on_event=None)->dict:
    completed=0
    if max_tasks==0:
        with _lock(target):
            plan=_load(target)
            if plan["status"]=="READY":plan["status"]="PAUSED";_event(plan,{"type":"plan_paused","reason":"task_limit"});_save(target,plan)
            return plan
    while True:
        with _lock(target):
            plan=_load(target)
            if plan["status"]!="READY":return plan
            remaining=deadline-time.monotonic()
            if remaining<=0:
                plan["status"]="PAUSED";_event(plan,{"type":"plan_paused","reason":"runtime_limit"});_save(target,plan);return plan
            enabled=next((t for t in plan["tasks"] if t["state"]=="PENDING" and all(next(x for x in plan["tasks"] if x["id"]==d)["state"]=="SUCCEEDED" for d in t["depends_on"])),None)
            if enabled is None:
                _refresh(plan);_save(target,plan);return plan
            enabled["state"]="RUNNING";enabled["attempt_id"]=uuid.uuid4().hex;_event(plan,{"type":"task_started","task_id":enabled["id"],"attempt_id":enabled["attempt_id"]});_save(target,plan)
            task=dict(enabled);payload={"schema":1,"plan_id":plan["plan_id"],"spec_sha256":plan["spec_sha256"],"task_id":task["id"],"requirement_ids":task["requirement_ids"],"acceptance_ids":task["acceptance_ids"],"dependencies":[next(x["evidence"] for x in plan["tasks"] if x["id"]==d) for d in task["depends_on"]],"attempt_id":task["attempt_id"]}
        if on_event:
            try:on_event({"type":"task_started","task_id":task["id"],"attempt_id":task["attempt_id"]})
            except Exception:pass
        try:
            raw=applications._run_json_command(task["argv"],payload,timeout=max(0.001,deadline-time.monotonic()),should_stop=lambda:_plan_paused(target))
            if (not isinstance(raw,dict) or raw.get("status")!="SUCCEEDED" or raw.get("task_id")!=task["id"]
                or raw.get("attempt_id")!=task["attempt_id"] or raw.get("requirement_ids")!=task["requirement_ids"] or raw.get("acceptance_ids")!=task["acceptance_ids"]
                or not isinstance(raw.get("evidence_id"),str) or not raw["evidence_id"]):
                outcome="UNKNOWN";evidence=None;error={"reason":"adapter response lacks exact task/attempt/acceptance evidence; outcome may be externally applied and requires reconciliation"}
            else:
                evidence={"task_id":task["id"],"attempt_id":task["attempt_id"],"evidence_id":raw["evidence_id"],"result_sha256":_sha(raw),"adapter_argv_sha256":_sha(task["argv"]),"spec_sha256":plan["spec_sha256"],"requirement_ids":task["requirement_ids"],"acceptance_ids":task["acceptance_ids"]}
                outcome="SUCCEEDED";error=None
        except BaseException as exc:
            outcome="UNKNOWN";evidence=None;error={"reason":"adapter outcome uncertain; not automatically replayed","exception_type":type(exc).__name__}
        with _lock(target):
            current=_load(target);t=next(t for t in current["tasks"] if t["id"]==task["id"])
            if t["attempt_id"]!=task["attempt_id"] or t["state"]!="RUNNING":_fail("inflight attempt changed before evidence commit")
            t["state"]=outcome;t["evidence"]=evidence;t["error"]=error;_event(current,{"type":"task_finished" if outcome!="UNKNOWN" else "task_unknown","task_id":t["id"],"state":outcome,"evidence":evidence,"error":error});_refresh(current);_save(target,current)
        if on_event:
            try:on_event({"type":"task_finished" if outcome!="UNKNOWN" else "task_unknown","task_id":task["id"],"state":outcome,"evidence":evidence,"error":error})
            except Exception:pass
        if outcome!="SUCCEEDED":return current
        completed+=1
        if max_tasks is not None and completed>=max_tasks:
            with _lock(target):
                current=_load(target)
                if current["status"]=="READY":current["status"]="PAUSED";_event(current,{"type":"plan_paused","reason":"task_limit"});_save(target,current)
                return current

def pause(path):
    target=Path(path)
    with _lock(target):
        plan=_load(target)
        if plan["status"]!="READY":_fail(f"cannot pause plan in {plan['status']}")
        plan["status"]="PAUSED";_event(plan,{"type":"plan_paused"});_save(target,plan);return plan

def resume(path):
    target=Path(path)
    with _lock(target):
        plan=_load(target)
        if plan["status"]!="PAUSED":_fail(f"cannot resume plan in {plan['status']}")
        plan["status"]="READY";_event(plan,{"type":"plan_resumed"});_save(target,plan);return plan

def reconcile(path, task_id:str, *, outcome:str, evidence:dict|None=None, operator:str, source:str):
    """Resolve UNKNOWN only with operator-supplied observed success/failure evidence; never auto-retry."""
    if outcome not in {"SUCCEEDED","FAILED"}:_fail("reconciliation outcome must be SUCCEEDED or FAILED")
    _text(operator,"operator");_text(source,"source")
    target=Path(path)
    with _execution_lock(target):
      with _lock(target):
        plan=_load(target);t=next((t for t in plan["tasks"] if t["id"]==task_id),None)
        if t is None or t["state"]!="UNKNOWN":_fail("task is not UNKNOWN")
        if outcome=="SUCCEEDED":
            if not isinstance(evidence,dict) or not isinstance(evidence.get("evidence_id"),str) or not evidence["evidence_id"]:_fail("successful reconciliation requires external evidence_id")
            t["evidence"]={"task_id":task_id,"attempt_id":t["attempt_id"],"evidence_id":evidence["evidence_id"],"result_sha256":_sha(evidence),"adapter_argv_sha256":_sha(t["argv"]),"spec_sha256":plan["spec_sha256"],"requirement_ids":t["requirement_ids"],"acceptance_ids":t["acceptance_ids"],"reconciled_by":operator,"reconciliation_source":source}
        else:t["evidence"]=None
        t["state"]=outcome;t["error"]=None if outcome=="SUCCEEDED" else {"reason":"operator reconciled task failure","operator":operator,"source":source};_event(plan,{"type":"task_reconciled","task_id":task_id,"state":outcome,"operator":operator,"source":source});_refresh(plan);_save(target,plan);return plan

def _text(value,label):
    if not isinstance(value,str) or not value.strip():_fail(f"{label} must be nonempty")
    return value.strip()

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest="action",required=True)
    for action in ("run","pause","resume","status","reconcile"):
        q=sub.add_parser(action);q.add_argument("plan",type=Path)
        if action=="run":q.add_argument("--max-tasks",type=int);q.add_argument("--timeout",type=float,default=applications.COMMAND_TIMEOUT)
        if action=="reconcile":q.add_argument("task_id");q.add_argument("outcome",choices=("SUCCEEDED","FAILED"));q.add_argument("--evidence",type=Path);q.add_argument("--operator",required=True);q.add_argument("--source",required=True)
    a=p.parse_args(argv)
    try:
        if a.action=="run":result=run(a.plan,max_tasks=a.max_tasks,timeout=a.timeout)
        elif a.action=="pause":result=pause(a.plan)
        elif a.action=="resume":result=resume(a.plan)
        elif a.action=="status":result=_load(a.plan)
        else:result=reconcile(a.plan,a.task_id,outcome=a.outcome,evidence=_json(a.evidence) if a.evidence else None,operator=a.operator,source=a.source)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0 if result["status"]=="SUCCEEDED" else 3
    except (PlanRefused,applications.Refused,OSError) as exc:print(json.dumps({"status":"BLOCKED","error":str(exc)},ensure_ascii=False));return 3
if __name__=="__main__":raise SystemExit(main())
