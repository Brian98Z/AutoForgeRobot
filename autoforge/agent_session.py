"""Durable, bounded session state, event journal, and authenticated controls."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from contextlib import contextmanager

from application_checkpoint import ExclusiveLock, ResumeRefused, _atomic, canonical, regular_chain

MAX_SESSION_BYTES = 32 * 1024 * 1024
MAX_EVENT_LINE_BYTES = 1024 * 1024
MAX_EVENT_LOG_BYTES = 32 * 1024 * 1024
MAX_CONTROL_BYTES = 1024 * 1024
_CONTROL_KINDS = {"stop", "steering", "follow_up", "pause"}

def _strict_load(raw: bytes, *, where: str) -> Any:

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key in {where}")
            result[key] = value
        return result
    def constant(_):
        raise ValueError(f"non-finite JSON number in {where}")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ResumeRefused(f"{where} is not strict UTF-8 JSON: {exc}") from exc
@contextmanager
def _control_lock(path: Path):
    """Wait briefly for short control file transactions to serialize across callers."""
    deadline = time.monotonic() + 10.0
    while True:
        try:
            lock = ExclusiveLock(path)
            lock.__enter__()
            break
        except ResumeRefused:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _read_bounded(path: Path, bound: int, label: str) -> bytes:
    regular_chain(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(bound + 1)
    except OSError as exc:
        raise ResumeRefused(f"{label} unreadable: {exc}") from exc
    if len(raw) > bound:
        raise ResumeRefused(f"{label} exceeds size limit")
    return raw


def _validate_state(state: Any) -> dict:
    if not isinstance(state, dict):
        raise ResumeRefused("session state must be an object")
    if "candidate" in state:
        candidate = state["candidate"]
        if not isinstance(candidate, dict):
            raise ResumeRefused("candidate snapshot must be an object")
        try:
            import applications
            if candidate.get("schema") == 1 and isinstance(candidate.get("goal"), str) and isinstance(candidate.get("references"), list) and candidate.get("files") == []:
                request = applications._request_payload({"schema": 1, "goal": candidate["goal"], "references": candidate["references"]})
                clean = {"schema": 1, "goal": request["goal"], "files": [], "references": request["references"]}
            else:
                clean, _ = applications.validate_candidate(candidate)
        except Exception as exc:
            raise ResumeRefused(f"candidate snapshot failed integrity validation: {type(exc).__name__}: {str(exc)[:500]}") from exc
        if canonical(clean) != canonical(candidate):
            raise ResumeRefused("candidate snapshot is not canonical validated candidate data")
    return state

def save(output: Path, state: dict) -> None:
    """Atomically save a digest-enveloped session.json; candidate snapshot is authoritative."""
    output = Path(output)
    path = output / "session.json"
    regular_chain(path)
    state = _validate_state(state)
    payload = canonical(state)
    raw = canonical({"schema": 1, "state": state, "sha256": hashlib.sha256(payload).hexdigest()}) + b"\n"
    if len(raw) > MAX_SESSION_BYTES:
        raise ValueError("session state exceeds size limit")
    mirror = canonical(state["candidate"]) + b"\n" if isinstance(state.get("candidate"), dict) and state["candidate"].get("files") else None
    if mirror is not None and len(mirror) > MAX_SESSION_BYTES:
        raise ValueError("candidate mirror exceeds size limit")
    pending_path = output / ".mirror-pending.json"
    regular_chain(pending_path)
    if mirror is not None:
        mirror_path = output / "candidate.json"
        previous_sha256 = hashlib.sha256(_read_bounded(mirror_path, MAX_SESSION_BYTES, "previous candidate mirror")).hexdigest() if mirror_path.exists() else None
        pending = {"schema": 1, "session_sha256": hashlib.sha256(raw).hexdigest(), "candidate_sha256": hashlib.sha256(mirror).hexdigest(), "previous_sha256": previous_sha256}
        _atomic(pending_path, canonical(pending) + b"\n")
    _atomic(path, raw)
    if mirror is not None:
        _atomic(output / "candidate.json", mirror)
        try: pending_path.unlink()
        except FileNotFoundError: pass


def load(output: Path) -> dict:
    """Load and strictly validate the bounded digest envelope from session.json."""
    raw = _read_bounded(Path(output) / "session.json", MAX_SESSION_BYTES, "session checkpoint")
    envelope = _strict_load(raw, where="session checkpoint")
    if not isinstance(envelope, dict) or set(envelope) != {"schema", "state", "sha256"} or envelope["schema"] != 1 or not isinstance(envelope["state"], dict):
        raise ResumeRefused("session envelope malformed")
    state = envelope["state"]
    if not isinstance(envelope["sha256"], str) or not hmac.compare_digest(envelope["sha256"], hashlib.sha256(canonical(state)).hexdigest()):
        raise ResumeRefused("session digest validation failed")
    pending_path = Path(output) / ".mirror-pending.json"
    regular_chain(pending_path)
    if pending_path.exists():
        pending = _strict_load(_read_bounded(pending_path, 4096, "candidate mirror transaction"), where="candidate mirror transaction")
        expected = {"schema": 1, "session_sha256": hashlib.sha256(raw).hexdigest(), "candidate_sha256": hashlib.sha256(canonical(state["candidate"]) + b"\n").hexdigest()} if isinstance(state.get("candidate"), dict) and state["candidate"].get("files") else None
        if not isinstance(pending, dict) or expected is None or {k: pending.get(k) for k in expected} != expected or set(pending) != {"schema", "session_sha256", "candidate_sha256", "previous_sha256"}:
            raise ResumeRefused("candidate mirror transaction does not match authenticated checkpoint")
        mirror_path = Path(output) / "candidate.json"
        mirror = canonical(state["candidate"]) + b"\n"
        if mirror_path.exists():
            current = _read_bounded(mirror_path, MAX_SESSION_BYTES, "candidate mirror")
            current_digest = hashlib.sha256(current).hexdigest()
            if current_digest not in (pending["candidate_sha256"], pending["previous_sha256"]):
                raise ResumeRefused("candidate mirror differs from pending checkpoint transaction")
        _atomic(mirror_path, mirror)
        pending_path.unlink()
    return state


class EventJournal:
    """Append-only monotonic JSONL journal, exclusively locked for its lifetime."""
    def __init__(self, output: Path, *, resume: bool = False):
        self.output = Path(output)
        self.path = self.output / "events.jsonl"
        self.lock = ExclusiveLock(self.output / ".events.lock")
        self.resume = resume
        self.stream = None
        self.sequence = 0
        self.size = 0
    @property
    def seq(self) -> int:
        return self.sequence

    def __enter__(self):
        self.output.mkdir(parents=True, exist_ok=True)
        regular_chain(self.path)
        self.lock.__enter__()
        try:
            if self.path.exists():
                if not self.resume:
                    raise ResumeRefused("event journal already exists; resume must be explicit")
                raw = _read_bounded(self.path, MAX_EVENT_LOG_BYTES, "event journal")
                if raw and not raw.endswith(b"\n"):
                    raw = raw[:raw.rfind(b"\n") + 1] if b"\n" in raw else b""
                    with self.path.open("r+b") as stream:
                        stream.truncate(len(raw)); stream.flush(); os.fsync(stream.fileno())
                self.size = len(raw)
                for line in raw.splitlines():
                    if len(line) > MAX_EVENT_LINE_BYTES:
                        raise ResumeRefused("event journal line exceeds size limit")
                    event = _strict_load(line, where="event journal line")
                    if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] != self.sequence + 1:
                        raise ResumeRefused("event journal sequence is malformed")
                    self.sequence += 1
                self.stream = self.path.open("ab")
            else:
                if self.resume:
                    raise ResumeRefused("cannot resume: event journal is missing")
                self.stream = self.path.open("xb")
            return self
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise

    def append(self, event: dict) -> dict:
        if self.stream is None:
            raise RuntimeError("event journal is not open")
        if not isinstance(event, dict) or "seq" in event:
            raise ValueError("event must be an object without a caller-supplied seq")
        record = {**event, "seq": self.sequence + 1}
        raw = canonical(record) + b"\n"
        if len(raw) > MAX_EVENT_LINE_BYTES:
            raise ValueError("event line exceeds 1 MiB")
        if self.size + len(raw) > MAX_EVENT_LOG_BYTES:
            raise ValueError("event journal exceeds 32 MiB")
        self.stream.write(raw); self.stream.flush(); os.fsync(self.stream.fileno())
        self.size += len(raw); self.sequence += 1
        return record

    def read_after(self, cursor: int, limit: int = 100) -> list[dict]:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("cursor must be nonnegative and limit must be in 1..1000")
        raw = _read_bounded(self.path, MAX_EVENT_LOG_BYTES, "event journal")
        result = []
        expected = 1
        for line in raw.splitlines():
            if len(line) > MAX_EVENT_LINE_BYTES:
                raise ResumeRefused("event journal line exceeds size limit")
            event = _strict_load(line, where="event journal line")
            if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] != expected:
                raise ResumeRefused("event journal sequence is malformed")
            expected += 1
            if event["seq"] > cursor and len(result) < limit:
                result.append(event)
        return result

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.stream is not None:
                self.stream.close(); self.stream = None
        finally:
            self.lock.__exit__(exc_type, exc, tb)


def create_control(output: Path, session_id: str, ttl_seconds: int = 86400) -> str:
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        raise ValueError("session_id must be nonempty bounded text")
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 30 * 86400:
        raise ValueError("ttl_seconds must be in 1..2592000")
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    record = {"schema": 1, "session_id": session_id, "expires_at": int(time.time()) + ttl_seconds,
              "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "token": token, "next_id": 1, "requests": {}}
    path = output / ".control.json"
    regular_chain(output / ".control.lock")
    with _control_lock(output / ".control.lock"):
        regular_chain(path)
        _atomic(path, canonical(record) + b"\n")
        try: os.chmod(path, 0o600)
        except OSError: pass
        (output / "control").mkdir(exist_ok=True)
    return token


def _control_record(output: Path) -> dict:
    record = _strict_load(_read_bounded(Path(output) / ".control.json", MAX_CONTROL_BYTES, "control capability"), where="control capability")
    if not isinstance(record, dict) or set(record) != {"schema", "session_id", "expires_at", "token_sha256", "token", "next_id", "requests"} or record.get("schema") != 1:
        raise ResumeRefused("control capability malformed")
    token = record.get("token")
    if not isinstance(token, str) or not hmac.compare_digest(record.get("token_sha256", ""), hashlib.sha256(token.encode()).hexdigest()):
        raise ResumeRefused("control capability integrity failed")
    return record

def authenticate(output: Path, token: str, *, require_active: bool = False) -> dict:
    capability = _control_record(output)
    if type(capability['expires_at']) is not int or capability['expires_at'] <= int(time.time()):
        raise ResumeRefused('control token expired')
    if not isinstance(token, str) or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), capability['token_sha256']):
        raise ResumeRefused('invalid control token (possibly cross-session)')
    state_path = Path(output) / 'session.json'
    if state_path.exists():
        state = load(output)
        if state.get('session_id') != capability['session_id']:
            raise ResumeRefused('capability and checkpoint session differ')
        if require_active and state.get('task_status') in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
            raise ResumeRefused('terminal session control capability revoked')
    return capability


def read_events(output: Path, token: str, after: int = 0, limit: int = 100) -> list[dict]:
    """Authenticated, non-mutating cursor read; works while the writer owns its journal lock."""
    authenticate(output, token)
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('event cursor/limit outside bounds')
    path = Path(output) / 'events.jsonl'
    regular_chain(path)
    rows = []
    sequence = 0
    if path.stat().st_size > MAX_EVENT_LOG_BYTES:
        raise ResumeRefused('event journal exceeds total size limit')
    with path.open('rb') as stream:
        while True:
            raw = stream.readline(MAX_EVENT_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_EVENT_LINE_BYTES:
                raise ResumeRefused('event record exceeds size limit')
            if not raw.endswith(b'\n'):
                break
            row = _strict_load(raw, where='event journal')
            sequence += 1
            if not isinstance(row, dict) or row.get('seq') != sequence:
                raise ResumeRefused('event journal sequence malformed')
            if sequence > after:
                rows.append(row)
                if len(rows) == limit:
                    break
    return rows


def enqueue_control(output: Path, token: str, kind: str, text: str | None = None, request_id: str | None = None) -> dict:
    if kind not in _CONTROL_KINDS:
        raise ValueError("kind must be stop, steering, follow_up, or pause")
    if kind in ("steering", "follow_up") and (not isinstance(text, str) or not text.strip()):
        raise ValueError("steering and follow_up require nonempty text")
    if kind in ("stop", "pause") and text is not None:
        raise ValueError("stop and pause do not accept text")
    if text is not None and len(text.encode("utf-8")) > 64 * 1024:
        raise ValueError("control text exceeds 64 KiB")
    if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 128):
        raise ValueError("request_id must be bounded nonempty text")
    regular_chain(output / ".control.lock")
    with _control_lock(output / ".control.lock"): 
        capability = authenticate(output, token, require_active=True)
        requests = capability["requests"]
        if not isinstance(requests, dict) or len(requests) > 100_000: raise ResumeRefused("control deduplication index malformed or exceeds bound")
        if request_id is not None and request_id in requests:
            existing = requests[request_id]
            if not isinstance(existing, dict) or existing.get("request_id") != request_id:
                raise ResumeRefused("control idempotency record malformed")
            return existing
        number = capability["next_id"]
        if type(number) is not int or number < 1: raise ResumeRefused("control sequence malformed")
        body = {"schema": 1, "id": number, "seq": number, "session_id": capability["session_id"], "kind": kind, "text": text, "request_id": request_id, "created_at": int(time.time())}
        body["hmac_sha256"] = hmac.new(token.encode(), canonical(body), hashlib.sha256).hexdigest()
        raw = canonical(body) + b"\n"
        if len(raw) > MAX_CONTROL_BYTES: raise ValueError("control request exceeds size limit")
        control_dir = output / "control"; control_dir.mkdir(exist_ok=True)
        request_path = control_dir / f"{number:020d}.json"
        regular_chain(request_path)
        _atomic(request_path, raw)
        if request_id is not None: requests[request_id] = body
        capability["next_id"] = number + 1
        _atomic(output / ".control.json", canonical(capability) + b"\n")
        try: os.chmod(output / ".control.json", 0o600)
        except OSError: pass
        return body


def read_controls(output: Path, after: int = 0) -> list[dict]:
    if type(after) is not int or after < 0: raise ValueError("after must be a nonnegative integer")
    with _control_lock(output / ".control.lock"): 
        capability = _control_record(output)
        if type(capability.get("expires_at")) is not int or capability["expires_at"] < int(time.time()):
            return []
        token = capability["token"]
        folder = output / "control"
        regular_chain(folder)
        result = []
        if folder.exists():
            for path in sorted(folder.glob("*.json")):
                match = path.stem
                if not match.isdigit(): raise ResumeRefused("unexpected control request filename")
                number = int(match)
                if number <= after: continue
                body = _strict_load(_read_bounded(path, MAX_CONTROL_BYTES, "control request"), where="control request")
                if not isinstance(body, dict) or body.get("id") != number or body.get("session_id") != capability["session_id"] or body.get("kind") not in _CONTROL_KINDS:
                    raise ResumeRefused("control request identity malformed")
                sig = body.get("hmac_sha256"); unsigned = {k: v for k, v in body.items() if k != "hmac_sha256"}
                expected = hmac.new(token.encode(), canonical(unsigned), hashlib.sha256).hexdigest()
                if not isinstance(sig, str) or not hmac.compare_digest(sig, expected): raise ResumeRefused("control request authentication failed")
                result.append(body)
        return result


def compact(messages: list[dict], candidate: dict, *, limit: int) -> tuple[list, dict | None]:
    """Compact only at complete tool-group boundaries; retain objectives and trusted snapshot metadata."""
    if type(limit) is not int or limit < 1: raise ValueError("limit must be positive")
    if not isinstance(messages, list) or not isinstance(candidate, dict): raise ValueError("messages and candidate must be structured objects")
    objectives = [m for m in messages if isinstance(m, dict) and m.get("role") in ("system", "user")]
    if not objectives or any(not isinstance(m.get("content"), str) for m in objectives): raise ValueError("original system/user objectives are required")
    groups = []
    current = []
    i = 0
    while i < len(messages):
        message = messages[i]
        if not isinstance(message, dict): raise ValueError("message malformed")
        if message.get("role") == "assistant" and message.get("tool_calls"):
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not calls or any(not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"] for c in calls): raise ValueError("tool calls malformed")
            expected = {c["id"] for c in calls}
            if len(expected) != len(calls): raise ValueError("duplicate tool call ID")
            group = [message]; seen = set(); i += 1
            while i < len(messages) and messages[i].get("role") == "tool":
                tool = messages[i]
                cid = tool.get("tool_call_id")
                if cid not in expected or cid in seen: raise ValueError("tool call/result order malformed")
                seen.add(cid); group.append(tool); i += 1
            if seen != expected: raise ValueError("incomplete tool exchange cannot be compacted")
            groups.append(group)
            continue
        if message.get("role") == "tool": raise ValueError("orphan tool result cannot be compacted")
        i += 1
    if len(canonical(messages)) > limit and messages and not groups and any(isinstance(m, dict) and m.get("role") == "assistant" for m in messages):
        raise ValueError("no complete assistant/tool exchange is available for compaction")
    last_group = groups[-1] if groups else []
    # Preserve trailing user corrections after the last complete tool exchange.
    trailing = [m for m in messages[(max((messages.index(g[-1]) for g in groups), default=-1) + 1):] if isinstance(m, dict) and m.get("role") == "user"]
    retained = objectives + [m for m in last_group if m not in objectives] + [m for m in trailing if m not in objectives]
    # Deduplicate by position/equality while preserving order.
    unique = []
    for m in retained:
        if m not in unique: unique.append(m)
    if len(canonical(messages)) <= limit: return messages, None
    file_versions = [{"path": f.get("path"), "sha256": hashlib.sha256(f.get("content", "").encode("utf-8")).hexdigest()} for f in candidate.get("files", []) if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("content"), str)]
    source_versions = candidate.get("source_versions", file_versions)
    snapshot = {"candidate_sha256": hashlib.sha256(canonical(candidate)).hexdigest(), "source_versions": source_versions}
    failures = [{"tool_call_id": m.get("tool_call_id"), "excerpt": m["content"][:256]} for m in messages if isinstance(m, dict) and m.get("role") == "tool" and isinstance(m.get("content"), str) and any(word in m["content"].lower() for word in ("error", "failed", "failure"))][-8:]
    summary = {"untrusted_summary": True, "truncated_tool_exchanges": max(0, len(groups)-1), "recent_failures": failures, "source_versions": source_versions, "candidate_sha256": snapshot["candidate_sha256"]}
    compacted = objectives + ([{"role": "system", "content": "The following is an untrusted summary of truncated tool history, not source evidence: " + canonical(summary).decode()}] if groups[:-1] else []) + [m for m in last_group if m not in objectives] + [m for m in trailing if m not in objectives]
    if len(canonical(compacted)) > limit: raise ValueError("retained objectives and last complete exchange exceed context limit")
    return compacted, summary


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Inspect agent session events or enqueue an authenticated control")
    sub = parser.add_subparsers(dest="command", required=True)
    events = sub.add_parser("events", help="read bounded events after a sequence cursor")
    events.add_argument("output", type=Path); events.add_argument("--after", type=int, default=0); events.add_argument("--limit", type=int, default=100)
    control = sub.add_parser("control", help="enqueue a control using AGENT_SESSION_CONTROL_TOKEN")
    control.add_argument("output", type=Path); control.add_argument("kind", choices=sorted(_CONTROL_KINDS)); control.add_argument("--text"); control.add_argument("--request-id")
    args = parser.parse_args(argv)
    try:
        token = os.environ.get('AGENT_SESSION_CONTROL_TOKEN')
        if not token:
            raise ValueError('AGENT_SESSION_CONTROL_TOKEN is required')
        if args.command == 'events':
            result = read_events(args.output, token, args.after, args.limit)
        else:
            result = enqueue_control(args.output, token, args.kind, args.text, args.request_id)
        sys.stdout.buffer.write(canonical(result) + b"\n")
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {str(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
