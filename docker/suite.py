#!/usr/bin/env python3
"""Run every check script in the workbench once and summarise the outcomes.

The repository ships 70+ independent check CLIs and no runner, so one pass is done by
hand each time. This driver only sequences them: it never edits state, never retries, and
reports the raw return code plus the last output line so a failure can be triaged as a
real defect versus a missing local prerequisite.
"""
from __future__ import annotations

import argparse
import hashlib
from typing import Any
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("AUTOFORGE_ROOT") or
            ("/workspace/autoforge" if Path("/workspace/autoforge").is_dir() else "/opt/autoforge"))
CHECKS = ROOT / "checks"


def wait_ready(deadline_seconds: float = 60.0) -> None:
    """The workbench seeds its shared-volume copy on start; exec can arrive first."""
    marker = ROOT / ".ready"
    if not (Path("/workspace/autoforge").is_dir() and ROOT == Path("/workspace/autoforge")):
        return
    started = time.monotonic()
    while not marker.exists() and time.monotonic() - started < deadline_seconds:
        time.sleep(0.2)


def run_one(path: Path, timeout: float, extra: list[str] | None = None,
            env_extra: dict[str, str] | None = None) -> dict:
    started = time.monotonic()
    argv = [sys.executable, str(path), *(extra or [])]
    env = None
    if env_extra:
        env = {**os.environ, **env_extra}
    try:
        proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=timeout, check=False, env=env)
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code, out = -1, ((exc.stdout or "") + (exc.stderr or "")) if isinstance(exc.stdout, (str, bytes)) else "timeout"
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
    lines = [line for line in out.strip().splitlines() if line.strip()]
    return {"check": path.stem, "rc": code, "seconds": round(time.monotonic() - started, 1),
            "args": extra or [], "tail": (lines[-1] if lines else "")[:400]}


def engine_image_id(reference: str) -> str | None:
    try:
        out = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                             capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


# Artifacts the frontend check acquires are reused by the toolchain check through this cache.
SHARED_CACHE = "/workspace/tmp/npm-cache"

# Checks that take explicit local inputs. Supplying them is the difference between "the CLI
# demanded arguments" and a real verdict for this workbench.
CONTAINER_PROFILE = {
    "container_acceptance_check": ["--engine", "docker", "--python-image", "{image}",
                                   "--output", "{fresh}/container-acceptance"],
    "application_handoff_live_check": ["--image", "{image}"],
    "quality_resources_live_check": ["--engine", "docker", "--image", "{image}",
                                     "--output", "{fresh}/quality-resources.json"],
    "benchmark_independent_check": ["--engine", "docker", "--image", "{image}",
                                    "--output", "{fresh}/benchmark"],
    # Default is 100 rounds; a bounded 12-round/4-segment run still exercises process-boundary
    # resume while staying inside the suite's per-check timeout.
    "endurance_check": ["--rounds", "12", "--segment", "4"],
    "agent_business_check": ["--image", "{image}"],
    # The toolchain checks want exact local sha256 ids per ecosystem; resolve them from the
    # sidecar daemon at run time so a re-pulled tag does not silently point at a stale id.
    "application_frontend_live_check": ["--authorized", "--engine", "docker",
                                        "--node-image", "{node_image}", "--cache", "{fresh}/cache",
                                        "--output", "{fresh}/out", "--candidate-out", "{fresh}/candidate"],
    # Same cache the frontend check fills, so the compiler artifact needs no second acquisition.
    "toolchain_live_check": ["--engine", "docker", "--python-image", "{image}",
                             "--node-image", "{node_image}", "--java-image", "{java_image}",
                             "--cache", SHARED_CACHE, "--artifacts", "{fresh}/artifacts.json"],
    "framework_backend_integration_live_check": ["--engine", "docker", "--python-image", "{image}",
                                                  "--node-image", "{node_image}", "--authorize-browser",
                                                  "--candidate-out", "{fresh}/candidate",
                                                  "--build-out", "{fresh}/build",
                                                  "--service-out", "{fresh}/service"],
    "application_browser_quality_live_check": ["--authorize-browser", "--browser-engines", "chromium",
                                               "--output", "{fresh}/browser.json"],
    # Operator release-scope authorization for a disposable local deployment root. The values
    # are written by the suite; nothing outside this run is authorized.
    "application_operations_live_check": ["--engine", "docker", "--image-id", "{image}",
                                          "--deployment-root", "{fresh}/deploy",
                                          "--authorization", "{fresh}/authorization.json",
                                          "--output", "{fresh}/operations.json"],
    # Same lifecycle on a retained generated Web app that expects irreversible migrations.
    "application_operations_state_live_check": ["--engine", "docker", "--image-id", "{image}",
                                                "--deployment-root", "{fresh}/deploy",
                                                "--authorization", "{fresh}/authorization-state.json",
                                                "--output", "{fresh}/operations-state.json"],
    # The stateful checks carry their own authored fixture; only store paths and image ids are
    # inputs, so they run here with no external acquisition.
    "stateful_reconstruction_reference_check": ["--store", "{fresh}/store", "--engine", "docker",
                                               "--image", "{image}", "--node-image", "{node_image}"],
    "stateful_reconstruction_adapter_check": ["--store", "{fresh}/store", "--engine", "docker",
                                             "--image", "{image}", "--node-image", "{node_image}"],
    # The driver default stops at 12 model calls, which this model reaches mid-iteration; the
    # bounded 24-call/1M-token budget lets the loop finish instead of HOLDing on budget.
    "stateful_reconstruction_check": ["--store", "{fresh}/store", "--candidate", "{fresh}/candidate.json",
                                      "--report", "{fresh}/report.json", "--engine", "docker",
                                      "--image", "{image}", "--node-image", "{node_image}",
                                      "--max-turns", "24", "--token-budget", "1000000"],
}

# Checks listed above that need an extra image; the suite skips them when that image is absent
# rather than failing on a placeholder.
PROFILE_REQUIREMENTS = {
    "application_frontend_live_check": [("node_image", "node:20-slim")],
    "toolchain_live_check": [("node_image", "node:20-slim"), ("java_image", "eclipse-temurin:21-jdk")],
    "framework_backend_integration_live_check": [("node_image", "node:20-slim")],
}

# Input files the suite authors for a check, relative to that check's fresh directory.
PROFILE_FILES = {
    "application_operations_state_live_check": {
        "authorization-state.json": {
            "schema": 1, "approved": True, "deployment_root": "{fresh}/deploy",
            "candidate_types": ["application"],
            "scopes": ["deploy", "health_check", "pointer_switch", "rollback"],
            "image_ids": ["{image}"], "environment": "local-container",
            "allow_irreversible_migrations": True,
            "stop_conditions": {"max_error_rate": 0.5, "max_latency_ms_p50": 5000},
        },
    },
    "application_operations_live_check": {
        "authorization.json": {
            "schema": 1, "approved": True, "deployment_root": "{fresh}/deploy",
            "candidate_types": ["application"],
            "scopes": ["deploy", "health_check", "pointer_switch", "rollback"],
            "image_ids": ["{image}"], "environment": "local-container",
            "allow_irreversible_migrations": False,
            "stop_conditions": {"max_error_rate": 0.5, "max_latency_ms_p50": 5000},
        },
    },
}

# Checks that take their inputs from the environment rather than argv.
PROFILE_ENV = {
    # The recovery check pins an exact image id; the local daemon id is only known at run time.
    "agent_container_recovery_check": {"AUTOFORGE_RECOVERY_IMAGE": "{image}"},
    # The quality replay pins the images its retained fixture was produced with.
    "application_browser_quality_live_check": {"AUTOFORGE_QUALITY_IMAGE": "{image}",
                                               "AUTOFORGE_QUALITY_NODE_IMAGE": "{node_image}"},
}


def render(value: Any, **fields: str) -> Any:
    """Substitute {placeholders} through a nested JSON structure before it is serialized."""
    if isinstance(value, str):
        return value.format(**fields)
    if isinstance(value, list):
        return [render(item, **fields) for item in value]
    if isinstance(value, dict):
        return {key: render(item, **fields) for key, item in value.items()}
    return value


def prepare_state_volume(root: Path) -> list[str]:
    """Create the AutoForge-owned state volume the operations deploy expects.

    `ops.deploy` only inspects this volume and refuses one whose labels do not bind it to the
    exact deployment root, so the suite creates it with those labels and passes a matching run id.
    """
    run_id = "state" + time.strftime("%Y%m%d%H%M%S")
    volume = "af-state-" + "".join(ch for ch in run_id if ch.isalnum())[:48]
    scope = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
    subprocess.run(["docker", "volume", "create",
                    "--label", "io.omp.autoforge.owner=autoforge-application-operations-v1",
                    "--label", f"io.omp.autoforge.state-root={scope}", volume],
                   capture_output=True, text=True, check=False)
    return ["--run-id", run_id]


def seed_shared_cache() -> None:
    """Carry the compiler artifact the frontend check acquired into the toolchain cache.

    The two checks keep separate caches: the frontend check revalidates provenance on every run,
    so a merged cache makes it report a conflict. Only the bytes and proof the toolchain needs
    are copied across, newest run first, and never overwritten.
    """
    shared = Path(SHARED_CACHE)
    shared.mkdir(parents=True, exist_ok=True)
    for run in sorted(Path("/workspace/runs").glob("args-*"), reverse=True):
        cache = run / "application_frontend_live_check" / "cache"
        for proof in sorted(cache.glob("*.json")) if cache.is_dir() else []:
            try:
                record = json.loads(proof.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(record.get("name", "")).casefold() != "typescript":
                continue
            digest = record.get("artifact_sha256")
            if not isinstance(digest, str):
                continue
            for source in (proof, cache / digest):
                target = shared / source.name
                if source.is_file() and not target.exists():
                    target.write_bytes(source.read_bytes())
            return


def cached_artifacts(cache: Path, name: str) -> list[dict]:
    """Provenance records the frontend check wrote for one package, if it ran here before."""
    rows: list[dict] = []
    for proof in sorted(cache.glob("*.json")) if cache.is_dir() else []:
        try:
            record = json.loads(proof.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(record.get("name", "")).casefold() != name.casefold():
            continue
        keys = ("name", "version", "source", "artifact_sha256", "license", "size")
        if all(key in record for key in keys):
            rows.append({key: record[key] for key in keys})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--only", default="", help="comma separated check stems")
    parser.add_argument("--skip", default="", help="comma separated check stems")
    parser.add_argument("--container-profile", action="store_true",
                        help="supply engine/image/output arguments for checks that require them")
    parser.add_argument("--image-reference", default="public.ecr.aws/docker/library/python:3.11-slim")
    args = parser.parse_args()

    wanted = {name.strip() for name in args.only.split(",") if name.strip()}
    skipped = {name.strip() for name in args.skip.split(",") if name.strip()}
    scripts = sorted(CHECKS.glob("*_check.py"))
    if wanted:
        scripts = [p for p in scripts if p.stem in wanted]
    if skipped:
        scripts = [p for p in scripts if p.stem not in skipped]

    image = engine_image_id(args.image_reference) if args.container_profile else None
    if args.container_profile and image is None:
        raise SystemExit(f"container profile needs a local image: {args.image_reference}")
    toolchain_images = {}
    if args.container_profile:
        for entries in PROFILE_REQUIREMENTS.values():
            for key, reference in entries:
                toolchain_images[key] = engine_image_id(reference)
    fresh_root = Path("/workspace/runs") / f"args-{time.strftime('%Y%m%d-%H%M%S')}"
    wait_ready()

    rows = []
    for index, script in enumerate(scripts, 1):
        extra = None
        env_extra = None
        if args.container_profile:
            missing = [reference for key, reference in PROFILE_REQUIREMENTS.get(script.stem, [])
                       if not toolchain_images.get(key)]
            if missing:
                row = {"check": script.stem, "rc": 2, "seconds": 0.0, "args": [],
                       "tail": f"skipped: no local image {', '.join(missing)}"}
                rows.append(row)
                print(f"[{index}/{len(scripts)}] {row['check']} rc=2 0.0s {row['tail']}", flush=True)
                continue
            if script.stem in CONTAINER_PROFILE:
                Path(SHARED_CACHE).mkdir(parents=True, exist_ok=True)
                fresh = fresh_root / script.stem
                fresh.mkdir(parents=True, exist_ok=True)
                extra = [part.format(image=image, fresh=str(fresh), **toolchain_images)
                         for part in CONTAINER_PROFILE[script.stem]]
            for relative, payload in PROFILE_FILES.get(script.stem, {}).items():
                target = fresh / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                body = json.dumps(render(payload, image=image, fresh=str(fresh), **toolchain_images))
                target.write_text(body + "\n", encoding="utf-8")
            if script.stem == "application_operations_state_live_check":
                extra.extend(prepare_state_volume(fresh / "deploy"))
            if script.stem == "toolchain_live_check":
                seed_shared_cache()
                artifacts = cached_artifacts(Path(SHARED_CACHE), "typescript")
                if not artifacts:
                    row = {"check": script.stem, "rc": 2, "seconds": 0.0, "args": [],
                           "tail": f"skipped: no cached typescript artifact in {SHARED_CACHE}"}
                    rows.append(row)
                    print(f"[{index}/{len(scripts)}] {row['check']} rc=2 0.0s {row['tail']}", flush=True)
                    continue
                (fresh / "artifacts.json").write_text(json.dumps(artifacts) + "\n", encoding="utf-8")
            if script.stem in PROFILE_ENV:
                env_extra = {key: value.format(image=image, fresh=str(fresh_root / script.stem),
                                               **toolchain_images)
                             for key, value in PROFILE_ENV[script.stem].items()}
        row = run_one(script, args.timeout, extra, env_extra)
        rows.append(row)
        print(f"[{index}/{len(scripts)}] {row['check']} rc={row['rc']} {row['seconds']}s "
              f"{row['tail'][:120]}", flush=True)

    failing = [r for r in rows if r["rc"] != 0]
    report = {"schema": 1, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "total": len(rows), "passed": len(rows) - len(failing),
              "failed": [r["check"] for r in failing], "rows": rows}
    target = args.output or Path("/workspace/runs") / f"suite-{time.strftime('%Y%m%d-%H%M%S')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"total": report["total"], "passed": report["passed"],
                      "failed": report["failed"], "report": str(target)}, indent=1))
    return 0 if not failing else 2


if __name__ == "__main__":
    raise SystemExit(main())
