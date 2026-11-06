"""Focused contract checks for bounded agent candidate tools."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
import json

import agent_tools as T


def reject(fn):
    try:
        fn()
    except (ValueError, TypeError):
        return
    raise AssertionError("expected rejection")


def run():
    reject(lambda: T.validate_arguments("list_files", '{"x":1,"x":2}'))
    reject(lambda: T.validate_arguments("list_files", '{"x":NaN}'))
    reject(lambda: T.validate_arguments("list_files", '{"x":true}'))
    base = {"schema": 1, "goal": "test", "references": [], "files": [{"path": "a.py", "content": "first\nsecond\n"}, {"path": "b.py", "content": "before"}]}
    original = json.loads(json.dumps(base))
    batch = {"files": [{"path": "a.py", "content": "changed", "expected_sha256": hashlib.sha256(b"first\nsecond\n").hexdigest()}, {"path": "b.py", "content": "changed", "expected_sha256": "0" * 64}]}
    reject(lambda: T.execute("write_files", batch, base, [], lambda _: {}))
    assert base == original, "stale second file must leave whole candidate unchanged"
    fresh = {'schema': 1, 'goal': 'test', 'references': [], 'files': []}
    T.execute('create_file', {'path': 'new.py', 'content': 'value = 1\n'}, fresh, [], lambda _: {})
    reject(lambda: T.execute('create_file', {'path': 'new.py', 'content': 'value = 2\n'}, fresh, [], lambda _: {}))
    assert fresh['files'] == [{'path': 'new.py', 'content': 'value = 1\n'}], 'create-only action replaced an existing file'
    reject(lambda: T.execute('create_file', {'path': 'newer.py', 'content': 'x' * 1501}, fresh, [], lambda _: {}))
    unicode_text = "界" * T.MAX_MUTATION_CHARS
    args = {"path": "unicode.txt", "content": unicode_text, "expected_sha256": None}
    unicode_candidate = {"schema": 1, "goal": "test", "references": [], "files": []}
    parsed = T.validate_arguments("write_file", json.dumps(args, ensure_ascii=False))
    written = T.execute("write_file", parsed, unicode_candidate, [], lambda _: {})
    assert unicode_candidate["files"] == [{"path": "unicode.txt", "content": unicode_text}]
    assert written["files"][0]["size"] == len(unicode_text.encode("utf-8")) > T.MAX_MUTATION_CHARS
    reject(lambda: T.validate_arguments("write_file", json.dumps({**args, "content": unicode_text + "界"}, ensure_ascii=False)))
    edit_args = {"path": "unicode.txt", "old": unicode_text, "new": "界" * T.MAX_MUTATION_CHARS, "expected_sha256": hashlib.sha256(unicode_text.encode("utf-8")).hexdigest()}
    T.validate_arguments("edit_file", json.dumps(edit_args, ensure_ascii=False))
    reject(lambda: T.validate_arguments("edit_file", json.dumps({**edit_args, "old": unicode_text + "界"}, ensure_ascii=False)))
    reject(lambda: T.validate_arguments("edit_file", json.dumps({**edit_args, "new": unicode_text + "界"}, ensure_ascii=False)))
    before = json.loads(json.dumps(base))
    oversized = {"files": [{"path": "a.py", "content": "x" * 1000, "expected_sha256": hashlib.sha256(b"first\nsecond\n").hexdigest()}, {"path": "b.py", "content": "y" * 501, "expected_sha256": hashlib.sha256(b"before").hexdigest()}]}
    reject(lambda: T.execute("write_files", oversized, base, [], lambda _: {}))
    assert base == before, "oversized batch must be atomic"
    ambiguous = {"schema": 1, "goal": "test", "references": [], "files": [{"path": "a.py", "content": "same same"}]}
    reject(lambda: T.execute("edit_file", {"path": "a.py", "old": "same", "new": "x", "expected_sha256": hashlib.sha256(b"same same").hexdigest()}, ambiguous, [], lambda _: {}))
    reject(lambda: T.validate_arguments("edit_file", json.dumps({"path":"a.py", "old":"", "new":"x", "expected_sha256":"0"*64})))
    core = {"schema": 1, "goal": "test", "references": [], "files": [{"path": "main.py", "content": "x"}]}
    reject(lambda: T.execute("write_file", {"path": "agent_runtime.py", "content": "x", "expected_sha256": None}, core, [], lambda _: {}))
    selected = {"schema": 1, "goal": "test", "references": ["allowed"], "files": [{"path": "main.py", "content": "x"}]}
    refs = [{"package_id": "allowed", "files": [{"path": "x.py", "content": "trusted selection"}]}, {"package_id": "other", "files": [{"path": "x.py", "content": "other selection"}]}]
    result = T.execute("search_references", {"query": "selection"}, selected, refs, lambda _: {})
    assert len(result["matches"]) == 1 and result["matches"][0]["text"] == "trusted selection"
    assert T.tool_class("validate") == "external"
    assert {T.tool_class("list_files"), T.tool_class("write_file")} == {"read", "idempotent_write"}
    print(json.dumps({"status": "PASS", "checks": ["strict-json-schema", "unicode-character-mutation-boundary", "oversized-batch-atomic", "empty-edit-needle-refused", "stale-batch-is-atomic", "ambiguous-edit-refused", "reference-scope-immutable", "core-protected", "resume-tool-classes"]}, sort_keys=True))



if __name__ == "__main__": run()
