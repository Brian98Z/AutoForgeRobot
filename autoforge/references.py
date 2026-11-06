"""Immutable, non-executing local reference packages for REA."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

SCHEMA = 1
ALLOWED_SUFFIXES = {".py", ".js", ".ts", ".rs", ".jl", ".wat", ".json", ".md", ".html", ".css"}
MAX_FILES = 128
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
SENSITIVE_PARTS = {".env", "id_rsa", "id_ed25519", "private.key", "credentials", "credential", "secret", "secrets", "token", "tokens", "password", "passwd", "api_key", "apikey"}
LIMITATIONS = [
    "Operator-supplied origin and license are declarations, not independently verified.",
    "Reference candidates are untrusted data; this tool does not execute them.",
    "Search uses deterministic token overlap for retrieval only, not semantic proof.",
]


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_string(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _is_reparse(path: Path) -> bool:
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        return True
    # Windows reparse-point attribute; kept explicit for junctions and other links.
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _sensitive(path: Path) -> bool:
    name = path.name.casefold()
    parts = {p.casefold() for p in path.parts}
    return name in SENSITIVE_PARTS or name.startswith(".env") or any(x in name for x in ("secret", "credential", "token", "password", "private_key", "api_key")) or bool(parts & SENSITIVE_PARTS)


def _source_files(source: Path) -> list[tuple[str, Path]]:
    for ancestor in (source.absolute(), *source.absolute().parents):
        if _is_reparse(ancestor):raise ValueError('symlink/reparse source ancestor is not allowed')
    if not source.exists():
        raise ValueError("source does not exist")
    if _is_reparse(source):
        raise ValueError("symlink/reparse source is not allowed")
    if source.is_file():
        candidates = [(source.name, source)]
    elif source.is_dir():
        candidates = []
        for root, dirs, names in os.walk(source, followlinks=False):
            root_path = Path(root)
            for dirname in list(dirs):
                item = root_path / dirname
                if _is_reparse(item):
                    raise ValueError(f"symlink/reparse path is not allowed: {item}")
                if _sensitive(item):
                    raise ValueError(f"sensitive key/environment-like directory is not allowed: {item.relative_to(source).as_posix()}")
            for name in names:
                item = root_path / name
                if _is_reparse(item):
                    raise ValueError(f"symlink/reparse path is not allowed: {item}")
                if _sensitive(item):
                    raise ValueError(f"sensitive key/environment-like file is not allowed: {item.relative_to(source).as_posix()}")
                if item.suffix.lower() in ALLOWED_SUFFIXES:
                    candidates.append((item.relative_to(source).as_posix(), item))
                    if len(candidates)>MAX_FILES:raise ValueError(f'package exceeds {MAX_FILES} files')
    else:
        raise ValueError("source must be a regular file or directory")
    accepted = []
    for rel, path in candidates:
        if _sensitive(path):
            raise ValueError(f"sensitive key/environment-like file is not allowed: {rel}")
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            if source.is_file():
                raise ValueError(f"unsupported source file extension: {path.suffix}")
            continue
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise ValueError("unsafe relative source path")
        accepted.append((pure.as_posix(), path))
    accepted.sort(key=lambda pair: pair[0])
    if not accepted:
        raise ValueError("source contains no allowed files")
    if len(accepted) > MAX_FILES:
        raise ValueError(f"package exceeds {MAX_FILES} files")
    return accepted


def ingest(source: str | Path, store: str | Path, origin: str, license_text: str, kind: str = "source") -> dict:
    origin = _validate_string(origin, "origin")
    license_text = _validate_string(license_text, "license")
    if kind not in {"source", "rea-evidence"}:
        raise ValueError("kind must be source or rea-evidence")
    src = Path(source)
    store_path = Path(store)
    pairs = _source_files(src)
    entries = []
    total = 0
    evidence_limitations = []
    evidence_records = []
    for rel, path in pairs:
        with path.open('rb') as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes: {rel}")
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"package exceeds {MAX_TOTAL_BYTES} total bytes")
        text = raw.decode("utf-8", errors="strict")
        entries.append({"path": rel, "sha256": digest(raw), "content": text})
        if kind == "rea-evidence":
            try:
                obj = json.loads(text)
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"rea-evidence input must be valid exported Evidence JSON: {rel}") from exc
            records = obj.get("records") if isinstance(obj, dict) else None
            if records is None:
                records = [obj] if isinstance(obj, dict) else None
            if not isinstance(records, list) or not records or any(not isinstance(record, dict) or not isinstance(record.get("evidence_id", record.get("id")), str) for record in records):
                raise ValueError(f"rea-evidence must preserve exported Evidence records with evidence_id: {rel}")
            for record in records:
                limitations = record.get("limitations", [])
                if not isinstance(limitations, list):
                    raise ValueError(f"invalid Evidence limitations: {rel}")
                evidence_limitations.extend(str(v) for v in limitations if isinstance(v, str))
            evidence_records.extend((rel, record) for record in records)
    payload = {
        "schema": SCHEMA,
        "origin": origin,
        "license": license_text,
        "kind": kind,
        "files": entries,
        "observations": [],
        "inferences": [],
        "unknowns": [],
        "limitations": LIMITATIONS + evidence_limitations + (["Imported REA Evidence authenticity and upstream validation are unverified; original bytes, id and limitations are preserved."] if kind == "rea-evidence" else []),
    }
    if kind == "rea-evidence":
        payload["upstream_evidence"] = [{"path": rel, "evidence_id": record.get("evidence_id", record.get("id")), "record": record} for rel, record in evidence_records]
    package_id = digest(canonical(payload))
    package = dict(payload, package_id=package_id)
    target = store_path / "packages" / f"{package_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical(package) + b"\n"
    if target.exists():
        if target.read_bytes() != encoded:
            raise ValueError("existing immutable package id has conflicting content")
    else:
        # Exclusive create prevents replacing an existing package on races.
        with target.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    return {"package_id": package_id, "files": len(entries), "bytes": total, "path": str(target)}


def _load_package(store: str | Path, package_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", package_id):
        raise ValueError("id must be a SHA-256 package id")
    path = Path(store) / "packages" / f"{package_id}.json"
    with path.open('rb') as stream:
        raw = stream.read(32 * MAX_TOTAL_BYTES + 1)
    if len(raw)>32 * MAX_TOTAL_BYTES:raise ValueError('oversized reference package')
    package = json.loads(raw.decode("utf-8"))
    if not isinstance(package, dict) or package.get("schema") != SCHEMA or package.get("package_id") != package_id:
        raise ValueError("invalid package metadata")
    payload = {key: value for key, value in package.items() if key != "package_id"}
    if digest(canonical(payload)) != package_id:
        raise ValueError("package manifest hash mismatch")
    files = package.get("files")
    if not isinstance(files, list):
        raise ValueError("invalid package files")
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("content"), str):
            raise ValueError("invalid package file entry")
        pure = PurePosixPath(entry["path"])
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != entry["path"]:
            raise ValueError("unsafe package path")
        if digest(entry["content"].encode("utf-8")) != entry.get("sha256"):
            raise ValueError(f"content hash mismatch: {entry['path']}")
    return package


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_]+|[^\W\d_]+", text.casefold(), flags=re.UNICODE))


def query(store: str | Path, query_text: str, limit: int = 10) -> list[dict]:
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be from 1 to 1000")
    terms = _tokens(_validate_string(query_text, "query"))
    if not terms:
        raise ValueError("query must contain searchable tokens")
    package_dir = Path(store) / "packages"
    if not package_dir.is_dir():
        raise ValueError("store has no packages")
    results = []
    for path in sorted(package_dir.glob("*.json"), key=lambda p: p.name):
        package = _load_package(store, path.stem)
        for entry in package["files"]:
            overlap = terms & _tokens(entry["path"] + " " + entry["content"])
            if overlap:
                results.append({"package_id": package["package_id"], "path": entry["path"], "score": len(overlap), "matched_tokens": sorted(overlap), "origin": package["origin"], "kind": package["kind"]})
    results.sort(key=lambda row: (-row["score"], row["package_id"], row["path"]))
    return results[:limit]


def export(store: str | Path, package_id: str, output: str | Path) -> dict:
    package = _load_package(store, package_id)
    compact = {"schema": SCHEMA, "origin": package["origin"], "license": package["license"], "package_id": package_id,
               "files": [{"path": f["path"], "sha256": f["sha256"], "content": f["content"]} for f in package["files"]],
               "kind": package["kind"], "limitations": package["limitations"]}
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(canonical(compact) + b"\n")
    return {"package_id": package_id, "output": str(out), "files": len(compact["files"])}


def attach(store, package_id, file_path, task_path, output):
    package = _load_package(store, package_id)
    matches = [f for f in package['files'] if f['path']==file_path]
    if len(matches)!=1:raise ValueError('reference file not found')
    entry=matches[0]
    if len(entry['content'].encode('utf-8'))>32768:raise ValueError('task reference exceeds 32 KiB')
    task=json.loads(Path(task_path).read_text(encoding='utf-8'))
    refs=task.setdefault('references',[])
    if not isinstance(refs,list) or len(refs)>=4:raise ValueError('at most four task references')
    refs.append({'id':package_id,'content_sha256':entry['sha256'],'content':entry['content'],
                 'kind':'wat' if file_path.endswith('.wat') else package['kind'],
                 'origin':package['origin'],'license':package['license']})
    with Path(output).open('xb') as stream:stream.write(canonical(task)+b'\n')
    return {'output':str(output),'package_id':package_id,'reference_path':file_path}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("--source", required=True); p.add_argument("--store", required=True)
    p.add_argument("--origin", required=True); p.add_argument("--license", required=True)
    p.add_argument("--kind", choices=("source", "rea-evidence"), default="source")
    p = sub.add_parser("query"); p.add_argument("--store", required=True); p.add_argument("--query", required=True); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("export"); p.add_argument("--store", required=True); p.add_argument("--id", required=True); p.add_argument("--output", required=True)
    p = sub.add_parser('attach'); p.add_argument('--store',required=True); p.add_argument('--id',required=True)
    p.add_argument('--file',required=True); p.add_argument('--task',required=True); p.add_argument('--output',required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest": result = ingest(args.source, args.store, args.origin, args.license, args.kind)
        elif args.command == "query": result = query(args.store, args.query, args.limit)
        elif args.command == 'attach': result = attach(args.store,args.id,args.file,args.task,args.output)
        else: result = export(args.store, args.id, args.output)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"reference error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
