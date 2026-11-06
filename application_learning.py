#!/usr/bin/env python3
"""Evidence-backed reference facts and reusable, invalidatable repair experiences.

This module records claims; it does not execute reference or candidate code and does
not turn prose/model assertions into validation evidence.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path
from typing import Any
import application_checkpoint as checkpoint
import references

SCHEMA = 1
PROVENANCE = {"observation", "inference", "unknown"}

class LearningRefused(ValueError): pass

def _text(x: Any, where: str) -> str:
    if not isinstance(x, str) or not x.strip(): raise LearningRefused(f"{where} must be nonempty text")
    return x.strip()

def _digest(x: str, where: str) -> str:
    if not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}", x): raise LearningRefused(f"{where} must be a SHA-256 digest")
    return x

def canonical_digest(value: Any) -> str: return checkpoint.digest(checkpoint.canonical(value))

def _source(store: str | Path, package_id: str, file_path: str, expected_sha256: str, *, permission: str) -> dict:
    package = references._load_package(store, package_id)
    if permission not in {"inspect", "derive", "implement", "redistribute"}:
        raise LearningRefused("permission must be inspect, derive, implement, or redistribute")
    entry = next((f for f in package["files"] if f.get("path") == file_path), None)
    if entry is None: raise LearningRefused("source file is not in declared package")
    actual = hashlib.sha256(entry["content"].encode("utf-8")).hexdigest()
    if actual != _digest(expected_sha256, "source.sha256") or actual != entry.get("sha256"):
        raise LearningRefused("source content digest mismatch")
    return {"package_id": package_id, "path": file_path, "sha256": actual,
            "origin": package["origin"], "license": package["license"], "permission": permission,
            "kind": package["kind"]}

def _items(items, name):
    if not isinstance(items, list) or len(items) > 512: raise LearningRefused(f"{name} must be a bounded list")
    return items

import ast

def extract_reference_observations(store: str | Path, package_id: str) -> dict:
    """Extract syntax-level observations with exact source coordinates; semantics stay unknown."""
    package=references._load_package(store,package_id)
    files=[]
    evidence_by_path={row["path"]:row for row in package.get("upstream_evidence",[])}
    for entry in package["files"]:
        path=entry["path"]; text=entry["content"]
        row={"path":path,"sha256":entry["sha256"],"modules":[],"interfaces":[],"state_writes":[],"evidence":[],"unknowns":[]}
        if path.endswith(".py"):
            try: tree=ast.parse(text,filename=path)
            except SyntaxError as exc:
                row["unknowns"].append({"claim":"Python module could not be parsed","provenance":"unknown","line":exc.lineno,"reason":exc.msg})
            else:
                for node in tree.body:
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                        row["interfaces"].append({"kind":"function","name":node.name,"line":node.lineno,"parameters":[arg.arg for arg in node.args.args]})
                    elif isinstance(node,ast.ClassDef):
                        row["interfaces"].append({"kind":"class","name":node.name,"line":node.lineno,"parameters":[]})
                    elif isinstance(node,(ast.Import,ast.ImportFrom)):
                        row["modules"].append({"kind":"import","text":ast.get_source_segment(text,node),"line":node.lineno})
                for node in ast.walk(tree):
                    if isinstance(node,(ast.Assign,ast.AnnAssign,ast.AugAssign)):
                        targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                        for target in targets:
                            if isinstance(target,ast.Name):row["state_writes"].append({"name":target.id,"line":node.lineno})
                            elif isinstance(target,ast.Attribute):row["state_writes"].append({"name":ast.get_source_segment(text,target),"line":node.lineno})
        upstream=evidence_by_path.get(path)
        if upstream: row["evidence"].append({"evidence_id":upstream["evidence_id"],"provenance":"observation","limitations":upstream["record"].get("limitations",[])})
        row["unknowns"].append({"claim":"Business behavior not established by syntax extraction","provenance":"unknown"})
        files.append(row)
    return {"schema":1,"package_id":package_id,"origin":package["origin"],"license":package["license"],"files":files,"limitations":package["limitations"]}

def build_reference_model(store: str | Path, facts: list[dict], tasks: list[dict]) -> dict:
    """Build claims from explicitly digest-bound package snippets and evidence IDs."""
    out_facts=[]; ids=set()
    for i, raw in enumerate(_items(facts,"facts")):
        if not isinstance(raw,dict) or set(raw)!={"id","claim","provenance","source","evidence_ids"}: raise LearningRefused(f"facts[{i}] shape invalid")
        fid=_text(raw["id"],f"facts[{i}].id")
        if fid in ids: raise LearningRefused("duplicate fact id")
        ids.add(fid); kind=raw["provenance"]
        if kind not in PROVENANCE: raise LearningRefused("provenance must be observation, inference, or unknown")
        src=raw["source"]
        if not isinstance(src,dict) or set(src)!={"package_id","path","sha256","permission"}: raise LearningRefused("fact source shape invalid")
        bound=_source(store,src["package_id"],src["path"],src["sha256"],permission=src["permission"])
        ev=_items(raw["evidence_ids"],"evidence_ids")
        if kind=="observation" and not ev: raise LearningRefused("observation requires explicit source evidence IDs")
        if kind=="unknown" and ev: raise LearningRefused("unknown facts cannot claim supporting evidence")
        if any(not isinstance(x,str) or not x.strip() for x in ev): raise LearningRefused("evidence IDs must be nonempty strings")
        out_facts.append({"id":fid,"claim":_text(raw["claim"],f"facts[{i}].claim"),"provenance":kind,"source":bound,"evidence_ids":ev})
    out_tasks=[]; tids=set()
    for i, raw in enumerate(_items(tasks,"tasks")):
        required={"id","title","depends_on","fact_ids","verification_obligations"}
        if not isinstance(raw,dict) or set(raw)!=required: raise LearningRefused(f"tasks[{i}] shape invalid")
        tid=_text(raw["id"],"task.id")
        if tid in tids: raise LearningRefused("duplicate task id")
        tids.add(tid)
        fact_ids=raw["fact_ids"]
        if not isinstance(fact_ids,list) or any(x not in ids for x in fact_ids): raise LearningRefused("task references unknown fact")
        out_tasks.append({"id":tid,"title":_text(raw["title"],"task.title"),"depends_on":_items(raw["depends_on"],"depends_on"),"fact_ids":fact_ids,"verification_obligations":[_text(x,"verification obligation") for x in _items(raw["verification_obligations"],"verification_obligations")]})
    for task in out_tasks:
        if any(dep not in tids or dep==task["id"] for dep in task["depends_on"]): raise LearningRefused("task dependency missing or self-referential")
    payload={"schema":SCHEMA,"facts":out_facts,"tasks":out_tasks}
    return {**payload,"sha256":canonical_digest(payload)}

def validate_reference_model(model: Any, store: str | Path) -> dict:
    if not isinstance(model,dict) or set(model)!={"schema","facts","tasks","sha256"} or model["schema"]!=SCHEMA: raise LearningRefused("reference model envelope invalid")
    source_fields={"package_id","path","sha256","permission"}
    facts=[{**fact,"source":{k:fact["source"][k] for k in source_fields}} for fact in model["facts"]]
    rebuilt=build_reference_model(store,facts,model["tasks"])
    if rebuilt!=model: raise LearningRefused("reference model digest or normalized contents mismatch")
    return rebuilt

def save_reference_model(path: str | Path, model: dict, store: str | Path) -> None:
    clean=validate_reference_model(model,store); checkpoint._atomic(Path(path),checkpoint.canonical(clean)+b"\n")

def record_experience(record: dict, store: str | Path) -> dict:
    """Accept only paired validator evidence and exact before/after source digests."""
    fields={"schema","experience_id","problem_tags","before_sha256","after_sha256","repair_summary","environment","validator","invalidation_conditions","source"}
    if not isinstance(record,dict) or set(record)!=fields or record.get("schema")!=SCHEMA: raise LearningRefused("experience shape invalid")
    _text(record["experience_id"],"experience_id")
    tags=_items(record["problem_tags"],"problem_tags")
    if not tags or any(not isinstance(t,str) or not t.strip() for t in tags): raise LearningRefused("problem_tags must be nonempty strings")
    before=_digest(record["before_sha256"],"before_sha256"); after=_digest(record["after_sha256"],"after_sha256")
    if before==after: raise LearningRefused("repair before/after digests must differ")
    _text(record["repair_summary"],"repair_summary")
    if not isinstance(record["environment"],dict) or not record["environment"]: raise LearningRefused("environment identity required")
    validator=record["validator"]
    if not isinstance(validator,dict) or set(validator)!={"status","report_sha256","validator_id","independent","report_source"} or validator["status"]!="PASS" or type(validator["independent"]) is not bool:
        raise LearningRefused("experience requires a digest-bound PASS validator report")
    report_sha=_digest(validator["report_sha256"],"validator.report_sha256"); validator_id=_text(validator["validator_id"],"validator.validator_id")
    report_ref=validator["report_source"]
    if not isinstance(report_ref,dict) or set(report_ref)!={"package_id","path","sha256","permission"}: raise LearningRefused("validator report source required")
    report_bound=_source(store,report_ref["package_id"],report_ref["path"],report_ref["sha256"],permission=report_ref["permission"])
    report_entry=next(f for f in references._load_package(store,report_ref["package_id"])["files"] if f["path"]==report_ref["path"])
    try: report=json.loads(report_entry["content"])
    except (ValueError,TypeError) as exc: raise LearningRefused("validator evidence must be an actual JSON report") from exc
    if not isinstance(report,dict) or report.get("status")!="PASS" or report.get("validator_id")!=validator_id or report.get("before_sha256")!=before or report.get("after_sha256")!=after or report.get("environment")!=record["environment"]:
        raise LearningRefused("validator report does not prove this exact repair and environment")
    inv=_items(record["invalidation_conditions"],"invalidation_conditions")
    if not inv or any(not isinstance(x,str) or not x.strip() for x in inv): raise LearningRefused("invalidation conditions required")
    src=record["source"]
    if not isinstance(src,dict) or set(src)!={"package_id","path","sha256","permission"}: raise LearningRefused("source reference required")
    bound=_source(store,src["package_id"],src["path"],src["sha256"],permission=src["permission"])
    result={**record,"source":bound,"validator":{**validator,"validator_id":validator_id,"report_sha256":report_sha,"report_source":report_bound},"problem_tags":sorted(set(tags)),"invalidation_conditions":list(inv)}
    result["sha256"]=canonical_digest(result)
    return result


def record_experience_use(directory: str | Path, *, reference_store: str | Path, problem_tags: list[str], environment: dict, task_id: str, baseline_run: str | Path, reused_run: str | Path, context_path: str | Path, executor_root: str | Path | None = None) -> dict:
    """Persist paired baseline/reuse observations after exact current retrieval and environment binding."""
    import application_experience_adapter as adapter
    selected_payload=adapter.select(directory,reference_store,problem_tags,environment,executor_root=executor_root)
    selected_ids={x["experience_id"] for x in selected_payload["references"]}
    try: context=json.loads(Path(context_path).read_text(encoding="utf-8"))
    except (OSError,ValueError) as exc: raise LearningRefused("pre-task experience context is unavailable or invalid") from exc
    claimed=context.pop("sha256",None)
    if claimed!=canonical_digest(context) or context.get("task_id")!=task_id or context.get("environment")!=environment: raise LearningRefused("pre-task experience context digest/task/environment mismatch")
    context_ids=[x.get("experience_id") for x in context.get("retrieved",{}).get("references",[])]
    if not context_ids or not set(context_ids)<=selected_ids: raise LearningRefused("recorded reuse must cite experiences actually present in pre-task context and current-executor-valid selection")
    if not selected_ids: raise LearningRefused("no current-executor-valid experiences available for this exact environment and task")
    selected=[x for x in retrieve_experiences(directory,reference_store=reference_store,problem_tags=problem_tags,environment=environment,ignore_environment_keys={"executor_sources"}) if x["experience_id"] in context_ids]
    comparison=adapter.compare(baseline_run,reused_run)
    if comparison.get("sample_match") is not True: raise LearningRefused("baseline and reuse runs must have the same task identity")
    baseline=adapter._run_summary(baseline_run); reused=adapter._run_summary(reused_run)
    if baseline["task_id"]!=task_id or baseline["run_id"]==reused["run_id"]: raise LearningRefused("paired runs must be distinct and bound to requested task")
    payload={"schema":1,"kind":"experience-use","task_id":task_id,"environment":environment,"problem_tags":sorted(set(problem_tags)),"experience_ids":[x["experience_id"] for x in selected],"experience_sha256":[x["sha256"] for x in selected],"baseline_run":{k:baseline[k] for k in ("run_id","outcome","report_sha256","events_sha256","validation_count","validation_pass_count","validation_fail_count")},"reused_run":{k:reused[k] for k in ("run_id","outcome","report_sha256","events_sha256","validation_count","validation_pass_count","validation_fail_count")},"comparison":comparison,"benefit":"not_proven"}
    if (reused["outcome"]=="SUCCEEDED" and baseline["outcome"]!="SUCCEEDED") or (reused["validation_pass_count"]>baseline["validation_pass_count"] and reused["validation_fail_count"]<=baseline["validation_fail_count"]): payload["benefit"]="observed_paired_improvement_noncausal"
    payload["sha256"]=canonical_digest(payload)
    target=Path(directory)/"uses"/(hashlib.sha256((task_id+payload["sha256"]).encode()).hexdigest()+".json")
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists(): raise LearningRefused("experience-use record already exists")
    checkpoint._atomic(target,checkpoint.canonical(payload)+b"\n")
    return payload

def map_diagnosis_to_obligations(spec: dict, diagnosis: dict, ownership: dict) -> dict:
    """Bind a real file/line diagnostic to operator-declared ownership and existing regression IDs."""
    import application_spec
    governed=application_spec.validate_spec(spec,require_approved=True)
    if not isinstance(diagnosis,dict) or not {"path","line","message","source_sha256"}<=set(diagnosis): raise LearningRefused("diagnosis requires path, line, message and source digest")
    path=_text(diagnosis["path"],"diagnosis.path"); line=diagnosis["line"]
    if type(line) is not int or line<1: raise LearningRefused("diagnosis line must be positive")
    _digest(diagnosis["source_sha256"],"diagnosis.source_sha256")
    if not isinstance(ownership,dict) or set(ownership)!={"files","requirement_ids","acceptance_ids"}: raise LearningRefused("explicit authorized ownership mapping required")
    if not isinstance(ownership["files"],dict) or path not in ownership["files"]: raise LearningRefused("diagnostic file missing from ownership registry")
    mapping=ownership["files"].get(path)
    if not isinstance(mapping,dict) or set(mapping)!={"owner","module","sha256","requirement_ids","acceptance_ids"}: raise LearningRefused("diagnostic file is outside declared ownership")
    if mapping["sha256"]!=diagnosis["source_sha256"]: raise LearningRefused("diagnostic belongs to stale source snapshot")
    req_ids=set(ownership["requirement_ids"]); acc_ids=set(ownership["acceptance_ids"])
    valid_req={x["id"] for x in governed["requirements"]}; valid_acc={a for x in governed["requirements"] for a in x["acceptance_ids"]}
    if not set(mapping["requirement_ids"])<=req_ids or not set(mapping["requirement_ids"])<=valid_req or not set(mapping["acceptance_ids"])<=acc_ids or not set(mapping["acceptance_ids"])<=valid_acc: raise LearningRefused("diagnostic mapping includes unauthorized or unknown requirement/regression IDs")
    return {"schema":1,"diagnosis":{"path":path,"line":line,"message":_text(diagnosis["message"],"diagnosis.message"),"source_sha256":diagnosis["source_sha256"]},"module":_text(mapping["module"],"module"),"owner":_text(mapping["owner"],"owner"),"spec_sha256":governed["sha256"],"requirement_ids":sorted(set(mapping["requirement_ids"])),"retained_acceptance_ids":sorted(set(mapping["acceptance_ids"])),"status":"MAPPED"}

def save_experience(path: str | Path, record: dict, store: str | Path) -> dict:
    raw={k:v for k,v in record.items() if k!="sha256"}
    raw["source"]={k:record["source"][k] for k in ("package_id","path","sha256","permission")}
    raw["validator"]={**record["validator"],"report_source":{k:record["validator"]["report_source"][k] for k in ("package_id","path","sha256","permission")}}
    clean=record_experience(raw,store); target=Path(path)
    # Append-only immutable experience store; no overwrite of a different record.
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():
        existing=json.loads(target.read_text(encoding="utf-8"))
        if existing!=clean: raise LearningRefused("experience path already contains different immutable record")
    else: checkpoint._atomic(target,checkpoint.canonical(clean)+b"\n")
def retrieve_experiences(directory: str | Path, *, reference_store: str | Path, problem_tags: list[str], environment: dict, limit: int=20, ignore_environment_keys: set[str] | None = None) -> list[dict]:
    if type(limit) is not int or not 1<=limit<=100: raise LearningRefused("limit must be 1..100")
    wanted=set(problem_tags)
    if not wanted or not isinstance(environment,dict) or not environment: raise LearningRefused("tags and exact environment required")
    rows=[]
    for path in sorted(Path(directory).glob("*.json"),key=lambda p:p.name):
        try: row=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError) as exc: raise LearningRefused(f"invalid experience file {path.name}: {exc}") from exc
        claimed=row.pop("sha256",None)
        if claimed!=canonical_digest(row): raise LearningRefused(f"experience digest mismatch: {path.name}")
        row["sha256"]=claimed
        # Re-read the exact package and validator report before reuse; old claims expire if evidence disappears or changes.
        validator=row.get("validator",{}); report_ref=validator.get("report_source",{})
        try:
            report_pkg=references._load_package(reference_store,report_ref.get("package_id",""))
        except (OSError,ValueError):
            report_pkg=None
        if report_pkg is None: raise LearningRefused(f"validator evidence package unavailable: {path.name}")
        report_entry=next((f for f in report_pkg["files"] if f.get("path")==report_ref.get("path")),None)
        if report_entry is None or hashlib.sha256(report_entry["content"].encode()).hexdigest()!=report_ref.get("sha256"): raise LearningRefused(f"validator evidence invalidated: {path.name}")
        try: report=json.loads(report_entry["content"])
        except (ValueError,TypeError) as exc: raise LearningRefused(f"validator report corrupted: {path.name}") from exc
        if report.get("status")!="PASS" or report.get("validator_id")!=validator.get("validator_id") or report.get("before_sha256")!=row.get("before_sha256") or report.get("after_sha256")!=row.get("after_sha256") or report.get("environment")!=row.get("environment"): raise LearningRefused(f"experience invalidated by validator report mismatch: {path.name}")
        record_environment=row.get("environment",{})
        current_environment={k:v for k,v in environment.items() if k not in (ignore_environment_keys or set())}
        recorded_environment={k:v for k,v in record_environment.items() if k not in (ignore_environment_keys or set())}
        if wanted.intersection(row.get("problem_tags",[])) and recorded_environment==current_environment:
            rows.append(row)
    return rows[:limit]

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="cmd",required=True)
    b=sub.add_parser("build"); b.add_argument("store"); b.add_argument("input"); b.add_argument("output")
    v=sub.add_parser("validate"); v.add_argument("store"); v.add_argument("model")
    r=sub.add_parser("retrieve"); r.add_argument("directory"); r.add_argument("--reference-store",required=True); r.add_argument("--tag",action="append",required=True); r.add_argument("--environment-json",required=True); r.add_argument("--limit",type=int,default=20)
    args=p.parse_args(argv)
    try:
        if args.cmd=="build":
            data=json.loads(Path(args.input).read_text(encoding="utf-8")); model=build_reference_model(args.store,data["facts"],data["tasks"]); save_reference_model(args.output,model,args.store); result=model
        elif args.cmd=="validate": result=validate_reference_model(json.loads(Path(args.model).read_text(encoding="utf-8")),args.store)
        else: result=retrieve_experiences(args.directory,reference_store=args.reference_store,problem_tags=args.tag,environment=json.loads(args.environment_json),limit=args.limit)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True)); return 0
    except (ValueError,OSError,KeyError) as exc: print(f"HOLD: {exc}",file=sys.stderr); return 2
if __name__=="__main__": raise SystemExit(main())
