#!/usr/bin/env python3
"""Stage and externally validate narrow multi-file application candidates.

This orchestrator does not execute candidate code or provide a sandbox. Adapter
commands are trusted operator-owned argv vectors and must provide any isolation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

MAX_REQUEST_BYTES = 1_048_576
MAX_CANDIDATE_BYTES = 4_194_304
MAX_FILE_BYTES = 1_048_576
MAX_FILES = 128
MAX_REFERENCES = 128
MAX_REPORT_BYTES = 1_048_576
MAX_STDOUT_BYTES = 4_194_304
MAX_STDERR_BYTES = 262_144
COMMAND_TIMEOUT = 120
MAX_PATH_LENGTH = 240
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
STOP_REQUESTED = False


class Refused(ValueError):
    """Input or adapter violated the narrow application contract."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path, limit: int = MAX_REQUEST_BYTES) -> Any:
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as exc:
        raise Refused(f"could not read JSON input: {exc}") from exc
    if len(data) > limit:
        raise Refused(f"JSON input exceeds {limit} bytes")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refused(f"invalid UTF-8 JSON: {exc}") from exc


def _validate_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_LENGTH:
        raise Refused("file path must be a nonempty bounded string")
    if "\x00" in raw or "\\" in raw:
        raise Refused(f"path must use relative POSIX separators: {raw!r}")
    posix = PurePosixPath(raw)
    win = PureWindowsPath(raw)
    if posix.is_absolute() or win.is_absolute() or win.drive or raw.startswith("/"):
        raise Refused(f"absolute or drive-qualified path refused: {raw!r}")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise Refused(f"empty, dot, and traversal path components are refused: {raw!r}")
    for part in parts:
        if part.endswith((".", " ")) or any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part):
            raise Refused(f"Windows-incompatible path component: {part!r}")
        stem = part.split(".", 1)[0].upper()
        if stem in RESERVED:
            raise Refused(f"Windows reserved path component: {part!r}")
    if parts[0].casefold() in {"manifest.json", "validation.json", "report.json"}:
        raise Refused(f"candidate path is reserved by staging protocol: {raw!r}")
    return raw


def validate_candidate(candidate: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not isinstance(candidate, dict) or set(candidate) != {"schema", "goal", "files", "references"}:
        raise Refused("candidate must contain exactly schema, goal, files, references")
    if type(candidate["schema"]) is not int or candidate["schema"] != 1:
        raise Refused("candidate schema must be integer 1")
    goal = candidate["goal"]
    if not isinstance(goal, str) or not goal.strip() or len(goal.encode("utf-8")) > 16_384:
        raise Refused("goal must be a nonempty string of at most 16384 UTF-8 bytes")
    files = candidate["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise Refused(f"files must be an array with 1..{MAX_FILES} entries")
    normalized: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    total = 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "content"}:
            raise Refused("each file must contain exactly path and content")
        path = _validate_path(entry["path"])
        key = path.casefold()
        if key in folded:
            raise Refused(f"case-insensitive path collision: {folded[key]!r} and {path!r}")
        if any(key.startswith(existing + "/") or existing.startswith(key + "/") for existing in folded):
            raise Refused(f"case-insensitive file/directory path collision at {path!r}")
        folded[key] = path
        if not isinstance(entry["content"], str):
            raise Refused(f"content for {path!r} must be a UTF-8 string")
        data = entry["content"].encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise Refused(f"file {path!r} exceeds {MAX_FILE_BYTES} bytes")
        total += len(data)
        if total > MAX_CANDIDATE_BYTES:
            raise Refused(f"candidate exceeds {MAX_CANDIDATE_BYTES} aggregate file bytes")
        normalized[path] = data
    refs = candidate["references"]
    if not isinstance(refs, list) or len(refs) > MAX_REFERENCES:
        raise Refused(f"references must be an array of at most {MAX_REFERENCES} package IDs")
    clean_refs: list[str] = []
    seen_refs: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}", ref):
            raise Refused("reference must be a bounded package ID, not a path or URL")
        if ref.casefold() in seen_refs:
            raise Refused(f"duplicate reference package ID: {ref}")
        seen_refs.add(ref.casefold())
        clean_refs.append(ref)
    clean = {"schema": 1, "goal": goal, "files": [{"path": p, "content": normalized[p].decode("utf-8")} for p in sorted(normalized)], "references": clean_refs}
    return clean, normalized


def _check_absent_directory(path: Path) -> Path:
    resolved_parent = path.parent.resolve(strict=True)
    destination = resolved_parent / path.name
    if destination.exists() or destination.is_symlink():
        raise Refused(f"output directory must be absent: {destination}")
    if destination.name in ("", ".", ".."):
        raise Refused("invalid output directory")
    if destination.name.casefold() in {"manifest.json", "validation.json", "report.json"}:
        raise Refused("output directory name is reserved by the staging protocol")
    return destination


def stage_candidate(candidate: Any, output: Path) -> dict[str, Any]:
    clean, contents = validate_candidate(candidate)
    destination = _check_absent_directory(output)
    records = [{"path": p, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for p, data in sorted(contents.items())]
    canonical = {"schema": 1, "goal": clean["goal"], "references": clean["references"], "files": records}
    candidate_digest = hashlib.sha256(_json_bytes(canonical)).hexdigest()
    manifest = {**canonical, "candidate_sha256": candidate_digest, "workspace": str(destination)}
    # Build a private sibling and rename after every file and manifest is written.
    temp = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=destination.parent))
    try:
        for rel, data in contents.items():
            target = temp.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (temp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
        # Candidate source is a public runtime input, never a secret store. mkdtemp
        # starts at 0700; publish explicit read/traverse modes independent of umask
        # so the container's UID 65534 can read the identical hashed revision.
        for node in temp.rglob("*"):
            node.chmod(0o755 if node.is_dir() else 0o644)
        temp.chmod(0o755)
        if destination.exists() or destination.is_symlink():
            raise Refused(f"output directory appeared during staging: {destination}")
        temp.rename(destination)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return {"status": "STAGED", "directory": str(destination), "candidate_sha256": candidate_digest, "files": records, "manifest": str(destination / "manifest.json")}

def _safe_argv(raw: str | None, label: str) -> list[str] | None:
    if raw is None:
        return None
    try:
        argv = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refused(f"{label} must be JSON argv: {exc}") from exc
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\x00" in x for x in argv):
        raise Refused(f"{label} must be a nonempty JSON array of nonempty strings")
    return argv


def _terminate_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _run_json_command(argv: list[str], payload: dict[str, Any], timeout: int = COMMAND_TIMEOUT, *, should_stop=None) -> Any:
    data = _json_bytes(payload)
    started = time.monotonic()
    try:
        with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            stdin.write(data)
            stdin.seek(0)
            proc = subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr, shell=False, start_new_session=os.name != "nt")
            try:
                while True:
                    if STOP_REQUESTED or (should_stop is not None and should_stop()):
                        raise Refused("operator STOP or adapter cancellation received during adapter execution")
                    if os.fstat(stdout.fileno()).st_size > MAX_STDOUT_BYTES or os.fstat(stderr.fileno()).st_size > MAX_STDERR_BYTES:
                        raise Refused("adapter output exceeded bounded stdout/stderr limit")
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise Refused(f"adapter deadline exceeded ({timeout}s)")
                    try:
                        proc.wait(timeout=min(0.05, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                stdout.seek(0)
                stderr.seek(0)
                out, err = stdout.read(MAX_STDOUT_BYTES + 1), stderr.read(MAX_STDERR_BYTES + 1)
                if len(out) > MAX_STDOUT_BYTES or len(err) > MAX_STDERR_BYTES:
                    raise Refused("adapter output exceeded bounded stdout/stderr limit")
                if proc.returncode != 0:
                    raise Refused(f"adapter exited {proc.returncode}: {err.decode('utf-8', 'replace')[:2000]}")
                if STOP_REQUESTED or (should_stop is not None and should_stop()):
                    raise Refused("operator STOP or adapter cancellation received before adapter acceptance")
                try:
                    return json.loads(out.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise Refused(f"adapter stdout was not one UTF-8 JSON value: {exc}") from exc
            finally:
                _terminate_process(proc)
    except OSError as exc:
        raise Refused(f"adapter could not be started: {exc}") from exc


def _candidate_payload(candidate: Any) -> bytes:
    raw = _json_bytes(candidate)
    if len(raw) > MAX_CANDIDATE_BYTES:
        raise Refused("candidate JSON exceeds bounded input size")
    return raw


def _request_payload(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != {"schema", "goal", "references"}:
        raise Refused("request must contain exactly schema, goal, references")
    if type(request["schema"]) is not int or request["schema"] != 1:
        raise Refused("request schema must be integer 1")
    goal = request["goal"]
    if not isinstance(goal, str) or not goal.strip() or len(goal.encode("utf-8")) > 16_384:
        raise Refused("request goal must be nonempty and bounded")
    refs = request["references"]
    if not isinstance(refs, list) or len(refs) > MAX_REFERENCES:
        raise Refused("request references must be a bounded package-ID array")
    # Reuse candidate rules to validate reference identity without accepting paths.
    validate_candidate({"schema": 1, "goal": goal, "references": refs, "files": [{"path": "placeholder.txt", "content": ""}]})
    return {"schema": 1, "goal": goal, "references": refs}


def _bounded_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Refused("validation report must be a JSON object")
    raw = _json_bytes(value)
    if len(raw) > MAX_REPORT_BYTES:
        raise Refused("validation report exceeds byte limit")
    return value


def _adapter_report(value: Any, candidate_digest: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema", "candidate_sha256", "build_pass", "test_pass", "observations", "limitations"}:
        raise Refused("builder must return exactly the bound schema-1 report fields")
    if type(value["schema"]) is not int or value["schema"] != 1 or value["candidate_sha256"] != candidate_digest:
        raise Refused("builder report schema or candidate digest does not bind to this candidate")
    if type(value["build_pass"]) is not bool or type(value["test_pass"]) is not bool:
        raise Refused("builder build_pass and test_pass must be booleans")
    for key in ("observations", "limitations"):
        if not isinstance(value[key], list) or len(value[key]) > 128 or any(not isinstance(x, str) or len(x.encode("utf-8")) > 8192 for x in value[key]):
            raise Refused(f"builder {key} must be a bounded list of strings")
    _bounded_report(value)
    return value


def _stop_handler(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def _resolve_references(reference_store: Path, package_ids: list[str]) -> list[dict[str, Any]]:
    try:
        from references import _load_package
    except ImportError as exc:
        raise Refused(f"reference loader unavailable: {exc}") from exc
    resolved = []
    try:
        for package_id in package_ids:
            package = _load_package(reference_store, package_id)
            resolved.append({"package_id": package_id, "origin": package["origin"], "license": package["license"], "kind": package["kind"], "limitations": package["limitations"], "files": package["files"], "upstream_evidence": package.get("upstream_evidence", [])})
    except (KeyError, OSError, ValueError, TypeError) as exc:
        raise Refused(f"reference package missing or integrity check failed: {exc}") from exc
    if len(_json_bytes(resolved)) > MAX_CANDIDATE_BYTES:
        raise Refused("resolved reference material exceeds bounded payload size")
    return resolved


def _verify_staged_files(workspace: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["files"]:
        current = workspace
        for part in entry["path"].split("/"):
            current = current / part
            if current.is_symlink():
                raise Refused(f"staged path traverses a symlink: {entry['path']}")
        if not current.is_file():
            raise Refused(f"staged file missing: {entry['path']}")
        with current.open("rb") as stream:
            data = stream.read(entry["size"] + 1)
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise Refused(f"staged file changed during validation: {entry['path']}")


def cycle(request: Any, proposer: list[str], builder: list[str], output: Path, rounds: int, trusted: bool, reference_store: Path, resume: bool = False, max_new_rounds: int | None = None) -> dict[str, Any]:
    global STOP_REQUESTED
    from application_checkpoint import ExclusiveLock, ResumeRefused, configuration, load, save, regular_chain
    req = _request_payload(request)
    if not trusted: raise Refused("--trusted-adapter required; operator adapter provides any isolation")
    if not 1 <= rounds <= 8: raise Refused("rounds must be between 1 and 8")
    if max_new_rounds is not None and max_new_rounds < 1: raise Refused("--max-new-rounds must be positive")
    references = _resolve_references(reference_store, req["references"])
    regular_chain(output)
    if resume:
        if output.is_symlink() or not output.is_dir(): raise Refused("resume output must be an existing regular directory")
        destination = output.resolve()
    else:
        destination = _check_absent_directory(output); destination.mkdir()
    checkpoint = destination / "cycle-state.json"
    identity = configuration(req, proposer, builder, rounds, references, reference_store, [Path(__file__).resolve(), Path(__file__).with_name("references.py").resolve(), Path(__file__).with_name("application_checkpoint.py").resolve()])
    STOP_REQUESTED = False
    old_int = signal.signal(signal.SIGINT, _stop_handler); old_term = signal.signal(signal.SIGTERM, _stop_handler)
    try:
        with ExclusiveLock(destination / ".cycle.lock"):
            if resume:
                state = load(checkpoint)
                if state["config"] != identity or state["rounds_requested"] != rounds:
                    raise ResumeRefused("resume configuration or reference/source identity changed")
                if state["output_identity"] != str(destination): raise ResumeRefused("checkpoint output identity mismatch")
                history = state["history"]
                if len(history) != state["next_round"] - 1 or [row.get("round") for row in history] != list(range(1, len(history) + 1)):
                    raise ResumeRefused("checkpoint history is incomplete or malformed")
                for row in history:
                    if not isinstance(row, dict) or set(row) - {"round", "previous_feedback", "previous_candidate", "validation", "adapter_report", "status"}:
                        raise ResumeRefused("checkpoint history row malformed")
                    if not isinstance(row.get("validation"), dict) or row.get("status") not in ("PASS", "FAIL", "HOLD", "INVALID") or row["validation"].get("status") not in ("VALIDATED", "INVALID"):
                        raise ResumeRefused("checkpoint history status malformed")
                    validation = row.get("validation", {})
                    stage_info = validation.get("stage") if isinstance(validation, dict) else None
                    if stage_info:
                        stage_path = Path(stage_info.get("directory", ""))
                        manifest_path = Path(stage_info.get("manifest", ""))
                        if stage_path.resolve().parent != destination or stage_path.is_symlink() or manifest_path.resolve() != (stage_path / "manifest.json").resolve():
                            raise ResumeRefused("history contains an escaped staged path")
                        manifest = _read_json(manifest_path)
                        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list): raise ResumeRefused("staged manifest malformed")
                        manifest_core = {key: manifest.get(key) for key in ("schema", "goal", "references", "files")}
                        if manifest.get("candidate_sha256") != hashlib.sha256(_json_bytes(manifest_core)).hexdigest() or manifest.get("workspace") != str(stage_path) or manifest.get('candidate_sha256') != validation.get('candidate_sha256'):
                            raise ResumeRefused("staged manifest identity mismatch")
                        for entry in manifest["files"]:
                            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"} or _validate_path(entry.get("path")) != entry.get("path"):
                                raise ResumeRefused("staged manifest path malformed")
                        regular_chain(stage_path)
                        regular_chain(manifest_path)
                        _verify_staged_files(stage_path, manifest)
                previous, prior_candidate = state["previous"], state["prior_candidate"]
                status = state["status"]
            else:
                history, previous, status = [], {"status": "NOT_VALIDATED", "issues": []}, "HOLD"
                prior_candidate = None
                state = {"schema": 1, "config": identity, "rounds_requested": rounds, "history": history, "previous": previous, "prior_candidate": prior_candidate, "next_round": 1, "inflight": None, "status": status, "output_identity": str(destination)}
                if state["next_round"] == 1 and (destination / "report.json").exists():
                    raise ResumeRefused("checkpoint contradicts existing report history")
                save(checkpoint, state)
            if status == "PASS":
                result = {"schema": 1, "status": "PASS", "scope": "adapter-reported build/test only; no runtime or deployment claim", "rounds_requested": rounds, "rounds_completed": len(history), "history": history, "resume": "validated checkpoint; no external invocation"}
                (destination / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                return result
            added = 0
            for n in range(state["next_round"], rounds + 1):
                if STOP_REQUESTED: break
                if max_new_rounds is not None and added >= max_new_rounds: break
                state["inflight"] = {"phase": "proposer", "round": n}; save(checkpoint, state)
                candidate = _run_json_command(proposer, {"schema": 1, "requirement": req, "references": references, "previous_validation": previous, "previous_candidate": prior_candidate})
                try:
                    clean, _ = validate_candidate(candidate)
                    if clean["goal"] != req["goal"] or clean["references"] != req["references"]: raise Refused("candidate goal/references differ from request")
                    prior_candidate = clean
                    revision = destination / f"revision-{n:02d}-{hashlib.sha256(_json_bytes(clean)).hexdigest()[:12]}"
                    stage = stage_candidate(clean, revision); digest = stage["candidate_sha256"]
                    validation = {"status": "VALIDATED", "candidate_sha256": digest, "issues": [], "stage": stage}
                except Refused as exc:
                    validation = {"status": "INVALID", "issues": [str(exc)]}; digest = None
                    revision = destination / f"revision-{n:02d}-invalid"; revision.mkdir(exist_ok=False)
                    (revision / "validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                row = {"round": n, "validation": validation}
                if validation["status"] == "VALIDATED" and not STOP_REQUESTED:
                    manifest = _read_json(Path(stage["manifest"]))
                    state.update(previous=previous, prior_candidate=prior_candidate, inflight={"phase": "builder", "round": n}); save(checkpoint, state)
                    adapter_raw = _run_json_command(builder, {"schema": 1, "workspace_manifest": manifest, "candidate_sha256": digest})
                    adapter = _adapter_report(adapter_raw, digest)
                    _verify_staged_files(Path(stage["directory"]), manifest)
                    if STOP_REQUESTED: raise Refused("STOP received before acceptance")
                    row["adapter_report"] = adapter; row["status"] = "PASS" if adapter["build_pass"] and adapter["test_pass"] else "FAIL"
                    status = row["status"]; previous = {"status": row["status"], "issues": adapter["observations"], "limitations": adapter["limitations"]}
                    (revision / "validation.json").write_text(json.dumps({"validation": validation, "adapter_report": row["adapter_report"], "status": row["status"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                else: row["status"] = "HOLD" if STOP_REQUESTED else "INVALID"
                if validation['status']=='INVALID': previous = {'status':'INVALID','issues':validation['issues']}
                history.append(row); status = row["status"]; added += 1
                state.update(history=history, previous=previous, prior_candidate=prior_candidate, next_round=n + 1, inflight=None, status=status)
                save(checkpoint, state)
                if status == "PASS": break
            if STOP_REQUESTED: status = "HOLD"; state["status"] = status; save(checkpoint, state)
    except (Refused, ResumeRefused, OSError, ValueError) as exc:
        if 'state' in locals() and not resume:
            failure = {"schema":1,"status":"HOLD","reason":str(exc),"rounds_completed":len(history),"history":history,"ambiguous_external_call":state.get('inflight')}
            (destination/'report.json').write_text(json.dumps(failure,ensure_ascii=True,indent=2)+'\n',encoding='utf-8')
        raise
    finally:
        signal.signal(signal.SIGINT, old_int); signal.signal(signal.SIGTERM, old_term)
    paused = status not in ("PASS",) and len(history) < rounds and not STOP_REQUESTED
    result = {"schema": 1, "status": status if not paused else "HOLD", "scope": "adapter-reported build/test only; no runtime or deployment claim", "rounds_requested": rounds, "rounds_completed": len(history), "history": history, "paused": paused, "history_regenerated_from_checkpoint": bool(resume)}
    (destination / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    stage = sub.add_parser("stage", help="validate and stage one candidate into an absent directory")
    stage.add_argument("--candidate", type=Path, required=True)
    stage.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("cycle", help="run bounded proposal and trusted-adapter validation cycles")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--proposer-command", required=True, help="operator-owned argv encoded as JSON")
    run.add_argument("--builder-command", required=True, help="trusted operator-owned argv encoded as JSON")
    run.add_argument("--trusted-adapter", action="store_true", help="acknowledge adapter command executes host code and provides any required isolation")
    run.add_argument("--rounds", type=int, choices=range(1, 9), default=1)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--reference-store", type=Path, required=True, help="operator-managed package reference store")
    run.add_argument("--resume", action="store_true", help="resume an existing output using its verified checkpoint")
    run.add_argument("--max-new-rounds", type=int, help="pause after at most this many newly committed rounds")
    args = parser.parse_args()
    try:
        if args.mode == "stage":
            result = stage_candidate(_read_json(args.candidate, MAX_CANDIDATE_BYTES), args.output)
        else:
            proposer = _safe_argv(args.proposer_command, "proposer-command")
            builder = _safe_argv(args.builder_command, "builder-command")
            result = cycle(_read_json(args.request), proposer, builder, args.output, args.rounds, args.trusted_adapter, args.reference_store, args.resume, args.max_new_rounds)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if result.get("status") in ("STAGED", "PASS") else 2
    except (Refused, OSError, ValueError) as exc:
        print(json.dumps({"status": "HOLD", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
