#!/usr/bin/env python3
"""Bounded self-iteration driver for the containerised AutoForge controller.

One cycle = one fresh model-driven agent session (application_build.py run --mode agent)
whose candidate is validated by the real container adapter. Cycles after the first seed
the next session with the previous candidate, so later cycles repair instead of restart.
Nothing is promoted without the unchanged validator accepting it; the driver never edits
the candidate itself.

Everything under /workspace is visible to the sidecar engine with identical paths, which
is why candidate bind mounts resolve.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path("/opt/autoforge")
BUILD = ROOT / "autoforge" / "application_build.py"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def engine(*argv: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False)


def resolve_image(reference: str) -> str:
    probe = engine("image", "inspect", "--format", "{{.Id}}", reference, timeout=30)
    if probe.returncode != 0:
        log(f"pulling engine image {reference}")
        pull = engine("pull", reference, timeout=1800)
        sys.stderr.write(pull.stdout[-2000:] + pull.stderr[-2000:])
        probe = engine("image", "inspect", "--format", "{{.Id}}", reference, timeout=30)
    if probe.returncode != 0:
        raise SystemExit(f"engine image unavailable: {reference}")
    return probe.stdout.strip()


def last_json(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    start = text.find("{")
    while start != -1:
        try:
            value = json.loads(text[start:])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return {}


def materialise_seed(candidate_path: Path, target: Path) -> Path | None:
    if not candidate_path.is_file():
        return None
    try:
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    files = candidate.get("files") or []
    if not files:
        return None
    target.mkdir(parents=True)
    for entry in files:
        relative = str(entry.get("path", ""))
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            return None
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(str(entry.get("content", "")), encoding="utf-8")
    return target


def build_command(args, image_id: str, output: Path, seed: Path | None) -> list[str]:
    command = [
        sys.executable, str(BUILD), "run",
        "--request", str(args.request),
        "--contract", str(args.contract),
        "--reference-store", str(args.reference_store),
        "--engine", "docker",
        "--image", image_id,
        "--output", str(output),
        "--mode", "agent",
        "--action-profile", "small",
        "--allow-model",
        "--trusted-adapter",
        "--max-turns", str(args.max_turns),
        "--tool-budget", str(args.tool_budget),
        "--token-budget", str(args.token_budget),
        "--time-budget", str(args.time_budget),
    ]
    if seed is not None:
        command += ["--project", str(seed)]
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, default=ROOT / "fixtures" / "business" / "request.json")
    parser.add_argument("--contract", type=Path, default=ROOT / "fixtures" / "business" / "contract.json")
    parser.add_argument("--reference-store", type=Path, default=ROOT / "fixtures")
    parser.add_argument("--output-root", type=Path,
                        default=Path(os.environ.get("AUTOFORGE_RUNS", "/workspace/runs")))
    parser.add_argument("--image-reference", default=os.environ.get("AUTOFORGE_ENGINE_IMAGE",
                                                                    "public.ecr.aws/docker/library/python:3.11-slim"))
    parser.add_argument("--cycles", type=int, default=int(os.environ.get("AUTOFORGE_CYCLES", "3")))
    parser.add_argument("--max-turns", type=int, default=int(os.environ.get("AUTOFORGE_MAX_TURNS", "12")))
    parser.add_argument("--tool-budget", type=int, default=int(os.environ.get("AUTOFORGE_TOOL_BUDGET", "96")))
    parser.add_argument("--token-budget", type=int, default=int(os.environ.get("AUTOFORGE_TOKEN_BUDGET", "1000000")))
    parser.add_argument("--time-budget", type=float, default=float(os.environ.get("AUTOFORGE_TIME_BUDGET", "900")))
    args = parser.parse_args()

    if not os.environ.get("AUTOFORGE_API_BASE") or not os.environ.get("AUTOFORGE_MODEL"):
        raise SystemExit("AUTOFORGE_API_BASE and AUTOFORGE_MODEL must be set")
    if args.cycles < 1:
        raise SystemExit("--cycles must be positive")

    image_id = resolve_image(args.image_reference)
    log(f"engine image {image_id}")

    preflight = subprocess.run(
        [sys.executable, str(BUILD), "check", "--request", str(args.request),
         "--contract", str(args.contract), "--reference-store", str(args.reference_store),
         "--engine", "docker", "--image", image_id],
        capture_output=True, text=True, timeout=180, check=False)
    report = last_json(preflight.stdout)
    if report.get("status") != "READY":
        sys.stderr.write(preflight.stdout[-4000:] + preflight.stderr[-2000:])
        raise SystemExit(f"preflight blocked: {report.get('blockers')}")
    log(f"preflight READY: {json.dumps(report.get('checks', {}).get('model_configuration', {}))}")

    run_dir = args.output_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(parents=True)
    cycles: list[dict] = []
    previous: Path | None = None
    status = "HOLD"
    for number in range(1, args.cycles + 1):
        output = run_dir / f"cycle-{number:02d}"
        seed = materialise_seed(run_dir / f"cycle-{number - 1:02d}" / "candidate.json",
                                run_dir / f"seed-{number:02d}") if number > 1 else None
        command = build_command(args, image_id, output, seed)
        log(f"cycle {number}/{args.cycles}: {'seed ' + str(seed.name) if seed else 'empty candidate'}")
        started = time.monotonic()
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=args.time_budget + 900, check=False)
        elapsed = round(time.monotonic() - started, 1)
        result = last_json(proc.stdout) or last_json(proc.stderr)
        status = result.get("status", "ERROR")
        record = {"cycle": number, "status": status, "returncode": proc.returncode,
                  "seconds": elapsed, "output": str(output),
                  "seed": str(seed) if seed else None,
                  "reason": result.get("reason"),
                  "observations": result.get("observations"),
                  "usage": result.get("usage") or result.get("budget")}
        (run_dir / f"cycle-{number:02d}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        cycles.append(record)
        log(f"cycle {number} -> {status} in {elapsed}s")
        if status == "PASS":
            break

    summary = {"schema": 1, "status": status, "cycles": cycles,
               "model": os.environ.get("AUTOFORGE_MODEL"),
               "endpoint": os.environ.get("AUTOFORGE_API_BASE"),
               "image_id": image_id, "request": str(args.request),
               "contract": str(args.contract), "run_dir": str(run_dir)}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n",
                                          encoding="utf-8")
    log(f"summary {run_dir / 'summary.json'}")
    print(json.dumps({"status": status, "run_dir": str(run_dir),
                      "cycles": [{"cycle": c["cycle"], "status": c["status"], "seconds": c["seconds"]}
                                 for c in cycles]}, indent=1))
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
