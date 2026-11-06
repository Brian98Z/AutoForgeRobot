"""Opt-in live local-model and Docker acceptance for the public inventory task."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import uuid

import agent_runtime
import agent_tools

ROOT = Path(__file__).resolve().parents[1]
MODEL_PROFILES = {
    "quality": ("http://127.0.0.1:8081/v1", r"H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\LLM\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_NL.gguf"),
    "speed": ("http://127.0.0.1:8084/v1", r"H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\text_encoders\Qwen\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf"),
}
FIXTURE = ROOT / "fixtures" / "business"
MAX_SECONDS = 480
MAX_CALLS = 16
OUTPUT_TOKENS = 4096


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()




def _events(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def _public_event(value):
    """Copy events while excluding capability material if future event schemas add it."""
    if isinstance(value, dict):
        return {key: _public_event(item) for key, item in value.items()
                if key.casefold() not in {"token", "control_token", "hmac", "hmac_sha256", "secret"}}
    if isinstance(value, list):
        return [_public_event(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="exact locally present sha256 image ID")
    parser.add_argument("--engine", choices=("docker", "podman"), default="docker")
    parser.add_argument("--model-profile", choices=sorted(MODEL_PROFILES), default=os.environ.get("AUTOFORGE_MODEL_PROFILE", "quality"),
                        help="quality (Qwen3.6) or speed (Qwen3.5); never falls back")
    parser.add_argument("--base-url", help="explicit OpenAI-compatible endpoint override")
    parser.add_argument("--model", help="explicit model ID override")
    parser.add_argument('--tool-choice', choices=('auto', 'required'), default='auto', help='explicit model tool-selection policy, bound to session resume')
    parser.add_argument('--request', type=Path, default=FIXTURE / 'request-v2.json', help='original public request-v2; never independent oracle')
    parser.add_argument('--token-budget', type=int, default=1_000_000, help='same conservative default as unified controller')
    parser.add_argument('--max-turns', type=int, choices=range(1, 1001), default=MAX_CALLS)
    args = parser.parse_args()
    profile_base, profile_model = MODEL_PROFILES[args.model_profile]
    base_url = args.base_url or os.environ.get("AUTOFORGE_API_BASE") or profile_base
    model = args.model or os.environ.get("AUTOFORGE_MODEL") or profile_model
    os.environ["AUTOFORGE_API_BASE"] = base_url
    os.environ["AUTOFORGE_MODEL"] = model
    os.environ["AUTOFORGE_MODEL_PROFILE"] = args.model_profile
    os.environ["AUTOFORGE_API_KEY"] = ""
    os.environ["AUTOFORGE_TOOL_CHOICE"] = args.tool_choice
    run_root = ROOT / "bootstrap-runs" / ("business-live-" + uuid.uuid4().hex)
    run_root.mkdir(parents=True, exist_ok=False)
    output = run_root / "session"
    request_path = args.request
    contract_path = FIXTURE / "contract.json"
    report = {"schema": 1, "status": "HOLD", "scope": "live local model; unified application_build agent; local Docker image",
              "provider": {"profile": args.model_profile, "base_url": base_url, "requested_model": model, "api_key": "not configured", "tool_choice": args.tool_choice},
              "image": {"engine": args.engine, "requested_image": args.image},
              "budgets": {"seconds": MAX_SECONDS, "model_calls": args.max_turns, "output_tokens": OUTPUT_TOKENS, "reserved_tokens": args.token_budget},
              "human_intervention": False,
              "limitations": ["Acceptance is limited to the supplied business contract, model, and exact local image.",
                              "A terminal resume proves only that no new model call was made; it does not prove runtime recovery.",
                              "Model-reported usage is not billing evidence."]}
    try:
        request_bytes = request_path.read_bytes()
        # Persist the public request byte-for-byte; the independent contract is only hashed here.
        request_copy = run_root / "request.json"
        request_copy.write_bytes(request_bytes)
        contract_digest = _digest(contract_path)
        request = urllib.request.Request(base_url + "/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            model_inventory = json.loads(response.read(1_048_577))
        if not isinstance(model_inventory, dict) or not isinstance(model_inventory.get("data"), list):
            raise ValueError("local /v1/models response is malformed")
        models = [item for item in model_inventory["data"] if isinstance(item, dict) and isinstance(item.get("id"), str)]
        selected = next((item for item in models if item["id"] == model), None)
        if selected is None:
            raise ValueError("requested model is not listed by local /v1/models")
        provider_path = run_root / "provider.json"
        provider_path.write_text(json.dumps(model_inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["provider"].update({"model_inventory": selected, "available_model_ids": [item["id"] for item in models],
                                   "inventory_sha256": _digest(provider_path)})
        initial_candidate = {"schema": 1, "goal": json.loads(request_bytes)["goal"], "files": [], "references": []}
        initial_path = run_root / "initial_candidate.json"
        initial_path.write_text(json.dumps(initial_candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["initial_candidate_sha256"] = hashlib.sha256(json.dumps(
            initial_candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        report["digests"] = {
            "request_sha256": hashlib.sha256(request_bytes).hexdigest(), "contract_sha256": contract_digest,
            "image_argument_sha256": hashlib.sha256(args.image.encode("utf-8")).hexdigest(),
            "sources": {name: _digest(ROOT / name) for name in
                        ("agent_business_check.py", "application_build.py", "application_builder.py", "application_proposer.py", "agent_runtime.py",
                         "agent_transport.py", "agent_tools.py", "agent_session.py", "agent_budget.py", "applications.py", "application_checkpoint.py")},
            "tool_schema_sha256": hashlib.sha256(json.dumps(agent_runtime.TOOLS, ensure_ascii=False, sort_keys=True,
                                                               separators=(",", ":")).encode("utf-8")).hexdigest(),
            "executor_tool_schema_sha256": hashlib.sha256(json.dumps(agent_tools.TOOLS, ensure_ascii=False, sort_keys=True,
                                                                        separators=(",", ":")).encode("utf-8")).hexdigest()}
        references = run_root / "empty-reference-store"
        references.mkdir()
        command = [sys.executable, str(ROOT / "autoforge" / "application_build.py"), "run", "--request", str(request_copy),
                   "--contract", str(contract_path), "--reference-store", str(references), "--engine", args.engine,
                   "--image", args.image, "--output", str(output), "--mode", "agent", "--max-turns", str(args.max_turns),
                   "--token-budget", str(args.token_budget), "--time-budget", str(MAX_SECONDS), "--tool-budget", "64",
                   "--allow-model", "--trusted-adapter"]
        report["initial_invocation"] = {"request_copy_sha256": _digest(request_copy), "file_count": 0,
                                        "project_import": False, "resume": False,
                                        "contract_context_to_model": False}
        if _digest(request_copy) != report["digests"]["request_sha256"]:
            raise ValueError("public request copy differs from fixture bytes")
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace")
        try:
            stdout, stderr = process.communicate(timeout=MAX_SECONDS + 20)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            report["subprocess"] = {"argv": command, "returncode": None, "timed_out": True,
                                    "timeout_seconds": MAX_SECONDS + 20,
                                    "termination": "terminate then kill after five seconds if still running"}
            raise TimeoutError("unified application_build subprocess exceeded bounded deadline and was terminated")
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        report["subprocess"] = {"argv": command, "returncode": completed.returncode,
                                "stdout": completed.stdout, "stderr": completed.stderr}
        try:
            cli_result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            cli_result = None
        report["cli_result"] = cli_result
        events = _events(output / "events.jsonl")
        report["events"] = [_public_event(event) for event in events]
        report["revision_history"] = _revision_manifest(output, events)
        intents = sum(event.get("type") == "model_call_intent" for event in events)
        report["model_call_intents"] = intents
        terminal = bool(cli_result and cli_result.get("status") == "PASS" and cli_result.get("task_status") == "SUCCEEDED")
        if terminal:
            from agent_session import load
            before_state = load(output)
            before_turn = before_state.get("turn")
            resumed = subprocess.run(command + ["--resume"], cwd=ROOT, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=30, check=False)
            resume_events = _events(output / "events.jsonl")
            try:
                resumed_result = json.loads(resumed.stdout)
            except (json.JSONDecodeError, TypeError):
                resumed_result = None
            after_state = load(output)
            after_intents = sum(event.get("type") == "model_call_intent" for event in resume_events)
            report["terminal_resume"] = {"returncode": resumed.returncode, "result": resumed_result,
                                          "model_calls_before": before_turn, "model_calls_after": after_state.get("turn"),
                                          "event_call_intents_before": intents, "event_call_intents_after": after_intents,
                                          "no_additional_model_calls": before_turn == after_state.get("turn") and intents == after_intents,
                                          "runtime_recovery_proven": False}
            terminal = bool(resumed.returncode == 0 and resumed_result and resumed_result.get("status") == "PASS"
                            and resumed_result.get("task_status") == "SUCCEEDED"
                            and report["terminal_resume"]["no_additional_model_calls"])
        else:
            report["terminal_resume"] = {"state": "NOT_EVALUATED", "reason": "initial run did not finish SUCCEEDED; no resume attempted"}
        session = None
        if output.exists() and (output / "session.json").exists():
            from agent_session import load
            session = load(output)
        report["initial_files_empty"] = initial_candidate["files"] == [] and json.loads(initial_path.read_text(encoding="utf-8"))["files"] == []
        report["initial_candidate_matches_request"] = initial_candidate["goal"] == json.loads(request_bytes)["goal"] and initial_candidate["references"] == []
        report["status"] = "PASS" if completed.returncode == 0 and terminal and report["initial_files_empty"] and report["initial_candidate_matches_request"] and intents <= args.max_turns else "HOLD"
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        report["status"] = "HOLD"
        try:
            events = _events(output / "events.jsonl")
            report["events"] = [_public_event(event) for event in events]
            report["revision_history"] = _revision_manifest(output, events)
        except Exception as evidence_exc:
            report["evidence_error"] = f"{type(evidence_exc).__name__}: {str(evidence_exc)[:1000]}"
    report_path = run_root / "business_live_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_root), "session_dir": str(output), "status": report["status"],
                      "report": str(report_path)}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


def _revision_manifest(output: Path, events: list[dict]) -> dict:
    """Link immutable validation revisions to candidate digests and their journal positions."""
    from agent_session import load
    state = load(output)
    revisions = []
    tools = {event.get("call_id"): (index, event.get("name")) for index, event in enumerate(events)
             if event.get("type") == "tool_started"}
    tool_events = [{"event_index": index, "event_seq": event.get("seq"), "call_id": event.get("call_id"),
                    "name": event.get("name"), "result_event_index": next((j for j in range(index + 1, len(events))
                    if events[j].get("type") == "tool_result" and events[j].get("call_id") == event.get("call_id")), None)}
                   for index, event in enumerate(events) if event.get("type") == "tool_started"]
    for index, event in enumerate(events):
        tool = tools.get(event.get("call_id"))
        if event.get("type") != "tool_result" or tool is None or tool[1] != "validate":
            continue
        result = event.get("result")
        if not isinstance(result, dict) or result.get("status") not in ("PASS", "FAIL"):
            continue
        digest = result.get("candidate_sha256")
        matches = sorted(path for path in (output / "revisions").glob("*")
                         if path.is_dir() and isinstance(digest, str) and path.name.endswith("-" + digest))
        revision = matches[0] if len(matches) == 1 else None
        if result.get('revision_path'):
            relative = Path(result['revision_path'])
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'revisions':
                raise ValueError('validation revision path escapes session')
            revision = output / relative
            if revision not in matches:
                raise ValueError('exact validation revision path/digest mismatch')
        validation_path = revision / "validation.json" if revision else None
        files = []
        if revision:
            files = [{"path": str(path.relative_to(output)), "sha256": _digest(path), "size": path.stat().st_size}
                     for path in sorted(p for p in revision.rglob("*") if p.is_file())]
        validation = json.loads(validation_path.read_text(encoding="utf-8")) if validation_path and validation_path.is_file() else None
        revisions.append({"event_index": index, "event_seq": event.get("seq"), "tool_started_event_index": tool[0],
                          "call_id": event.get("call_id"), "tool": tool[1], "status": result["status"],
                          "candidate_sha256": digest, "matching_revision_count": len(matches),
                          "matching_revision_directories": [str(path.relative_to(output)) for path in matches],
                          "revision_directory": str(revision.relative_to(output)) if revision else None,
                          "validation_sha256": _digest(validation_path) if validation_path and validation_path.is_file() else None,
                          "validation": validation, "files": files})
    import applications
    return {"schema": 1, "journal_sha256": _digest(output / "events.jsonl") if (output / "events.jsonl").is_file() else None,
            "revisions": revisions, "tool_events": tool_events, "event_count": len(events),
            "final_candidate_sha256": hashlib.sha256(applications._json_bytes(state["candidate"])).hexdigest()}


if __name__ == "__main__":
    raise SystemExit(main())
