"""Durable idempotent effect ledger and real loopback HTTP peer adapter.

This is a local peer protocol, not a financial or email provider integration.
"""
from __future__ import annotations
import hashlib, http.client, json, time, urllib.parse, uuid
from pathlib import Path
import application_checkpoint

class EffectRefused(ValueError): pass
CLASSES={"read", "idempotent", "compensatable", "irreversible"}
STATES={"PREPARED", "INFLIGHT", "SUCCEEDED", "FAILED", "UNKNOWN", "COMPENSATED"}

def _canonical(v): return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
def _digest(v): return hashlib.sha256(_canonical(v)).hexdigest()
def _lock(path): return application_checkpoint.ExclusiveLock(Path(path).with_suffix(Path(path).suffix+".lock"))
def _load(path):
    p=Path(path)
    try: env=json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:return {"schema":1,"requests":{},"events":[]}
    except (OSError,ValueError) as e: raise EffectRefused("effect ledger unreadable") from e
    if set(env)!={"schema","state","sha256"} or env["schema"]!=1 or _digest(env["state"])!=env["sha256"]: raise EffectRefused("effect ledger integrity failure")
    return env["state"]
def _save(path,state):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True); env={"schema":1,"state":state,"sha256":_digest(state)}
    application_checkpoint._atomic(p,_canonical(env)+b"\n")
def prepare(path, *, request_id: str, effect_class: str, endpoint: str, method: str, resource: str, payload: dict, scope: str, deadline: int) -> dict:
    if not isinstance(request_id,str) or not request_id or len(request_id)>128 or effect_class not in CLASSES: raise EffectRefused("request identity/classification invalid")
    if effect_class=="irreversible": raise EffectRefused("irreversible external actions are not enabled")
    u=urllib.parse.urlsplit(endpoint)
    if u.scheme!="http" or u.hostname not in {"127.0.0.1","localhost","::1"} or u.username or u.password: raise EffectRefused("peer must be explicitly authorized localhost HTTP")
    if method not in {"GET","POST","PUT","DELETE"} or not isinstance(resource,str) or not resource.startswith("/") or ".." in resource.split("/"): raise EffectRefused("method/resource invalid")
    if not isinstance(payload,dict) or not isinstance(scope,str) or not scope or type(deadline)is not int or deadline<=int(time.time()): raise EffectRefused("scope, payload, and future deadline required")
    record={"request_id":request_id,"effect_class":effect_class,"endpoint":endpoint,"method":method,"resource":resource,"payload":payload,"scope":scope,"deadline":deadline,"request_sha256":_digest({"method":method,"resource":resource,"payload":payload,"scope":scope})}
    with _lock(path):
        state=_load(path);old=state["requests"].get(request_id)
        if old:
            if old["request_sha256"]!=record["request_sha256"]: raise EffectRefused("request ID already bound to different effect")
            return old
        record.update({"state":"PREPARED","attempts":0,"evidence":None,"compensation":None})
        state["requests"][request_id]=record;state["events"].append({"event":"prepared","request_id":request_id,"time":int(time.time())});_save(path,state)
    return record

def execute(path, request_id: str, *, timeout: float=3.0) -> dict:
    if not 0<timeout<=30: raise EffectRefused("timeout outside bound")
    with _lock(path):
        state=_load(path);r=state["requests"].get(request_id)
        if not r: raise EffectRefused("unknown request ID")
        if r["state"] in {"SUCCEEDED","FAILED","UNKNOWN","COMPENSATED"}: return r
        if r["state"]!="PREPARED": raise EffectRefused("request already inflight; reconcile before replay")
        if int(time.time())>=r["deadline"]: raise EffectRefused("request deadline elapsed")
        r["state"]="INFLIGHT";r["attempts"]+=1;attempt=r["attempts"];_save(path,state)
    try:
        u=urllib.parse.urlsplit(r["endpoint"]);conn=http.client.HTTPConnection(u.hostname,u.port or 80,timeout=timeout)
        body=_canonical({"request_id":request_id,"scope":r["scope"],"payload":r["payload"]}) if r["method"]!="GET" else None
        conn.request(r["method"],r["resource"],body=body,headers={"Content-Type":"application/json","Idempotency-Key":request_id})
        resp=conn.getresponse();data=resp.read(65536);status=resp.status;conn.close()
        if status<200 or status>=300: raise EffectRefused(f"peer returned HTTP {status}")
        result=json.loads(data) if data else {}
        if not isinstance(result,dict) or result.get("request_id")!=request_id: raise EffectRefused("peer response lacks matching durable request identity")
        outcome="SUCCEEDED";ev={"http_status":status,"response":result,"response_sha256":hashlib.sha256(data).hexdigest()}
    except Exception as exc:
        # Once sent, even a timeout/non-2xx is ambiguous; never silently replay.
        outcome="UNKNOWN";ev={"reason":"peer outcome requires query-state reconciliation","exception_type":type(exc).__name__}
    with _lock(path):
        state=_load(path);r=state["requests"][request_id]
        if r["state"]!="INFLIGHT" or r["attempts"]!=attempt: raise EffectRefused("effect attempt changed before commit")
        r["state"]=outcome;r["evidence"]=ev;state["events"].append({"event":"effect_finished","request_id":request_id,"state":outcome,"evidence":ev});_save(path,state);return r

def reconcile(path, request_id: str, *, observed_state: str, evidence: dict, operator: str, source: str) -> dict:
    if observed_state not in {"SUCCEEDED","FAILED"} or not isinstance(evidence,dict) or not evidence or not isinstance(operator,str) or not operator.strip() or not isinstance(source,str) or not source.strip(): raise EffectRefused("reconciliation requires observed state, evidence, operator and source")
    with _lock(path):
        state=_load(path);r=state["requests"].get(request_id)
        if not r or r["state"]!="UNKNOWN": raise EffectRefused("request is not UNKNOWN")
        r["state"]=observed_state;r["evidence"]={"observed":evidence,"sha256":_digest(evidence),"operator":operator,"source":source};state["events"].append({"event":"reconciled","request_id":request_id,"state":observed_state,"evidence":r["evidence"]});_save(path,state);return r

def compensate(path, request_id: str, *, evidence: dict, compensation_id: str, operator: str, source: str) -> dict:
    if not isinstance(evidence,dict) or not evidence or not all(isinstance(x,str) and x.strip() for x in (compensation_id,operator,source)): raise EffectRefused("compensation needs explicit evidence and authorization")
    with _lock(path):
        state=_load(path);r=state["requests"].get(request_id)
        if not r or r["state"]!="SUCCEEDED" or r["effect_class"]!="compensatable": raise EffectRefused("only a proven compensatable success can be compensated")
        if r["compensation"] is not None: raise EffectRefused("compensation already recorded")
        r["compensation"]={"id":compensation_id,"evidence":evidence,"evidence_sha256":_digest(evidence),"operator":operator,"source":source};r["state"]="COMPENSATED";state["events"].append({"event":"compensated","request_id":request_id,"compensation_id":compensation_id});_save(path,state);return r
def query_peer_state(path, request_id: str, *, timeout: float=3.0) -> dict:
    """Read durable state from the actual authorized localhost HTTP peer."""
    if not isinstance(request_id,str) or not request_id or not 0<timeout<=30: raise EffectRefused("query parameters invalid")
    with _lock(path):
        state=_load(path);r=state["requests"].get(request_id)
        if not r or r["state"]!="UNKNOWN": raise EffectRefused("only UNKNOWN requests require peer query")
        u=urllib.parse.urlsplit(r["endpoint"]);status_resource="/effects/"+urllib.parse.quote(request_id,safe="")
    conn=http.client.HTTPConnection(u.hostname,u.port or 80,timeout=timeout)
    try:
        conn.request("GET",status_resource,headers={"Accept":"application/json"})
        response=conn.getresponse();data=response.read(65536)
        if response.status!=200: raise EffectRefused(f"peer state query returned HTTP {response.status}")
        observed=json.loads(data)
    finally: conn.close()
    if not isinstance(observed,dict) or observed.get("request_id")!=request_id or observed.get("state") not in {"SUCCEEDED","FAILED","NOT_FOUND"}:
        raise EffectRefused("peer state query lacks durable matching state evidence")
    return {"request_id":request_id,"state":observed["state"],"evidence":observed,"evidence_sha256":_digest(observed)}

def reconcile_from_peer(path, request_id: str, *, observation: dict, operator: str, source: str) -> dict:
    if not isinstance(observation,dict) or observation.get("request_id")!=request_id or observation.get("state") not in {"SUCCEEDED","FAILED"}:
        raise EffectRefused("reconciliation needs peer proof of a terminal outcome")
    return reconcile(path,request_id,observed_state=observation["state"],evidence=observation,operator=operator,source=source)
def recover_inflight(path) -> dict:
    """Convert process-abandoned writes to UNKNOWN; never infer failure or replay."""
    with _lock(path):
        state=_load(path);changed=[]
        for request_id,row in state["requests"].items():
            if row["state"]=="INFLIGHT":
                row["state"]="UNKNOWN";row["evidence"]={"reason":"abandoned inflight effect requires peer query/operator evidence"};changed.append(request_id)
        for request_id in changed: state["events"].append({"event":"effect_unknown_after_recovery","request_id":request_id})
        if changed:_save(path,state)
        return {"recovered_unknown":changed,"requests":state["requests"]}
