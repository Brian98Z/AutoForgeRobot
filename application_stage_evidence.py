#!/usr/bin/env python3
"""Capture immutable G0-G4 evidence bytes for separate review, never approval."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

import application_evaluation as evaluation
import application_spec
import application_project

MAX_EVIDENCE_BYTES = 64 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load(path):
    application_project._regular_chain(Path(path))
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError("evidence exceeds 64 MiB")
    return raw


def capture(plan, root, output):
    """Trusted-controller plan only; never expose root selection to candidates.

    Both source and destination require exclusive trusted ownership during capture.
    This command does not grant permission to disclose captured files externally.
    """
    application_project._regular_chain(Path(root))
    application_project._regular_chain(Path(output))
    root = Path(root).resolve(strict=True)
    output = Path(output).absolute()
    if output.exists():
        raise ValueError("output already exists; evidence cannot be overwritten")
    if not isinstance(plan, dict) or set(plan) != {"schema", "scope", "stages"} or type(plan["schema"]) is not int or plan["schema"] != 1:
        raise ValueError("stage plan schema invalid")
    if not isinstance(plan["scope"], str) or not plan["scope"].strip() or not isinstance(plan["stages"], dict) or set(plan["stages"]) != set(evaluation.GATES):
        raise ValueError("scope and all five stages required")
    # Prepare and validate everything before creating the destination.
    prepared = []
    for gate, definition in evaluation.GATES.items():
        row = plan["stages"][gate]
        if not isinstance(row, dict) or set(row) != {"scope", "evidence", "blockers"}:
            raise ValueError("stage fields invalid: " + gate)
        if not isinstance(row["scope"], str) or not row["scope"].strip() or not isinstance(row["blockers"], list) or any(not isinstance(x, str) or not x.strip() for x in row["blockers"]):
            raise ValueError("stage scope/blockers invalid: " + gate)
        if not isinstance(row["evidence"], dict) or set(row["evidence"]) != set(definition["evidence"]):
            raise ValueError("all required evidence roles must be declared: " + gate)
        roles = {}
        for role, sources in row["evidence"].items():
            if not isinstance(sources, list) or not sources:
                raise ValueError("each role requires actual files: " + role)
            items = []
            for relative in sources:
                if not isinstance(relative, str):
                    raise ValueError("relative source path required")
                path = Path(relative)
                if path.is_absolute() or ".." in path.parts or path.drive:
                    raise ValueError("source path escapes root")
                application_project._regular_chain(root / path)
                resolved = (root / path).resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    raise ValueError("source must be a regular file within root")
                raw = load(resolved)
                items.append(({"source": path.as_posix(), "sha256": digest(raw), "bytes": len(raw)}, raw))
            roles[role] = items
        prepared.append((gate, row, roles))
    output.mkdir(parents=True, exist_ok=False)
    try:
        previous = None
        index = []
        for gate, row, roles in prepared:
            directory = output / gate
            directory.mkdir()
            records = {}
            for role, items in roles.items():
                records[role] = []
                for number, (record, raw) in enumerate(items):
                    filename = role + "-" + str(number) + ".bin"
                    (directory / filename).write_bytes(raw)
                    records[role].append({**record, "snapshot": filename})
            payload = {"schema": 1, "gate": gate, "scope": row["scope"], "criteria": evaluation.GATES[gate],
                       "original_stage_contract": application_spec.phase_gates()[len(index)],
                       "previous_packet_sha256": previous, "evidence": records, "blockers": row["blockers"],
                       "status": "READY_FOR_REVIEW", "approval": None,
                       "authority": "Captured bytes and explicit declarations only; neither semantic acceptance nor promotion."}
            raw = canonical(payload) + b"\n"
            (directory / "packet.json").write_bytes(raw)
            previous = digest(raw)
            index.append({"gate": gate, "packet": gate + "/packet.json", "sha256": previous})
        result = {"schema": 1, "scope": plan["scope"], "stages": index, "status": "READY_FOR_REVIEW", "approval": None}
        (output / "index.json").write_bytes(canonical(result) + b"\n")
        return result
    except BaseException:
        # Only this newly created, controller-owned incomplete destination is removed.
        shutil.rmtree(output)
        raise


def verify(directory, *, expected_index_sha256):
    """Verify against a separately retained digest in a trusted, quiescent directory.

    This is not a filesystem transaction against an adversarial concurrent writer.
    """
    evaluation._digest(expected_index_sha256, "expected_index_sha256")
    application_project._regular_chain(Path(directory))
    directory = Path(directory).resolve(strict=True)
    index_raw = load(directory / "index.json")
    if digest(index_raw) != expected_index_sha256:
        raise ValueError("index does not match independently retained SHA-256")
    index = json.loads(index_raw)
    if not isinstance(index, dict) or set(index) != {"schema", "scope", "stages", "status", "approval"} or type(index["schema"]) is not int or index["schema"] != 1 or index["approval"] is not None or index["status"] != "READY_FOR_REVIEW" or not isinstance(index["scope"], str) or not index["scope"].strip():
        raise ValueError("invalid evidence index")
    rows = index.get("stages")
    if not isinstance(rows, list) or len(rows) != len(evaluation.GATES):
        raise ValueError("complete ordered stage chain required")
    previous = None
    files = {"index.json"}
    for number, (gate, row) in enumerate(zip(evaluation.GATES, rows)):
        if not isinstance(row, dict) or set(row) != {"gate", "packet", "sha256"} or row["gate"] != gate or row["packet"] != gate + "/packet.json":
            raise ValueError("stage order/path mismatch")
        path = directory / row["packet"]
        application_project._regular_chain(path)
        raw = load(path)
        if digest(raw) != row["sha256"]:
            raise ValueError("stage packet digest mismatch: " + gate)
        packet = json.loads(raw)
        required = {"schema", "gate", "scope", "criteria", "original_stage_contract", "previous_packet_sha256", "evidence", "blockers", "status", "approval", "authority"}
        if not isinstance(packet, dict) or set(packet) != required or type(packet["schema"]) is not int or packet["schema"] != 1:
            raise ValueError("invalid stage packet schema")
        if not isinstance(packet["scope"], str) or not packet["scope"].strip() or not isinstance(packet["authority"], str) or not packet["authority"].strip() or not isinstance(packet["blockers"], list) or any(not isinstance(item, str) or not item.strip() for item in packet["blockers"]):
            raise ValueError("invalid stage scope/blockers")
        if packet.get("gate") != gate or packet.get("previous_packet_sha256") != previous or packet.get("criteria") != evaluation.GATES[gate] or packet.get("original_stage_contract") != application_spec.phase_gates()[number]:
            raise ValueError("stage dependency or contract mismatch: " + gate)
        if packet.get("approval") is not None or packet.get("status") != "READY_FOR_REVIEW" or set(packet.get("evidence", {})) != set(evaluation.GATES[gate]["evidence"]):
            raise ValueError("evidence role or approval mismatch: " + gate)
        files.add(row["packet"])
        for role, records in packet["evidence"].items():
            if not isinstance(records, list) or not records:
                raise ValueError("empty evidence role")
            for n, record in enumerate(records):
                if not isinstance(record, dict) or set(record) != {"source", "sha256", "bytes", "snapshot"} or record["snapshot"] != role + "-" + str(n) + ".bin":
                    raise ValueError("invalid snapshot record")
                snap = path.parent / record["snapshot"]
                application_project._regular_chain(snap)
                content = load(snap)
                if len(content) != record["bytes"] or digest(content) != record["sha256"]:
                    raise ValueError("snapshot digest mismatch: " + str(snap))
                files.add(gate + "/" + record["snapshot"])
        previous = row["sha256"]
    entries = list(directory.rglob("*"))
    for entry in entries:
        application_project._regular_chain(entry)
    actual = {p.relative_to(directory).as_posix() for p in entries if p.is_file()}
    if actual != files:
        raise ValueError("unlisted evidence entries")
    if load(directory / "index.json") != index_raw:
        raise ValueError("index changed during verification")
    return {"schema": 1, "status": "INTEGRITY_PASS", "stage_count": len(rows), "index_sha256": digest(index_raw),
            "approval": None, "scope": "Externally digest-bound snapshot bytes and G0-G4 chain in a trusted quiescent directory only; independent semantic review remains required."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("capture")
    p.add_argument("plan", type=Path)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p = sub.add_parser("verify")
    p.add_argument("directory", type=Path)
    p.add_argument("--expected-index-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = capture(json.loads(load(args.plan)), args.root, args.output) if args.command == "capture" else verify(args.directory, expected_index_sha256=args.expected_index_sha256)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "HOLD", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
