"""Run opt-in real-provider boundary probes through the existing agent transport."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

import agent_budget
import agent_runtime
import agent_session
import agent_transport
import application_proposer

ROOT = Path(__file__).resolve().parent
MODEL_PROFILES = {
    "quality": ("http://127.0.0.1:8081/v1", r"H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\LLM\Qwen3.6-35B-A3B\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ4_NL.gguf"),
    "speed": ("http://127.0.0.1:8084/v1", r"H:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\text_encoders\Qwen\Qwen3.5-9B-Uncensored-HauhauCS-Aggressive-Q6_K.gguf"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-profile", choices=sorted(MODEL_PROFILES), default=os.environ.get("AUTOFORGE_MODEL_PROFILE", "quality"))
    parser.add_argument("--base-url", help="explicit OpenAI-compatible endpoint override")
    parser.add_argument("--model", help="explicit model ID override")
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--probe-group", choices=("all", "remaining"), default="all",
                        help="run all probes, or only newly added boundary probes")
    args = parser.parse_args()
    profile_base, profile_model = MODEL_PROFILES[args.model_profile]
    base_url, model = args.base_url or profile_base, args.model or profile_model
    os.environ.update(AUTOFORGE_API_BASE=base_url, AUTOFORGE_MODEL=model, AUTOFORGE_API_KEY="")
    result = {"schema": 1, "status": "HOLD", "profile": args.model_profile, "base_url": base_url, "model": model,
              "probe_group": args.probe_group, "probes": [], "limitations": ["Provider observations are not independent acceptance.",
                                              "Wire-level refusal is only claimed when refusal metadata or content_filter is observed."]}
    if args.probe_group == "all":
        _probe_long_source(result)
        _probe_truncation(result)
        _probe_cancellation(result, args.timeout)
    _probe_unparseable_candidate(result)
    _probe_benign_decline(result)
    _probe_http_deadline(result, model)
    result["wire_refusal_observed"] = any(p.get("reason_code") == "refusal" for p in result["probes"])
    if not result["wire_refusal_observed"]:
        result["limitations"].append("No provider wire-level refusal was observed; benign natural-language decline is not wire-refusal evidence.")
    states = [item["status"] for item in result["probes"]]
    result["status"] = "PASS" if states and all(state == "PASS" for state in states) else "HOLD"
    out = ROOT / "bootstrap-runs" / ("model-boundary-live-" + uuid.uuid4().hex)
    out.mkdir(parents=True, exist_ok=False)
    (out / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(out / "verification.json")}, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 2


def _probe_long_source(result: dict) -> None:
    marker = "OMP_BOUNDARY_" + uuid.uuid4().hex[:12]
    long_source = ("# Public boundary probe; no candidate is executed.\n" +
                   "def calculate(value):\n    return value + 1\n" +
                   ("# ordinary public source line\n" * 1700) + "# " + marker + " 中文标记\n")
    start = time.monotonic()
    try:
        response = agent_transport.complete([{"role": "user", "content": "Read the source below and return only the unique marker appearing in its final line. Do not copy a marker from the instructions; none is present there.\n\n" + long_source}], [], max_tokens=512, on_event=lambda _event: None)
        content = response.get("content", "") if isinstance(response, dict) else ""
        status = "PASS" if marker in content else "FAIL"
        probe = {"name": "long-source-chinese-marker", "status": status, "source_characters": len(long_source),
                 "response_characters": len(content), "marker_sha256": hashlib.sha256(marker.encode()).hexdigest()}
    except Exception as exc:
        probe = {"name": "long-source-chinese-marker", "status": "HOLD", "error_type": type(exc).__name__, "reason_code": getattr(exc, "reason_code", None), "reason": str(exc)[:300]}
    result["probes"].append({**probe, "elapsed_seconds": time.monotonic() - start})


def _probe_truncation(result: dict) -> None:
    start = time.monotonic()
    try:
        agent_transport.complete([{"role": "user", "content": "In one short sentence, state that you received this truncation-boundary request."}], [], max_tokens=1)
        status, reason = "FAIL", "response completed within one token"
    except Exception as exc:
        code = getattr(exc, "reason_code", None)
        status, reason = ("PASS", code) if code == "truncation" else ("HOLD", code or type(exc).__name__)
    result["probes"].append({"name": "real-provider-one-token-truncation", "status": status, "reason_code": reason, "elapsed_seconds": time.monotonic() - start})


def _probe_cancellation(result: dict, timeout: float) -> None:
    stop, started, outcome = threading.Event(), threading.Event(), {}
    def observe_event(_event):
        started.set()
        stop.set()
    worker = threading.Thread(target=lambda: _call_with_capture(outcome, observe_event, stop.is_set), daemon=True)
    begun = time.monotonic()
    worker.start()
    if not started.wait(min(30.0, timeout)):
        status = "HOLD"
    else:
        worker.join(timeout=min(timeout, 90.0))
        status = "PASS" if not worker.is_alive() and outcome.get("reason_code") == "cancellation" else "HOLD"
    result["probes"].append({"name": "real-provider-interruptible-cancellation", "status": status,
                             "elapsed_seconds": time.monotonic() - begun, "reason_code": outcome.get("reason_code")})


def _probe_unparseable_candidate(result: dict) -> None:
    start = time.monotonic()
    try:
        response = application_proposer.completion({
            "model": os.environ["AUTOFORGE_MODEL"],
            "messages": [{"role": "user", "content": "Reply in one plain-language sentence only. Do not output JSON, code, a tool call, or a candidate."}],
            "max_tokens": 64,
            "chat_template_kwargs": {"enable_thinking": False},
        })
        choices = response.get("choices") if isinstance(response, dict) else None
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        message = choice.get("message") if isinstance(choice, dict) else None
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            probe = {"status": "HOLD", "outcome": "no-text-content", "finish_reason": finish_reason}
        elif finish_reason != "stop":
            probe = {"status": "HOLD", "outcome": "nonterminal-response", "finish_reason": finish_reason,
                     "content_characters": len(content)}
        elif not content.strip():
            probe = {"status": "HOLD", "outcome": "empty-completed-content", "finish_reason": finish_reason,
                     "content_characters": len(content)}
        else:
            try:
                application_proposer._json_loads(content, "candidate")
                probe = {"status": "HOLD", "outcome": "content-was-valid-json", "finish_reason": finish_reason}
            except application_proposer.Refused as exc:
                invalid_json = "invalid candidate JSON" in str(exc)
                probe = {"status": "PASS" if invalid_json else "HOLD",
                         "outcome": "actual-content-parser-refused" if invalid_json else "other-parser-refusal",
                         "reason_code": getattr(exc, "reason_code", "protocol"), "finish_reason": finish_reason,
                         "content_characters": len(content)}
    except Exception as exc:
        probe = {"status": "HOLD", "outcome": "request-error", "error_type": type(exc).__name__,
                 "reason_code": getattr(exc, "reason_code", None)}
    result["probes"].append({"name": "real-provider-content-candidate-parser", **probe,
                             "elapsed_seconds": time.monotonic() - start})
def _probe_benign_decline(result: dict) -> None:
    start = time.monotonic()
    runtime_dir = ROOT / "bootstrap-runs" / ("model-boundary-runtime-" + uuid.uuid4().hex)
    prior_choice = os.environ.get("AUTOFORGE_TOOL_CHOICE")
    os.environ["AUTOFORGE_TOOL_CHOICE"] = "auto"
    try:
        report = agent_runtime.run({"schema": 1, "goal": "This is a benign request. Do not call any tool, do not create or propose anything. Decline in one brief plain-language sentence because approval is absent.", "references": []}, [], [os.environ.get("COMSPEC", "cmd.exe"), "/c", "exit", "0"], runtime_dir, max_turns=1,
                                   limits=agent_budget.Limits(model_calls=1), allowed_tools=["query_validation"])
        state = agent_session.load(runtime_dir)
        refusal_text = " ".join(str(message.get("content", "")) for message in state.get("messages", []) if message.get("role") == "assistant")
        tool_calls = state.get("tool_calls", 0)
        status = "PASS" if report.get("status") == "HOLD" and report.get("task_status") == "WAITING_INPUT" and tool_calls == 0 and refusal_text else "HOLD"
        probe = {"name": "real-runtime-benign-decline-hold", "status": status, "runtime_status": report.get("status"),
                 "task_status": report.get("task_status"), "reason_code": report.get("reason_code"),
                 "tool_calls": tool_calls, "assistant_response_characters": len(refusal_text)}
    except Exception as exc:
        probe = {"name": "real-runtime-benign-decline-hold", "status": "HOLD", "error_type": type(exc).__name__,
                 "reason_code": getattr(exc, "reason_code", None), "error": str(exc)[:300]}
    finally:
        if prior_choice is None:
            os.environ.pop("AUTOFORGE_TOOL_CHOICE", None)
        else:
            os.environ["AUTOFORGE_TOOL_CHOICE"] = prior_choice
    result["probes"].append({**probe, "elapsed_seconds": time.monotonic() - start})




def _probe_http_deadline(result: dict, model: str) -> None:
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with one short word."}], "max_tokens": 8, "stream": True}
    prior_timeout, attempted = application_proposer.HTTP_TIMEOUT, False
    start = time.monotonic()
    try:
        application_proposer.HTTP_TIMEOUT = 0.000001
        stream = application_proposer.stream_completion(body)
        attempted = True
        next(stream)
        status, reason = "HOLD", "provider returned before timeout"
    except Exception as exc:
        code = getattr(exc, "reason_code", None)
        status, reason = ("PASS", "timeout") if attempted and code == "timeout" else ("HOLD", code or type(exc).__name__)
    finally:
        application_proposer.HTTP_TIMEOUT = prior_timeout
    result["probes"].append({"name": "real-provider-http-deadline", "status": status, "request_attempted": attempted,
                             "reason_code": reason, "timeout_seconds": 0.000001, "elapsed_seconds": time.monotonic() - start})


def _call_with_capture(outcome: dict, on_event, should_stop) -> None:
    try:
        agent_transport.complete([{"role": "user", "content": "Count from 1 to 1000, writing each integer on a separate line."}], [], on_event=on_event,
                                 should_stop=should_stop, max_tokens=256)
    except Exception as exc:
        outcome["reason_code"] = getattr(exc, "reason_code", type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())

