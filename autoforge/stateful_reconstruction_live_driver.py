#!/usr/bin/env python3
"""Generate a Python stateful reconstruction through application_build, then independently compare it."""
from __future__ import annotations
import argparse, hashlib, json, shutil, sys, tempfile
from pathlib import Path
import agent_budget, agent_runtime, application_builder, application_proposer, applications, references, reference_reconstruction

ROOT = Path(__file__).resolve().parents[1]
# Adapters ship beside this module; the project root holds fixtures, not adapter scripts.
MODULES = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "stateful_reference"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--engine", choices=("docker", "podman"), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--node-image", required=True)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--token-budget", type=int, default=500000)
    parser.add_argument("--contract", default=str(FIXTURE / "contract.json"))
    parser.add_argument("--prepare-only", action="store_true")
    return main_from_args(parser.parse_args())


def main_from_args(args) -> int:
    store = Path(args.store)
    request = FIXTURE / "request.json"
    contract_path = Path(args.contract).resolve()
    if not request.is_file() or not contract_path.is_file():
        raise SystemExit("HOLD: authored stateful request/contract fixture is missing")
    config=application_proposer.check_config()
    if not config.get("configured") or not config.get("endpoint_valid") or not config.get("transport_allowed"):
        raise SystemExit("HOLD: local model endpoint configuration is missing or invalid; no model dispatch")
    try:
        with tempfile.TemporaryDirectory(prefix="autoforge-stateful-reference-") as source_temp:
            source_root = Path(source_temp)
            for rel in ("main.js", "ledger.js"):
                (source_root / rel).write_bytes((FIXTURE / rel).read_bytes())
            package = references.ingest(source_root, store, "AutoForge stateful reconstruction fixture authored for this task", "MIT", kind="source")
        package_data = references._load_package(store, package["package_id"])
        if package_data.get("license") != "MIT" or {row["path"] for row in package_data["files"]} != {"main.js", "ledger.js"}:
            raise SystemExit("HOLD: immutable source package does not match the authored two-module reference")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["reference_package_id"] = package["package_id"]
        pinned = {row["path"]: row["sha256"] for row in package_data["files"]}
        if contract.get("reference_files") != pinned:
            raise SystemExit("HOLD: packaged reference module bytes do not match the independent contract digests")
        if contract.get("state_file") != "/tmp/autoforge-stateful-ledger.json" or contract.get("state_file_max_bytes") != 128:
            raise SystemExit("HOLD: contract state path and bound do not match fixed runtime protocol")
        bound_contract = Path(args.candidate).resolve().parent / (Path(args.candidate).name + "-stateful-contract.json")
        builder_contract = Path(args.candidate).resolve().parent / (Path(args.candidate).name + "-builder-contract.json")
        if bound_contract.exists() or builder_contract.exists():
            raise SystemExit("HOLD: contract output already exists")
        bound_contract.write_text(json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
        builder_scenarios=[]
        for source_case in contract["scenarios"]:
            if "invocations" in source_case:
                builder_scenarios.extend({"name":f"stateful-{source_case['name']}-{index}","requests":invocation["requests"],"expected":invocation["expected"]} for index,invocation in enumerate(source_case["invocations"]))
            elif "requests" in source_case and "expected" in source_case:
                builder_scenarios.append({"name":f"stateful-{source_case['name']}","requests":source_case["requests"],"expected":source_case["expected"]})
        builder_contract.write_text(json.dumps({"schema":1,"entrypoint":"main.py","scenarios":builder_scenarios},ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n",encoding="utf-8",newline="\n")
        raw_request=json.loads(request.read_text(encoding="utf-8"))
        req={"schema":raw_request["schema"],"goal":raw_request["goal"],"references":[package["package_id"]]}
        req=applications._request_payload(req)
        req_path = Path(args.candidate).resolve().parent / (Path(args.candidate).name + "-stateful-request.json")
        if req_path.exists():
            raise SystemExit("HOLD: model request output already exists")
        req_path.write_text(json.dumps(req, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
        if args.prepare_only:
            report={"status":"PREPARED","package_id":package["package_id"],"license":package_data["license"],"reference_files":pinned,"contract":str(bound_contract),"adapter_contract":str(builder_contract),"request":str(req_path),"candidate":str(Path(args.candidate).resolve()),"report":str(Path(args.report).resolve()),"model_contacted":False,"model_endpoint":application_proposer.check_config()}
            print(json.dumps(report,ensure_ascii=False,indent=2));return 0
        info=application_builder.preflight(args.engine,args.image)
        reference_info=application_builder.preflight(args.engine,args.node_image)
        output=Path(args.candidate).resolve();run_dir=output.parent/(output.name+"-model-session")
        if output.exists() or run_dir.exists() or Path(args.report).exists():
            raise SystemExit("HOLD: candidate, model-session, and report outputs must be absent")
        builder_argv=[sys.executable,str(MODULES/"stateful_reconstruction_adapter.py"),"--store",str(store.resolve()),"--package-id",package["package_id"],"--contract",str(bound_contract.resolve()),"--engine",args.engine,"--image",args.image,"--node-image",args.node_image]
        refs=applications._resolve_references(store,req["references"])
        session=agent_runtime.run(req,refs,builder_argv,run_dir,args.max_turns,limits=agent_budget.Limits(model_calls=args.max_turns,tool_calls=96,tokens=args.token_budget,seconds=600),allowed_tools=["list_files","read_file","create_file","edit_file","search_files","read_reference","search_references","validate"])
        child_report=run_dir/"candidate.json"
        if session.get("status")!="PASS":
            raise SystemExit("HOLD: full persisted-state evaluator did not accept model candidate; session="+str(run_dir))
        candidate_data=json.loads((run_dir/"candidate.json").read_text(encoding="utf-8"))
        candidate_data,_=applications.validate_candidate(candidate_data)
        output.mkdir(parents=True,exist_ok=False)
        for entry in candidate_data["files"]:
            target=output.joinpath(*Path(entry["path"]).parts)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(entry["content"],encoding="utf-8",newline="\n")
        acceptance=reference_reconstruction.evaluate_stateful(store,package["package_id"],output,bound_contract,
            engine=args.engine,image=args.image,reference_engine=args.engine,reference_image=args.node_image)
        acceptance["model_session"]={"path":str(run_dir),"session_id":session.get("session_id"),"status":session.get("status"),"task_status":session.get("task_status"),"candidate_json_sha256":hashlib.sha256((run_dir/"candidate.json").read_bytes()).hexdigest()}
        acceptance.pop("sha256",None);acceptance["sha256"]=hashlib.sha256(references.canonical(acceptance)).hexdigest()
        Path(args.report).write_bytes(references.canonical(acceptance)+b"\n")
        print(json.dumps({"status":acceptance["status"],"report":str(Path(args.report).resolve()),"model_session":str(run_dir),"reference_package_id":package["package_id"],"python_environment":info,"node_environment":reference_info,"reference_license":package_data["license"],"reference_source_sha256":hashlib.sha256(references.canonical(package_data)).hexdigest(),"limitations":acceptance["limitations"]},ensure_ascii=False,indent=2))
        return 0 if acceptance["status"]=="PASS" else 1
    except (OSError,ValueError,application_builder.BuildFailure,application_builder.MissingPrerequisite) as exc:
        print(json.dumps({"status":"HOLD","reason":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
