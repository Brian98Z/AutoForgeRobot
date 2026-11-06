#!/usr/bin/env python3
"""Contract regression using a protocol simulator, not proof of real model behavior."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import agent_transport

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "autoforge" / "application_proposer.py"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Simulator(BaseHTTPRequestHandler):
    mode = "valid"
    observed: dict = {}

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        Simulator.observed = {"path": self.path, "authorization": self.headers.get("Authorization"),
                              "model": body.get("model"), "messages": body.get("messages"),
                              "tools": body.get("tools"), "tool_choice": body.get("tool_choice"),
                              "max_tokens": body.get("max_tokens")}
        if Simulator.mode == "redirect":
            self.send_response(307); self.send_header("Location", "http://127.0.0.1:1/steal"); self.end_headers(); return
        if Simulator.mode == "oversized":
            raw = b"{" + b" " * (4_500_000) + b"}"
        elif Simulator.mode == "native_tool":
            calls = [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": '{"q":"x"}'}}]
            raw = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": calls}}]}).encode()
        elif Simulator.mode == "duplicate_tool_id":
            calls = [{"id": "same", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
                     {"id": "same", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
            raw = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": calls}}]}).encode()
        elif Simulator.mode == "unknown_tool":
            calls = [{"id": "call-1", "type": "function", "function": {"name": "missing", "arguments": "{}"}}]
            raw = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": calls}}]}).encode()
        elif Simulator.mode == "invalid_tool_args":
            calls = [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "[]"}}]
            raw = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": calls}}]}).encode()
        elif Simulator.mode == "tool_truncated":
            calls = [{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]
            raw = json.dumps({"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": None, "tool_calls": calls}}]}).encode()
        elif Simulator.mode == "invalid_finish":
            raw = json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}).encode()
        elif Simulator.mode == "refusal":
            raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"refusal": "not allowed", "content": "{}"}}]}).encode()
        elif Simulator.mode == "missing_choices":
            raw = b'{"choices": []}'
        elif Simulator.mode == "duplicate_candidate":
            content = '{"schema":1,"schema":1,"goal":"Build a small app","files":[{"path":"app.py","content":""}],"references":[]}'
            raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()
        elif Simulator.mode == "goal_mismatch":
            cand = {"schema": 1, "goal": "different", "files": [{"path": "app.py", "content": ""}], "references": []}
            raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(cand)}}]}).encode()
        elif Simulator.mode == "valid":
            cand = {"schema": 1, "goal": "Build a small app", "files": [{"path": "app.py", "content": "# 库存处理\\ndef main():\\n    return 1\\n"}], "references": []}
            raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(cand)}}]}).encode()
        else:
            raw = b'{"choices":[{"finish_reason":"stop","message":{"content":"{"}}]}'
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Simulator)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    env = os.environ.copy()
    env.update({"AUTOFORGE_API_BASE": f"http://127.0.0.1:{server.server_port}/v1",
                "AUTOFORGE_MODEL": "simulated-model", "AUTOFORGE_API_KEY": "simulated-secret"})
    base_request = {"schema": 1, "requirement": {"schema": 1, "goal": "Build a small app", "references": []},
                    "references": [], "previous_validation": {"status": "none"}}

    def invoke(payload: dict = base_request) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ADAPTER)], input=json.dumps(payload), text=True,
                              cwd=ROOT, env=env, capture_output=True, encoding='utf-8', timeout=35, check=False)

    try:
        Simulator.mode = "valid"
        ok = invoke()
        require(ok.returncode == 0, f"valid simulated response rejected: {ok.stderr}")
        require('库存处理' in json.loads(ok.stdout)['files'][0]['content'],'model Unicode source corrupted')

        outcomes = {}
        for mode in ("invalid_finish", "refusal", "missing_choices", "duplicate_candidate", "goal_mismatch", "redirect", "oversized", "truncated"):
            Simulator.mode = mode
            result = invoke()
            require(result.returncode == 2 and not result.stdout.strip(), f"{mode} did not fail closed")
            outcomes[mode] = "rejected"
        Simulator.mode = "valid"
        bad_input = invoke({**base_request, "extra": "forbidden"})
        require(bad_input.returncode == 2, "unexpected proposer input field accepted")
        dup_input = subprocess.run([sys.executable, str(ADAPTER)], input='{"schema":1,"schema":1}', text=True,
                                   cwd=ROOT, env=env, capture_output=True, timeout=35, check=False)
        require(dup_input.returncode == 2, "duplicate input JSON key accepted")
        tool_spec = [{"type": "function", "function": {"name": "lookup", "description": "lookup", "parameters": {"type": "object"}}}]
        original_env={key:os.environ.get(key) for key in ('AUTOFORGE_API_BASE','AUTOFORGE_MODEL','AUTOFORGE_API_KEY')}
        os.environ.update({key:env[key] for key in original_env})
        Simulator.mode = "native_tool"
        assistant = agent_transport.complete([{"role": "user", "content": "find"},
                                              {"role": "user", "content": "find", "unexpected": "dropped"}], tool_spec)
        require(Simulator.observed["max_tokens"] == agent_transport.MAX_TOKENS, "default token budget changed")
        try:
            agent_transport.complete([{"role": "user", "content": "find"}], tool_spec, max_tokens=0)
        except agent_transport.Refused as exc:
            require(exc.reason_code == "protocol", "invalid token budget missing protocol reason code")
        else:
            raise AssertionError("out-of-range max_tokens accepted")
        tool_outcomes = {}
        for mode in ("duplicate_tool_id", "unknown_tool", "invalid_tool_args", "tool_truncated", "refusal", "missing_choices"):
            Simulator.mode = mode
            try:
                agent_transport.complete([{"role": "user", "content": "find"}], tool_spec)
            except agent_transport.Refused:
                tool_outcomes[mode] = "rejected"
            else:
                raise AssertionError(f"{mode} tool response did not fail closed")
        outcomes["native_tool_calls"] = "accepted"
        outcomes.update(tool_outcomes)
        check_env = env.copy(); check_env["AUTOFORGE_API_KEY"] = "never-print-this-secret"
        status = subprocess.run([sys.executable, str(ADAPTER), "--check-config"], cwd=ROOT, env=check_env,
                                capture_output=True, text=True, timeout=10, check=False)
        require(status.returncode == 0 and "never-print-this-secret" not in status.stdout + status.stderr, "config check leaked secret")
        for unsafe in ('http://127.attacker.example/v1','http://127.0.0.1.attacker.example/v1','http://remote.example/v1'):
            bad_env=env.copy(); bad_env['AUTOFORGE_API_BASE']=unsafe
            status=subprocess.run([sys.executable,str(ADAPTER),'--check-config'],cwd=ROOT,env=bad_env,capture_output=True,text=True,timeout=10)
            require(not json.loads(status.stdout)['endpoint_valid'],'non-loopback HTTP allowed')
        print(json.dumps({"status": "PASS", "scope": "local protocol simulator exercises the actual HTTP client; not proof of real model quality/behavior",
                          "valid_http_request": True, "negative_cases": outcomes}, indent=2))
    finally:
        if 'original_env' in locals():
            for key,value in original_env.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value
        server.shutdown(); server.server_close(); worker.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
