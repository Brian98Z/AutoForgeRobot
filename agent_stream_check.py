"""Exercise actual HTTP streaming and runtime dispatch; no real model or candidate execution."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

import agent_runtime
import agent_transport
import application_proposer


class Simulator(BaseHTTPRequestHandler):
    mode = "valid"
    gate = None
    requests = 0

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests += 1
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert self.headers["Accept"] == "text/event-stream"
        if self.mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:1/forbidden")
            self.end_headers()
            return
        if self.mode == "header_wait":
            time.sleep(5)
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.mode == "wrong_type" else "text/event-stream")
        self.end_headers()

        def send(value):
            raw = b"data: " + (value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")) + b"\r\n\r\n"
            # Split inside UTF-8 and SSE delimiters as well as JSON tokens.
            for start in range(0, len(raw), 7):
                self.wfile.write(raw[start:start + 7])
                self.wfile.flush()

        def chunk(delta, finish=None):
            return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        try:
            if self.mode == "idle_stream":
                time.sleep(5)
                return
            if any(isinstance(message, dict) and message.get("role") == "tool" for message in body.get("messages", [])) and self.mode in ("valid", "schema"):
                send(chunk({"role": "assistant", "content": "结束"}, "stop"))
                send(b"[DONE]")
                return
            send(chunk({"role": "assistant", "content": "修复库存"}))
            arguments = json.dumps({"path": "main.py", "content": "# 中文\nvalue = 1\n", "expected_sha256": None}, ensure_ascii=False)
            if self.mode == "schema":
                arguments = '{"path":"main.py","content":true,"expected_sha256":null}'
            if self.mode == "invalid_json":
                arguments = '{"path":'
            calls = [{"index": 0, "id": "write-1", "type": "function", "function": {"name": "write_", "arguments": arguments[:12]}},
                     {"index": 1, "id": "write-1" if self.mode == "duplicate_id" else "read-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":'}}]
            send(chunk({"tool_calls": calls}))
            if self.gate is not None and not self.gate.wait(5):
                return
            if self.mode == "disconnect":
                return
            calls = [{"index": 1, "function": {"arguments": '"main.py"}'}},
                     {"index": 0, "function": {"name": "file", "arguments": arguments[12:]}}]
            send(chunk({"tool_calls": calls}))
            if self.mode == "refusal":
                send(chunk({"refusal": "not allowed"}))
            if self.mode == "oversized":
                send(chunk({"content": "x" * 3000}))
            send(chunk({}, "length" if self.mode == "truncated" else "tool_calls"))
            if self.mode == "after_finish":
                send(chunk({"content": "late"}))
            if self.mode != "no_usage":
                usage_total = 99 if self.mode == "malformed_usage" else 13
                send({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 10,
                                                 "total_tokens": usage_total}})
            if self.mode != "missing_done":
                send(b"[DONE]")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Simulator)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    env = {"AUTOFORGE_API_BASE": f"http://127.0.0.1:{server.server_port}/v1", "AUTOFORGE_MODEL": "stream-simulator", "AUTOFORGE_API_KEY": ""}
    request = {"schema": 1, "goal": "stream protocol check", "references": []}
    outcomes = {}
    try:
        with patch.dict(os.environ, env), tempfile.TemporaryDirectory(prefix="agent-stream-") as tmp:
            root = Path(tmp)
            Simulator.mode = "valid"
            Simulator.requests = 0
            Simulator.gate = threading.Event()
            output = root / "valid"
            observed = []

            def observe():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    path = output / "events.jsonl"
                    if path.exists() and b'"model_tool_delta"' in path.read_bytes():
                        observed.append(not (output / "candidate.json").exists())
                        Simulator.gate.set()
                        return
                    time.sleep(0.01)
                Simulator.gate.set()

            watcher = threading.Thread(target=observe)
            watcher.start()
            report = agent_runtime.run(request, [], ["never-run-builder"], output, 2)
            watcher.join()
            assert observed == [True], "tool dispatched before the fragmented argument completed"
            assert report["status"] == "HOLD", report
            candidate = json.loads((output / "candidate.json").read_text(encoding="utf-8"))
            assert candidate["files"] == [{"path": "main.py", "content": "# 中文\nvalue = 1\n"}]
            events = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            results = [e for e in events if e["type"] == "tool_result"]
            assert [e["call_id"] for e in results] == ["write-1", "read-1"]
            assert results[1]["result"]["content"] == candidate["files"][0]["content"]
            assert "修复库存" == "".join(e["text"] for e in events if e["type"] == "model_text_delta" and e["turn"] == 1)
            assert not (output / "revisions").exists(), "model final text must not establish success"
            outcomes["fragmented_unicode_interleaved_tools"] = "PASS; deltas visible before dispatch, exact read-after-write"
            Simulator.gate = None
            for mode in ("schema", "invalid_json", "duplicate_id", "disconnect", "missing_done", "refusal", "truncated", "after_finish", "oversized", "wrong_type", "redirect", "malformed_usage"): 
                Simulator.mode = mode
                Simulator.requests = 0
                output = root / mode
                with patch.object(application_proposer, "MAX_RESPONSE_BYTES", 2048 if mode == "oversized" else 4_500_000):
                    report = agent_runtime.run(request, [], ["never-run-builder"], output, 2)
                assert report["status"] == "HOLD", (mode, report)
                assert not (output / "candidate.json").exists(), mode
                assert not (output / "revisions").exists(), mode
                if mode not in ('schema', 'disconnect', 'missing_done'):
                    assert Simulator.requests == 1, (mode, Simulator.requests)
                outcomes[mode] = "HOLD; no candidate write or builder dispatch"
            try:
                agent_transport.complete([{"role": "user", "content": "usage"}], agent_runtime.TOOLS,
                                         on_event=lambda _event: None)
            except agent_transport.Refused as exc:
                assert exc.reason_code == "protocol"
            else:
                raise AssertionError("malformed server usage was accepted")
            Simulator.mode = "valid"
            Simulator.requests = 0
            deltas = []
            def cancel(event):
                deltas.append(event)
                raise application_proposer.Refused("cancelled on delta")
            try:
                agent_transport.complete([{"role": "user", "content": "cancel"}], agent_runtime.TOOLS, on_event=cancel)
            except application_proposer.Refused:
                pass
            else:
                raise AssertionError("cancelled stream returned an assistant turn")
            assert deltas[0]["type"] == "model_text_delta"
            outcomes["callback_cancellation"] = "aborted without returning a dispatchable turn"
            Simulator.mode = "no_usage"
            events_without_usage = []
            agent_transport.complete([{"role": "user", "content": "optional usage"}],
                                     agent_runtime.TOOLS, on_event=events_without_usage.append)
            assert any(event.get("type") == "model_usage_unavailable" and
                       event.get("source") == "unavailable" for event in events_without_usage)
            outcomes["optional_usage"] = "turn accepted; no counter fabricated"
            for mode in ("header_wait", "idle_stream"):
                Simulator.mode = mode
                Simulator.requests = 0
                started = time.monotonic()
                try:
                    agent_transport.complete([{"role": "user", "content": "cancel idle"}], agent_runtime.TOOLS,
                                             should_stop=lambda: time.monotonic() - started > 0.5)
                except agent_transport.Refused as exc:
                    assert exc.reason_code == "cancellation", (mode, exc.reason_code)
                else:
                    raise AssertionError(f"{mode} cancellation returned an assistant turn")
                assert time.monotonic() - started < 3, mode
                outcomes[mode + "_cancel"] = "HTTP worker terminated during blocked wait"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    print(json.dumps({"status": "PASS", "scope": "real loopback HTTP/SSE transport and candidate-file tool execution; simulator is not a model; no generated code executed", "checks": outcomes}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
