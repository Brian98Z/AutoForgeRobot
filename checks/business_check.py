#!/usr/bin/env python3
"""Validate business contract parsing and optionally exercise a real local container engine."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "autoforge" / "application_builder.py"
FIXTURE = ROOT / "fixtures" / "business"
def require(ok: bool, message: str) -> None:
    if not ok:
        raise AssertionError(message)
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"))
    parser.add_argument("--image", help="exact locally present sha256 image ID")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    import application_builder as builder
    from applications import stage_candidate
    contract = builder._load_json(FIXTURE / 'contract.json', builder.MAX_INPUT)
    public_request = json.loads((FIXTURE / "request.json").read_text(encoding="utf-8"))
    require(public_request["schema"] == 1 and isinstance(public_request["goal"], str), "public business request is malformed")
    require(bool(builder._contract(contract)), "business contract rejected")
    def rejected(label: str, operation: object) -> None:
        try:
            operation()
        except (builder.BuildFailure, ValueError, json.JSONDecodeError):
            return
        raise AssertionError(label)
    rejected("duplicate JSON keys accepted", lambda: builder._strict_loads(b'{"a":1,"a":2}'))
    rejected("nonfinite JSON constant accepted", lambda: builder._strict_loads(b'{"a":NaN}'))
    rejected("duplicate contract key accepted", lambda: builder._contract(builder._strict_loads(b'{"schema":1,"entrypoint":"main.py","scenarios":[],"schema":1}')))
    rejected("wrong response count accepted", lambda: builder._contract({"schema": 1, "entrypoint": "main.py", "scenarios": [{"name": "x", "requests": [{}], "expected": []}]}))
    rejected("nonfinite expected value accepted", lambda: builder._contract({"schema": 1, "entrypoint": "main.py", "scenarios": [{"name": "nan", "requests": [{}], "expected": [float("nan")]}]}))
    rejected('aggregate request bytes accepted', lambda: builder._contract({'schema':1,'entrypoint':'main.py','scenarios':[{'name':'large','requests':['x'*900000]*5,'expected':[None]*5}]}))
    with tempfile.TemporaryDirectory(prefix="autoforge-business-") as td:
        base = Path(td)
        candidate = {"schema": 1, "goal": "中文库存应用验收", "references": [], "files": [{"path": "main.py", "content": (FIXTURE / "main.py").read_text(encoding="utf-8")}]} 
        traceback_probe = (b'Traceback (most recent call last):\n  File "/candidate/main.py", line 12, in main\n'
                           b'  File "/candidate/../oracle.py", line 5, in hidden\n'
                           b'ValueError: secret-request-value\n')
        probe_root = base / 'traceback-probe'; probe_root.mkdir()
        (probe_root / 'main.py').write_text('pass\n', encoding='utf-8')
        require(builder._failure_locations(traceback_probe, probe_root) == ['main.py:12', 'ValueError'], 'traceback leaked values or noncandidate paths')
        for error in (b'sqlite3.OperationalError: no such column: SECRET_COLUMN',
                      b'sqlite3.OperationalError: near "SECRET_SQL": syntax error',
                      b'TypeError: unsupported operand type(s) for +: SECRET_TYPES'):
            details = builder._failure_locations(error, probe_root)
            require(len(details) == 2 and 'SECRET' not in json.dumps(details), 'fixed diagnosis missing or private exception value exposed')
        require(builder._failure_locations(b'sqlite3.OperationalError: SECRET_UNKNOWN', probe_root) == ['sqlite3.OperationalError'], 'unknown exception text leaked')
        (probe_root / 'main.py').write_text('value = Optional[int]\n', encoding='utf-8')
        name_error = (b'Traceback (most recent call last):\n  File "/candidate/main.py", line 1, in <module>\n'
                      b'NameError: name SECRET_REQUEST_VALUE is not defined\n')
        source_details = builder._failure_locations(name_error, probe_root)
        require('Candidate source names at main.py:1: Optional, int' in source_details
                and 'SECRET_REQUEST_VALUE' not in json.dumps(source_details), 'name diagnosis did not stay source-bound')
        (probe_root / 'main.py').write_text('value = "SECRET_SOURCE_LITERAL"\n', encoding='utf-8')
        require(builder._failure_locations(name_error, probe_root) == ['main.py:1', 'NameError'],
                'candidate string literal exposed as diagnostic name')
        require(builder._failure_locations(name_error.replace(b'main.py', b'../oracle.py'), probe_root) == ['NameError'],
                'outside-candidate name diagnostic exposed')
        (probe_root / 'main.py').write_text('broken(\n', encoding='utf-8')
        require(builder._failure_locations(name_error, probe_root) == ['main.py:1', 'NameError'],
                'unparseable candidate prevented bounded failure feedback')
        staged = stage_candidate(candidate, base / "source")
        manifest = json.loads(Path(staged["manifest"]).read_text(encoding="utf-8"))
        envelope = {"schema": 1, "workspace_manifest": manifest, "candidate_sha256": staged["candidate_sha256"]}
        require(builder._manifest(manifest)[1] == staged["candidate_sha256"], "Chinese-goal staged manifest digest mismatch")
        tampered = dict(manifest); tampered["candidate_sha256"] = "0" * 64
        try:
            builder._manifest(tampered)
        except builder.BuildFailure:
            pass
        else:
            raise AssertionError("tampered candidate accepted")
        # Host oracle mocks only _container; this is NOT real runtime evidence.
        builder.STOP_REQUESTED = False
        test_contract = {"schema": 1, "entrypoint": "main.py", "scenarios": [
            {"name": "first", "requests": [{}], "expected": [{"ok": True}]},
            {"name": "second", "requests": [{}], "expected": [{"ok": True}]},
        ]}
        outputs = iter([(b'{"ok":false}\n', b"", 0, False), (b'{"ok":true}\n', b"", 0, False)])
        real_container = builder._container
        builder._container = lambda *_args, **_kwargs: next(outputs)
        try:
            report = builder.build(envelope, test_contract, "mock-engine", "sha256:" + "a" * 64, {"image_id": "sha256:" + "a" * 64})
        finally:
            builder._container = real_container
        require(not report["build_pass"] and not report["test_pass"], "later passing scenario overwrote prior failure")
        type_contract = {"schema": 1, "entrypoint": "main.py", "scenarios": [{"name": "types", "requests": [{}], "expected": [1]}]}
        builder._container = lambda *_args, **_kwargs: (b'"1"\n', b"", 0, False)
        try:
            typed = builder.build(envelope, type_contract, "mock-engine", "sha256:" + "a" * 64, {"image_id": "sha256:" + "a" * 64})
        finally:
            builder._container = real_container
        require(not typed["test_pass"], "response type mismatch was accepted")
        for raw in (b'1e999\n',b'{"ok":true,"ok":false}\n',b'NaN\n',b'1\n2\n'):
            builder._container = lambda *_a, raw=raw, **_k: (raw,b'',0,False)
            try:
                bad = builder.build(envelope,type_contract,'mock-engine','sha256:'+'a'*64,{'image_id':'sha256:'+'a'*64})
                require(not bad['test_pass'],'malformed or over-count output accepted')
            finally:
                builder._container = real_container
        expected_values = {json.dumps(v, ensure_ascii=False, sort_keys=True) for scenario in test_contract["scenarios"] for v in scenario["expected"]}
        require(not any(value in " ".join(report["observations"]) for value in expected_values), "observations exposed hidden expected responses")
        builder.STOP_REQUESTED = True
        stopped = builder.build(envelope, contract, "mock-engine", "sha256:" + "a" * 64, {"image_id": "sha256:" + "a" * 64})
        require(not stopped["build_pass"] and not stopped["test_pass"], "STOP did not fail the report")
        builder.STOP_REQUESTED = False
        print(json.dumps({"status": "PASS", "runtime": "MOCKED_TRANSPORT_NOT_REAL_RUNTIME", "checks": ["failure-is-sticky", "strict-json", "aggregate-byte-bound", "response-type-mismatch", "stop-fails", "expected-values-hidden"]}))
    for bad in (
        {**contract, "entrypoint": "other.py"},
        {**contract, "schema": 2},
        {**contract, "unexpected": True},
        {"schema": 1, "entrypoint": "main.py", "scenarios": [{"name": "x", "requests": [{}], "expected": []}]},
    ):
        try:
            builder._contract(bad)
        except builder.BuildFailure:
            pass
        else:
            raise AssertionError("malformed contract was accepted")
    engine = args.engine or ("docker" if shutil.which("docker") else "podman" if shutil.which("podman") else None)
    if engine is None or not args.image:
        print(json.dumps({"status": "BLOCKED", "host_checks": "PASS", "runtime": "NOT_TESTED", "reason": "No usable engine plus exact local image digest was supplied; candidate application was not executed on host."}))
        return 0
    with tempfile.TemporaryDirectory(prefix="autoforge-business-engine-") as td:
        workspace = Path(td) / "candidate"
        stage_candidate(candidate, workspace)
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        envelope = {"schema": 1, "workspace_manifest": manifest, "candidate_sha256": manifest["candidate_sha256"]}
        result = subprocess.run([sys.executable, str(BUILDER), "--engine", engine, "--image", args.image, "--contract", str(FIXTURE / "contract.json")], input=builder._json_bytes(envelope), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=100, check=False, cwd=ROOT, close_fds=True)
        if result.returncode == 3:
            print(json.dumps({"status": "BLOCKED", "host_checks": "PASS", "runtime": "NOT_TESTED", "reason": result.stderr.decode("utf-8", "replace").strip()})); return 0
        if result.returncode != 0:
            print(json.dumps({"status": "FAIL", "runtime": "TESTED", "reason": result.stderr.decode("utf-8", "replace").strip(), "report": result.stdout.decode("utf-8", "replace")})); return 1
        report = json.loads(result.stdout.decode("utf-8"))
        require(report["candidate_sha256"] == manifest["candidate_sha256"] and report["build_pass"] and report["test_pass"], "real container business contract failed")
        print(json.dumps({"status": "PASS", "runtime": "TESTED_IN_CONTAINER", "candidate_sha256": manifest["candidate_sha256"], "observations": report["observations"], "limitations": report["limitations"]}))
        return 0
if __name__ == "__main__":
    raise SystemExit(main())
