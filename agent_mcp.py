#!/usr/bin/env python3
"""Bounded MCP (Model Context Protocol) stdio client for agent tool access.

One server process per configured name, newline-delimited JSON-RPC 2.0, no shell,
allowlisted environment, bounded responses and a hard request timeout. Tool names are
namespaced as ``mcp__<server>__<tool>`` so a server cannot shadow a built-in agent tool.

This module is the controller-side integration point. It does not inject tools into the
model loop (see docs/MCP.md); it exposes `list` and `call` for adapters and operators.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
MAX_RESPONSE_BYTES = 1_048_576
MAX_TOOLS = 200
MAX_SERVERS = 32
MAX_TIMEOUT_SECONDS = 600
NAME_PATTERN = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

class McpRefused(ValueError):
    """Configuration or protocol input refused before or during a bounded call."""

def _check_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64 or any(ch not in NAME_PATTERN for ch in value):
        raise McpRefused(f"{where} must be 1-64 characters of [A-Za-z0-9_-]")
    return value

def _check_path(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise McpRefused(f"{where} must be a nonempty string")
    return value

def load_config(path: str | Path) -> dict[str, Any]:
    """Validate an MCP server configuration; never accepts a shell string."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"servers"} or not isinstance(raw["servers"], dict):
        raise McpRefused('configuration must be {"servers": {...}}')
    if not raw["servers"] or len(raw["servers"]) > MAX_SERVERS:
        raise McpRefused(f"1-{MAX_SERVERS} servers required")
    servers: dict[str, dict[str, Any]] = {}
    for name, spec in raw["servers"].items():
        _check_name(name, "server name")
        if not isinstance(spec, dict) or not set(spec) <= {"command", "env", "cwd", "timeout_seconds"}:
            raise McpRefused(f"server {name}: only command/env/cwd/timeout_seconds are allowed")
        command = spec.get("command")
        if not isinstance(command, list) or not 1 <= len(command) <= 32 or any(not isinstance(x, str) or not x or "\x00" in x for x in command):
            raise McpRefused(f"server {name}: command must be a nonempty argv list")
        cwd = spec.get("cwd")
        if cwd is not None:
            _check_path(cwd, f"server {name}.cwd")
            if not Path(cwd).is_dir():
                raise McpRefused(f"server {name}.cwd is not a directory")
        env = spec.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise McpRefused(f"server {name}.env must map strings to strings")
        timeout = spec.get("timeout_seconds", 30)
        if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise McpRefused(f"server {name}.timeout_seconds must be 1-{MAX_TIMEOUT_SECONDS}")
        servers[name] = {"command": command, "env": env, "cwd": cwd, "timeout_seconds": timeout}
    return {"servers": servers}

def _child_env(extra: dict[str, str]) -> dict[str, str]:
    allowed = {"PATH", "PATHEXT", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "COMSPEC"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update(extra)
    return env

class Server:
    """One stdio MCP server process with a bounded request/response loop."""

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.spec = spec
        self.proc: subprocess.Popen[bytes] | None = None
        self.lines: queue.Queue[bytes | None] = queue.Queue()
        self.next_id = 0
        self.tools: list[dict[str, Any]] = []

    def __enter__(self) -> "Server":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def start(self) -> "Server":
        argv = self.spec["command"]
        if shutil.which(argv[0]) is None and not Path(argv[0]).is_file():
            raise McpRefused(f"server {self.name}: executable not found: {argv[0]}")
        self.proc = subprocess.Popen(
            argv, cwd=self.spec.get("cwd"), env=_child_env(self.spec.get("env", {})),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            shell=False, bufsize=0,
        )
        threading.Thread(target=self._reader, name=f"mcp-{self.name}", daemon=True).start()
        self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                     "clientInfo": {"name": "autoforge-mcp", "version": "1"}})
        self._notify("notifications/initialized", {})
        return self

    def _reader(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            if len(raw) > MAX_RESPONSE_BYTES:
                self.lines.put(b"\x00oversize")
                break
            self.lines.put(raw)
        self.lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise McpRefused(f"server {self.name}: not running")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            raise McpRefused(f"server {self.name}: request exceeds byte limit")
        try:
            self.proc.stdin.write(payload + b"\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpRefused(f"server {self.name}: write failed: {exc}") from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        request_id = self.next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.spec["timeout_seconds"]
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpRefused(f"server {self.name}: {method} timed out")
            try:
                raw = self.lines.get(timeout=remaining)
            except queue.Empty:
                raise McpRefused(f"server {self.name}: {method} timed out") from None
            if raw is None:
                raise McpRefused(f"server {self.name}: process exited")
            if raw == b"\x00oversize":
                raise McpRefused(f"server {self.name}: response exceeds byte limit")
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise McpRefused(f"server {self.name}: invalid JSON: {exc}") from exc
            if not isinstance(message, dict):
                raise McpRefused(f"server {self.name}: message is not an object")
            if message.get("id") != request_id:
                continue  # notifications and stale responses are ignored
            if "error" in message:
                raise McpRefused(f"server {self.name}: {message['error']}")
            return message.get("result")

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpRefused(f"server {self.name}: tools/list returned no tools array")
        out = []
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                raise McpRefused(f"server {self.name}: invalid tool entry")
            schema = tool.get("inputSchema", {"type": "object", "properties": {}})
            out.append({"name": tool["name"], "description": tool.get("description", ""), "inputSchema": schema})
        self.tools = out
        return out

    def call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool not in {t["name"] for t in self.tools}:
            raise McpRefused(f"server {self.name}: unknown tool {tool}; call list first")
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        if not isinstance(result, dict):
            raise McpRefused(f"server {self.name}: tools/call returned no object")
        return result

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        finally:
            self.proc = None

def qualified(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"

def split(name: str) -> tuple[str, str]:
    if not isinstance(name, str) or not name.startswith("mcp__") or name.count("__") != 2:
        raise McpRefused("tool name must be mcp__<server>__<tool>")
    _, server, tool = name.split("__")
    return _check_name(server, "server"), tool

def list_tools(config: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name, spec in config["servers"].items():
        with Server(name, spec) as server:
            for tool in server.list_tools():
                rows.append({"name": qualified(name, tool["name"]), "server": name, "tool": tool["name"],
                             "description": tool["description"], "inputSchema": tool["inputSchema"]})
                if len(rows) > MAX_TOOLS:
                    raise McpRefused(f"more than {MAX_TOOLS} tools exposed")
    return rows

def call_tool(config: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    server_name, tool_name = split(name)
    if server_name not in config["servers"]:
        raise McpRefused(f"unknown server: {server_name}")
    if not isinstance(arguments, dict):
        raise McpRefused("arguments must be an object")
    with Server(server_name, config["servers"][server_name]) as server:
        server.list_tools()
        return server.call_tool(tool_name, arguments)

def text_of(result: dict[str, Any]) -> str:
    parts = []
    for item in result.get("content", []) if isinstance(result.get("content"), list) else []:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list", help="discover tools from every configured server")
    p.add_argument("--config", required=True, type=Path)
    p = sub.add_parser("call", help="call one namespaced MCP tool")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--tool", required=True)
    p.add_argument("--arguments", default="{}", help="JSON object")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "list":
            print(json.dumps({"tools": list_tools(config)}, ensure_ascii=False, indent=1))
        else:
            arguments = json.loads(args.arguments)
            result = call_tool(config, args.tool, arguments)
            print(json.dumps({"result": result, "text": text_of(result)}, ensure_ascii=False, indent=1))
        return 0
    except (McpRefused, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
