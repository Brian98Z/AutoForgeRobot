# MCP Integration Guide

AutoForge connects to [Model Context Protocol](https://modelcontextprotocol.io) servers through
a bounded stdio client (`agent_mcp.py`). The client speaks newline-delimited JSON-RPC 2.0,
performs the `initialize` / `notifications/initialized` handshake, discovers tools with
`tools/list`, and calls them with `tools/call`.

## Scope

- **Controller side only.** MCP tools are discovered and called by trusted controller code,
  adapters, and operators. They are **not** injected into the model tool loop
  (`agent_runtime.py`): a model still sees only the fixed `agent_tools.TOOLS` schema.
- **stdio transport only.** No SSE, WebSocket, or HTTP transport; run an HTTP-based MCP server
  behind a stdio bridge if you need one.
- **No shell.** `command` is an argv list and runs with `shell=False`.

## Configuration

```json
{
  "servers": {
    "files": {
      "command": ["python", "C:/tools/mcp_files.py"],
      "env": {"LOG_LEVEL": "warn"},
      "cwd": "C:/tools",
      "timeout_seconds": 30
    }
  }
}
```

| Field | Rules |
| --- | --- |
| `servers` | 1–32 entries; names match `[A-Za-z0-9_-]{1,64}` |
| `command` | Nonempty argv list, ≤ 32 entries, no shell |
| `env` | String-to-string map; only added to an allowlisted environment (`PATH`, `PATHEXT`, `SystemRoot`, `TEMP`, `TMP`, `HOME`, `USERPROFILE`, `COMSPEC`) |
| `cwd` | Optional existing directory |
| `timeout_seconds` | 1–600, default 30, applied per request |

Unknown keys are refused so a typo cannot silently change behavior.

## CLI

```bash
python agent_mcp.py list --config mcp.json
python agent_mcp.py call --config mcp.json --tool mcp__files__read --arguments '{"path":"README.md"}'
```

`list` prints every discovered tool with its `inputSchema`. Tool names are namespaced as
`mcp__<server>__<tool>`, so a server cannot shadow a built-in agent tool.
`call` prints the raw MCP result plus a flattened `text` field.

## Safety bounds

| Bound | Value | Why |
| --- | --- | --- |
| Response / request size | 1 MiB per JSON message | Prevents a server from exhausting controller memory |
| Tool count | 200 across all servers | Keeps discovery bounded |
| Request timeout | `timeout_seconds` per call | A hung server cannot stall the controller |
| Environment | Allowlist + explicit `env` | No accidental credential inheritance |
| Process lifetime | Started per operation, terminated on exit | No idle background servers |
| Output | `text_of()` only reads `content[].type == "text"` | Non-text blocks are not silently converted |

A call refused by the client (unknown server/tool, malformed arguments, timeout, oversize
message) raises `McpRefused` or exits with status 2 in the CLI.

## Using MCP from agent runs

Two supported patterns:

1. **Adapter pattern (recommended).** Wrap the MCP call in a trusted adapter that the agent
   invokes as an external step, exactly like the existing builder/oracle adapters. The adapter
   owns the MCP config, validates arguments, and returns a bounded JSON envelope. The model never
   gains arbitrary tool names.
2. **Pre-fetch pattern.** Discover tools with `list` in the controller, select the ones relevant
   to the task, and pass their descriptions/schemas into the agent prompt as untrusted context.
   Calls remain controller-side.

Direct injection into the model loop is intentionally not implemented: `agent_runtime` computes a
configuration digest over tool names, permissions, budgets, and source hashes, and treats external
tool effects as requiring reconciliation after a crash. Adding dynamic remote tools would weaken
those invariants.

## Verification

```bash
python agent_mcp_check.py
```

The check runs a loopback fake server and asserts: namespaced discovery, a real `tools/call`
round trip, notification skipping, client-side refusal of unknown server/tool names, shell-string
and unknown-key configuration refusal, a bounded timeout, and process cleanup.

## Limitations

- stdio only; no OAuth, no HTTP/SSE, no resumable streams.
- `inputSchema` is passed through, not enforced by the client; the server validates arguments.
- Tool results are not treated as trusted instructions; caller-side policy still applies.
- No automatic reconnection; a failed call is refused, not retried.
