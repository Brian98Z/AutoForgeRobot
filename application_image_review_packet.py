"""Portable, unsigned evidence packets for external image-applicability review."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any


class PacketRefused(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _linked_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _safe_directory(path: Path) -> None:
    """Reject symlinks and Windows reparse points throughout a directory path."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise PacketRefused(f"missing directory path: {path}") from exc
        if _linked_or_reparse(info):
            raise PacketRefused(f"linked or reparse path component: {path}")
        if current != absolute and not stat.S_ISDIR(info.st_mode):
            raise PacketRefused(f"path component is not a directory: {current}")


def _safe_regular(path: Path) -> bytes:
    """Read a regular file without following links at any path component."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise PacketRefused(f"missing evidence path: {path}") from exc
        if _linked_or_reparse(info):
            raise PacketRefused(f"linked or reparse path component: {path}")
    info = absolute.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise PacketRefused(f"evidence must be a regular file: {path}")
    return absolute.read_bytes()


def _index(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_safe_regular(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PacketRefused(f"invalid evidence index: {exc}") from exc
    if (not isinstance(value, dict) or value.get("schema") != 1
            or value.get("kind") != "review-evidence-index"
            or value.get("status") != "AWAITING_EXTERNAL_REVIEW"
            or value.get("not_a_signed_decision") is not True
            or value.get("candidate_execution_authorized") is not False
            or not isinstance(value.get("files"), list) or not value["files"]):
        raise PacketRefused("index is not an unsigned external review evidence index")
    return value


def _basename(path: str) -> str:
    # Accept original Windows or POSIX paths while emitting only a safe leaf name.
    leaf = path.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = re.sub(r"[^A-Za-z0-9._-]", "_", leaf).strip("._")
    return leaf or "evidence.bin"
def export_packet(index_path: Path, output_dir: Path) -> dict[str, Any]:
    index = _index(index_path)
    target = Path(os.path.abspath(output_dir))
    if target.exists() or target.is_symlink():
        raise PacketRefused("output directory must be fresh and absent")
    ancestor = target.parent
    while not ancestor.exists() and not ancestor.is_symlink():
        ancestor = ancestor.parent
    _safe_directory(ancestor)
    if not ancestor.is_dir():
        raise PacketRefused('output ancestor must be a real directory')
    items = []
    names: set[str] = set()
    for ordinal, record in enumerate(index["files"], 1):
        if (not isinstance(record, dict) or set(record) != {"path", "sha256"}
                or not isinstance(record["path"], str) or not record["path"]
                or not isinstance(record["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            raise PacketRefused("each index file record must contain path and lowercase SHA-256")
        original = Path(record["path"])
        data = _safe_regular(original)
        digest = _sha(data)
        if digest != record["sha256"]:
            raise PacketRefused(f"source evidence hash mismatch: {record['path']}")
        # Prefix each source basename with an ordinal to ensure uniqueness, even
        # when distinct originals have the same leaf name.
        relative = f"evidence/{ordinal:03d}-{_basename(record['path'])}"
        if relative in names:
            raise PacketRefused("duplicate packet member name")
        names.add(relative)
        items.append((record["path"], relative, digest, data))

    # mkdir(exist_ok=False) provides the required no-overwrite boundary.
    target.mkdir(parents=True, exist_ok=False)
    try:
        evidence_dir = target / "evidence"
        evidence_dir.mkdir()
        manifest = {
            "schema": 1,
            "kind": "portable-image-review-packet",
            "source_index": str(Path(os.path.abspath(index_path))),
            "source_index_sha256": _sha(_safe_regular(index_path)),
            "original_scan_status": "FAIL",
            "candidate_execution_authorized": False,
            "files": [
                {"original_path": original, "path": relative, "sha256": digest}
                for original, relative, digest, _ in items
            ],
            "instructions": {
                "decision": "This packet is evidence only, not a signed decision or authorization.",
                "missing_trust_inputs": [
                    "Operator must establish and pin the contract-signing public key.",
                    "Operator must establish and pin a distinct reviewer public key.",
                    "The operator must establish both key identities through trusted channels; candidate data is not trust evidence.",
                ],
                "required_signatures": [
                    "Contract signer and independent reviewer must both sign the canonical applicability contract accepted by verify-image-applicability.",
                    "The contract must bind the exact unchanged scanner report, scan gate, finding decisions, source evidence and behavior-regression evidence.",
                ],
                "original_path_reconciliation": (
                    "The existing verify_image_applicability verifier requires the original absolute report_path and original evidence paths. "
                    "These copied files do not make that verifier portable. Before invoking it, a trusted operator must reconcile the original report_path and every evidence path to the authentic original files and confirm the exact hashes; do not rewrite or rebase the scanner report or its report_path."
                ),
                "handling": "Do not include private keys or send this packet externally without the operator's authorization.",
            },
        }
        for _, relative, _, data in items:
            (target / PurePosixPath(relative)).write_bytes(data)
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8", newline="\n")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"status": "EXPORTED_UNSIGNED_PACKET", "packet_dir": str(target),
            "file_count": len(items), "original_scan_status": "FAIL",
            "candidate_execution_authorized": False}


def verify_packet(packet_dir: Path) -> dict[str, Any]:
    root = Path(os.path.abspath(packet_dir))
    _safe_directory(root)
    if not root.is_dir():
        raise PacketRefused("packet directory must be a real directory")
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(_safe_regular(manifest_path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PacketRefused(f"invalid packet manifest: {exc}") from exc
    required_top = {"schema", "kind", "source_index", "source_index_sha256", "original_scan_status",
                    "candidate_execution_authorized", "files", "instructions"}
    if (not isinstance(manifest, dict) or set(manifest) != required_top
            or manifest.get("schema") != 1 or manifest.get("kind") != "portable-image-review-packet"
            or manifest.get("original_scan_status") != "FAIL"
            or manifest.get("candidate_execution_authorized") is not False
            or not isinstance(manifest.get("source_index"), str)
            or not isinstance(manifest.get("source_index_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest["source_index_sha256"])
            or not isinstance(manifest.get("files"), list) or not manifest["files"]
            or not isinstance(manifest.get("instructions"), dict)):
        raise PacketRefused("packet manifest schema/status invalid")
    expected = {"manifest.json"}
    seen_originals: set[str] = set()
    count = 0
    for record in manifest["files"]:
        if (not isinstance(record, dict) or set(record) != {"original_path", "path", "sha256"}
                or not all(isinstance(record.get(k), str) for k in ("original_path", "path", "sha256"))
                or not record["original_path"] or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            raise PacketRefused("packet file record malformed")
        rel = PurePosixPath(record["path"])
        if ("\\" in record["path"] or ":" in record["path"] or rel.is_absolute()
                or ".." in rel.parts or "." in rel.parts or len(rel.parts) != 2
                or rel.parts[0] != "evidence" or not rel.parts[1]):
            raise PacketRefused("unsafe packet member path")
        rel_text = rel.as_posix()
        if rel_text in expected or record["original_path"] in seen_originals:
            raise PacketRefused("duplicate packet path or original source")
        expected.add(rel_text)
        seen_originals.add(record["original_path"])
        member = root.joinpath(*rel.parts)
        data = _safe_regular(member)
        if _sha(data) != record["sha256"]:
            raise PacketRefused(f"packet evidence hash mismatch: {rel_text}")
        count += 1
    actual: set[str] = set()
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        for name in list(dirs):
            item = base_path / name
            info = item.lstat()
            if _linked_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise PacketRefused("packet contains a symlink, reparse point, or non-directory")
            actual.add(item.relative_to(root).as_posix() + "/")
        for name in files:
            item = base_path / name
            info = item.lstat()
            if _linked_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                raise PacketRefused("packet contains a symlink, reparse point, or non-regular file")
            actual.add(item.relative_to(root).as_posix())
    expected_tree = expected | {"evidence/"}
    if actual != expected_tree:
        raise PacketRefused(f"packet member set differs (missing={sorted(expected_tree-actual)}, extra={sorted(actual-expected_tree)})")
    return {"status": "PACKET_VALID_UNSIGNED", "file_count": count,
            "original_scan_status": "FAIL", "candidate_execution_authorized": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="export INDEX.json OUTPUT_DIR")
    exp.add_argument("index", type=Path)
    exp.add_argument("output_dir", type=Path)
    ver = sub.add_parser("verify", help="verify PACKET_DIR")
    ver.add_argument("packet_dir", type=Path)
    args = parser.parse_args()
    try:
        result = export_packet(args.index, args.output_dir) if args.command == "export" else verify_packet(args.packet_dir)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (PacketRefused, OSError, ValueError, KeyError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
