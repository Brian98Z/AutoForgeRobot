"""Bounded, in-memory tools for an untrusted application implementation agent."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

import applications
import application_policy

MAX_RESULT_BYTES = 1_048_576
MAX_SEARCH_MATCHES = 100
MAX_QUERY_BYTES = 512
MAX_MUTATION_CHARS = 1500
TOOL_VERSION = "agent-tools-v4"

TOOLS = [
    {"type": "function", "function": {"name": "list_files", "description": "List candidate paths and hashes.", "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a candidate file.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "create_file", "description": "Create a NEW absent candidate file with at most 1500 characters. Never replaces an existing file. No hash or null argument needed.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string", "maxLength": MAX_MUTATION_CHARS}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write_file", "description": "Create or replace a small candidate file (at most 1500 characters). For larger files use successive edit_file changes.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string", "maxLength": MAX_MUTATION_CHARS}, "expected_sha256": {"type": ["string", "null"]}}, "required": ["path", "content", "expected_sha256"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_reference", "description": "Read a selected reference file.", "parameters": {"type": "object", "properties": {"package_id": {"type": "string"}, "path": {"type": "string"}}, "required": ["package_id", "path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_files", "description": "Search candidate text for a literal string, returning bounded matches.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_references", "description": "Search selected reference package text for a literal string, returning bounded matches.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace one unique nonempty exact string using the current SHA-256. old and new each allow at most 1500 characters. Make one small change, inspect the result, then validate.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string", "minLength": 1, "maxLength": MAX_MUTATION_CHARS}, "new": {"type": "string", "maxLength": MAX_MUTATION_CHARS}, "expected_sha256": {"type": "string"}}, "required": ["path", "old", "new", "expected_sha256"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write_files", "description": "Atomically update small candidate files after checking all hashes. Total content must not exceed 1500 characters; split larger work into small edits.", "parameters": {"type": "object", "properties": {"files": {"type": "array", "items": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string", "maxLength": MAX_MUTATION_CHARS}, "expected_sha256": {"type": ["string", "null"]}}, "required": ["path", "content", "expected_sha256"], "additionalProperties": False}}}, "required": ["files"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "validate", "description": "Run the parent's trusted immutable-snapshot validator.", "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "query_validation", "description": "Read the latest committed validation result without executing the candidate or consuming another oracle query. The controller marks whether it still matches current source.", "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}},
]
_SCHEMAS = {x["function"]["name"]: x["function"]["parameters"] for x in TOOLS}


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _check_type(value: Any, schema: dict, where: str) -> None:
    expected = schema.get("type")
    types = expected if isinstance(expected, list) else [expected]
    checks = {"object": lambda x: isinstance(x, dict), "array": lambda x: isinstance(x, list), "string": lambda x: isinstance(x, str), "null": lambda x: x is None}
    if not any(checks[t](value) if t in checks else False for t in types):
        raise ValueError(f"{where} has invalid type")
    if isinstance(value, str):
        minimum, maximum = schema.get('minLength', 0), schema.get('maxLength', MAX_RESULT_BYTES)
        if not minimum <= len(value) <= maximum:
            raise ValueError(f'{where} has {len(value)} characters; permitted range is {minimum}..{maximum}. Nothing executed. Split source into separate complete modules within this limit; do not resubmit the same oversized content.')
    if isinstance(value, dict):
        props = schema.get("properties", {})
        missing = set(schema.get("required", ())) - value.keys()
        extra = value.keys() - props.keys() if schema.get("additionalProperties") is False else set()
        if missing or extra:
            raise ValueError(f"{where} fields mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
        for key, item in value.items():
            _check_type(item, props[key], f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check_type(item, schema["items"], f"{where}[{index}]")


def validate_arguments(name: str, raw_json: str) -> dict[str, Any]:
    if name not in _SCHEMAS:
        raise ValueError("unknown tool")
    if not isinstance(raw_json, str) or len(raw_json.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("arguments must be bounded JSON text")
    try:
        value = json.loads(raw_json, object_pairs_hook=_pairs, parse_constant=_constant)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError(f"invalid strict JSON: {exc}") from exc
    _check_type(value, _SCHEMAS[name], "arguments")
    return value


def tool_class(name: str) -> str:
    if name not in _SCHEMAS:
        raise ValueError("unknown tool")
    return {"validate": "external", "create_file": "idempotent_write", "write_file": "idempotent_write", "write_files": "idempotent_write", "edit_file": "idempotent_write"}.get(name, "read")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file(candidate: dict, path: str):
    path = applications._validate_path(path)
    application_policy.check_candidate_path(path)
    return path, next((f for f in candidate["files"] if f["path"] == path), None)


def _references(references: list, ids: list[str]) -> list[dict]:
    allowed = set(ids)
    return [p for p in references if isinstance(p, dict) and p.get("package_id") in allowed]


def _read_ref(references: list, package_id: str, path: str) -> str:
    path = applications._validate_path(path)
    for package in references:
        if isinstance(package, dict) and package.get("package_id") == package_id:
            for entry in package.get("files", []):
                if isinstance(entry, dict) and entry.get("path") == path:
                    text = entry.get("content")
                    if not isinstance(text, str):
                        raise ValueError("reference source is not text")
                    return text
            raise ValueError("reference path not found")
    raise ValueError("reference package not found")


def _commit(candidate: dict, updates: list[dict]) -> dict:
    by_path = {f["path"]: f["content"] for f in candidate["files"]}
    for row in updates:
        by_path[row["path"]] = row["content"]
    clean, _ = applications.validate_candidate({**candidate, "files": [{"path": p, "content": c} for p, c in by_path.items()]})
    candidate.clear()
    candidate.update(clean)
    return {"files": [{"path": x["path"], "sha256": _digest(x["content"]), "size": len(x["content"].encode("utf-8"))} for x in updates]}


def execute(name: str, args: dict, candidate: dict, references: list, validate: Callable[[dict], dict]) -> dict:
    result = _execute(name, args, candidate, references, validate)
    if len(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("tool result exceeds byte limit")
    return result

def _execute(name: str, args: dict, candidate: dict, references: list, validate: Callable[[dict], dict]) -> dict:
    """Execute a schema-validated tool. Mutations are candidate-memory-only and all-or-nothing."""
    if name not in _SCHEMAS:
        raise ValueError("unknown tool")
    _check_type(args, _SCHEMAS[name], "arguments")
    if name == "query_validation":
        raise ValueError("validation history requires the owning runtime")
    if name == "list_files":
        return {"files": [{"path": p, "sha256": _digest(c), "size": len(c.encode("utf-8"))} for p, c in sorted((f["path"], f["content"]) for f in candidate["files"])]}
    if name == "read_file":
        path, item = _file(candidate, args["path"])
        if item is None: raise ValueError("candidate file not found")
        return {"path": path, "content": item["content"], "sha256": _digest(item["content"])}
    if name == "read_reference":
        text = _read_ref(_references(references, candidate["references"]), args["package_id"], args["path"])
        return {"package_id": args["package_id"], "path": args["path"], "content": text, "sha256": _digest(text)}
    if name == "search_files":
        return _search([(f["path"], f["content"]) for f in candidate["files"]], args["query"])
    if name == "search_references":
        rows = [(f.get("path"), f.get("content")) for p in _references(references, candidate["references"]) for f in p.get("files", []) if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("content"), str)]
        return _search(rows, args["query"])
    if name == 'create_file':
        path, current = _file(candidate, args['path'])
        if current is not None:
            raise ValueError('create_file only accepts an absent path; use read_file and edit_file for existing content')
        return _commit(candidate, [{'path': path, 'content': args['content']}])
    if name == "write_file":
        path, current = _file(candidate, args["path"])
        expected = args["expected_sha256"]
        if expected is not None and not _valid_hash(expected): raise ValueError("expected_sha256 must be lowercase SHA-256 or null")
        actual = _digest(current["content"]) if current else None
        if actual != expected: raise ValueError("stale edit: expected_sha256 mismatch")
        return _commit(candidate, [{"path": path, "content": args["content"]}])
    if name == "write_files":
        if not args["files"]: raise ValueError("files must not be empty")
        if sum(len(row["content"]) for row in args["files"]) > MAX_MUTATION_CHARS:
            raise ValueError("batch content exceeds mutation limit; use smaller local edits")
        seen = set()
        pending = []
        for row in args["files"]:
            path, current = _file(candidate, row["path"])
            folded = path.casefold()
            if folded in seen: raise ValueError("duplicate path in batch")
            seen.add(folded)
            expected = row["expected_sha256"]
            if expected is not None and not _valid_hash(expected): raise ValueError("invalid expected_sha256")
            if (_digest(current["content"]) if current else None) != expected: raise ValueError("stale edit: expected_sha256 mismatch")
            pending.append({"path": path, "content": row["content"]})
        return _commit(candidate, pending)
    if name == "edit_file":
        path, current = _file(candidate, args["path"])
        if current is None: raise ValueError("candidate file not found")
        if not _valid_hash(args['expected_sha256']) or _digest(current['content']) != args['expected_sha256']:
            raise ValueError('stale edit: expected_sha256 must be the file sha256 from a fresh read_file/list_files result, not candidate_sha256; no change made')
        count = current["content"].count(args["old"])
        if count != 1:
            raise ValueError(f'edit target matched {count} time(s); no change made; call read_file again and copy the smallest unique statement exactly, with decoded newlines rather than literal backslash-n; do not repeat this rejected target')
        return _commit(candidate, [{"path": path, "content": current["content"].replace(args["old"], args["new"], 1)}])
    return validate(candidate)


def _valid_hash(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _search(rows: list[tuple[str, str]], query: str) -> dict:
    if not query or len(query.encode("utf-8")) > MAX_QUERY_BYTES: raise ValueError("query must be nonempty and bounded")
    matches = []
    for path, content in rows:
        for number, line in enumerate(content.splitlines(), 1):
            if query in line:
                matches.append({"path": path, "line": number, "text": line[:2000]})
                if len(matches) == MAX_SEARCH_MATCHES: return {"matches": matches, "truncated": True}
    return {"matches": matches, "truncated": False}
