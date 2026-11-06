#!/usr/bin/env python3
"""Prepare or run a locally approved natural-language spec experiment with controller-owned acceptance."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import application_builder as builder
import application_spec as spec
import applications
import agent_budget
import agent_runtime

ROOT = Path(__file__).resolve().parent
IMAGE = "sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60"
GOAL = ("Build a small in-memory address-book application. Users can add a contact with a name and phone number, "
        "look up a contact by name, and list all contacts in the order they were added.")
DESIGN = ("Implementation interface: provide contacts.py with add_contact(name, phone_number), find_contact(name), "
          "and list_contacts(). Use Python dict records with exactly name and phone_number keys; do not use a "
          "dataclass, custom object, or other record type. Store records in one in-memory ordered list; never read "
          "or write files, databases, or other persistent storage. Names and phone numbers must be Python str "
          "values; otherwise raise TypeError before mutation. Do not trim, normalize, format-validate, or restrict "
          "string contents: preserve accepted strings exactly. Duplicate names are allowed; find_contact performs "
          "exact case-sensitive equality and returns a new dict copy of the first matching contact, or None if "
          "absent. Resolution AMB-001: the controller explicitly selects Python None for a missing exact match; "
          "this is a resolved design choice, not an open ambiguity. add_contact returns a new dict copy. "
          "list_contacts returns a detached list of new dict copies in "
          "insertion order, so callers cannot mutate internal storage. No deletion, editing, sorting, persistence, "
          "accounts, HTTP service, or CLI.")
ACCEPTANCE_IDS = ["A-ADD", "A-FIND-FIRST", "A-LIST-ORDER", "A-INPUT-ATOMICITY", "A-MEMORY-ONLY"]

# Independent acceptance is kept here in the controller, never placed in the model prompt or design context.
CONTRACT = {"schema": 1, "entrypoint": "main.py", "scenarios": [
    {"name": "stateful-behavior", "requests": [
        {"op": "add", "name": "Ada", "phone": "+1 555"},
        {"op": "add", "name": "Grace", "phone": " g-2 "},
        {"op": "add", "name": "Ada", "phone": "second"},
        {"op": "find", "name": "Ada"},
        {"op": "find", "name": "ada"},
        {"op": "find", "name": "Nobody"},
        {"op": "list"},
        {"op": "mutate-snapshot"},
        {"op": "list"}],
     "expected": [
        {"name": "Ada", "phone_number": "+1 555"},
        {"name": "Grace", "phone_number": " g-2 "},
        {"name": "Ada", "phone_number": "second"},
        {"name": "Ada", "phone_number": "+1 555"},
        None, None,
        [{"name": "Ada", "phone_number": "+1 555"}, {"name": "Grace", "phone_number": " g-2 "}, {"name": "Ada", "phone_number": "second"}],
        {"ok": True},
        [{"name": "Ada", "phone_number": "+1 555"}, {"name": "Grace", "phone_number": " g-2 "}, {"name": "Ada", "phone_number": "second"}]]},
    {"name": "invalid-input-is-atomic", "requests": [
        {"op": "add", "name": "Ada", "phone": "111"},
        {"op": "invalid-add", "name": 17, "phone": "bad"},
        {"op": "list"}],
     "expected": [{"name": "Ada", "phone_number": "111"}, {"error": "TypeError"},
                  [{"name": "Ada", "phone_number": "111"}]]}
]}
CONTROLLER_MAIN = '''import json\nimport contacts\nimport sys\nfor raw in sys.stdin:\n    request = json.loads(raw)\n    op = request["op"]\n    if op == "add":\n        result = contacts.add_contact(request["name"], request["phone"])\n    elif op == "find":\n        result = contacts.find_contact(request["name"])\n    elif op == "list":\n        result = contacts.list_contacts()\n    elif op == "mutate-snapshot":\n        snapshot = contacts.list_contacts()\n        snapshot[0]["name"] = "TAMPERED"\n        snapshot.append({"name": "TAMPERED", "phone_number": "TAMPERED"})\n        result = {"ok": contacts.find_contact("Ada")["name"] == "Ada"}\n    elif op == "invalid-add":\n        try:\n            contacts.add_contact(request["name"], request["phone"])\n        except TypeError:\n            result = {"error": "TypeError"}\n        else:\n            result = {"error": "NO_ERROR"}\n    else:\n        raise ValueError("unknown controller harness operation")\n    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))\n'''


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def write_new(path: Path, data: bytes) -> None:
    if path.exists():
        raise ValueError(f"refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(data)


def preliminary_screen(draft: dict) -> list[str]:
    errors = []
    if draft.get("goal") != GOAL:
        errors.append("draft goal differs byte-for-byte from controller goal")
    if draft.get("design_context") != DESIGN:
        errors.append("draft does not retain exact controller design context")
    reqs = {r["id"]: r for r in draft.get("requirements", [])}
    public = {a for r in reqs.values() for a in r.get("acceptance_ids", [])}
    if public != set(ACCEPTANCE_IDS) or draft.get("oracle", {}).get("public_acceptance_ids") != ACCEPTANCE_IDS:
        errors.append("public acceptance IDs do not match controller-owned ID set")
    if draft.get("approval", {}).get("status") != "draft":
        errors.append("proposal is not an unapproved draft")
    if draft.get("oracle", {}).get("hidden_oracle_ids"):
        errors.append("candidate draft exposes hidden oracle identifiers")
    if draft.get("ambiguities") or draft.get("conflicts"):
        errors.append("unresolved ambiguity/conflict remains")
    for rid, requirement in reqs.items():
        if requirement.get("provenance") == "user" and requirement.get("source") != "authoritative goal":
            errors.append(f"{rid} misattributes user provenance")
        if requirement.get("provenance") == "design" and requirement.get("source") != "controller design":
            errors.append(f"{rid} misattributes design provenance")
    if not any(r.get("provenance") == "user" and "add" in r.get("statement", "").lower() for r in reqs.values()):
        errors.append("user add-contact obligation missing")
    if not any(r.get("provenance") == "user" and ("find" in r.get("statement", "").lower() or "look up" in r.get("statement", "").lower()) for r in reqs.values()):
        errors.append("user contact-lookup obligation missing")
    if not any(r.get("provenance") == "user" and "list" in r.get("statement", "").lower() for r in reqs.values()):
        errors.append("user list-in-order obligation missing")
    if not any(r.get("provenance") == "design" and "type" in r.get("statement", "").lower() for r in reqs.values()):
        errors.append("controller type-rejection design decision missing")
    if not any(t.get("kind") == "implementation" for t in draft.get("tasks", [])):
        errors.append("no implementation task")
    for task in draft.get("tasks", []):
        named = set(task.get("requirement_ids", []))
        obligations = {a for rid in named if rid in reqs for a in reqs[rid].get("acceptance_ids", [])}
        if not named or not set(task.get("acceptance_ids", [])) <= obligations:
            errors.append(f"task {task.get('id')} has unbound acceptance IDs")
    return errors


def stage_harness(candidate_root: Path, stage_root: Path) -> dict:
    entries = list(candidate_root.iterdir())
    source = candidate_root / "contacts.py"
    if source.is_symlink() or not source.is_file() or {x.name for x in entries} != {"contacts.py"}:
        raise ValueError("candidate workspace must contain exactly one regular contacts.py")
    candidate = {"schema": 1, "goal": GOAL, "references": [], "files": [
        {"path": "contacts.py", "content": source.read_text(encoding="utf-8")},
        {"path": "main.py", "content": CONTROLLER_MAIN}]}
    return applications.stage_candidate(candidate, stage_root)


def run_builder(candidate_root: Path, stage_root: Path, out: Path, image: str, engine: str):
    staged = stage_harness(candidate_root, stage_root)
    envelope = json.loads(Path(staged["manifest"]).read_text(encoding="utf-8"))
    manifest = {"schema": 1, "workspace_manifest": envelope, "candidate_sha256": staged["candidate_sha256"]}
    report = builder.build(manifest, CONTRACT, engine, image, builder.preflight(engine, image))
    report_path = out / "container-acceptance.json"
    write_new(report_path, canonical(report) + b"\n")
    return {"stage_manifest": staged["manifest"], "candidate_sha256": staged["candidate_sha256"],
            "report": str(report_path), "result": report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--propose", action="store_true", help="make one real local-model proposal; never approves or executes")
    parser.add_argument("--repair-review", type=Path, help="feed public reviewer findings into staged proposal; draft remains unapproved")
    parser.add_argument("--draft", type=Path, help="review model draft only; never executes")
    parser.add_argument("--approved-spec", type=Path, help="operator-approved specification required with --run")
    parser.add_argument("--candidate", type=Path, help="directory containing exactly contacts.py")
    parser.add_argument("--run", action="store_true", help="execute accepted candidate only after external approval")
    parser.add_argument("--implement", action="store_true", help="generate contacts.py with a real model and validate it in isolated containers after approval")
    parser.add_argument("--implementation-session", type=Path, help="fresh durable implementation session directory")
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--run-dir", type=Path, help="fresh experiment output directory")
    args = parser.parse_args()
    run_dir = (args.run_dir or ROOT / "bootstrap-runs" / ("spec-positive-" + uuid.uuid4().hex)).resolve()
    if run_dir.exists(): raise SystemExit("run directory must be absent")
    run_dir.mkdir(parents=True)
    controller = run_dir / "controller"
    controller.mkdir()
    write_new(controller / "goal.txt", (GOAL + "\n").encode())
    write_new(controller / "design-context.txt", (DESIGN + "\n").encode())
    write_new(controller / "acceptance-contract.json", canonical(CONTRACT) + b"\n")
    write_new(controller / "harness-main.py", CONTROLLER_MAIN.encode())
    plan = {"schema": 1, "goal_sha256": hashlib.sha256(GOAL.encode()).hexdigest(),
            "design_sha256": hashlib.sha256(DESIGN.encode()).hexdigest(),
            "acceptance_ids": ACCEPTANCE_IDS, "engine": args.engine, "image": args.image,
            "model_contacted": False, "approved": False, "candidate_executed": False,
            "stage": "PREPARED_NO_MODEL_CALL"}
    write_new(run_dir / "experiment.json", canonical(plan) + b"\n")
    result = {"status": "PREPARED", "run_dir": str(run_dir),
              "model_contacted": False, "approved": False, "candidate_executed": False}
    if args.propose:
        if not os.environ.get("AUTOFORGE_API_BASE") or not os.environ.get("AUTOFORGE_MODEL"):
            raise SystemExit("--propose requires explicit AUTOFORGE_API_BASE and AUTOFORGE_MODEL")
        os.environ.setdefault("AUTOFORGE_ENABLE_THINKING", "false")
        os.environ["AUTOFORGE_TOOL_CHOICE"] = "required"
        events = []
        try:
            feedback = args.repair_review.read_text(encoding="utf-8") if args.repair_review else ""
            draft = spec.propose_from_goal(GOAL, design_context=DESIGN,
                public_acceptance_ids=ACCEPTANCE_IDS, on_event=events.append, review_feedback=feedback)
            write_new(run_dir / "draft.json", canonical(draft) + b"\n")
            result.update(status="DRAFT_UNAPPROVED", model_contacted=True, draft=str(run_dir / "draft.json"))
        except (spec.SpecRefused, spec.agent_transport.Refused) as exc:
            outcome = {"status": "BLOCKED", "error": str(exc), "proposal_stage": getattr(exc, "proposal_stage", None)}
            if isinstance(exc, spec.SpecRefused) and exc.proposal_stage is not None:
                write_new(run_dir / "proposal-stage.json", canonical(exc.proposal_stage) + b"\n")
            write_new(run_dir / "proposal-outcome.json", canonical(outcome) + b"\n")
        finally:
            write_new(run_dir / "proposal-events.jsonl", b"".join(canonical(event) + b"\n" for event in events))
    if args.implement:
        if args.approved_spec is None or args.implementation_session is None:
            raise SystemExit("--implement requires --approved-spec and --implementation-session")
        approved = spec.validate_spec(json.loads(args.approved_spec.read_text(encoding="utf-8")), require_approved=True)
        implementation_builder = controller / "implementation_builder.py"
        wrapper = "from __future__ import annotations\nimport json,sys\nfrom pathlib import Path\nROOT=Path(__file__).resolve().parents[3]\nsys.path.insert(0,str(ROOT))\nimport application_builder as builder\nimport applications\nimport spec_positive_driver as driver\ndef main():\n envelope=builder._strict_loads(sys.stdin.buffer.read(builder.MAX_INPUT+1))\n root,digest=builder._manifest(envelope['workspace_manifest'],require_entrypoint=False)\n if envelope.get('schema')!=1 or envelope.get('candidate_sha256')!=digest: raise ValueError('candidate envelope identity mismatch')\n paths={x['path'] for x in envelope['workspace_manifest']['files']}\n if paths!={'contacts.py'}:\n  print(json.dumps({'schema':1,'candidate_sha256':digest,'build_pass':False,'test_pass':False,'observations':['Candidate must contain exactly contacts.py.'],'limitations':['No candidate source was executed.']},separators=(',',':'))); return\n contact=(root/'contacts.py').read_text(encoding='utf-8')\n files=[{'path':'contacts.py','content':contact},{'path':'main.py','content':driver.CONTROLLER_MAIN}]\n staged=applications.stage_candidate({'schema':1,'goal':driver.GOAL,'references':[],'files':files},Path(envelope['workspace_manifest']['workspace']).parent/'trusted-stage')\n manifest=json.loads(Path(staged['manifest']).read_text(encoding='utf-8'))\n contract=Path(__file__).with_name('acceptance-contract.json')\n report=builder.build({'schema':1,'workspace_manifest':manifest,'candidate_sha256':staged['candidate_sha256']},builder._load_json(contract,builder.MAX_INPUT), 'ENGINE','IMAGE',builder.preflight('ENGINE','IMAGE'))\n report['candidate_sha256']=digest\n print(json.dumps(report,ensure_ascii=False,separators=(',',':')))\nif __name__=='__main__': main()\n"
        wrapper=wrapper.replace("Path(envelope['workspace_manifest']['workspace']).parent/'trusted-stage'", "root.parent/('trusted-stage-'+root.name)")
        wrapper=wrapper.replace(" report['candidate_sha256']=digest", " (Path(staged['manifest']).parent/'execution-report.json').write_text(json.dumps(report),encoding='utf-8')\n report['candidate_sha256']=digest")
        wrapper=wrapper.replace("'ENGINE'",repr(args.engine)).replace("'IMAGE'",repr(args.image))
        write_new(implementation_builder,wrapper.encode('utf-8'))
        if approved["goal"] != GOAL or approved.get("design_context") != DESIGN:
            raise SystemExit("approved spec goal/design differs from this experiment")
        if not os.environ.get("AUTOFORGE_API_BASE") or not os.environ.get("AUTOFORGE_MODEL"):
            raise SystemExit("--implement requires explicit AUTOFORGE_API_BASE and AUTOFORGE_MODEL")
        write_new(controller / "approved-spec.json", canonical(approved) + b"\n")
        implementation_goal = ("Implement exactly the approved spec below in contacts.py. Follow all user and design provenance obligations; preserve string contents exactly; use only Python dict records. Create only contacts.py. Run the trusted validator and repair failures using its public observations. The validator is the independent acceptance contract. Do not claim success without validator PASS. Approved specification:\n" + json.dumps(approved,ensure_ascii=False,separators=(",",":")))
        implementation_request = {"schema":1,"goal":implementation_goal,"references":[]}
        validation_command = [sys.executable,str(implementation_builder)]
        initial_candidate = {"schema":1,"goal":implementation_goal,"references":[],"files":[]}
        session_path=args.implementation_session.resolve()
        if session_path.exists(): raise SystemExit("implementation session directory must be absent")
        outcome=agent_runtime.run(implementation_request,[],validation_command,session_path,max_turns=32,
            limits=agent_budget.Limits(model_calls=32,tool_calls=96,tokens=1_000_000,seconds=600,output_tokens=4096),
            initial_candidate=initial_candidate,
            allowed_tools=["list_files","read_file","create_file","edit_file","search_files","validate"])
        write_new(run_dir/"implementation-outcome.json",canonical(outcome)+b"\n")
        result.pop('candidate_executed',None)
        result.update(status=outcome['status'],implementation=outcome,approved=True,model_contacted=True,
                      candidate_execution_scope='Trusted validator executes contacts.py only in isolated containers; runtime does not execute candidate code on the host.')
    if args.draft is not None:
        draft = json.loads(args.draft.read_text(encoding="utf-8"))
        errors = preliminary_screen(spec.validate_spec(draft))
        review = {"status": "PASS" if not errors else "REJECTED", "errors": errors,
                  "semantic_reviewed": False, "screen_kind": "preliminary-identity-provenance-completeness",
                  "operator_approval": "NOT_GRANTED",
                  "limitation": "A human must perform full semantic review; this screen does not establish meaning or completeness."}
        write_new(controller / "preliminary-screen.json", canonical(review) + b"\n")
        result.update(status="PRELIMINARY_SCREEN_REJECTED" if errors else "PRELIMINARY_SCREEN_PASS_WAITING_FOR_INDEPENDENT_REVIEW", review=review)
    if args.run:
        if args.candidate is None or args.approved_spec is None:
            raise SystemExit("--run requires --candidate and --approved-spec")
        approved = spec.validate_spec(json.loads(args.approved_spec.read_text(encoding="utf-8")), require_approved=True)
        if approved["goal"] != GOAL or approved.get("design_context") != DESIGN:
            raise SystemExit("approved spec goal/design differs from prepared experiment")
        run = run_builder(args.candidate.resolve(strict=True), run_dir / "staged-candidate", run_dir, args.image, args.engine)
        write_new(run_dir / "candidate-run.json", canonical(run) + b"\n")
        result.update(status="CONTAINER_PASS" if run["result"].get("test_pass") else "CONTAINER_FAIL", run=run)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
