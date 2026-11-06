"""Safe source import and immutable candidate revision transactions.

Source trees are read-only inputs. Publishing only changes an atomic pointer in a
separate transaction directory; user work is never overwritten.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import re
import stat
import uuid
from typing import Any, Callable

import argparse
import applications

MAX_IMPORT_FILES = applications.MAX_FILES
MAX_IMPORT_BYTES = applications.MAX_CANDIDATE_BYTES
SKIP_DIRS: set[str] = set()
_SECRET_PATH = re.compile(r"(^|/)(\.env(?:\..*)?|[^/]*(?:secret|credential|private[-_]?key|id_rsa|id_ed25519)[^/]*)$", re.I)
_SECRET_VALUE = re.compile(
    rb"(?i:[\"']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)[\"']?\s*:\s*[\"'][^\"']{8,}[\"']|"
    rb"(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[^\s'\"]{8,})"
)
_JSON_SECRET_KEYS = {"password", "passwd", "pass", "api_key", "apikey", "access_token", "refresh_token", "client_secret", "secret", "token"}


def _json_has_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold()) if isinstance(key, str) else ""
            is_secret = normalized in {re.sub(r"[^a-z0-9]", "", name) for name in _JSON_SECRET_KEYS}
            if is_secret and item is not None and item != "" and item != [] and item != {}:
                return True
            if _json_has_credentials(item):
                return True
    elif isinstance(value, list):
        return any(_json_has_credentials(item) for item in value)
    return False


class ProjectRefused(ValueError):
    """Project import, conflict, or transaction cannot be completed safely."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular_chain(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for item in (absolute, *absolute.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ProjectRefused(f"path traverses a symlink or reparse point: {item}")


def _read_source(root: Path) -> tuple[dict[str, bytes], dict[str, Any]]:
    root = Path(os.path.abspath(root))
    _regular_chain(root)
    if not root.exists():
        empty_records: list[dict[str, Any]] = []
        baseline = {"root": str(root), "exists": False, "files": empty_records,
                    "tree_sha256": _digest(_json_bytes(empty_records))}
        return {}, baseline
    if not root.is_dir():
        raise ProjectRefused("source must be a directory")
    found: dict[str, bytes] = {}
    total = 0
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        here = Path(current)
        for name in list(dirs) + names:
            item = here / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ProjectRefused(f"source contains link/reparse point: {item.relative_to(root)}")
        dirs[:] = sorted(d for d in dirs if d.casefold() not in SKIP_DIRS)
        for name in sorted(names):
            item = here / name
            info = item.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            rel = item.relative_to(root).as_posix()
            try:
                rel = applications._validate_path(rel)
            except applications.Refused as exc:
                raise ProjectRefused(str(exc)) from exc
            if _SECRET_PATH.search(rel):
                raise ProjectRefused(f"secret-bearing path refused: {rel}")
            if info.st_size > applications.MAX_FILE_BYTES:
                raise ProjectRefused(f"source file exceeds byte limit: {rel}")
            with item.open("rb") as stream:
                data = stream.read(applications.MAX_FILE_BYTES + 1)
            if len(data) != info.st_size:
                raise ProjectRefused(f"source changed while being read: {rel}")
            if Path(rel).suffix.casefold() == ".json":
                try:
                    parsed_json = json.loads(data.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    parsed_json = None
                if parsed_json is not None and _json_has_credentials(parsed_json):
                    raise ProjectRefused(f"credential-bearing JSON property refused: {rel}")
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ProjectRefused(f"source file is not UTF-8 text: {rel}") from exc
            total += len(data)
            if total > MAX_IMPORT_BYTES or len(found) >= MAX_IMPORT_FILES:
                raise ProjectRefused("source exceeds candidate file or aggregate byte limit")
            found[rel] = data
    baseline = {"root": str(root), "exists": True, "files": [
        {"path": p, "size": len(found[p]), "sha256": _digest(found[p])} for p in sorted(found)
    ]}
    baseline["tree_sha256"] = _digest(_json_bytes(baseline["files"]))
    return found, baseline


def _candidate(goal: str, references: list[str], files: dict[str, bytes]) -> dict[str, Any]:
    candidate = {"schema": 1, "goal": goal, "references": references,
                 "files": [{"path": p, "content": files[p].decode("utf-8")} for p in sorted(files)]}
    if not files:
        applications._request_payload({"schema": 1, "goal": goal, "references": references})
        return candidate
    return applications.validate_candidate(candidate)[0]


def import_project(source: Path, *, goal: str, references: list[str] | None = None,
                   mode: str | None = None) -> dict[str, Any]:
    """Import an existing tree or initialize an explicitly new project candidate."""
    if references is None:
        references = []
    files, baseline = _read_source(Path(source))
    inferred = "existing" if baseline["exists"] and files else "new"
    chosen = inferred if mode is None else mode
    if chosen not in ("new", "existing"):
        raise ProjectRefused("mode must be 'new' or 'existing'")
    if chosen == "new" and files:
        raise ProjectRefused("new mode requires an absent or empty source directory")
    if chosen == "existing" and not files:
        raise ProjectRefused("existing mode requires at least one source file")
    candidate = _candidate(goal, references, files)
    return {"mode": chosen, "candidate": candidate, "baseline": baseline}


def _check_baseline(source: Path, expected: dict[str, Any]) -> dict[str, bytes]:
    if not isinstance(expected, dict) or set(expected) != {"root", "exists", "files", "tree_sha256"}:
        raise ProjectRefused("source baseline malformed")
    current, actual = _read_source(source)
    if actual != expected:
        raise ProjectRefused("source baseline conflict: project changed since import")
    return current


def _pointer(output: Path) -> dict[str, Any] | None:
    path = output / "active.json"
    if not path.exists():
        return None
    _regular_chain(path)
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectRefused(f"active pointer is unreadable: {exc}") from exc
    if not isinstance(value, dict) or set(value) != {"schema", "revision", "candidate_sha256", "previous"} or value["schema"] != 1:
        raise ProjectRefused("active pointer malformed")
    return value


def _atomic_pointer(path: Path, value: dict[str, Any], fault: Callable[[str], None] | None) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(_json_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if fault:
            fault("before_pointer_replace")
        os.replace(temp, path)
        try:
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def write_files(source: Path, files: list[dict[str, str]], *, expected_baseline: dict[str, Any],
                output: Path, goal: str = "Updated existing application", references: list[str] | None = None,
                fault: Callable[[str], None] | None = None) -> dict[str, Any]:
    references = [] if references is None else references
    if not files:
        _check_baseline(Path(os.path.abspath(source)), expected_baseline)
        return {"status": "EMPTY", "mode": "new", "revision": None, "candidate_sha256": None, "files": [], "active_pointer": None, "diff": []}
    candidate, contents = applications.validate_candidate({"schema": 1, "goal": goal, "references": references, "files": files})
    source_abs = Path(os.path.abspath(source))
    _check_baseline(source_abs, expected_baseline)
    try:
        output_abs = Path(os.path.abspath(output))
        output_abs.relative_to(source_abs)
    except ValueError:
        pass
    else:
        raise ProjectRefused("transaction output must be outside the source tree")
    if source_abs == output_abs:
        raise ProjectRefused("transaction output must be outside the source tree")
    out = output_abs
    if out.exists() and (out.is_symlink() or not out.is_dir()):
        raise ProjectRefused("transaction output must be a directory")
    out.mkdir(parents=True, exist_ok=True)
    _regular_chain(out)
    with _locked_transaction(out):
        revisions = out / "revisions"
        revisions.mkdir(exist_ok=True)
        if revisions.is_symlink():
            raise ProjectRefused("revisions directory cannot be a link")
        old = _pointer(out)
        clean, _ = applications.validate_candidate(candidate)
        canonical = {"schema": 1, "goal": clean["goal"], "references": clean["references"],
                     "files": [{"path": p, "size": len(contents[p]), "sha256": _digest(contents[p])} for p in sorted(contents)]}
        candidate_digest = _digest(_json_bytes(canonical))
        revision = f"{candidate_digest}-{uuid.uuid4().hex[:12]}"
        destination = revisions / revision
        staged = applications.stage_candidate(candidate, destination)
        if fault:
            fault("after_revision_stage")
        _check_baseline(source_abs, expected_baseline)
        if _pointer(out) != old:
            raise ProjectRefused("active revision changed concurrently")
        new_pointer = {"schema": 1, "revision": revision, "candidate_sha256": candidate_digest,
                       "previous": old["revision"] if old else None}
        _atomic_pointer(out / "active.json", new_pointer, fault)
        return {"status": "COMMITTED", "mode": "existing" if expected_baseline.get("files") else "new",
                "revision": revision, "candidate_sha256": candidate_digest, "files": staged["files"],
                "active_pointer": str(out / "active.json"), "diff": _diff(expected_baseline, contents)}


def _diff(baseline: dict[str, Any], contents: dict[str, bytes]) -> list[dict[str, Any]]:
    before = {x["path"]: x["sha256"] for x in baseline.get("files", [])}
    after = {p: _digest(data) for p, data in contents.items()}
    return [{"path": p, "change": "deleted" if p not in after else "added" if p not in before else "modified",
             "before_sha256": before.get(p), "after_sha256": after.get(p)}
            for p in sorted(set(before) | set(after)) if p not in before or p not in after or before[p] != after[p]]


def _locked_transaction(output: Path):
    class Lock:
        def __enter__(self):
            self.path = output / ".transaction.lock"
            try:
                self.stream = self.path.open("xb")
            except FileExistsError as exc:
                raise ProjectRefused("another project transaction is active") from exc
            self.stream.write(f"{os.getpid()}\n".encode()); self.stream.flush(); os.fsync(self.stream.fileno())
            return self
        def __exit__(self, *_):
            self.stream.close(); self.path.unlink(missing_ok=True)
    return Lock()


def _verified_manifest(output: Path, revision: str, candidate_digest: str) -> dict[str, Any]:
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{12}", revision):
        raise ProjectRefused("revision identifier malformed")
    root = output / "revisions" / revision
    manifest_path = root / "manifest.json"
    _regular_chain(manifest_path)
    manifest = applications._read_json(manifest_path)
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "goal", "references", "files", "candidate_sha256", "workspace"}:
        raise ProjectRefused("revision manifest malformed")
    if type(manifest["schema"]) is not int or manifest["schema"] != 1:
        raise ProjectRefused("revision manifest schema invalid")
    canonical = {key: manifest[key] for key in ("schema", "goal", "references", "files")}
    computed = _digest(_json_bytes(canonical))
    if manifest["candidate_sha256"] != computed or computed != candidate_digest or revision[:64] != computed:
        raise ProjectRefused("revision manifest candidate digest mismatch")
    try:
        applications._verify_staged_files(root, manifest)
    except applications.Refused as exc:
        raise ProjectRefused(f"revision payload verification failed: {exc}") from exc
    expected = {"manifest.json", *(record["path"] for record in manifest["files"])}
    actual: set[str] = set()
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(dirs)
        for name in dirs + names:
            item = Path(current) / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ProjectRefused("revision contains a link/reparse point")
        actual.update((Path(current) / name).relative_to(root).as_posix() for name in names)
    if actual != expected:
        raise ProjectRefused("revision contains unmanifested or missing files")
    return manifest


def status(output: Path) -> dict[str, Any]:
    out = Path(output)
    current = _pointer(out)
    if current is None:
        return {"status": "EMPTY"}
    manifest = _verified_manifest(out, current["revision"], current["candidate_sha256"])
    return {"status": "ACTIVE", "revision": current["revision"], "candidate_sha256": current["candidate_sha256"], "manifest": manifest}





def rollback(output: Path, *, expected_revision: str | None = None,
             fault: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Atomically restore the previous immutable revision pointer; never touch source."""
    out = Path(output)
    _regular_chain(out)
    with _locked_transaction(out):
        current = _pointer(out)
        if current is None or current["previous"] is None:
            raise ProjectRefused("no prior committed revision to roll back to")
        if expected_revision is not None and current["revision"] != expected_revision:
            raise ProjectRefused("active revision conflict")
        _verified_manifest(out, current["revision"], current["candidate_sha256"])
        target = current["previous"]
        manifest = _verified_manifest(out, target, target[:64])
        _atomic_pointer(out / "active.json", {"schema": 1, "revision": target,
                        "candidate_sha256": manifest["candidate_sha256"], "previous": None}, fault)
        return {"status": "ROLLED_BACK", "revision": target, "candidate_sha256": manifest["candidate_sha256"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import and transactionally revise candidates without overwriting source files")
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import"); imp.add_argument("source", type=Path); imp.add_argument("--goal", required=True)
    imp.add_argument("--mode", choices=("new", "existing")); imp.add_argument("--references", default="[]")
    commit = sub.add_parser("commit"); commit.add_argument("source", type=Path); commit.add_argument("baseline", type=Path)
    commit.add_argument("candidate", type=Path); commit.add_argument("output", type=Path)
    roll = sub.add_parser("rollback"); roll.add_argument("output", type=Path)
    show = sub.add_parser("status"); show.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "import": result = import_project(args.source, goal=args.goal, mode=args.mode, references=json.loads(args.references))
        elif args.command == "commit": result = write_files(args.source, applications._read_json(args.candidate), expected_baseline=applications._read_json(args.baseline), output=args.output)
        elif args.command == "rollback": result = rollback(args.output)
        else: result = status(args.output)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True)); return 0
    except (ProjectRefused, applications.Refused, OSError, ValueError) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
