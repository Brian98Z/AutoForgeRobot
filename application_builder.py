#!/usr/bin/env python3
"""Run a staged Python JSON-lines application in a locked-down local container."""
from __future__ import annotations

import argparse
import hashlib
import stat
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
from typing import Any

MAX_INPUT = 4_194_304
MAX_REPORT = 1_048_576
MAX_OUT = 1_048_576
MAX_ERR = 262_144
MAX_SCENARIOS = 8
MAX_REQUESTS = 32
MAX_LINE = 1_048_576
STOP_REQUESTED = False

class MissingPrerequisite(Exception):
    pass


class BuildFailure(Exception):
    pass


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

def _stop_handler(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result: raise BuildFailure("duplicate JSON object key")
        result[key] = value
    return result


def _strict_loads(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object, parse_constant=lambda _value: (_ for _ in ()).throw(BuildFailure("invalid JSON constant")))


def _load_json(path: Path, limit: int) -> Any:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise BuildFailure("input exceeds byte limit")
    try:
        return _strict_loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildFailure(f"invalid UTF-8 JSON: {exc}") from exc


def _is_reparse(path: Path) -> bool:
    try:
        attributes = os.lstat(path).st_file_attributes
    except AttributeError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _manifest(raw: Any) -> tuple[Path, str]:
    if not isinstance(raw, dict) or set(raw) != {"schema", "goal", "references", "files", "candidate_sha256", "workspace"}:
        raise BuildFailure("invalid workspace manifest shape")
    if type(raw["schema"]) is not int or raw["schema"] != 1:
        raise BuildFailure("unsupported workspace manifest schema")
    if not isinstance(raw["workspace"], str) or not isinstance(raw["candidate_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", raw["candidate_sha256"]):
        raise BuildFailure("invalid workspace or candidate digest")
    raw_root = Path(raw["workspace"])
    absolute = Path(os.path.abspath(raw_root))
    for ancestor in (absolute, *absolute.parents):
        if ancestor.is_symlink() or _is_reparse(ancestor):
            raise BuildFailure("workspace path contains a symlink or reparse point")
    root = raw_root.resolve(strict=True)
    if not root.is_dir():
        raise BuildFailure("workspace must be a real directory")
    allowed_files = {"manifest.json"}
    for node in root.rglob("*"):
        if node.is_symlink() or _is_reparse(node) or not (node.is_file() or node.is_dir()):
            raise BuildFailure("workspace contains a symlink, reparse point, or special file")
        if node.is_file():
            allowed_files.add(node.relative_to(root).as_posix())
    if "," in str(root):
        raise BuildFailure("workspace path contains an unsupported mount-option delimiter")
    if not isinstance(raw["goal"], str) or not isinstance(raw["references"], list):
        raise BuildFailure("invalid manifest goal or references")
    records = raw["files"]
    if not isinstance(records, list) or len(records) > 128:
        raise BuildFailure("invalid manifest files")
    paths: set[str] = set()
    folded: set[str] = set()
    total = 0
    clean_records = []
    for row in records:
        if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
            raise BuildFailure("invalid manifest file record")
        rel, size, digest = row["path"], row["size"], row["sha256"]
        if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or "\\" in rel or any(x in ("", ".", "..") for x in rel.split("/")) or re.match(r"^[A-Za-z]:", rel):
            raise BuildFailure("unsafe manifest path")
        folded_path = rel.casefold()
        if rel in paths or folded_path in folded or type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise BuildFailure("invalid or duplicate/case-colliding manifest record")
        if any(existing.startswith(folded_path + "/") or folded_path.startswith(existing + "/") for existing in folded):
            raise BuildFailure("manifest path collides with a file or directory path")
        paths.add(rel); folded.add(folded_path); total += size
        if total > MAX_INPUT:
            raise BuildFailure("candidate exceeds byte limit")
        target = root.joinpath(*rel.split("/"))
        cur = root
        for part in rel.split("/"):
            cur = cur / part
            if cur.is_symlink() or _is_reparse(cur):
                raise BuildFailure(f"link or reparse point in staged path: {rel}")
        if not target.is_file():
            raise BuildFailure(f"missing staged file: {rel}")
        with target.open("rb") as stream:
            data = stream.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise BuildFailure(f"staged content digest mismatch: {rel}")
        clean_records.append({"path": rel, "size": size, "sha256": digest})
    if paths != allowed_files - {"manifest.json"}:
        raise BuildFailure("workspace has unmanifested or missing candidate files")
    canonical = {"schema": 1, "goal": raw["goal"], "references": raw["references"], "files": clean_records}
    try:
        import applications
        actual = hashlib.sha256(applications._json_bytes(canonical)).hexdigest()
    except ImportError:
        actual = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if actual != raw["candidate_sha256"]:
        raise BuildFailure("candidate manifest digest mismatch")
    if "main.py" not in paths:
        raise BuildFailure("fixed entrypoint main.py is absent")
    return root, actual



def _contract(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or set(raw) != {"schema", "entrypoint", "scenarios"} or type(raw["schema"]) is not int or raw["schema"] != 1 or raw["entrypoint"] != "main.py":
        raise BuildFailure("contract must have schema 1 and fixed entrypoint main.py")
    scenarios = raw["scenarios"]
    if not isinstance(scenarios, list) or not scenarios or len(scenarios) > MAX_SCENARIOS:
        raise BuildFailure("contract scenarios must be a nonempty bounded list")
    total_request_bytes = 0
    names: set[str] = set()
    for sc in scenarios:
        if not isinstance(sc, dict) or set(sc) != {"name", "requests", "expected"}:
            raise BuildFailure("invalid scenario shape")
        name = sc["name"]
        if not isinstance(name, str) or not name or len(name) > 100 or name in names:
            raise BuildFailure("invalid or duplicate scenario name")
        names.add(name)
        req, exp = sc["requests"], sc["expected"]
        if not isinstance(req, list) or not req or len(req) > MAX_REQUESTS or not isinstance(exp, list) or len(exp) != len(req):
            raise BuildFailure(f"scenario {name}: requests and expected must be equal nonempty bounded arrays")
        if any(len(_json_bytes(x)) > MAX_LINE for x in req + exp):
            raise BuildFailure(f"scenario {name}: JSON line exceeds limit")
        total_request_bytes += sum(len(_json_bytes(request)) + 1 for request in req)
    if total_request_bytes > MAX_INPUT:
        raise BuildFailure("aggregate contract request bytes exceed input limit")
    try:
        _json_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise BuildFailure("contract contains a non-JSON value") from exc
    return scenarios

def _run(argv: list[str], timeout: int = 20) -> bytes:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False, env={"PATH": os.environ.get("PATH", "")}, close_fds=True)
    except FileNotFoundError as exc:
        raise MissingPrerequisite(f"container engine unavailable: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BuildFailure(f"engine command timed out: {argv[0]}") from exc
    if result.returncode:
        raise MissingPrerequisite(f"container engine prerequisite failed ({argv[0]}): {result.stderr[:4096].decode('utf-8', 'replace')}")
    if len(result.stdout) > MAX_OUT:
        raise BuildFailure("engine response exceeded byte limit")
    return result.stdout


def preflight(engine: str, image: str) -> dict[str, str]:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise BuildFailure("--image must be an exact sha256 digest/image ID")
    info = _run([engine, "info", "--format", "{{.OSType}}"], timeout=15).decode("utf-8", "replace").strip()
    if info != "linux":
        raise MissingPrerequisite(f"container engine server OS must be Linux (observed {info!r})")
    raw = _run([engine, "image", "inspect", "--format", "{{.Id}}", image], timeout=15).decode("ascii", "replace").strip()
    if raw != image:
        raise MissingPrerequisite("exact image ID is not available locally")
    return {"engine": engine, "server_os": "linux", "image_id": image}


def _container(engine: str, image: str, root: Path, payload: bytes, timeout: int) -> tuple[bytes, bytes, int, bool]:
    if len(payload) > MAX_INPUT:
        raise BuildFailure("container request payload exceeds byte limit")
    name = "autoforge-" + uuid.uuid4().hex
    cmd = [engine, "run", "--rm", "--name", name, "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=256m", "--cpus=1", "--mount", f"type=bind,src={root},dst=/candidate,readonly", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777", "--workdir", "/candidate", "--interactive", "--entrypoint", "python", image, "-I", "-c", f"import runpy,signal,sys;signal.alarm({timeout});sys.path.insert(0,'/candidate');runpy.run_path('/candidate/main.py',run_name='__main__')"]
    env = {"PATH": os.environ.get("PATH", "")}
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, close_fds=True, bufsize=0)
    out = bytearray(); err = bytearray(); overflow = [False]
    import threading
    def drain(stream: Any, target: bytearray, cap: int) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk: break
            if len(target) + len(chunk) > cap:
                overflow[0] = True
            if len(target) < cap: target.extend(chunk[:cap-len(target)])
    threads = [threading.Thread(target=drain, args=(proc.stdout, out, MAX_OUT), daemon=True), threading.Thread(target=drain, args=(proc.stderr, err, MAX_ERR), daemon=True)]
    for t in threads: t.start()
    timed_out = False
    try:
        deadline = time.monotonic() + timeout
        write_errors: list[str] = []
        def send_input() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(payload); proc.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                write_errors.append(type(exc).__name__)
        writer = threading.Thread(target=send_input, daemon=True)
        writer.start()
        while proc.poll() is None and time.monotonic() < deadline and not STOP_REQUESTED and not overflow[0]:
            time.sleep(0.1)
        if STOP_REQUESTED or overflow[0]:
            timed_out = True
            subprocess.run([engine, "kill", name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False, env=env, close_fds=True)
            proc.kill(); proc.wait(timeout=5)
        elif proc.poll() is None:
            timed_out = True
            subprocess.run([engine, "kill", name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False, env=env, close_fds=True)
            proc.kill(); proc.wait(timeout=5)
        else:
            proc.wait()
        writer.join(timeout=1)
        for t in threads: t.join(timeout=1)
        if writer.is_alive() or any(t.is_alive() for t in threads) or write_errors:timed_out = True
    finally:
        try:
            if proc.poll() is None:
                subprocess.run([engine, "kill", name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False, env=env, close_fds=True)
                proc.kill()
                proc.wait(timeout=5)
        finally:
            subprocess.run([engine, "rm", "-f", name], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False, env=env, close_fds=True)
    return bytes(out), bytes(err), proc.returncode or 0, timed_out or overflow[0]


def build(manifest: Any, contract_raw: Any, engine: str, image: str, preflight_info: dict[str, str]) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "workspace_manifest", "candidate_sha256"} or type(manifest["schema"]) is not int or manifest["schema"] != 1:
        raise BuildFailure("adapter input must be the exact schema-1 workspace_manifest envelope")
    if not isinstance(manifest["workspace_manifest"], dict) or manifest["candidate_sha256"] != manifest["workspace_manifest"].get("candidate_sha256"):
        raise BuildFailure("adapter envelope candidate digest mismatch")
    root, digest = _manifest(manifest["workspace_manifest"])
    scenarios = _contract(contract_raw)
    info = preflight_info
    observations: list[str] = [f"Preflight confirmed Linux engine and locally present image {info['image_id']}."]
    limitations = ["Only the declared JSON-lines scenarios are checked; no deployment or general business-correctness proof.", "Requires a trusted local image and Linux container engine. Host timeout/cleanup is the enforcement path; candidate Python alarm is cooperative and may be disabled. Abrupt host/controller termination is not a containment guarantee.", "No host credentials are passed. Image-baked environment and contents remain operator-trusted."]
    build_pass = True
    test_pass = True
    deadline = time.monotonic() + 55
    for scenario in scenarios:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            build_pass = False; test_pass = False; observations.append("Cumulative container execution budget exhausted before remaining scenarios."); break
        if STOP_REQUESTED:
            build_pass = False; test_pass = False; observations.append("STOP received before all contract scenarios completed."); break
        timeout_seconds = min(8, int(remaining))
        try:
            lines = b"".join(_json_bytes(req) + b"\n" for req in scenario["requests"])
        except (TypeError, ValueError) as exc:
            raise BuildFailure("contract contains invalid JSON values") from exc
        if len(lines) > MAX_INPUT:
            build_pass = False; test_pass = False; observations.append(f"{scenario['name']}: request payload exceeded limit."); break
        out, err, code, timeout = _container(engine, image, root, lines, timeout_seconds)
        if timeout:
            build_pass = False; test_pass = False; observations.append(f"{scenario['name']}: container timed out or exceeded bounded output."); continue
        if code != 0:
            build_pass = False; test_pass = False; observations.append(f"{scenario['name']}: container exited with status {code}."); continue
        if len(out) > MAX_OUT:
            build_pass = False; test_pass = False; observations.append(f"{scenario['name']}: stdout exceeded limit."); continue
        try:
            raw_lines = out.decode("utf-8", "strict").splitlines()
            actual = [json.loads(line, object_pairs_hook=_strict_object, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonstandard JSON"))) for line in raw_lines]
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, BuildFailure):
            build_pass = False; test_pass = False; observations.append(f"{scenario['name']}: invalid JSON-lines output; details omitted."); continue
        if len(actual) != len(scenario["expected"]):
            build_pass = False; test_pass = False; observations.append(f"{scenario['name']}: response count mismatch; contract responses were not exposed."); continue
        try:
            mismatches = [i for i, (got, expected) in enumerate(zip(actual, scenario["expected"])) if _json_bytes(got) != _json_bytes(expected)]
        except (TypeError, ValueError):
            build_pass = False; test_pass = False; observations.append(f"{scenario['name']}: nonfinite or invalid JSON response."); continue
        if mismatches:
            build_pass = False; test_pass = False; observations.append(f"{scenario['name']}: exact response mismatch at request indexes {mismatches[:32]}.")
        else:
            observations.append(f"{scenario['name']}: matched all {len(actual)} independently checked responses.")
    if STOP_REQUESTED:
        build_pass = False; test_pass = False
    return {"schema": 1, "candidate_sha256": digest, "build_pass": build_pass, "test_pass": test_pass, "observations": observations, "limitations": limitations}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    try:
        signal.signal(signal.SIGINT, _stop_handler)
        signal.signal(signal.SIGTERM, _stop_handler)
        info = preflight(args.engine, args.image)
        if args.probe:
            sys.stdout.buffer.write(json.dumps({"status": "READY", **info}, sort_keys=True, ensure_ascii=True).encode("ascii") + b"\n"); return 0
        if args.contract is None: raise BuildFailure("--contract is required unless --probe is used")
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(data) > MAX_INPUT: raise BuildFailure("manifest stdin exceeds byte limit")
        manifest = _strict_loads(data)
        report = build(manifest, _load_json(args.contract, MAX_INPUT), args.engine, args.image, info)
        encoded = _json_bytes(report)
        if len(encoded) > MAX_REPORT: raise BuildFailure("report exceeds byte limit")
        sys.stdout.buffer.write(encoded + b"\n"); return 0
    except MissingPrerequisite as exc:
        sys.stderr.buffer.write(json.dumps({"status": "BLOCKED", "kind": "MISSING_PREREQUISITE", "reason": str(exc)}, ensure_ascii=True).encode("ascii") + b"\n"); return 3
    except (BuildFailure, OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        sys.stderr.buffer.write(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=True).encode("ascii") + b"\n"); return 2


if __name__ == "__main__":
    raise SystemExit(main())
