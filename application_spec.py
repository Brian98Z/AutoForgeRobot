#!/usr/bin/env python3
"""Strict, versioned application specifications with explicit operator approval.

Natural-language goals can be converted into a bounded structured draft; interpretation
is not semantic approval, and only an independent operator may approve a specification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
import agent_transport

SCHEMA = 1
PROVENANCE = {"user", "design", "observation", "inference", "unknown"}
REQUIRED_TOP = {"schema", "spec_id", "version", "goal", "requirements", "entities", "tasks", "oracle", "ambiguities", "conflicts", "non_goals", "approval"}
OPTIONAL_TOP = {"sha256", "revision", "design_context"}
REQUIRED_REQUIREMENT = {"id", "statement", "provenance", "source", "inputs", "outputs", "failures", "invariants", "state_transitions", "non_goals", "acceptance_ids", "ambiguity_ids", "conflict_ids"}
REQUIRED_ENTITY = {"id", "fields", "constraints", "roles"}
REQUIRED_FIELD = {"name", "type", "required", "constraints", "provenance", "source"}
REQUIRED_TASK = {"id", "title", "depends_on", "requirement_ids", "acceptance_ids", "kind"}

class SpecRefused(ValueError):
    """Malformed or incomplete specification, unsafe oracle mixing, or blocked change."""

    def __init__(self, message: str, *, proposal_stage: dict[str, Any] | None = None):
        super().__init__(message)
        self.proposal_stage = proposal_stage

def _fail(message: str) -> None:
    raise SpecRefused(message)

def _text(value: Any, where: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where} must be a non-empty string")
    return value.strip()

def _strings(value: Any, where: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        _fail(f"{where} must be {'a non-empty ' if not allow_empty else ''}list of strings")
    result = [_text(item, f"{where}[{i}]") for i, item in enumerate(value)]
    if len(set(result)) != len(result):
        _fail(f"{where} contains duplicates")
    return result

def _keys(value: Any, required: set[str], where: str, allowed: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{where} must be an object")
    missing = required - value.keys()
    extra = value.keys() - (allowed or required)
    if missing or extra:
        _fail(f"{where} keys mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    return value

def _provenance(value: Any, where: str) -> dict[str, str]:
    _keys(value, {"kind", "source"}, where)
    kind = _text(value["kind"], f"{where}.kind")
    if kind not in PROVENANCE:
        _fail(f"{where}.kind must be one of {sorted(PROVENANCE)}")
    source = _text(value["source"], f"{where}.source")
    return {"kind": kind, "source": source}

def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def validate_spec(candidate: Any, *, require_approved: bool = False) -> dict[str, Any]:
    """Validate and canonicalize one explicit structured spec; never parses prose."""
    obj = _keys(candidate, REQUIRED_TOP, "spec", REQUIRED_TOP | OPTIONAL_TOP)
    if obj["schema"] != SCHEMA:
        _fail(f"spec.schema must equal {SCHEMA}")
    spec_id = _text(obj["spec_id"], "spec.spec_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", spec_id):
        _fail("spec.spec_id has invalid characters")
    version = obj["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        _fail("spec.version must be a positive integer")
    goal = obj["goal"]
    if not isinstance(goal,str) or not goal.strip(): _fail("spec.goal must be nonempty text")
    if not isinstance(obj["requirements"],list):_fail("spec.requirements must be a list")
    requirements=[];req_ids=set();acceptance_ids=set()
    for i, raw in enumerate(obj["requirements"]):
        w = f"requirements[{i}]"
        req = _keys(raw, REQUIRED_REQUIREMENT, w)
        rid = _text(req["id"], f"{w}.id")
        if rid in req_ids: _fail(f"duplicate requirement id: {rid}")
        req_ids.add(rid)
        provenance = _text(req["provenance"], f"{w}.provenance")
        if provenance not in PROVENANCE: _fail(f"{w}.provenance is invalid")
        source = _text(req["source"], f"{w}.source")
        ids = _strings(req["acceptance_ids"], f"{w}.acceptance_ids", allow_empty=False)
        acceptance_ids.update(ids)
        requirements.append({"id":rid,"statement":_text(req["statement"],f"{w}.statement"),"provenance":provenance,"source":source,
            "inputs":_strings(req["inputs"],f"{w}.inputs"),"outputs":_strings(req["outputs"],f"{w}.outputs"),"failures":_strings(req["failures"],f"{w}.failures"),
            "invariants":_strings(req["invariants"],f"{w}.invariants"),"state_transitions":_strings(req["state_transitions"],f"{w}.state_transitions"),"non_goals":_strings(req["non_goals"],f"{w}.non_goals"),"acceptance_ids":ids,
            "ambiguity_ids":_strings(req["ambiguity_ids"],f"{w}.ambiguity_ids"),"conflict_ids":_strings(req["conflict_ids"],f"{w}.conflict_ids")})
    entities = []
    entity_ids=set()
    if not isinstance(obj["entities"],list):_fail("spec.entities must be a list")
    for i, raw in enumerate(obj["entities"] if isinstance(obj["entities"], list) else []):
        w=f"entities[{i}]"; ent=_keys(raw, REQUIRED_ENTITY, w); eid=_text(ent["id"],f"{w}.id")
        if eid in entity_ids: _fail(f"duplicate entity id: {eid}")
        entity_ids.add(eid); fields=[]; field_names=set()
        if not isinstance(ent["fields"], list): _fail(f"{w}.fields must be a list")
        for j, fr in enumerate(ent["fields"]):
            fw=f"{w}.fields[{j}]"; field=_keys(fr,REQUIRED_FIELD,fw); name=_text(field["name"],f"{fw}.name")
            if name in field_names: _fail(f"duplicate field {name} in {eid}")
            field_names.add(name)
            if not isinstance(field["required"],bool): _fail(f"{fw}.required must be boolean")
            if not isinstance(field["provenance"],str) or field["provenance"] not in PROVENANCE:_fail(f"{fw}.provenance is invalid")
            fields.append({"name":name,"type":_text(field["type"],f"{fw}.type"),"required":field["required"],"constraints":_strings(field["constraints"],f"{fw}.constraints"),"provenance":_text(field["provenance"],f"{fw}.provenance"),"source":_text(field["source"],f"{fw}.source")})
        entities.append({"id":eid,"fields":fields,"constraints":_strings(ent["constraints"],f"{w}.constraints"),"roles":_strings(ent["roles"],f"{w}.roles")})
    oracle=_keys(obj["oracle"],{"public_acceptance_ids","hidden_oracle_ids","separation"},"oracle")
    public=_strings(oracle["public_acceptance_ids"],"oracle.public_acceptance_ids")
    hidden=_strings(oracle["hidden_oracle_ids"],"oracle.hidden_oracle_ids")
    if set(public)&set(hidden): _fail("public and hidden oracle IDs must be disjoint")
    if set(public)!=acceptance_ids: _fail("every acceptance ID must be declared public exactly once in oracle; hidden tests are separate")
    separation=_text(oracle["separation"],"oracle.separation")
    requirement_acceptance={req["id"]:set(req["acceptance_ids"]) for req in requirements}
    tasks=[]; task_ids=set(); graph={}
    for i, raw in enumerate(obj["tasks"]):
        w=f"tasks[{i}]"
        task=_keys(raw,REQUIRED_TASK,w)
        tid=_text(task["id"],f"{w}.id")
        if tid in task_ids:_fail(f"duplicate task id: {tid}")
        task_ids.add(tid); deps=_strings(task["depends_on"],f"{w}.depends_on"); aids=_strings(task["acceptance_ids"],f"{w}.acceptance_ids",allow_empty=False)
        if not set(aids)<=acceptance_ids:_fail(f"{w} references unknown acceptance IDs")
        rids=_strings(task["requirement_ids"],f"{w}.requirement_ids",allow_empty=False)
        if not set(rids)<=set(requirement_acceptance):_fail(f"{w} references unknown requirement IDs")
        allowed_aids={aid for rid in rids for aid in requirement_acceptance[rid]}
        if not set(aids)<=allowed_aids or any(not (requirement_acceptance[rid]&set(aids)) for rid in rids):_fail(f"{w} acceptance IDs are not bound to its requirements")
        if not isinstance(task["kind"],str) or task["kind"] not in {"design","implementation","verification","review","delivery"}:_fail(f"{w}.kind invalid")
        tasks.append({"id":tid,"title":_text(task["title"],f"{w}.title"),"depends_on":deps,"requirement_ids":rids,"acceptance_ids":aids,"kind":task["kind"]}); graph[tid]=deps
    for tid,deps in graph.items():
        if tid in deps or not set(deps)<=task_ids:_fail(f"task {tid} has self or unknown dependency")
    visiting=set(); visited=set()
    def visit(tid):
        if tid in visiting:_fail("task dependency graph contains a cycle")
        if tid in visited:return
        visiting.add(tid)
        for dep in graph[tid]:visit(dep)
        visiting.remove(tid);visited.add(tid)
    for tid in graph:visit(tid)
    ambiguities=obj["ambiguities"]; conflicts=obj["conflicts"]
    for name,items in (("ambiguities",ambiguities),("conflicts",conflicts)):
        if not isinstance(items,list):_fail(f"spec.{name} must be a list")
        seen=set()
        for i,item in enumerate(items):
            w=f"{name}[{i}]"; _keys(item,{"id","description","blocking"},w)
            iid=_text(item["id"],w+".id")
            if iid in seen:_fail(f"duplicate {name} ID: {iid}")
            seen.add(iid);_text(item["description"],w+".description")
            if not isinstance(item["blocking"],bool):_fail(w+".blocking must be boolean")
            if not item["blocking"]:_fail(f"all unresolved {name} must block; resolve explicitly before approval")
    for name,items in (("ambiguities",ambiguities),("conflicts",conflicts)):
        linked={issue_id for req in requirements for issue_id in req["ambiguity_ids" if name=="ambiguities" else "conflict_ids"]}
        if linked!={item["id"] for item in items}:_fail(f"every unresolved {name} must be linked to requirements exactly")
    known_ambiguities={x["id"] for x in ambiguities}; known_conflicts={x["id"] for x in conflicts}
    for req in requirements:
        if not set(req["ambiguity_ids"])<=known_ambiguities or not set(req["conflict_ids"])<=known_conflicts:_fail(f"requirement {req['id']} references unknown ambiguity/conflict")
    if ambiguities or conflicts:_fail("unresolved ambiguities or conflicting requirements block task planning and approval")
    approval=_keys(obj["approval"],{"status","operator","breaking_change_authorized","authorization_source"},"approval")
    if approval["status"] not in {"draft","approved"}:_fail("approval.status must be draft or approved")
    if not isinstance(approval["breaking_change_authorized"],bool):_fail("approval.breaking_change_authorized must be boolean")
    operator=approval["operator"]; authorization=approval["authorization_source"]
    if approval["status"]=="approved":
        _text(operator,"approval.operator");_text(authorization,"approval.authorization_source")
    elif operator!="" or authorization!="":_fail("draft approval must not claim operator identity or authorization")
    canonical={"schema":SCHEMA,"spec_id":spec_id,"version":version,"goal":goal,"requirements":requirements,"entities":entities,"tasks":tasks,
      "oracle":{"public_acceptance_ids":public,"hidden_oracle_ids":hidden,"separation":separation},"ambiguities":[],"conflicts":[],"non_goals":_strings(obj["non_goals"],"spec.non_goals"),
      "approval":{"status":approval["status"],"operator":operator,"breaking_change_authorized":approval["breaking_change_authorized"],"authorization_source":authorization}}
    if 'design_context' in obj:
        context = obj['design_context']
        if not isinstance(context,str) or len(context.encode('utf-8')) > 32768:
            _fail('design_context must be text of at most 32768 UTF-8 bytes')
        canonical['design_context'] = context
    if "revision" in obj:
        rev=_keys(obj["revision"],{"previous_sha256","retained_acceptance_ids","removed_acceptance_ids","breaking_change_authorized","operator","authorization_source"},"revision")
        if not re.fullmatch(r"[0-9a-f]{64}",_text(rev["previous_sha256"],"revision.previous_sha256")) or not isinstance(rev["breaking_change_authorized"],bool): _fail("revision metadata is invalid")
        removed_ids=_strings(rev["removed_acceptance_ids"],"revision.removed_acceptance_ids")
        if removed_ids and (not rev["breaking_change_authorized"] or not rev["operator"].strip() or not rev["authorization_source"].strip()): _fail("removing regression obligations requires recorded explicit breaking authorization")
        canonical["revision"]={"previous_sha256":rev["previous_sha256"],"retained_acceptance_ids":_strings(rev["retained_acceptance_ids"],"revision.retained_acceptance_ids"),"removed_acceptance_ids":removed_ids,"breaking_change_authorized":rev["breaking_change_authorized"],"operator":_text(rev["operator"],"revision.operator",optional=True),"authorization_source":_text(rev["authorization_source"],"revision.authorization_source",optional=True)}
    calculated=_digest(canonical)
    if "sha256" in obj and obj["sha256"]!=calculated:_fail("spec.sha256 digest does not match canonical contents")
    canonical["sha256"]=calculated
    if require_approved and approval["status"]!="approved":_fail("specification requires explicit operator approval")
    return canonical

def propose_from_goal(goal: str, *, design_context: str = "", hidden_oracle_ids: list[str] | None = None, public_acceptance_ids: list[str] | None = None, on_event=None, review_feedback: str = "") -> dict[str, Any]:
    """Build a draft through bounded stages; only an operator can approve it."""
    if not isinstance(goal,str) or not goal.strip(): _fail("goal must be nonempty text")
    if not isinstance(design_context,str) or len(design_context.encode('utf-8')) > 32768:
        _fail('design_context must be text of at most 32768 UTF-8 bytes')
    design_input = ("\nController engineering decisions (provenance=design only for details absent from the authoritative goal; repeated user obligations remain provenance=user, source=authoritative goal). These decisions resolve implementation choices unless they actually contradict the goal; do not ask for reapproval merely because the goal omitted that detail:\n"+design_context) if design_context else ""
    trusted_hidden_ids = _strings(hidden_oracle_ids if hidden_oracle_ids is not None else [], "hidden_oracle_ids")
    if len(trusted_hidden_ids) != len(set(trusted_hidden_ids)):
        _fail("hidden oracle IDs must be unique")
    trusted_public_ids = _strings(public_acceptance_ids if public_acceptance_ids is not None else [], "public_acceptance_ids")
    if len(trusted_public_ids) != len(set(trusted_public_ids)):
        _fail("public acceptance IDs must be unique")
    if not isinstance(review_feedback,str) or len(review_feedback.encode('utf-8')) > 12000:
        _fail('review_feedback must be text of at most 12000 UTF-8 bytes')

    strings={"type":"array","items":{"type":"string","minLength":1}}
    required_ids={**strings,"minItems":1}
    req_schema={"type":"object","additionalProperties":False,"required":sorted(REQUIRED_REQUIREMENT),"properties":{"id":{"type":"string"},"statement":{"type":"string"},"provenance":{"type":"string","enum":sorted(PROVENANCE)},"source":{"type":"string"},"inputs":strings,"outputs":strings,"failures":strings,"invariants":strings,"state_transitions":strings,"non_goals":strings,"acceptance_ids":required_ids,"ambiguity_ids":strings,"conflict_ids":strings}}
    field_schema={"type":"object","additionalProperties":False,"required":sorted(REQUIRED_FIELD),"properties":{"name":{"type":"string"},"type":{"type":"string"},"required":{"type":"boolean"},"constraints":strings,"provenance":{"type":"string","enum":sorted(PROVENANCE)},"source":{"type":"string"}}}
    entity_schema={"type":"object","additionalProperties":False,"required":sorted(REQUIRED_ENTITY),"properties":{"id":{"type":"string"},"fields":{"type":"array","items":field_schema},"constraints":strings,"roles":strings}}
    task_schema={"type":"object","additionalProperties":False,"required":sorted(REQUIRED_TASK),"properties":{"id":{"type":"string"},"title":{"type":"string"},"depends_on":strings,"requirement_ids":required_ids,"acceptance_ids":required_ids,"kind":{"type":"string","enum":["design","implementation","verification","review","delivery"]}}}
    issue_schema={"type":"object","additionalProperties":False,"required":["id","description","blocking"],"properties":{"id":{"type":"string","minLength":1},"description":{"type":"string","minLength":1},"blocking":{"type":"boolean","enum":[True]}}}
    public_schema=({"type":"array","items":{"type":"string","enum":trusted_public_ids},"minItems":len(trusted_public_ids),"maxItems":len(trusted_public_ids)} if trusted_public_ids else strings)
    def _check_provenance(rows, label):
        for index, row in enumerate(rows):
            source = {"user": "authoritative goal", "design": "controller design"}.get(row.get("provenance"))
            if source and row.get("source") != source:
                _fail(f"{label}[{index}] {row['provenance']} provenance must cite {source}")

    def stage(name: str, required: list[str], properties: dict[str,Any], user_content: str, semantic_check=None) -> dict[str,Any]:
        tool={"type":"function","function":{"name":name,"description":"Submit only this bounded stage; do not claim approval.","parameters":{"type":"object","additionalProperties":False,"required":required,"properties":properties}}}
        messages=[{'role':'system','content':'Return exactly one tool call for this stage only. Preserve the exact goal. Separate user obligations (provenance=user, source=authoritative goal) from additional controller design details (provenance=design). Split mixed-source obligations when needed. Use complete prose statements; put JSON formats in outputs, not unfinished statement fragments. Record state transitions for state-changing obligations. Code-writing tasks use implementation, not design. Only genuinely unresolved business choices or actual contradictions block; explicitly supplied compatible engineering decisions are resolved. Never invent approval or hidden evaluator identifiers or answers.'},{'role':'user','content':user_content}]
        if review_feedback:
            messages.append({'role':'user','content':'Independent public reviewer findings from the previously rejected draft (repair instructions, not new product requirements):\n'+review_feedback+'\nCorrect these findings in this stage without changing the authoritative goal, dropping obligations, changing acceptance IDs, changing controller design decisions, or claiming approval. The final whole draft remains subject to independent review.'})
        def check(value, schema, where):
            kind=schema.get('type')
            if kind=='object':
                _keys(value,set(schema.get('required',[])),where,set(schema.get('properties',{})))
                for key,item in value.items(): check(item,schema['properties'][key],where+'.'+key)
            elif kind=='array':
                if not isinstance(value,list): _fail(where+' must be an array')
                if len(value)<schema.get('minItems',0) or len(value)>schema.get('maxItems',10000): _fail(where+' array size invalid')
                if all(isinstance(item,str) for item in value) and len(value)!=len(set(value)): _fail(where+' contains duplicates')
                for index,item in enumerate(value): check(item,schema['items'],f'{where}[{index}]')
            elif kind=='string':
                if not isinstance(value,str) or not value.strip(): _fail(where+' must be nonempty text')
            elif kind=='boolean':
                if type(value) is not bool: _fail(where+' must be boolean')
            if 'enum' in schema and value not in schema['enum']: _fail(where+' is outside allowed values')
        for attempt in range(3):
            try:
                answer=agent_transport.complete(messages,[tool],on_event=on_event)
            except agent_transport.Refused as exc:
                raise SpecRefused(f'{name} provider call refused: {exc}',proposal_stage={'stage':name,'goal':goal,'design_context':design_context,'approved':False,'semantically_validated':False}) from exc
            calls=answer.get('tool_calls',[])
            if not isinstance(calls,list) or len(calls)!=1 or not isinstance(calls[0],dict) or not isinstance(calls[0].get('function'),dict) or calls[0]['function'].get('name')!=name:
                _fail(f'configured model did not submit exactly one {name} stage')
            try:
                result=json.loads(calls[0]['function']['arguments'])
                if name=='submit_requirements' and isinstance(result,dict) and (result.get('ambiguities') or result.get('conflicts')):
                    raise SpecRefused('unresolved issues block structural correction',proposal_stage={'stage':'requirements','goal':goal,'design_context':design_context,'result':result,'approved':False,'semantically_validated':False})
                check(result,tool['function']['parameters'],name)
                if semantic_check is not None:
                    semantic_check(result)
                if on_event is not None:
                    on_event({'type':'proposal_stage_result','stage':name,'result':result})
                return result
            except (KeyError,TypeError,json.JSONDecodeError,SpecRefused) as exc:
                if isinstance(exc,SpecRefused) and exc.proposal_stage is not None:
                    raise
                if attempt==2:
                    raise SpecRefused(f'{name} correction limit exhausted: {exc}') from exc
                messages.append(answer)
                messages.append({'role':'tool','tool_call_id':calls[0]['id'],'content':json.dumps({'status':'INVALID','error':str(exc),'instruction':'Resubmit the complete same stage. Fix structural errors only; preserve all obligations and unresolved issues. No approval is granted.'})})
                if on_event is not None: on_event({'type':'proposal_correction','stage':name,'attempt':attempt+1,'error':str(exc)})

    req_result=stage("submit_requirements",["requirements","public_acceptance_ids","ambiguities","conflicts","non_goals"],
        {"requirements":{"type":"array","items":req_schema},"public_acceptance_ids":public_schema,"ambiguities":{"type":"array","items":issue_schema},"conflicts":{"type":"array","items":issue_schema},"non_goals":strings},
        "Exact authoritative user goal (do not rewrite it):\n"+goal+design_input+"\nController public acceptance IDs (preserve exactly; [] means controller did not enumerate them): "+json.dumps(trusted_public_ids,ensure_ascii=False)+"\nEvery requirement needs a nonempty acceptance_ids list. public_acceptance_ids must be the exact unique union of those lists, with no missing or extra IDs. When the controller did not enumerate IDs, propose concise public IDs and reuse those exact IDs in the requirements. For provenance=user source MUST be exactly authoritative goal; provenance=design source MUST be exactly controller design. Attribution mismatches are correctable stage-semantic errors; retain all obligations. Unspecified response shapes remain explicit design choices or blocking ambiguities, not invented user requirements.",
        semantic_check=lambda value: _check_provenance(value["requirements"],"requirements"))
    proposed_public=req_result["public_acceptance_ids"]
    if not isinstance(proposed_public,list) or any(not isinstance(x,str) or not x.strip() for x in proposed_public) or len(proposed_public)!=len(set(proposed_public)):
        _fail("model proposal public acceptance IDs must be a unique nonempty string list")
    if trusted_public_ids and proposed_public!=trusted_public_ids:
        _fail("model proposal public acceptance IDs must exactly match controller-declared IDs")
    requirement_ids=req_result["requirements"]
    if not isinstance(requirement_ids,list): _fail("requirements stage must return a list")
    req_acceptance={}
    for i,req in enumerate(requirement_ids):
        if not isinstance(req,dict) or not isinstance(req.get("acceptance_ids"),list): _fail(f"requirements[{i}].acceptance_ids must be a list")
        for aid in req["acceptance_ids"]:
            if isinstance(aid,str): req_acceptance.setdefault(aid,None)
    if set(req_acceptance)!=set(proposed_public):
        _fail("requirements acceptance IDs must cover exactly the public acceptance IDs")
    for key, field_key in (("ambiguities", "ambiguity_ids"), ("conflicts", "conflict_ids")):
        issues=req_result[key]
        if not isinstance(issues,list): _fail(f"{key} stage value must be a list")
        reported={issue["id"] for issue in issues if isinstance(issue,dict) and isinstance(issue.get("id"),str)}
        if reported!={issue_id for req in requirement_ids for issue_id in req[field_key]}:_fail(f"{key} must be linked to requirements exactly")
        for index, issue in enumerate(issues):
            item=_keys(issue,{"id","description","blocking"},f"{key}[{index}]")
            _text(item["id"],f"{key}[{index}].id")
            _text(item["description"],f"{key}[{index}].description")
            if not isinstance(item["blocking"],bool): _fail(f"{key}[{index}].blocking must be boolean")
            if not item["blocking"]: _fail(f"all unresolved {key} must block; resolve explicitly before approval")
    _strings(req_result["non_goals"],"non_goals")
    if req_result["ambiguities"] or req_result["conflicts"]:
        issues=req_result["ambiguities"]+req_result["conflicts"]
        detail="; ".join(str(x.get("id","?"))+": "+str(x.get("description","?")) for x in issues if isinstance(x,dict))
        raise SpecRefused("unresolved ambiguities or conflicting requirements block task planning and approval"+(": "+detail if detail else ""),
            proposal_stage={"stage":"requirements","goal":goal,"design_context":design_context,
                            "result":req_result,"approved":False,"semantically_validated":False})
    entity_result=stage("submit_entities",["entities"],{"entities":{"type":"array","items":entity_schema}},
        "Exact authoritative user goal:\n"+goal+design_input+"\nRequirements as domain context:\n"+json.dumps(requirement_ids,ensure_ascii=False,separators=(",",":"))+"\nModel DOMAIN BUSINESS entities and fields, NOT requirement IDs or specification-schema metadata as entities. Use provenance=user/source=authoritative goal only for field facts explicitly stated by the goal; use provenance=design/source=controller design for design-derived field facts. Correct attribution without dropping domain entities or fields.",
        semantic_check=lambda value: _check_provenance([field for entity in value["entities"] for field in entity["fields"]],"entity fields"))
    def _check_complete_tasks(value):
        proposal={"schema":SCHEMA,"spec_id":"proposal-"+hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16],"version":1,"goal":goal,
            "requirements":requirement_ids,"entities":entity_result["entities"],"tasks":value["tasks"],
            "oracle":{"public_acceptance_ids":proposed_public,"hidden_oracle_ids":trusted_hidden_ids,"separation":"Hidden evaluator identifiers and answers are controller-held and unavailable to proposal and task planning."},
            "ambiguities":req_result["ambiguities"],"conflicts":req_result["conflicts"],"non_goals":req_result["non_goals"],
            "approval":{"status":"draft","operator":"","breaking_change_authorized":False,"authorization_source":""}}
        if design_context: proposal["design_context"]=design_context
        validate_spec(proposal)
    task_result=stage("submit_tasks",["tasks"],{"tasks":{"type":"array","items":task_schema}},

        "Return one tool call immediately, with no prose. Build the smallest useful dependency-ordered task DAG covering all obligations. Include one or more implementation tasks followed by independent verification task(s) that cover every public acceptance ID; every task acceptance ID must belong to one of its named requirements. Group related work to keep titles short, but do not omit any requirement or acceptance. Do not claim verification has passed.\nPublic IDs: "+json.dumps(proposed_public,ensure_ascii=False,separators=(",",":"))+"\nRequirements: "+json.dumps(requirement_ids,ensure_ascii=False,separators=(",",":"))+"\nEntities: "+json.dumps(entity_result["entities"],ensure_ascii=False,separators=(",",":")),
        semantic_check=_check_complete_tasks)
    proposal={"schema":SCHEMA,"spec_id":"proposal-"+hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16],"version":1,"goal":goal,
      "requirements":requirement_ids,"entities":entity_result["entities"],"tasks":task_result["tasks"],
      "oracle":{"public_acceptance_ids":proposed_public,"hidden_oracle_ids":trusted_hidden_ids,"separation":"Hidden evaluator identifiers and answers are controller-held and unavailable to proposal and task planning."},
      "ambiguities":req_result["ambiguities"],"conflicts":req_result["conflicts"],"non_goals":req_result["non_goals"],
      "approval":{"status":"draft","operator":"","breaking_change_authorized":False,"authorization_source":""}}
    if design_context:
        proposal['design_context'] = design_context
    return propose_spec(proposal)

def propose_spec(proposal: Any) -> dict[str, Any]:
    if not isinstance(proposal,dict):_fail("proposal must be a structured spec object, not natural-language text")
    item=dict(proposal); item["approval"]={"status":"draft","operator":"","breaking_change_authorized":False,"authorization_source":""}
    item.pop("sha256",None)
    item.pop("revision",None)
    return validate_spec(item)

def revise_spec(previous: Any, proposal: Any, *, operator: str, authorization_source: str,
                breaking_change_authorized: bool = False) -> dict[str, Any]:
    """Create next draft; preserve prior acceptance IDs unless explicitly authorized breaking change."""
    old=validate_spec(previous,require_approved=True); new=dict(proposal)
    if old["spec_id"]!=new.get("spec_id") or new.get("version")!=old["version"]+1:_fail("revision must retain spec_id and increment version by exactly one")
    previous_ids={a for r in old["requirements"] for a in r["acceptance_ids"]}
    proposed_ids={a for r in new.get("requirements",[]) if isinstance(r,dict) for a in r.get("acceptance_ids",[]) if isinstance(a,str)}
    removed=previous_ids-proposed_ids
    if removed and not breaking_change_authorized:_fail(f"revision removes regression obligations without explicit breaking authorization: {sorted(removed)}")
    old_requirements={r["id"]:r for r in old["requirements"]}
    new_requirements={r.get("id"):r for r in new.get("requirements",[]) if isinstance(r,dict)}
    for rid,old_req in old_requirements.items():
        if rid not in new_requirements:
            if not breaking_change_authorized:_fail(f"revision removes requirement {rid} without explicit breaking authorization")
            continue
        target=new_requirements[rid]
        if not breaking_change_authorized:
            target["acceptance_ids"]=sorted(set(target.get("acceptance_ids",[]))|set(old_req["acceptance_ids"]))
            target["invariants"]=sorted(set(target.get("invariants",[]))|set(old_req["invariants"]))
            target["state_transitions"]=sorted(set(target.get("state_transitions",[]))|set(old_req["state_transitions"]))
    new["approval"]={"status":"draft","operator":"","breaking_change_authorized":False,"authorization_source":""}
    new.pop("sha256",None);new.pop("revision",None)
    draft=validate_spec(new)
    draft["revision"]={"previous_sha256":old["sha256"],"retained_acceptance_ids":sorted(previous_ids&proposed_ids),"removed_acceptance_ids":sorted(removed),"breaking_change_authorized":breaking_change_authorized,"operator":_text(operator,"operator"),"authorization_source":_text(authorization_source,"authorization_source")}
    draft["sha256"]=_digest({k:v for k,v in draft.items() if k!="sha256"})
    return draft

def approve_spec(draft: Any, *, operator: str, authorization_source: str, breaking_change_authorized: bool = False) -> dict[str, Any]:
    """Operator approval is explicit data supplied by caller; no model can self-approve."""
    obj=dict(draft); obj.pop("sha256",None)
    if obj.get("revision") is not None:
        obj["revision"]["breaking_change_authorized"]=bool(breaking_change_authorized)
    obj["approval"]={"status":"approved","operator":_text(operator,"operator"),"breaking_change_authorized":bool(breaking_change_authorized),"authorization_source":_text(authorization_source,"authorization_source")}
    return validate_spec(obj,require_approved=True)

GATES = [
 {"id":"G0","title":"Read-only/configuration preflight","depends_on":[],"entry_conditions":["scope and authorization are declared"],"outputs":["preflight evidence"],"exit_conditions":["read-only/configuration checks have a recorded result"],"blocked_is_reported":"independently; scope is not silently reduced"},
 {"id":"G1","title":"Real-model tool loop","depends_on":["G0"],"entry_conditions":["G0 exit evidence","explicit provider configuration"],"outputs":["real tool-loop evidence"],"exit_conditions":["schema, permissions, stop behavior and a real tool interaction are evidenced"],"excluded_requirements":["multiple providers","GUI","full context compression","multi-agent"],"blocked_is_reported":"independently; scope is not silently reduced"},
 {"id":"G2","title":"Isolated modification and execution","depends_on":["G1"],"entry_conditions":["G1 exit evidence","approved isolated environment"],"outputs":["candidate and isolated execution evidence"],"exit_conditions":["candidate changes and execution are isolated and evidenced"],"blocked_is_reported":"independently; scope is not silently reduced"},
 {"id":"G3","title":"Real business correction and acceptance","depends_on":["G2"],"entry_conditions":["G2 exit evidence","independent acceptance criteria"],"outputs":["business correction and independent acceptance evidence"],"exit_conditions":["real correction feedback and independent acceptance are evidenced"],"blocked_is_reported":"independently; scope is not silently reduced"},
 {"id":"G4","title":"Persistent multi-round operation and deployment expansion","depends_on":["G3"],"entry_conditions":["G3 exit evidence","deployment authorization where relevant"],"outputs":["persistent-operation/deployment evidence"],"exit_conditions":["expanded path is separately verified"],"blocked_is_reported":"independently; scope is not silently reduced"},
]
def phase_gates() -> list[dict[str, Any]]:
    """Return the A13 stage contract; this is governance metadata, not a scheduler."""
    gates = json.loads(json.dumps(GATES))
    for gate in gates:
        gate['owners'] = {'execution': 'agent_runtime', 'enforcement': 'trusted controller', 'acceptance': 'independent validator', 'authorization': 'operator'}
    return gates

def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="action",required=True)
    check=sub.add_parser("check");check.add_argument("spec",type=Path);check.add_argument("--require-approved",action="store_true")
    sub.add_parser("gates")
    approve=sub.add_parser("approve");approve.add_argument("spec",type=Path);approve.add_argument("--operator",required=True);approve.add_argument("--authorization-source",required=True);approve.add_argument("--authorize-breaking-change",action="store_true");approve.add_argument("--output",type=Path,required=True)
    propose=sub.add_parser("propose");propose.add_argument("--goal",required=True);propose.add_argument("--hidden-oracle-id",action="append",default=[]);propose.add_argument("--acceptance-id",action="append",default=[]);propose.add_argument("--output",type=Path,required=True)
    propose.add_argument('--design-context',type=Path,help='UTF-8 engineering design input, distinct from the authoritative goal; no evaluator inputs or answers')
    propose.add_argument('--review-feedback',type=Path,help='UTF-8 public reviewer findings for a bounded repair proposal; no approval is implied')
    plan_cmd=sub.add_parser("plan");plan_cmd.add_argument("spec",type=Path);plan_cmd.add_argument("--adapters",type=Path,required=True);plan_cmd.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=="gates": result={"schema":1,"gates":phase_gates()}
        elif args.action=="propose":
            if args.output.exists():_fail("proposal output already exists")
            design_context = args.design_context.read_text(encoding='utf-8') if args.design_context else ''
            review_feedback = ''
            if args.review_feedback:
                with args.review_feedback.open('rb') as stream: raw_feedback = stream.read(12001)
                if len(raw_feedback) > 12000: _fail('review feedback exceeds 12000 UTF-8 bytes')
                review_feedback = raw_feedback.decode('utf-8')
            result=propose_from_goal(args.goal,design_context=design_context,hidden_oracle_ids=args.hidden_oracle_id,public_acceptance_ids=args.acceptance_id,on_event=lambda _event: None,review_feedback=review_feedback)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(result,ensure_ascii=False,sort_keys=True)+"\n",encoding="utf-8")
        elif args.action=="plan":
            import application_plan
            result=application_plan.create(json.loads(args.spec.read_text(encoding="utf-8")),json.loads(args.adapters.read_text(encoding="utf-8")),args.output)
        elif args.action=="approve":
            if args.output.exists():_fail("approval output already exists")
            draft=json.loads(args.spec.read_text(encoding="utf-8"))
            result=approve_spec(draft,operator=args.operator,authorization_source=args.authorization_source,breaking_change_authorized=args.authorize_breaking_change)
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(result,ensure_ascii=False,sort_keys=True)+"\n",encoding="utf-8")
        else: result=validate_spec(json.loads(args.spec.read_text(encoding="utf-8")),require_approved=args.require_approved)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True));return 0
    except (OSError,UnicodeError,json.JSONDecodeError,ValueError) as exc:
        report={"status":"BLOCKED","error":str(exc)}
        if isinstance(exc,SpecRefused) and exc.proposal_stage is not None:
            report['proposal_stage']=exc.proposal_stage
        print(json.dumps(report,ensure_ascii=False));return 3

if __name__=="__main__": raise SystemExit(main())
