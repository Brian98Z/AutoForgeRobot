"""Process-level crash/recovery checks for agent_runtime; no candidate code is executed.
The model endpoint is an in-process loopback HTTP/SSE simulator. The trusted builder
adapter is generated in a temporary directory and only parses candidate Python with
ast.parse. Test-only worker instrumentation injects process death/control timing without
editing production sources.
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import agent_session
ROOT = Path(__file__).resolve().parents[1]
WAIT_SECONDS = 12.0
WORKER_TIMEOUT = 18.0
GOAL = "durable process recovery protocol check"
SOURCE = "value = 1\n"
_CHILDREN: list[subprocess.Popen] = []
_ADAPTERS: list[tuple[Path, Path]] = []
def _check(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)
def _tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, separators=(",", ":"))}}
def _tool_response(*calls: dict) -> list[dict]:
    fragments = [{"index": index, "id": call["id"], "type": "function",
                  "function": call["function"]} for index, call in enumerate(calls)]
    return [{"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": fragments},
                          "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32}}]
def _stop_response(text: str = "awaiting next instruction") -> list[dict]:
    return [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                          "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}}]
class _Scenario:
    def __init__(self, responses: list[dict], gated_indices: tuple[int, ...] = ()):
        self.responses = responses
        self.gated_indices = set(gated_indices)
        self.release: dict[int, threading.Event] = {}
        self.arrived: dict[int, threading.Event] = {}
        self.requests: list[dict] = []
        self.errors: list[str] = []
        self.lock = threading.Lock()
    def response_gate(self, index: int) -> threading.Event:
        return self.release.setdefault(index, threading.Event())
    def request_event(self, index: int) -> threading.Event:
        return self.arrived.setdefault(index, threading.Event())
class _ProtocolServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address, handler, scenarios: dict[str, _Scenario]):
        self.scenarios = scenarios
        super().__init__(address, handler)
class _ProtocolHandler(BaseHTTPRequestHandler):
    server: _ProtocolServer
    def log_message(self, *_args):
        pass
    def do_POST(self):
        try:
            if not self.path.endswith("/chat/completions"):
                self.send_error(404)
                return
            key = self.path.strip("/").split("/")[0]
            scenario = self.server.scenarios.get(key)
            if scenario is None:
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw.decode("utf-8"))
            with scenario.lock:
                index = len(scenario.requests)
                scenario.requests.append(body)
                scenario.request_event(index).set()
            if body.get("stream") is not True or body.get("model") != "recovery-simulator":
                raise AssertionError("runtime did not use the expected real HTTP streaming protocol")
            if index >= len(scenario.responses):
                raise AssertionError(f"unexpected extra model request {index + 1}")
            if index in scenario.gated_indices and not scenario.response_gate(index).wait(WAIT_SECONDS):
                raise TimeoutError("test did not release the gated HTTP response")
            payload = b"".join(b"data: " + json.dumps(frame, ensure_ascii=False,
                                                     separators=(",", ":")).encode("utf-8") + b"\r\n\r\n"
                               for frame in scenario.responses[index])
            payload += b"data: [DONE]\r\n\r\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            if "scenario" in locals():
                with scenario.lock:
                    scenario.errors.append(f"{type(exc).__name__}: {exc}")
            try:
                self.send_error(500)
            except OSError:
                pass
def _write_adapter(path: Path) -> None:
    """Create a trusted test adapter whose only candidate operation is AST parsing."""
    code = '''from __future__ import annotations
import argparse
import ast
import json
import os
from pathlib import Path
import sys
import time
p = argparse.ArgumentParser()
a = p.parse_args()
log_path = Path(os.environ["RECOVERY_ADAPTER_LOG"])
gate_path = Path(os.environ["RECOVERY_ADAPTER_GATE"])
payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
manifest = payload["workspace_manifest"]
root = Path(manifest["workspace"])
# Parse syntax only; never import, compile, execute, or otherwise run candidate code.
for row in manifest["files"]:
    source = (root / row["path"]).read_text(encoding="utf-8")
    if row["path"].endswith(".py"):
        ast.parse(source, filename=row["path"])
record = {"candidate_sha256": payload["candidate_sha256"],
          "files": manifest["files"], "pid": os.getpid()}
with log_path.open("a", encoding="utf-8", newline="\\n") as stream:
    stream.write(json.dumps(record, sort_keys=True) + "\\n")
    stream.flush()
    os.fsync(stream.fileno())
if gate_path.exists():
    gate_path.with_suffix(gate_path.suffix + ".started").write_text("started\\n", encoding="utf-8")
    deadline = time.monotonic() + 15.0
    release = gate_path.with_suffix(gate_path.suffix + ".release")
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not release.exists():
        raise SystemExit("test adapter gate deadline exceeded")
result = {"schema": 1, "candidate_sha256": payload["candidate_sha256"],
          "build_pass": True, "test_pass": True,
          "observations": ["Trusted fixture performed AST parsing only; candidate was never executed."],
          "limitations": ["No runtime, dependency, or business-contract behavior was tested."]}
sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\\n")
with gate_path.with_suffix(gate_path.suffix + ".done").open("w", encoding="utf-8") as stream:
    stream.write("done\\n")
    stream.flush()
    os.fsync(stream.fileno())
'''
    path.write_text(code, encoding="utf-8", newline="\n")
    _ADAPTERS.append((path, Path(path.with_suffix(path.suffix + ".done"))))
def _worker(config: dict) -> int:
    """Run the real controller; optional monkeypatches are test-only crash gates."""
    import agent_runtime
    import agent_session as session_module
    crash_call = config.get("crash_after_tool_result")
    if crash_call:
        original_append = session_module.EventJournal.append
        def append_then_crash(journal, event):
            record = original_append(journal, event)
            if event.get("type") == "tool_result" and event.get("call_id") == crash_call:
                os._exit(74)  # EventJournal.append has fsynced the committed event.
            return record
        session_module.EventJournal.append = append_then_crash
    response_gate = config.get("after_http_gate")
    if response_gate:
        original_complete = agent_runtime.agent_transport.complete
        gate_path = Path(response_gate)
        def complete_then_gate(*args, **kwargs):
            # Emulate an already-arrived response racing with a steering command.
            # Actual simulator HTTP/SSE decoding still happens in original_complete.
            kwargs["should_stop"] = lambda: False
            answer = original_complete(*args, **kwargs)
            gate_path.write_text("returned\n", encoding="utf-8")
            release = gate_path.with_suffix(gate_path.suffix + ".release")
            deadline = time.monotonic() + 10.0
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not release.exists():
                raise TimeoutError("test did not release post-HTTP gate")
            return answer
        agent_runtime.agent_transport.complete = complete_then_gate
    limits = agent_runtime.agent_budget.Limits(model_calls=4, tool_calls=8, tokens=100_000,
                                                seconds=30.0, output_tokens=256)
    report = agent_runtime.run({"schema": 1, "goal": GOAL, "references": []}, [],
                               config["builder_argv"], Path(config["output"]), max_turns=4,
                               resume=config["resume"], limits=limits,
                               allowed_tools=["write_file", "read_file", "validate"])
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0
def _worker_entry(config_text: str) -> int:
    try:
        return _worker(json.loads(config_text))
    except BaseException as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2
def _environment(base: str, model: str = "recovery-simulator") -> dict[str, str]:
    env = os.environ.copy()
    env["AUTOFORGE_API_BASE"] = base
    env["AUTOFORGE_MODEL"] = model
    env["AUTOFORGE_API_KEY"] = ""
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env
def _command(output: Path, adapter: Path, log: Path, gate: Path, base: str, *,
             resume: bool, env: dict[str, str] | None = None, **instrumentation) -> tuple[list[str], dict[str, str]]:
    config = {"output": str(output), "builder_argv": [sys.executable, str(adapter)],
              "resume": resume, **instrumentation}
    worker_env = _environment(base)
    worker_env["RECOVERY_ADAPTER_LOG"] = str(log)
    worker_env["RECOVERY_ADAPTER_GATE"] = str(gate)
    if env:
        worker_env.update(env)
    return [sys.executable, str(Path(__file__).resolve()), "_worker", json.dumps(config, separators=(",", ":"))], worker_env
def _invoke(command: list[str], env: dict[str, str], *, expected: bool = True) -> dict | subprocess.CompletedProcess:
    proc = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=WORKER_TIMEOUT)
    if expected:
        _check(proc.returncode == 0, f"worker failed ({proc.returncode}): {proc.stderr}\n{proc.stdout}")
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise AssertionError(f"worker returned no JSON report: {proc.stdout!r}; {proc.stderr!r}") from exc
    return proc
def _start(command: list[str], env: dict[str, str]) -> subprocess.Popen:
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, close_fds=True)
    _CHILDREN.append(proc)
    return proc
def _wait_file(path: Path, proc: subprocess.Popen | None = None, timeout: float = WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc is not None and proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=2)
            raise AssertionError(f"worker exited before {path.name}: rc={proc.returncode}; {stderr}; {stdout}")
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")
def _kill_pid_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise AssertionError(f"adapter process {pid} did not terminate after SIGKILL")
def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
    else:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()
def _control(output: Path, kind: str, text: str | None, request_id: str) -> dict:
    record = agent_session._control_record(output)
    return agent_session.enqueue_control(output, record["token"], kind, text, request_id)
def _raw_events(output: Path) -> list[dict]:
    return [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
def _events(output: Path) -> list[dict]:
    rows = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    _check([row.get("seq") for row in rows] == list(range(1, len(rows) + 1)), "event sequence is not contiguous")
    # Exercise the public journal cursor API with bounded pages and the exact end cursor.
    cursor = 0
    paged = []
    while True:
        with agent_session.EventJournal(output, resume=True) as journal:
            page = journal.read_after(cursor, 3)
        if not page:
            break
        paged.extend(page)
        cursor = page[-1]["seq"]
    _check(paged == rows, "event cursor pagination differs from journal contents")
    with agent_session.EventJournal(output, resume=True) as journal:
        _check(not journal.read_after(cursor), "end cursor returned events")
    return rows
def _tool_results(events: list[dict], call_id: str) -> list[dict]:
    return [event for event in events if event.get("type") == "tool_result" and event.get("call_id") == call_id]
def _adapter_count(path: Path) -> int:
    if not path.exists():
        return 0
    return len(path.read_text(encoding="utf-8").splitlines())
def _await_request(scenario: _Scenario, index: int, timeout: float = WAIT_SECONDS) -> None:
    _check(scenario.request_event(index).wait(timeout), f"HTTP simulator did not receive request {index + 1}")
    if scenario.errors:
        raise AssertionError("HTTP simulator errors: " + "; ".join(scenario.errors))
def _test_crash_recovery(root: Path, server_base: str, scenarios: dict[str, _Scenario], adapter: Path) -> list[str]:
    checks = []
    output = root / "committed-write-crash"
    log = root / "crash-adapter.jsonl"
    gate = root / "external-validate.gate"
    gate.write_text("hold trusted adapter response\n", encoding="utf-8")
    scenario = scenarios["crash"]
    builder_call = _tool_call("write_file", {"path": "main.py", "content": SOURCE, "expected_sha256": None}, "write-once")
    read_call = _tool_call("read_file", {"path": "main.py"}, "read-after-crash")
    validate_call = _tool_call("validate", {}, "validate-ambiguous")
    scenario.responses[:] = [_tool_response(builder_call, read_call, validate_call), _stop_response()]
    base = server_base + "/crash"
    command, env = _command(output, adapter, log, gate, base, resume=False,
                            crash_after_tool_result="write-once")
    crashed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=WORKER_TIMEOUT)
    _check(crashed.returncode == 74,
           f"worker did not exit abruptly after committed write result: rc={crashed.returncode}; stderr={crashed.stderr!r}; stdout={crashed.stdout!r}; simulator={scenario.errors!r}; requests={len(scenario.requests)}")
    _check(len(scenario.requests) == 1, "crash turn was not exactly one actual model request")
    state_after_write = agent_session.load(output)
    _check(state_after_write["pending_index"] == 1, "write tool result was not checkpointed before crash")
    candidate_path = output / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    _check(candidate["files"] == [{"path": "main.py", "content": SOURCE}], "committed write source differs from exact requested bytes")
    checks.append("abrupt process exit after fsynced write tool_result; committed candidate exact source")
    command, env = _command(output, adapter, log, gate, base, resume=True)
    resuming = _start(command, env)
    started = gate.with_suffix(gate.suffix + ".started")
    _wait_file(started, resuming)
    _check(len(scenario.requests) == 1, "resume requested another model turn instead of continuing pending calls")
    _check(len(_tool_results(_raw_events(output), "write-once")) == 1, "committed write call ID was replayed")
    read_results = _tool_results(_raw_events(output), "read-after-crash")
    _check(len(read_results) == 1 and read_results[0]["result"].get("content") == SOURCE,
           "pending read did not continue with the exact committed source")
    # The child is blocked inside the trusted adapter's simulated external side effect.
    adapter_pid = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])["pid"]
    _kill(resuming)
    _kill_pid_tree(adapter_pid)
    checks.append("resume continued pending read once and entered validate without replaying the write or model request")
    base_session = (output / "session.json").read_bytes()
    base_candidate = candidate_path.read_bytes()
    adapter_bytes = adapter.read_bytes()
    baseline_model_count = len(scenario.requests)
    baseline_build_count = _adapter_count(log)
    envelope = json.loads(base_session.decode("utf-8"))
    envelope["sha256"] = "0" * 64
    (output / "session.json").write_text(json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    refused = _invoke(command, env, expected=False)
    _check(refused.returncode != 0, "tampered session digest was accepted")
    (output / "session.json").write_bytes(base_session)
    mirror = json.loads(base_candidate.decode("utf-8"))
    mirror["files"][0]["content"] = "value = 2\n"
    candidate_path.write_text(json.dumps(mirror, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    refused = _invoke(command, env, expected=False)
    _check(refused.returncode != 0, "tampered candidate mirror was accepted")
    candidate_path.write_bytes(base_candidate)
    adapter.write_bytes(adapter_bytes + b"\n# test-only tamper probe\n")
    refused = _invoke(command, env, expected=False)
    _check(refused.returncode != 0, "modified trusted adapter was accepted on resume")
    adapter.write_bytes(adapter_bytes)
    wrong_provider = _environment(base, model="different-model")
    refused = _invoke(command, wrong_provider, expected=False)
    _check(refused.returncode != 0, "changed provider configuration was accepted on resume")
    _check(len(scenario.requests) == baseline_model_count and _adapter_count(log) == baseline_build_count,
           "tamper probes made model or builder requests")
    checks.append("state digest, candidate mirror, trusted adapter identity, and provider configuration tampering refused")
    blocked = _invoke(command, env)
    _check(blocked["task_status"] == "BLOCKED" and blocked["reason_code"] == "unknown_tool_side_effect",
           f"ambiguous external validate was not blocked: {blocked}")
    durable_blocked = agent_session.load(output)
    _check(durable_blocked['task_status'] == 'BLOCKED' and durable_blocked['reason_code'] == 'unknown_tool_side_effect',
           'unknown validation refusal was returned but not persisted in checkpoint')
    _check(len(scenario.requests) == 1 and _adapter_count(log) == 1, "unknown validate side effect was replayed")
    _check(not list((output / "revisions").glob("*/validation.json")), "killed validate unexpectedly committed a validation result")
    final_events = _events(output)
    _check(len(_tool_results(final_events, "write-once")) == 1, "write call ID appeared more than once in committed journal")
    _check(len(_tool_results(final_events, "read-after-crash")) == 1, "pending read result is not exactly once")
    _check(not _tool_results(final_events, "validate-ambiguous"), "uncommitted validate result appeared in event journal")
    checks.append("unknown interrupted external validate is BLOCKED and never blindly replayed")
    return checks
def _test_terminal_success(root: Path, server_base: str, scenarios: dict[str, _Scenario], adapter: Path) -> list[str]:
    output = root / "terminal-success"
    log = root / "terminal-adapter.jsonl"
    gate = root / "terminal.gate"  # absent: trusted AST-only adapter returns immediately
    scenario = scenarios["terminal"]
    scenario.responses[:] = [_tool_response(
        _tool_call("write_file", {"path": "main.py", "content": SOURCE, "expected_sha256": None}, "terminal-write"),
        _tool_call("read_file", {"path": "main.py"}, "terminal-read"),
        _tool_call("validate", {}, "terminal-validate")), _stop_response()]
    command, env = _command(output, adapter, log, gate, server_base + "/terminal", resume=False)
    report = _invoke(command, env)
    _check(report["status"] == "PASS" and report["task_status"] == "SUCCEEDED", f"trusted adapter did not establish terminal success: {report}")
    _check(len(scenario.requests) == 1 and _adapter_count(log) == 1, "terminal run used unexpected request counts")
    resumed = _command(output, adapter, log, gate, server_base + "/terminal", resume=True)
    final = _invoke(*resumed)
    _check(final["status"] == "PASS" and final["task_status"] == "SUCCEEDED", "terminal resume changed success")
    _check(len(scenario.requests) == 1 and _adapter_count(log) == 1, "terminal resume contacted model or builder again")
    _events(output)
    return ["terminal SUCCEEDED state resumes without extra model or builder requests"]
def _test_idle_controls(root: Path, server_base: str, scenarios: dict[str, _Scenario], adapter: Path) -> list[str]:
    checks = []
    for kind, expected in (("stop", "CANCELLED"), ("pause", "PAUSED")):
        output = root / f"idle-{kind}"
        log = root / f"idle-{kind}-adapter.jsonl"
        gate = root / f"idle-{kind}.gate"
        scenario = scenarios[kind]
        scenario.responses[:] = [_tool_response(_tool_call("write_file", {"path": "main.py", "content": SOURCE,
                                                                           "expected_sha256": None}, f"stale-{kind}-write"))]
        scenario.gated_indices.add(0)
        command, env = _command(output, adapter, log, gate, server_base + f"/{kind}", resume=False)
        proc = _start(command, env)
        _wait_file(output / ".control.json", proc)
        _await_request(scenario, 0)
        first = _control(output, kind, None, f"request-{kind}")
        duplicate = _control(output, kind, None, f"request-{kind}")
        _check(first == duplicate, f"{kind} control request ID was not idempotent")
        scenario.response_gate(0).set()
        stdout, stderr = proc.communicate(timeout=WORKER_TIMEOUT)
        _check(proc.returncode == 0, f"idle {kind} worker failed: {stderr}; {stdout}")
        report = json.loads(stdout.strip().splitlines()[-1])
        _check(report["task_status"] == expected and report["reason_code"] == kind,
               f"idle {kind} control was not applied: {report}")
        events = _events(output)
        consumed = [e for e in events if e.get("type") == "control_consumed" and e.get("request_id") == f"request-{kind}"]
        _check(len(consumed) == 1, f"{kind} control was not consumed exactly once")
        _check(not _tool_results(events, f"stale-{kind}-write") and _adapter_count(log) == 0,
               f"stale model result executed after idle {kind}")
        _check(len(scenario.requests) == 1, f"idle {kind} generated another model request")
        checks.append(f"authenticated {kind} applied while model request was idle; stale action not executed")
    return checks
def _test_steering_stale_result(root: Path, server_base: str, scenarios: dict[str, _Scenario], adapter: Path) -> list[str]:
    output = root / "steering-stale"
    log = root / "steering-adapter.jsonl"
    gate = root / "steering.gate"
    response_gate = root / "after-http.returned"
    scenario = scenarios["steering"]
    scenario.responses[:] = [
        _tool_response(_tool_call("write_file", {"path": "main.py", "content": SOURCE,
                                                 "expected_sha256": None}, "stale-steering-write")),
        _stop_response("fresh response after steering")]
    command, env = _command(output, adapter, log, gate, server_base + "/steering", resume=False,
                            after_http_gate=str(response_gate))
    proc = _start(command, env)
    _wait_file(response_gate, proc)
    _check(len(scenario.requests) == 1, "steering test did not isolate the first idle result")
    first = _control(output, "steering", "replace the stale objective", "steer-once")
    duplicate = _control(output, "steering", "replace the stale objective", "steer-once")
    _check(first == duplicate, "steering deduplication failed")
    response_gate.with_suffix(response_gate.suffix + ".release").write_text("release\n", encoding="utf-8")
    stdout, stderr = proc.communicate(timeout=WORKER_TIMEOUT)
    _check(proc.returncode == 0, f"steering worker failed: {stderr}; {stdout}")
    report = json.loads(stdout.strip().splitlines()[-1])
    _check(report["task_status"] == "WAITING_INPUT", f"fresh post-steering model stop did not hold: {report}")
    events = _events(output)
    _check(any(event.get("type") == "stale_model_result_discarded" for event in events),
           "stale response was not explicitly discarded after steering")
    _check(not _tool_results(events, "stale-steering-write") and _adapter_count(log) == 0,
           "stale response tool call was executed")
    consumed = [e for e in events if e.get("type") == "control_consumed" and e.get("request_id") == "steer-once"]
    _check(len(consumed) == 1 and len(scenario.requests) == 2, "steering was not consumed once before a fresh model request")
    return ["steering during post-HTTP idle gate discards stale model result and never executes its tool call"]
def _test_follow_up(root: Path, server_base: str, scenarios: dict[str, _Scenario], adapter: Path) -> list[str]:
    output = root / "follow-up-once"
    log = root / "follow-up-adapter.jsonl"
    gate = root / "follow-up.gate"
    scenario = scenarios["follow"]
    scenario.responses[:] = [
        _tool_response(_tool_call("write_file", {"path": "main.py", "content": SOURCE,
                                                 "expected_sha256": None}, "follow-write"),
                       _tool_call("validate", {}, "follow-validate-first")),
        _tool_response(_tool_call("validate", {}, "follow-validate-second")),
        _stop_response("unexpected extra request")]
    scenario.gated_indices.add(0)
    command, env = _command(output, adapter, log, gate, server_base + "/follow", resume=False)
    proc = _start(command, env)
    _wait_file(output / ".control.json", proc)
    _await_request(scenario, 0)
    follow_text = "Follow-up: preserve exact source and report its AST-only scope."
    first = _control(output, "follow_up", follow_text, "follow-once")
    duplicate = _control(output, "follow_up", follow_text, "follow-once")
    _check(first == duplicate, "follow-up deduplication failed")
    scenario.response_gate(0).set()
    stdout, stderr = proc.communicate(timeout=WORKER_TIMEOUT)
    _check(proc.returncode == 0, f"follow-up worker failed: {stderr}; {stdout}")
    report = json.loads(stdout.strip().splitlines()[-1])
    _check(report["task_status"] == "SUCCEEDED" and report["status"] == "PASS", f"follow-up cycle did not complete: {report}")
    _check(len(scenario.requests) == 2 and _adapter_count(log) == 2, "follow-up should require exactly one subsequent model/build cycle")
    messages = agent_session.load(output)["messages"]
    _check(sum(message.get("content") == follow_text for message in messages) == 1,
           "follow-up was not consumed exactly once in the resumed model context")
    _check(agent_session.load(output)["follow_ups"] == [], "follow-up remained queued after successful continuation")
    second_user = [message.get("content", "") for message in scenario.requests[1]["messages"]
                   if message.get("role") == "user"]
    _check(sum(text == follow_text for text in second_user) == 1, "follow-up was missing/duplicated in the next actual model request")
    _events(output)
    resumed = _command(output, adapter, log, gate, server_base + "/follow", resume=True)
    _invoke(*resumed)
    _check(len(scenario.requests) == 2 and _adapter_count(log) == 2, "terminal follow-up resume repeated model or builder")
    return ["follow-up request deduplicated, consumed exactly once, and terminal resume made no extra requests"]
def _main() -> int:
    with tempfile.TemporaryDirectory(prefix="agent-recovery-check-") as temporary:
        root = Path(temporary)
        adapter = root / "trusted_ast_adapter.py"
        _write_adapter(adapter)
        scenarios = {
            "crash": _Scenario([]), "terminal": _Scenario([]), "stop": _Scenario([]),
            "pause": _Scenario([]), "steering": _Scenario([]), "follow": _Scenario([]),
        }
        server = _ProtocolServer(("127.0.0.1", 0), _ProtocolHandler, scenarios)
        thread = threading.Thread(target=server.serve_forever, name="agent-recovery-http", daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        checks: list[str] = []
        try:
            checks.extend(_test_crash_recovery(root, base, scenarios, adapter))
            checks.extend(_test_terminal_success(root, base, scenarios, adapter))
            checks.extend(_test_idle_controls(root, base, scenarios, adapter))
            checks.extend(_test_steering_stale_result(root, base, scenarios, adapter))
            checks.extend(_test_follow_up(root, base, scenarios, adapter))
            for key, scenario in scenarios.items():
                _check(not scenario.errors, f"{key} simulator errors: {scenario.errors}")
        finally:
            for scenario in scenarios.values():
                for event in scenario.release.values():
                    event.set()
            for proc in _CHILDREN:
                if proc.poll() is None:
                    _kill(proc)
                else:
                    if proc.stdout and not proc.stdout.closed:
                        proc.stdout.close()
                    if proc.stderr and not proc.stderr.closed:
                        proc.stderr.close()
            for adapter_path, _done_path in _ADAPTERS:
                if adapter_path.exists():
                    adapter_path.unlink()
                for suffix in (".started", ".release", ".done"):
                    marker = adapter_path.with_suffix(adapter_path.suffix + suffix)
                    if marker.exists():
                        marker.unlink()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        print(json.dumps({"status": "PASS", "scope": {
            "model": "actual agent_transport HTTP/SSE requests to local deterministic simulator; no real model",
            "builder": "trusted temporary adapter performs ast.parse only; generated candidate code is never executed",
            "durability": "real subprocess exits/kills and runtime/session files; test-only instrumentation lives only in this file",
            "excluded": ["no production container/build execution", "no business-contract behavior validation", "no external network"]},
            "checks": checks}, ensure_ascii=False, indent=2))
    return 0
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "_worker":
        if len(args) != 2:
            print("worker requires one JSON configuration argument", file=sys.stderr)
            return 2
        return _worker_entry(args[1])
    if args:
        print("agent_recovery_check.py takes no arguments", file=sys.stderr)
        return 2
    return _main()
if __name__ == "__main__":
    raise SystemExit(main())
