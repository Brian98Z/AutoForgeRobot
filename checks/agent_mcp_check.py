#!/usr/bin/env python3
"""Checks the bounded MCP stdio client against a loopback fake server."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "autoforge"))
import agent_mcp as mcp
SERVER = r'''
import json, sys, time
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    m = json.loads(line)
    method = m.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2024-11-05", "capabilities": {},
              "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [
            {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object",
             "properties": {"text": {"type": "string"}}, "required": ["text"]}},
            {"name": "slow", "description": "Sleep", "inputSchema": {"type": "object",
             "properties": {"seconds": {"type": "number"}}}}]}})
    elif method == "tools/call":
        params = m.get("params") or {}
        if params.get("name") == "echo":
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [
                {"type": "text", "text": str((params.get("arguments") or {}).get("text"))}]}})
        elif params.get("name") == "slow":
            time.sleep(float((params.get("arguments") or {}).get("seconds", 5)))
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "late"}]}})
        else:
            send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "unknown tool"}})
'''
def reject(fn):
    try:
        fn()
    except mcp.McpRefused:
        return
    raise AssertionError("expected McpRefused")
def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mcp-check-") as tmp:
        server = Path(tmp) / "fake_server.py"
        server.write_text(SERVER, encoding="utf-8")
        config_path = Path(tmp) / "mcp.json"
        config_path.write_text(json.dumps({"servers": {"fake": {
            "command": [sys.executable, str(server)], "timeout_seconds": 2}}}), encoding="utf-8")
        config = mcp.load_config(config_path)
        tools = mcp.list_tools(config)
        names = [t["name"] for t in tools]
        assert names == ["mcp__fake__echo", "mcp__fake__slow"], names
        result = mcp.call_tool(config, "mcp__fake__echo", {"text": "hello"})
        assert mcp.text_of(result) == "hello"
        reject(lambda: mcp.call_tool(config, "mcp__fake__missing", {}))
        reject(lambda: mcp.call_tool(config, "mcp__other__echo", {}))
        reject(lambda: mcp.call_tool(config, "echo", {}))
        bad = Path(tmp) / "bad.json"
        bad.write_text(json.dumps({"servers": {"x": {"command": "python -c pass"}}}), encoding="utf-8")
        reject(lambda: mcp.load_config(bad))
        bad.write_text(json.dumps({"servers": {"x": {"command": [sys.executable], "shell": True}}}), encoding="utf-8")
        reject(lambda: mcp.load_config(bad))
        started = time.monotonic()
        reject(lambda: mcp.call_tool(config, "mcp__fake__slow", {"seconds": 9}))
        elapsed = time.monotonic() - started
        assert elapsed < 8, f"timeout did not bound the call: {elapsed:.1f}s"
        with mcp.Server("fake", config["servers"]["fake"]) as s:
            s.list_tools()
        assert s.proc is None
        print(f"agent_mcp_check: PASS (tools/list, namespaced call, notification skip, bounds, "
              f"timeout {elapsed:.2f}s, shell/env refusal, process cleanup)")
if __name__ == "__main__":
    main()
