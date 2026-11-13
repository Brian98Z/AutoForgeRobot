"""Local protocol-simulator checks for bounded agent_runtime behavior."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

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
        original = {"schema": 1, "goal": request["goal"], "references": [], "files": [{"path": "main.py", "content": "value = (\n"}]}
        queue = [
            {"role": "assistant", "content": None, "tool_calls": [_call("write_file", {"path": "main.py", "content": "value = 9\n", "expected_sha256": hashlib.sha256(b"value = (\n").hexdigest()}, "premature")]},
            {"role": "assistant", "content": None, "tool_calls": [_call("validate", {}, "fail")]},
            {"role": "assistant", "content": None, "tool_calls": [_call("query_validation", {}, "query-current")]},
            {"role": "assistant", "content": None, "tool_calls": [_call("read_file", {"path": "main.py"}, "read-baseline"), _call("write_file", {"path": "main.py", "content": "value = 9\n", "expected_sha256": "0" * 64}, "stale"), _call("read_file", {"path": "main.py"}, "read-after-stale"), _call("write_file", {"path": "main.py", "content": "value = 2\n", "expected_sha256": hashlib.sha256(b"value = (\n").hexdigest()}, "repair")]},
            {"role": "assistant", "content": None, "tool_calls": [_call("query_validation", {}, "query-stale")]},
            {"role": "assistant", "content": None, "tool_calls": [_call("validate", {}, "validate")] }]
        calls = []
        def transport(messages, tools, *, on_event, should_stop, max_tokens):
            calls.append((messages, tools))
            return queue.pop(0)
        revision_digests = []
        validation_outcomes = []
        validated_sources = []
        def trusted_builder(argv, payload):
            manifest = payload["workspace_manifest"]
            revision_digests.append(manifest["candidate_sha256"])
            import ast
            workspace = Path(manifest["workspace"])
            source = (workspace / "main.py").read_text(encoding="utf-8")
            validated_sources.append(source)
            try:
                ast.parse(source)
                passed = True
                validation_outcomes.append("PASS")
            except SyntaxError:
                passed = False
                validation_outcomes.append("FAIL")
            return {"schema": 1, "candidate_sha256": manifest["candidate_sha256"], "build_pass": passed, "test_pass": passed, "observations": ["AST-only simulator result."], "limitations": ["No runtime or business-contract validation was performed."]}
        with patch.object(agent_runtime.agent_transport, "complete", side_effect=transport), patch.object(agent_runtime.applications, "_run_json_command", side_effect=trusted_builder):
            report = agent_runtime.run(request, [], builder_argv, root / "iteration", initial_candidate=original)
        assert report["status"] == "PASS", report
        assert len(revision_digests) == 2 and len(calls) == 6
        events = [json.loads(line) for line in (root / "iteration" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        tool_results = {event["call_id"]: event["result"] for event in events if event["type"] == "tool_result"}
        assert tool_results["premature"].get("error", "").startswith("ValueError: baseline validation required")
        assert tool_results["read-baseline"]["content"] == original["files"][0]["content"]
        assert tool_results["stale"].get("error", "").startswith("ValueError: stale edit")
        assert json.loads((root / "iteration" / "candidate.json").read_text(encoding="utf-8"))["files"][0]["content"] == "value = 2\n"
        assert tool_results["read-after-stale"]["content"] == original["files"][0]["content"], "stale write changed candidate before repair"
        assert validated_sources == [original["files"][0]["content"], "value = 2\n"], "refused mutation changed baseline candidate"
        assert tool_results["fail"]["status"] == "FAIL" and tool_results["validate"]["status"] == "PASS"
        assert tool_results['query-current']['current'] is True
        assert tool_results['query-current']['validation'] == tool_results['fail']
        assert tool_results['query-stale']['current'] is False
        assert tool_results['query-stale']['validation'] == tool_results['fail']
        validation_paths = list((root / "iteration" / "revisions").glob("*/validation.json"))
        assert len(validation_paths) == 2
        assert json.loads((root / "iteration" / "report.json").read_text(encoding="utf-8"))["status"] == "PASS"
        state = agent_runtime.agent_session.load(root / "iteration")
        expected_digest = agent_runtime._candidate_digest(state["candidate"])
        assert state["validated_candidate"] == expected_digest
        with patch.object(agent_runtime.agent_transport, "complete", side_effect=AssertionError("terminal resume must not call model")):
            resumed = agent_runtime.run(request, [], builder_argv, root / "iteration", resume=True)
        resumed_state = agent_runtime.agent_session.load(root / "iteration")
        assert resumed["status"] == "PASS" and resumed_state["validated_candidate"] == expected_digest
        continuing = root / "continue-after-baseline"
        queue[:] = [
            {"role": "assistant", "content": None, "tool_calls": [_call("validate", {}, "baseline-before-wait")]},
            {"role": "assistant", "content": "Waiting", "tool_calls": []}]
        with patch.object(agent_runtime.agent_transport, "complete", side_effect=transport), patch.object(agent_runtime.applications, "_run_json_command", side_effect=trusted_builder):
            waiting = agent_runtime.run(request, [], builder_argv, continuing, initial_candidate=original)
        assert waiting["task_status"] == "WAITING_INPUT"
        queue[:] = [{"role": "assistant", "content": None, "tool_calls": [
            _call("query_validation", {}, "query-after-resume"),
            _call("edit_file", {"path": "main.py", "old": "(", "new": "2", "expected_sha256": hashlib.sha256(b"value = (\n").hexdigest()}, "repair-after-resume"),
            _call("validate", {}, "pass-after-resume")]}]
        with patch.object(agent_runtime.agent_transport, "complete", side_effect=transport), patch.object(agent_runtime.applications, "_run_json_command", side_effect=trusted_builder):
            continued = agent_runtime.run(request, [], builder_argv, continuing, resume=True)
        assert continued["status"] == "PASS", continued
        assert validated_sources[-2:] == ["value = (\n", "value = 2\n"]
        resumed_events = [json.loads(line) for line in (continuing / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        resumed_result = next(event["result"] for event in resumed_events if event.get("type") == "tool_result" and event.get("call_id") == "repair-after-resume")
        assert "error" not in resumed_result
        recovered_query=next(event['result'] for event in resumed_events if event.get('type')=='tool_result' and event.get('call_id')=='query-after-resume')
        assert recovered_query['current'] is True and recovered_query['validation']['status']=='FAIL'
        premature = root / "premature"
        with patch.object(agent_runtime.agent_transport, "complete", return_value={"role": "assistant", "content": "Done", "tool_calls": []}):
            held = agent_runtime.run(request, [], builder_argv, premature)
        assert held["status"] == "HOLD" and "without a trusted builder PASS" in held["observations"][0]
        assert not (premature / "revisions").exists()
        rejected_output = root / 'schema-repair'
        queue[:] = [
            {'role': 'assistant', 'content': None, 'tool_calls': [
                _call('write_file', {'path': 'must-not-exist.py', 'content': 'x = 1', 'expected_sha256': None}, 'batch-first'),
                _call('write_file', {'path': 'large.py', 'content': 'x' * 1501, 'expected_sha256': None}, 'batch-invalid')]},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                _call('write_file', {'path': 'main.py', 'content': 'value = 2\n', 'expected_sha256': None}, 'small-repair'),
                _call('validate', {}, 'small-pass')]}]
        def corrected_transport(messages, tools, **kwargs):
            if len(queue) == 1:
                assert [m['tool_call_id'] for m in messages[-2:]] == ['batch-first', 'batch-invalid']
                assert not (rejected_output / 'candidate.json').exists(), 'invalid batch executed its valid first write'
            return queue.pop(0)
        with patch.object(agent_runtime.agent_transport, 'complete', side_effect=corrected_transport), patch.object(agent_runtime.applications, '_run_json_command', side_effect=trusted_builder):
            corrected = agent_runtime.run(request, [], builder_argv, rejected_output)
        assert corrected['status'] == 'PASS'
        final_files = agent_runtime.agent_session.load(rejected_output)['candidate']['files']
        assert final_files == [{'path': 'main.py', 'content': 'value = 2\n'}]
        empty_output = root / 'empty-validate'
        queue[:] = [
            {'role': 'assistant', 'content': None, 'tool_calls': [_call('validate', {}, 'empty-first')]},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                _call('create_file', {'path': 'main.py', 'content': 'value = 2\n'}, 'create-after-empty'),
                _call('validate', {}, 'real-pass')]}]
        with patch.object(agent_runtime.agent_transport, 'complete', side_effect=transport), patch.object(agent_runtime.applications, '_run_json_command', side_effect=trusted_builder):
            empty_result = agent_runtime.run(request, [], builder_argv, empty_output)
        assert empty_result['status'] == 'PASS'
        empty_events = [json.loads(line) for line in (empty_output / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        empty_feedback = next(e['result'] for e in empty_events if e.get('type') == 'tool_result' and e.get('call_id') == 'empty-first')
        assert empty_feedback['status'] == 'FAIL' and empty_feedback['candidate_sha256'] is None
        assert len(list((empty_output / 'revisions').iterdir())) == 1
        # A module-only syntax task must reach its own adapter without a JSONL entrypoint.
        import sys
        modules = {**original, 'files': [{'path': 'module.py', 'content': 'def add(a, b)\n    return a + b\n'}]}
        module_output = root / 'module-only'
        module_output.mkdir()
        syntax_argv = [sys.executable, str(Path(agent_runtime.__file__).parents[1] / 'fixtures' / 'agent_syntax_builder.py')]
        broken_module = agent_runtime._validate_and_build(modules, module_output, syntax_argv)
        assert broken_module['status'] == 'FAIL' and any('SyntaxError in module.py' in x for x in broken_module['observations'])
        modules['files'][0]['content'] = 'def add(a, b):\n    return a + b\n'
        parsed_module = agent_runtime._validate_and_build(modules, module_output, syntax_argv)
        assert parsed_module['status'] == 'PASS' and 'revision_path' in parsed_module
        import application_builder
        stage = agent_runtime.applications.stage_candidate(modules, root / 'jsonl-no-entrypoint')
        manifest = json.loads(Path(stage['manifest']).read_text(encoding='utf-8'))
        try: application_builder._manifest(manifest)
        except application_builder.BuildFailure as exc: assert 'entrypoint main.py is absent' in str(exc)
        else: raise AssertionError('JSONL adapter accepted a module-only candidate')
        queue[:] = [{'role': 'assistant', 'content': None, 'tool_calls': [_call('validate', {}, f'repeat-{number}')]} for number in range(4)]
        with patch.object(agent_runtime.agent_transport, 'complete', side_effect=transport), patch.object(agent_runtime.applications, '_run_json_command', side_effect=trusted_builder):
            repeated = agent_runtime.run(request, [], builder_argv, root / 'no-progress', initial_candidate=original, limits=agent_runtime.agent_budget.Limits(repeated_failures=3))
        assert repeated['reason_code'] == 'repeated_no_progress' and repeated['turns'] == 3 and len(queue) == 1
        repeated_state = agent_runtime.agent_session.load(root / 'no-progress')
        assert repeated_state['candidate'] == original and repeated_state['inflight'] is None
        unchanged = {**original, 'files': [{'path': 'main.py', 'content': 'value = 2\n'}]}
        queue[:] = [{'role': 'assistant', 'content': None, 'tool_calls': [_call('write_file', {'path': 'main.py', 'content': 'value = 2\n', 'expected_sha256': hashlib.sha256(b'value = 2\n').hexdigest()}, f'unchanged-{number}')]} for number in range(4)]
        queue.insert(0, {'role': 'assistant', 'content': None, 'tool_calls': [_call('validate', {}, 'known-failing-baseline')]})
        def failing_builder(argv, payload):
            return {'schema': 1, 'candidate_sha256': payload['candidate_sha256'], 'build_pass': True, 'test_pass': False, 'observations': ['authored failing behavior'], 'limitations': ['controller regression only']}
        with patch.object(agent_runtime.agent_transport, 'complete', side_effect=transport), patch.object(agent_runtime.applications, '_run_json_command', side_effect=failing_builder):
            noop = agent_runtime.run(request, [], builder_argv, root / 'unchanged-progress', initial_candidate=unchanged)
        assert noop['reason_code'] == 'repeated_no_progress' and noop['turns'] == 4 and len(queue) == 1, noop
        noop_state = agent_runtime.agent_session.load(root / 'unchanged-progress')
        assert noop_state['candidate'] == unchanged
        assert all(json.loads(m['content'])['changed'] is False for m in noop_state['messages'] if m.get('tool_call_id', '').startswith('unchanged-'))
        print(json.dumps({"status": "PASS", "scope": "protocol simulation; builder stub is AST-only, not real runtime/business acceptance", "checks": ["baseline required before existing-file mutation", "validation FAIL permits repair", "exact repaired candidate validated PASS", "premature model stop is HOLD", "event log and final report persisted", "unchanged writes stop as repeated no progress"]}, ensure_ascii=False))
if __name__ == "__main__":
    main()
