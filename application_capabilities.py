#!/usr/bin/env python3
"""Readiness, evidence-backed check execution, and bounded capability matrix."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent
# Explicit, reviewed check inventory. This CLI is deliberately absent to prevent recursion.
CHECKS = (
    ("agent_session_check.py", "local session state and persistence"),
    ("agent_tools_check.py", "tool schemas and dispatch"),
    ("agent_stream_check.py", "local SSE transport protocol"),
    ("agent_model_retry_check.py", "real HTTP bounded model retries and no partial-tool dispatch"),
    ("agent_runtime_check.py", "runtime lifecycle and controls"),
    ("agent_budget_check.py", "budget accounting"),
    ("agent_stop_check.py", "real HTTP stop, capability revocation and terminal resume"),
    ("agent_control_check.py", "authenticated cursor reads and capability ownership"),
    ("agent_oracle_check.py", "coarse hidden validation HOLD/unknown and source binding"),
    ("application_oracle_check.py", "finite heldout queries and feedback redaction"),
    ("application_toolchain_check.py", "offline build input and lock boundaries; no container run"),
    ("application_data_policy_check.py", "exact-endpoint disclosure and retention lifecycle"),
    ("application_effects_check.py", "actual local peer lost-response reconciliation"),
    ("reference_reconstruction_check.py", "isolated reconstruction protocol; no real container run"),
    ("application_operations_check.py", "authorized lifecycle scope and evidence integrity; no deployment run"),
    ("application_release_check.py", "release gate and package integrity"),
    ("application_experience_adapter_check.py", "experience adapter contracts"),
    ("agent_recovery_check.py", "runtime recovery and replay safeguards"),
    ("agent_reconcile_check.py", "operator reconciliation refusal guards; no candidate execution"),
    ("proposer_check.py", "proposal validation"),
    ("reference_check.py", "local reference corpus governance"),
    ("application_auth_check.py", "credential handling and authorization"),
    ("application_dependencies_check.py", "offline dependency artifact governance"),
    ("application_image_scan_check.py", "exact local image scanner identity and fail-closed evidence"),
    ("application_components_check.py", "durable components and real local TLS side effects"),
    ("application_components_service_check.py", "authenticated component HTTP service and persistent restart"),
    ("application_platform_check.py", "native authority guards and authored loopback realtime protocol; no native or container acceptance"),
    ("application_android_build_check.py", "offline SDK metadata, archive and explicit-license guards; no native execution"),
    ("benchmark_independent_check_check.py", "public benchmark semantic boundaries and deliberate wrong-output detection"),
    ("application_project_check.py", "project transactions"),
    ("application_database_check.py", "database adapter and evidence"),
    ("application_browser_check.py", "browser adapter and evidence"),
    ("application_service_check.py", "service adapter contract"),
    ("application_plan_check.py", "planning contract"),
    ("application_spec_check.py", "specification contract"),
    ("specification_adapter_check.py", "data-only autonomous specification repair and controller obligation preservation"),
    ("application_quality_check.py", "quality policy"),
    ("application_learning_check.py", "learning provenance"),
    ("application_delivery_check.py", "delivery lifecycle"),
    ("application_delivery_maintenance_check.py", "immutable package export and read-only checkpoint diagnostics"),
    ("application_build_check.py", "candidate build policy"),
    ("application_evaluation_check.py", "evaluation evidence reconciliation"),
    ("application_stage_evidence_check.py", "immutable G0-G4 snapshot bytes, chained dependencies and no automatic approval"),
    ("application_check.py", "bounded candidate adapter protocol"),
    ("application_policy_check.py", "application policy safeguards"),
    ("application_resume_check.py", "resume lifecycle"),
    ("application_capabilities_check.py", "readiness, mixed-result, and capability denominator safeguards"),
    ("agent_live_check.py", "local live-model endpoint"),
)
KINDS = ("inventory", "order", "ticket", "content")


def _json(path: str | None, value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    print(text, end="")


def _module_ready(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check() -> dict:
    """Inspect local prerequisites only. Never contacts endpoints or runs candidates."""
    files = {name: (ROOT / name).is_file() for name, _ in CHECKS}
    py = shutil.which("python") or shutil.which("python3")
    optional_modules = {name: _module_ready(name) for name in ("sqlalchemy", "playwright", "selenium", "psycopg")}
    optional_modules["sqlite3"] = _module_ready("sqlite3")
    engine = {"docker_or_podman": bool(shutil.which("docker") or shutil.which("podman")),
              "container_configured": bool(os.environ.get("AUTOFORGE_CONTAINER_IMAGE"))}
    # Live acceptance uses a fixed localhost endpoint; this is configuration,
    # not service ownership or evidence that it is reachable (check must not probe).
    model_prerequisite = {"endpoint": "http://127.0.0.1:8081/v1", "reachability": "NOT_CHECKED", "model_id": "NOT_CHECKED"}
    database_configured = bool(os.environ.get("AUTOFORGE_DATABASE_URL"))
    browser_configured = bool(os.environ.get("AUTOFORGE_BROWSER_ENDPOINT"))
    blockers = []
    if not all(files.values()): blockers.append("one or more curated check scripts are missing")
    if not py: blockers.append("Python interpreter is unavailable")
    if not engine["docker_or_podman"]: blockers.append("container engine is unavailable")
    if not engine["container_configured"]: blockers.append("container image is not configured")
    # Endpoint reachability is intentionally unknown without a live request.
    if not optional_modules["playwright"]: blockers.append("Playwright dependency is unavailable; browser execution remains blocked")
    if not optional_modules["sqlite3"]: blockers.append("SQLite Python module is unavailable")
    if not database_configured: blockers.append("database endpoint is not configured")
    if not browser_configured: blockers.append("browser endpoint is not configured")
    return {"schema": 1, "status": "READY" if not blockers else "PARTIAL", "scope": "local prerequisite inspection only; no HTTP, model request, database/browser connection, install, or candidate execution", "python": py, "curated_checks": files, "dependencies": optional_modules, "container": engine, "model": model_prerequisite, "configured": {"database_endpoint": database_configured, "browser_endpoint": browser_configured}, "blockers": blockers}


def verify(live_model: bool = False, timeout: int = 120) -> dict:
    rows = []
    for script, scope in CHECKS:
        path = ROOT / script
        if script == "agent_live_check.py" and not live_model:
            rows.append({"check": script, "status": "BLOCKED", "scope": scope, "reason": "requires explicit --live-model authorization; skipped"})
            continue
        if not path.is_file():
            rows.append({"check": script, "status": "BLOCKED", "scope": scope, "reason": "curated check script absent"})
            continue
        try:
            with tempfile.TemporaryFile(mode="w+b") as out_file, tempfile.TemporaryFile(mode="w+b") as err_file:
                proc = subprocess.Popen([sys.executable, str(path)], cwd=ROOT, stdout=out_file, stderr=err_file)
                try:
                    returncode = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise
                def read_bounded(stream):
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    stream.seek(max(0, size - 65536))
                    return stream.read(65536).decode("utf-8", errors="replace")
                stdout, stderr = read_bounded(out_file), read_bounded(err_file)
            output = (stdout + ("\n" + stderr if stderr else "")).strip()
            structured = None
            for line in reversed(stdout.splitlines()[-200:]):
                try:
                    parsed = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(parsed, dict):
                    structured = parsed
                    break
            observed = []
            if isinstance(structured, dict):
                def collect_statuses(value, key=""):
                    if isinstance(value, dict):
                        for child_key, child in value.items():
                            collect_statuses(child, str(child_key))
                    elif isinstance(value, list):
                        for child in value:
                            collect_statuses(child, key)
                    elif isinstance(value, str):
                        marker = value.upper()
                        key_marker = key.upper()
                        if marker in {"BLOCKED", "NOT_RUN", "NOT_TESTED", "NOT_EXERCISED", "IMPLEMENTED_NOT_EXERCISED", "UNSUPPORTED"}:
                            observed.append(marker)
                        if marker in {"PASS", "FAIL", "HOLD", "REFUSED"} and ("STATUS" in key_marker or key_marker in {"BROWSER", "CONTAINER", "RUNTIME"}):
                            observed.append(marker)
                collect_statuses(structured)
                if structured.get("blockers"): observed.append("BLOCKED")
            reported = {str(value).upper() for value in observed if value is not None}
            if returncode != 0:
                status = "BLOCKED" if reported & {"BLOCKED", "NOT_RUN", "IMPLEMENTED_NOT_EXERCISED", "NOT_TESTED"} else "FAIL"
            elif reported & {"BLOCKED", "NOT_RUN", "IMPLEMENTED_NOT_EXERCISED", "NOT_TESTED"}:
                status = "BLOCKED"
            elif reported & {"FAIL", "HOLD", "REFUSED"}:
                status = "FAIL"
            elif "PASS" in reported or structured is None:
                status = "PASS"
            else:
                status = "BLOCKED"
            rows.append({"check": script, "status": status, "exit_code": returncode, "scope": scope,
                         "evidence": output, "evidence_paths": [], "reported_statuses": sorted(reported)})
        except subprocess.TimeoutExpired:
            rows.append({"check": script, "status": "BLOCKED", "scope": scope, "reason": f"timed out after {timeout}s"})
        except OSError as exc:
            rows.append({"check": script, "status": "BLOCKED", "scope": scope, "reason": str(exc)})
    status = "PASS" if rows and all(row["status"] == "PASS" for row in rows) else "PARTIAL"
    capability_obligations_lacking_evidence = len(KINDS)
    if capability_obligations_lacking_evidence:
        status = "PARTIAL"
    counts = {state.lower(): sum(row["status"] == state for row in rows) for state in ("PASS", "FAIL", "BLOCKED")}
    per_check = {row["check"]: {"status": row["status"], "reported_statuses": row.get("reported_statuses", [])} for row in rows}
    not_tested = sorted({marker for row in rows for marker in row.get("reported_statuses", []) if marker in {"NOT_RUN", "NOT_TESTED", "NOT_EXERCISED", "IMPLEMENTED_NOT_EXERCISED"}})
    return {"schema": 1, "status": status, "scope": "curated existing check scripts only; no install or candidate execution except behavior explicitly exercised by a named script", "live_model_authorized": live_model, "checks": rows, "counts": counts, "per_check": per_check, "scope_limitations": {"not_tested_markers": not_tested}, "capability_obligations_lacking_verified_evidence": capability_obligations_lacking_evidence, "limitations": ["A passing check is evidence only for its stated scope; this report does not establish end-to-end application capability.", "Paid or remote model endpoints are never invoked by this CLI."]}


def matrix() -> dict:
    modules = ("application_service.py", "application_database.py", "application_browser.py")
    present = [p for p in modules if (ROOT / p).is_file()]
    rows = []
    for kind in KINDS:
        rows.append({"task_kind": kind, "stack": "application_service + application_database + application_browser",
                     "status": "unsupported", "adapter_availability": "implemented_unverified" if len(present) == len(modules) else "blocked",
                     "scope": "shared adapter modules are not task-kind-specific implementations; no task-kind execution verified",
                     "evidence": present, "blocked_prerequisites": [] if len(present) == len(modules) else [p for p in modules if p not in present]})
    obligations = len(rows)
    return {"schema": 1, "status": "PARTIAL", "task_kinds": list(KINDS),
            "denominator": {"unit": "task-kind × declared shared stack", "obligations": obligations,
                            "verified": 0, "lacking_verified_evidence": obligations},
            "capabilities": rows,
            "limitations": ["Shared adapter availability is not a task-kind implementation or acceptance result.",
                            "No task-kind-specific engine or live integration evidence is inferred.",
                            "No percentage or fabricated sample count is reported."]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="inspect prerequisites without network or execution")
    c.add_argument("--output")
    v = sub.add_parser("verify", help="run the fixed curated check-script inventory")
    v.add_argument("--live-model", action="store_true", help="explicitly authorize the local live-model check only")
    v.add_argument("--timeout", type=int, default=120)
    v.add_argument("--output")
    m = sub.add_parser("matrix", help="report implemented, verified, and blocked support by task kind")
    m.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command == "verify" and args.timeout < 1:
        parser.error("--timeout must be positive")
    result = check() if args.command == "check" else verify(args.live_model, args.timeout) if args.command == "verify" else matrix()
    _json(args.output, result)
    return 0 if result["status"] in {"READY", "PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())


