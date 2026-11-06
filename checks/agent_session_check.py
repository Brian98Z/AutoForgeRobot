"""Focused filesystem checks for agent_session durability and control contracts."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
import tempfile
import threading
import time
from pathlib import Path

import agent_session as session
from application_checkpoint import ResumeRefused, canonical


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    with tempfile.TemporaryDirectory(prefix="agent-session-check-") as tmp:
        root = Path(tmp)
        output = root / "run"
        output.mkdir()

        # Digest envelope round-trip, duplicate-key rejection, and tamper rejection.
        state = {"session_id": "s-1", "candidate": {"schema": 1, "goal": "goal", "files": [], "references": []}, "history": []}
        session.save(output, state)
        check(session.load(output) == state, "session state round-trip failed")
        check(not (output / "candidate.json").exists(), "empty candidate should not create a candidate mirror")
        (output / "session.json").write_bytes(b'{"schema":1,"schema":1,"state":{},"sha256":"x"}')
        try:
            session.load(output)
            raise AssertionError("duplicate key accepted")
        except ResumeRefused:
            pass
        filled = {"schema": 1, "goal": "goal", "files": [{"path": "main.py", "content": "print('ok')"}], "references": []}
        session.save(output, {"session_id": "s-1", "candidate": filled})
        check(session.load(output)["candidate"] == filled, "candidate snapshot should remain authoritative")
        session.save(output, state)
        envelope = json.loads((output / "session.json").read_text(encoding="utf-8"))
        interrupted = {"schema": 1, "goal": "goal", "files": [{"path": "main.py", "content": "print('new')"}], "references": []}
        import subprocess, sys
        crash_script = """import pathlib, sys, agent_session
out=pathlib.Path(sys.argv[1]); candidate={\"schema\":1,\"goal\":\"goal\",\"files\":[{\"path\":\"main.py\",\"content\":\"print('new')\"}],\"references\":[]}
original=agent_session._atomic
def crash(path,data):
    if pathlib.Path(path).name == 'candidate.json': raise OSError('simulated process crash after session commit')
    return original(path,data)
agent_session._atomic=crash
agent_session.save(out,{\"session_id\":\"s-1\",\"candidate\":candidate})
"""
        proc = subprocess.run([sys.executable, "-c", crash_script, str(output)], cwd=str(Path(__file__).resolve().parents[1] / "autoforge"), capture_output=True, text=True)
        check(proc.returncode != 0, "crash subprocess should stop before candidate mirror replacement")
        check(session.load(output)["candidate"] == interrupted, "pending authenticated candidate mirror was not recovered after process exit")
        check(json.loads((output / "candidate.json").read_text(encoding="utf-8")) == interrupted, "recovered candidate mirror mismatch")
        envelope["state"]["session_id"] = "tampered"
        (output / "session.json").write_text(json.dumps(envelope), encoding="utf-8")
        try:
            session.load(output)
            raise AssertionError("tampered digest accepted")
        except ResumeRefused:
            pass

        # Journal cursor, monotonic sequence, bounded reads, durable resume,
        # and only partial final-line recovery.
        with session.EventJournal(output) as journal:
            for i in range(5): journal.append({"type": "event", "value": i})
            check([e["seq"] for e in journal.read_after(2, 2)] == [3, 4], "cursor pagination wrong")
            check(journal.read_after(5) == [], "end cursor should return no events")
        with (output / "events.jsonl").open("ab") as stream:
            stream.write(b'{"partial":')
        with session.EventJournal(output, resume=True) as journal:
            check(journal.sequence == 5, "partial tail recovery lost complete events")
            journal.append({"type": "resumed"})
        raw = (output / "events.jsonl").read_bytes()
        resumed = json.loads(raw.splitlines()[-1])
        check(resumed["seq"] == 6 and resumed["type"] == "resumed", "resumed event was not durably sequenced")
        with (output / "events.jsonl").open("ab") as stream:
            stream.write(b'{"seq":999,"type":"bad"}\n')
        try:
            with session.EventJournal(output, resume=True): pass
            raise AssertionError("corrupt complete event line accepted")
        except ResumeRefused:
            pass

        # Control authentication, expiry/session binding, deduplication and concurrent numbering.
        control_out = root / "controls"
        control_out.mkdir()
        token = session.create_control(control_out, "session-A", 600)
        first = session.enqueue_control(control_out, token, "steering", "please refine", "req-1")
        again = session.enqueue_control(control_out, token, "steering", "please refine", "req-1")
        check(first == again and first["id"] == 1, "request idempotency failed")
        try:
            session.enqueue_control(control_out, "wrong-token", "stop")
            raise AssertionError("unauthenticated control accepted")
        except ResumeRefused:
            pass
        ids = []
        errors = []
        def enqueue(n):
            try:
                ids.append(session.enqueue_control(control_out, token, "follow_up", f"task {n}")["id"])
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=enqueue, args=(i,)) for i in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        check(not errors and sorted(ids) == list(range(2, 10)), "concurrent control numbering failed")
        controls = session.read_controls(control_out, after=0)
        check([x["id"] for x in controls] == list(range(1, 10)), "control reader ordering/authentication failed")
        # Authenticated record tampering must be detected.
        record_path = control_out / "control" / "00000000000000000001.json"
        record = json.loads(record_path.read_text(encoding="utf-8")); record["text"] = "changed"
        record_path.write_text(json.dumps(record), encoding="utf-8")
        try:
            session.read_controls(control_out)
            raise AssertionError("tampered control accepted")
        except ResumeRefused:
            pass

        # Compaction retains objectives/correction and complete last group, rejects orphan calls.
        old_group = [{"role": "assistant", "content": None, "tool_calls": [{"id": "old", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}, {"role": "tool", "tool_call_id": "old", "content": '{"error":"old failure"}' + ("x" * 5000)}]
        last_group = [{"role": "assistant", "content": None, "tool_calls": [{"id": "last", "type": "function", "arguments": "{}"}]}, {"role": "tool", "tool_call_id": "last", "content": '{"ok":true}'}]
        messages = [{"role": "system", "content": "objective"}, {"role": "user", "content": "initial task"}, *old_group, {"role": "user", "content": "correction"}, *last_group]
        compacted, summary = session.compact(messages, state["candidate"], limit=1000)
        check(summary is not None and any(x.get("content") == "objective" for x in compacted), "compaction lost objective or summary")
        check(any(x.get("content") == "correction" for x in compacted), "compaction lost correction")
        check(any(x.get("tool_call_id") == "last" for x in compacted), "compaction lost complete last tool group")
        try:
            session.compact([{ "role": "system", "content": "s" }, {"role": "user", "content": "u"}, old_group[0]], state["candidate"], limit=10000)
            raise AssertionError("orphan tool call accepted")
        except ValueError:
            pass
        try:
            session.compact(messages, state["candidate"], limit=1)
            raise AssertionError("over-budget retained minimum accepted")
        except ValueError:
            pass
    print("agent session filesystem checks passed")


if __name__ == "__main__":
    main()
