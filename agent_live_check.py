"""Live localhost-model syntax-repair acceptance; candidate code is AST-parsed, never run."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request
import uuid

import agent_budget
import agent_runtime
import agent_session

ROOT = Path(__file__).resolve().parent
MODEL_PROFILES = {
    "quality": ("http://127.0.0.1:8081/v1", r"H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\LLM\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_NL.gguf"),
    "speed": ("http://127.0.0.1:8084/v1", r"H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\text_encoders\Qwen\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf"),
}
SENTINEL = "leave this unrelated file unchanged\n"
DEFAULT_GOAL = ("Inspect the project, call validate before writing anything, then repair both Python syntax errors "
                "using the validator observations. Preserve sentinel.txt exactly. Revalidate and stop only after "
                "trusted validation passes.")
CASES = {
    "composite": {
        "scope": "two independent Python syntax defects including a malformed f-string",
        "files": [
            {"path": "module_alpha.py", "content": "def add(a, b)\n    return a + b\n"},
            {"path": "module_beta.py", "content": "def describe(value):\n    return f\"value={value\"\n"},
        ],
    },
    "basic": {
        "scope": "two independent function-definition missing-colon syntax defects",
        "files": [
            {"path": "module_alpha.py", "content": "def add(a, b)\n    return a + b\n"},
            {"path": "module_beta.py", "content": "def multiply(a, b)\n    return a * b\n"},
        ],
    },
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _events(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=sorted(CASES), default="composite", help="independent fresh syntax case; composite remains the default")
    parser.add_argument("--goal", default=DEFAULT_GOAL, help="goal for a fresh independent live session")
    parser.add_argument("--model-profile", choices=sorted(MODEL_PROFILES), default=os.environ.get("AUTOFORGE_MODEL_PROFILE", "quality"),
                        help="quality (Qwen3.6) or speed (Qwen3.5); never falls back")
    parser.add_argument("--base-url", help="explicit OpenAI-compatible endpoint override")
    parser.add_argument("--model", help="explicit model ID override")
    args = parser.parse_args()
    profile_base, profile_model = MODEL_PROFILES[args.model_profile]
    base_url = args.base_url or os.environ.get("AUTOFORGE_API_BASE") or profile_base
    model = args.model or os.environ.get("AUTOFORGE_MODEL") or profile_model
    os.environ["AUTOFORGE_API_BASE"] = base_url
    os.environ["AUTOFORGE_MODEL"] = model
    os.environ["AUTOFORGE_MODEL_PROFILE"] = args.model_profile
    os.environ["AUTOFORGE_API_KEY"] = ""
    run_root = ROOT / "bootstrap-runs" / ("live-session-" + uuid.uuid4().hex)
    run_root.mkdir(parents=True, exist_ok=False)
    output = run_root / "session"
    case = CASES[args.case]
    initial = {"schema": 1, "goal": args.goal, "references": [],
               "files": [*case["files"], {"path": "sentinel.txt", "content": SENTINEL}]}
    (run_root / "initial_candidate.json").write_text(json.dumps(initial, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    model_inventory = None
    report: dict = {"schema": 1, "status": "HOLD", "case": args.case, "case_scope": case["scope"],
                    "scope": "real local model; trusted AST syntax-only adapter",
                    "provider": {"profile": args.model_profile, "base_url": base_url, "requested_model": model, "api_key": "not configured"},
                    "limitations": ["No candidate code execution.", "AST syntax only; no business-correctness, dependency, security, or container claim.",
                                    "Server-reported usage is not billing evidence."]}
    try:
        request = urllib.request.Request(base_url + "/models", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            model_inventory = json.loads(response.read(1_048_577).decode("utf-8"))
        if not isinstance(model_inventory, dict) or not isinstance(model_inventory.get("data"), list):
            raise ValueError("/v1/models returned an unsupported response")
        models = [m for m in model_inventory["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)]
        matching = [m for m in models if m["id"] == model]
        if not matching:
            raise ValueError("requested model is not listed by local /v1/models")
        report["provider"]["model_inventory"] = matching[0]
        report["provider"]["available_model_ids"] = [m["id"] for m in models]
        (run_root / "provider.json").write_text(json.dumps(model_inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        candidate_digest = hashlib.sha256(json.dumps(initial, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        report["initial_candidate_sha256"] = candidate_digest
        report["initial_file_sha256"] = {f["path"]: hashlib.sha256(f["content"].encode()).hexdigest() for f in initial["files"]}
        report["source_sha256"] = {name: _digest(ROOT / name) for name in
                                    ("agent_live_check.py", "agent_runtime.py", "agent_transport.py", "agent_tools.py",
                                     "agent_session.py", "agent_budget.py", "application_builder.py",
                                     "application_checkpoint.py", "applications.py", "fixtures/agent_syntax_builder.py")}
        report["tool_schema_sha256"] = hashlib.sha256(json.dumps(
            agent_runtime.TOOLS, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        result = agent_runtime.run({"schema": 1, "goal": args.goal, "references": []}, [],
                                  [sys.executable, str(ROOT / "fixtures" / "agent_syntax_builder.py")], output,
                                  max_turns=16, limits=agent_budget.Limits(model_calls=16, tool_calls=64, tokens=250_000,
                                                                         seconds=600, output_tokens=4096),
                                  initial_candidate=initial,
                                  allowed_tools=["list_files", "read_file", "edit_file", "validate"])
        report.update(result)
        # Final artifact details, deltas and server usage are derived only from durable model events.
        events = _events(output / "events.jsonl")
        calls = [event for event in events if event.get("type") == "model_call_intent"]
        tool_names = {event.get("call_id"): event.get("name") for event in events if event.get("type") == "tool_started"}
        validations = [event.get("result") for event in events
                       if event.get("type") == "tool_result" and tool_names.get(event.get("call_id")) == "validate"]
        report["event_summary"] = {"model_call_intents": len(calls),
                                   "streamed_text_deltas": [e.get("text", "") for e in events if e.get("type") == "model_text_delta"],
                                   "streamed_tool_deltas": [e for e in events if e.get("type") == "model_tool_delta"],
                                   "usage_records": [e.get("usage") for e in events if e.get("type") == "model_usage"],
                                   "validate_results": validations}
        final = result.get("status") == "PASS" and result.get("task_status") == "SUCCEEDED"
        session_state = agent_session.load(output)
        before_calls = session_state["turn"]
        if result.get("task_status") in ("SUCCEEDED", "CANCELLED", "FAILED"):
            resumed = agent_runtime.run({"schema": 1, "goal": args.goal, "references": []}, [],
                                        [sys.executable, str(ROOT / "fixtures" / "agent_syntax_builder.py")], output,
                                        max_turns=16, resume=True,
                                        limits=agent_budget.Limits(model_calls=16, tool_calls=64, tokens=250_000,
                                                                   seconds=600, output_tokens=4096),
                                        allowed_tools=["list_files", "read_file", "edit_file", "validate"])
            after_events = _events(output / "events.jsonl")
            after_intents = sum(e.get("type") == "model_call_intent" for e in after_events)
            report["terminal_resume"] = {"state": "CHECKED", "status": resumed.get("status"), "task_status": resumed.get("task_status"),
                                         "model_calls_before": before_calls, "model_calls_after": resumed.get("turns"),
                                         "event_call_intents_after": after_intents,
                                         "no_additional_model_calls": resumed.get("turns") == before_calls == after_intents}
            final = final and report["terminal_resume"]["no_additional_model_calls"] and resumed.get("status") == "PASS" and resumed.get("task_status") == "SUCCEEDED"
        else:
            report["terminal_resume"] = {"state": "NOT_EVALUATED", "reason": "original run did not reach a terminal task status; no resume attempted",
                                         "model_calls_before": before_calls}
            final = False
        candidate = json.loads((output / "candidate.json").read_text(encoding="utf-8"))
        final_files = {f["path"]: f["content"] for f in candidate["files"]}
        report["final_file_sha256"] = {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sorted(final_files.items())}
        report["sentinel_preserved"] = final_files.get("sentinel.txt") == SENTINEL
        validation_events = [(i, event.get("result")) for i, event in enumerate(events)
                             if event.get("type") == "tool_result" and tool_names.get(event.get("call_id")) == "validate"]
        first_write = next((i for i, event in enumerate(events)
                            if event.get("type") == "tool_started" and event.get("name") in ("write_file", "write_files", "edit_file")), None)
        first_failure = next((i for i, value in validation_events if isinstance(value, dict) and value.get("status") == "FAIL"), None)
        failures = [value for _, value in validation_events if isinstance(value, dict) and value.get("status") == "FAIL"]
        passes = [value for _, value in validation_events if isinstance(value, dict) and value.get("status") == "PASS"]
        failure_preceded_write = first_failure is not None and (first_write is None or first_failure < first_write)
        report["syntax_failure_observed_before_repair"] = bool(failures)
        report["syntax_pass_observed_after_repair"] = bool(passes)
        report["initial_failure_preceded_any_write"] = failure_preceded_write
        final = final and report["sentinel_preserved"] and bool(failures) and bool(passes) and failure_preceded_write
        report["status"] = "PASS" if final else "HOLD"
        report["acceptance"] = {"model_inventory_confirmed": bool(model_inventory), "real_model_required": True,
                                "observed_validate_fail_then_pass": bool(failures) and bool(passes),
                                "initial_failure_before_writes": failure_preceded_write,
                                "sentinel_unchanged": report["sentinel_preserved"]}
    except Exception as exc:
        report["status"] = "HOLD"
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
    report_path = run_root / "live_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(output), "status": report["status"], "report": str(report_path)}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
