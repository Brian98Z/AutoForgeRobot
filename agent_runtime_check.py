"""Local protocol-simulator checks for bounded agent_runtime behavior."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import agent_runtime


def _call(name: str, args: dict, call_id: str = "call") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, separators=(",", ":"))}}


def main() -> None:
    request = {"schema": 1, "goal": "protocol simulation", "references": []}
    builder_argv = ["unused-trusted-builder"]
    with tempfile.TemporaryDirectory(prefix="agent-runtime-check-") as tmp:
        root = Path(tmp)
        queue = [
            {"role": "assistant", "content": None, "tool_calls": [_call("write_file", {"path": "main.py", "content": "value = 1\n", "expected_sha256": None}, "new")]},
            {"role": "assistant", "content": None, "tool_calls": [_call("read_file", {"path": "main.py"}, "read"), _call("write_file", {"path": "main.py", "content": "value = 2\n", "expected_sha256": "0" * 64}, "stale"), _call("validate", {}, "validate")]}]
        calls = []
        def transport(messages, tools):
            calls.append((messages, tools))
            return queue.pop(0)
        revision_digests = []
        def trusted_builder(argv, payload):
            manifest = payload["workspace_manifest"]
            revision_digests.append(manifest["candidate_sha256"])
            # Simulate an AST-only contract builder: syntax-valid Python passes; no model result is trusted.
            import ast
            workspace = Path(manifest["workspace"])
            source = (workspace / "main.py").read_text(encoding="utf-8")
            ast.parse(source)
            return {"schema": 1, "candidate_sha256": manifest["candidate_sha256"], "build_pass": True, "test_pass": True, "observations": ["AST-only simulator accepted Python syntax."], "limitations": ["No runtime or business-contract validation was performed."]}
        with patch.object(agent_runtime.agent_transport, "complete", side_effect=transport), patch.object(agent_runtime.applications, "_run_json_command", side_effect=trusted_builder):
            report = agent_runtime.run(request, [], builder_argv, root / "iteration")
        assert report["status"] == "PASS", report
        assert len(revision_digests) == 1 and len(calls) == 2
        events = [json.loads(line) for line in (root / "iteration" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        tool_results = [event["result"] for event in events if event["type"] == "tool_result"]
        assert any(result.get("error", "").startswith("ValueError: stale edit") for result in tool_results), tool_results
        assert any(result.get("content") == "value = 1\n" for result in tool_results), tool_results
        assert (root / "iteration" / "revisions" / f"0000-{revision_digests[0]}" / "validation.json").is_file()
        assert json.loads((root / "iteration" / "report.json").read_text(encoding="utf-8"))["status"] == "PASS"

        premature = root / "premature"
        with patch.object(agent_runtime.agent_transport, "complete", return_value={"role": "assistant", "content": "Done", "tool_calls": []}):
            held = agent_runtime.run(request, [], builder_argv, premature)
        assert held["status"] == "HOLD" and "without a trusted builder PASS" in held["observations"][0]
        assert not (premature / "revisions").exists()
        print(json.dumps({"status": "PASS", "scope": "protocol simulation; builder stub is AST-only, not real runtime/business acceptance", "checks": ["read-after-write exact source", "expected-hash stale-edit rejection", "immutable staged snapshot and digest-bound PASS", "premature model stop is HOLD", "event log and final report persisted"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
