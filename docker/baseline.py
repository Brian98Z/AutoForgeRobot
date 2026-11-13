#!/usr/bin/env python3
"""Append one iteration record to the durable baseline ledger.

Each iteration writes: environment identity (image digest, toolchain versions), the model
endpoint actually used, the key source/config digests, and the suite score with its failing
list. Records are append-only so later iterations can be compared and an earlier run can be
reproduced from its parameters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_LEDGER = Path("/workspace/autoforge/bootstrap-runs/docker-baseline/iterations.jsonl")
TRACKED = (
    "docker/Dockerfile",
    "docker/compose.yaml",
    "docker/entrypoint.sh",
    "docker/suite.py",
    "docker/iterate.py",
    "autoforge/container_env.py",
    "autoforge/paths.py",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = ((out.stdout or "") + (out.stderr or "")).strip().splitlines()
    return lines[0] if lines else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/autoforge"))
    parser.add_argument("--report", type=Path, required=True, help="suite report JSON for this iteration")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--label", default="")
    parser.add_argument("--note", default="")
    # The engine a suite run actually used; the workbench image name differs between the host
    # daemon and the sidecar daemon, so it is recorded explicitly rather than assumed.
    parser.add_argument("--engine-image", default="autoforge:0.1.0-linux")
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    image = run(["docker", "image", "inspect", "--format", "{{.Id}}|{{.Size}}", args.engine_image])
    image_id, _, image_size = image.partition("|")

    record = {
        "schema": 1,
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "label": args.label,
        "note": args.note,
        "image": {"id": image_id, "bytes": int(image_size or 0)},
        "model": {
            "endpoint_in_container": os.environ.get("AUTOFORGE_API_BASE", ""),
            "forward_target": os.environ.get("AUTOFORGE_MODEL_FORWARD", ""),
            "model_id": os.environ.get("AUTOFORGE_MODEL", ""),
        },
        "toolchain": {
            "python": sys.version.split()[0],
            "node": run(["node", "--version"]),
            "julia": run([str(args.root / "runtime/julia-1.13.3/bin/julia"), "--version"]),
            "controller": run([str(args.root / "target/release/autoforge.exe"), "--version"]),
        },
        "suite": {"total": report.get("total"), "passed": report.get("passed"),
                  "failed": report.get("failed", [])},
        "sources": {rel: sha(args.root / rel) for rel in TRACKED if (args.root / rel).is_file()},
        "engine": {"docker_server": run(["docker", "version", "--format", "{{.Server.Version}}"])},
    }
    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    with args.ledger.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    print(json.dumps({"appended": str(args.ledger), "passed": record["suite"]["passed"],
                      "total": record["suite"]["total"], "image": image_id[:19]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
