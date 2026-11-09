#!/usr/bin/env python3
"""Exercise a freshly built AutoForge executable's Windows spawn/SSE delivery path."""
from __future__ import annotations

import argparse
import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time


class _SSEHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        if body.get("model") != "frozen-delivery-check" or body.get("stream") is not True:
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        first = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}],
                 "service_tier": "default", "usage": None}
        self.wfile.write(b"data: " + json.dumps(first).encode() + b"\n\n")
        self.wfile.flush()
        if self.path == "/cancel/chat/completions":
            time.sleep(8)
            return
        last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "service_tier": "default"}
        usage = {"choices": [], "service_tier": "default", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\ndata: " + json.dumps(usage).encode() + b"\n\ndata: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *_args) -> None:
        pass


_SCRIPT = r'''import json, multiprocessing, os, sys, time
from application_proposer import interruptible_stream_completion, TransportError
base = os.environ["AUTOFORGE_API_BASE"]
body = {"model": "frozen-delivery-check", "messages": []}
if base.endswith("/cancel"):
    started = time.monotonic()
    try:
        list(interruptible_stream_completion(body, lambda: time.monotonic() - started > 0.35))
        raise AssertionError("cancellation did not interrupt stream")
    except TransportError as exc:
        if exc.reason_code != "cancellation": raise
    result = {"cancelled": True, "elapsed": time.monotonic() - started}
else:
    chunks = list(interruptible_stream_completion(body))
    assert len(chunks) == 3, chunks
    assert all(chunk.get("service_tier") == "default" for chunk in chunks), chunks
    import agent_transport
    events = []
    answer = agent_transport.complete([{"role": "user", "content": "smoke"}], [], on_event=events.append)
    assert answer == {"role": "assistant", "content": "ok"}, answer
    assert any(e.get("type") == "model_usage" and e["usage"]["total_tokens"] == 2 for e in events), events
    result = {"chunks": len(chunks), "service_tier": True, "parsed_turn": answer}
children = multiprocessing.active_children()
if children:
    raise AssertionError("spawn worker remains after completion/cancellation")
print(json.dumps({**result, "active_children": len(children)}))
'''


def _invoke(exe: Path, script: Path, endpoint: str, timeout: float = 20) -> dict:
    env = os.environ.copy()
    env["AUTOFORGE_API_BASE"] = endpoint
    env["AUTOFORGE_MODEL"] = "frozen-delivery-check"
    env.pop("AUTOFORGE_API_KEY", None)
    completed = subprocess.run([str(exe), str(script)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, env=env)
    if completed.returncode:
        raise AssertionError(f"{endpoint}: exit={completed.returncode}; stdout={completed.stdout}; stderr={completed.stderr}")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"{endpoint}: no result; stderr={completed.stderr}")
    return json.loads(lines[-1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path, help="path to the freshly built autoforge.exe")
    args = parser.parse_args()
    exe = args.exe.resolve()
    if not exe.is_file():
        raise FileNotFoundError(exe)
    with tempfile.TemporaryDirectory(prefix="autoforge-frozen-check-") as temp:
        script = Path(temp) / "stream_worker_check.py"
        script.write_text(_SCRIPT, encoding="utf-8")
        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SSEHandler) as server:
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                success = _invoke(exe, script, base + "/complete")
                cancelled = _invoke(exe, script, base + "/cancel")
            finally:
                server.shutdown()
                thread.join(timeout=2)
    if success != {"chunks": 3, "service_tier": True, "parsed_turn": {"role": "assistant", "content": "ok"}, "active_children": 0}:
        raise AssertionError(f"unexpected completion result: {success}")
    if not cancelled.get("cancelled") or cancelled.get("active_children") != 0 or cancelled.get("elapsed", 99) > 4:
        raise AssertionError(f"unexpected cancellation result: {cancelled}")
    help_result = subprocess.run([str(exe), "agent_mcp", "--help"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15)
    if help_result.returncode != 0 or "usage:" not in help_result.stdout.lower() or "unknown module" in help_result.stderr.lower():
        raise AssertionError(f"agent_mcp --help failed: exit={help_result.returncode}; stdout={help_result.stdout}; stderr={help_result.stderr}")
    print(json.dumps({"status": "PASS", "checks": {
        "spawned_http_sse": success, "cancel_cleanup": cancelled,
        "agent_mcp_help": "PASS"}, "scope": "the supplied frozen executable; loopback deterministic HTTP/SSE only"}, sort_keys=True))


if __name__ == "__main__":
    main()
