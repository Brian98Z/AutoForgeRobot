#!/usr/bin/env python3
"""Run a model-generated REA reconstruction, then independently execute both sides."""
from __future__ import annotations
import argparse, hashlib, json, os, sys
from pathlib import Path
import applications, agent_runtime, agent_budget, application_builder, references, reference_reconstruction as runner

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_IDS = ["ev_7e3f57f9b575b7645eedb6b7418eae4d1f24bc270f838a21e23a2af4c93f90a1", "ev_b04ce6629ab278fc7482d58fded7a4b4afef64bc2d38ec342cfe51588a3952e4"]
ARTIFACT_SHA256 = "0af087828b287131ba6b90b01feb43da989fc2e2f21861b798f75247c58c759c"

def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store",required=True); p.add_argument("--evidence-package-id",required=True); p.add_argument("--model-context-package-id",required=True); p.add_argument("--reference-package-id",required=True)
    p.add_argument("--artifact-root",required=True); p.add_argument("--candidate",required=True); p.add_argument("--request",required=True)
    p.add_argument("--acceptance-contract",required=True); p.add_argument("--report",required=True); p.add_argument("--evidence-contract",required=True)
    p.add_argument("--engine",choices=("docker","podman"),required=True); p.add_argument("--image",required=True); p.add_argument("--node-image",required=True); p.add_argument("--max-turns",type=int,default=12)
    a=p.parse_args()
    evidence=references._load_package(a.store,a.evidence_package_id)
    model_context=references._load_package(a.store,a.model_context_package_id)
    selected=[row for row in evidence.get("upstream_evidence",[]) if row.get("evidence_id") in EVIDENCE_IDS]
    if {row.get("evidence_id") for row in selected} != set(EVIDENCE_IDS) or len(selected)!=len(EVIDENCE_IDS): raise SystemExit("HOLD: required exact REA Evidence IDs are absent or duplicated")
    if not all(evidence_id in "".join(f.get("content","") for f in model_context.get("files",[])) for evidence_id in EVIDENCE_IDS): raise SystemExit("HOLD: compact context does not cite selected Evidence IDs")
    artifact=Path(a.artifact_root); source=artifact/"main.js"
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=ARTIFACT_SHA256: raise SystemExit("HOLD: shipped artifact bytes do not match the selected evidence identity")
    reference=references._load_package(a.store,a.reference_package_id)
    if reference.get("kind")!="source" or not reference.get("license","").strip(): raise SystemExit("HOLD: licensed package containing the exact JavaScript reference bytes is required")
    request=applications._request_payload(json.loads(Path(a.request).read_text(encoding="utf-8")))
    if request["references"] != [a.model_context_package_id,a.reference_package_id]: raise SystemExit("HOLD: model must receive compact excerpt and exact-source package")
    refs=applications._resolve_references(Path(a.store),request["references"])
    info=application_builder.preflight(a.engine,a.image)
    output=Path(a.candidate)
    result=agent_runtime.run(request,refs,[sys.executable,str(ROOT / "autoforge" / "application_builder.py"),"--engine",a.engine,"--image",a.image,"--contract",str(Path(a.acceptance_contract).resolve())],output,a.max_turns,
                            limits=agent_budget.Limits(model_calls=a.max_turns,tool_calls=96,tokens=500000,seconds=600),
                             allowed_tools=["list_files","read_file","create_file","edit_file","search_files","read_reference","search_references","validate"])
    if result["status"] != "PASS":
        raise SystemExit("HOLD: model session did not produce a builder-accepted candidate; no manual candidate substituted")
    candidate_data=json.loads((output/"candidate.json").read_text(encoding="utf-8"))
    candidate_data,_=applications.validate_candidate(candidate_data)
    generated=output/"model-candidate"
    generated.mkdir(exist_ok=False)
    for entry in candidate_data["files"]:
        target=generated.joinpath(*Path(entry["path"]).parts)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(entry["content"],encoding="utf-8",newline="\n")
    acceptance=runner.evaluate_evidence(a.store,a.evidence_package_id,a.reference_package_id,generated,a.evidence_contract,a.artifact_root,engine=a.engine,image=a.image,reference_engine=a.engine,reference_image=a.node_image)
    acceptance["model_session"]={"path":str(output.resolve()),"session_id":result["session_id"],"status":result["status"],"task_status":result["task_status"],"candidate_file_sha256":hashlib.sha256((output/"candidate.json").read_bytes()).hexdigest()}
    acceptance.pop("sha256",None)
    acceptance["sha256"]=hashlib.sha256(references.canonical(acceptance)).hexdigest()
    report={"schema":1,"status":acceptance["status"],"task_status":result["task_status"],"model_session":str(output.resolve()),"acceptance_report":str(Path(a.report).resolve()),
            "evidence_package_id":a.evidence_package_id,"selected_evidence_ids":EVIDENCE_IDS,"artifact_sha256":ARTIFACT_SHA256,
            "reference_package_id":a.reference_package_id,"reference_license":reference["license"],"reference_kind":"byte-identical-shipped-javascript",
            "container":info,"model_profile":{"base_url":os.environ.get("AUTOFORGE_API_BASE"),"model":os.environ.get("AUTOFORGE_MODEL"),"enable_thinking":False},
            "limitations":acceptance["limitations"]}
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if acceptance["status"]=="PASS" else 1
if __name__=="__main__":raise SystemExit(main())
