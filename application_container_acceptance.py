#!/usr/bin/env python3
"""Opt-in container acceptance run for authored HTTP/browser fixtures; generated contracts are comparison inputs only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import application_builder
import application_secrets
import application_service

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "container_acceptance"
MAX_CONTRACT = application_builder.MAX_INPUT
TOKEN_MAP = {
    "fixture-token-alpha": "FIXTURE_AUTH_TOKEN",
    "fixture-token-beta": "OTHER_TENANT_TOKEN",
    "fixture-token-readonly": "READ_ONLY_TOKEN",
}


def _strict_json(path: Path) -> Any:
    return application_builder._load_json(path, MAX_CONTRACT)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _fixture_source() -> bytes:
    source_path = FIXTURES / "http_app.py"
    source = source_path.read_bytes()
    required = (
        b"SECRET=Path('/run/autoforge/secrets/FIXTURE_AUTH_TOKEN').read_text(encoding='utf-8')",
        b"OTHER=Path('/run/autoforge/secrets/OTHER_TENANT_TOKEN').read_text(encoding='utf-8')",
    )
    if any(source.count(declaration) != 1 for declaration in required):
        raise ValueError("authored fixture must load its two authentication secrets from private mount files")
    return source


def _manifest(workspace: Path, source: bytes) -> dict[str, Any]:
    (workspace / "main.py").write_bytes(source)
    record = {"path": "main.py", "size": len(source), "sha256": hashlib.sha256(source).hexdigest()}
    base = {"schema": 1, "goal": "authored disposable container HTTP fixture", "references": [], "files": [record]}
    digest = hashlib.sha256(_canonical(base)).hexdigest()
    raw = {**base, "candidate_sha256": digest, "workspace": str(workspace.resolve())}
    (workspace / "manifest.json").write_bytes(_canonical(raw))
    # Validate exactly through the same strict path used by application_service.
    application_builder._manifest(raw)
    return {"schema": 1, "workspace_manifest": raw, "candidate_sha256": digest}


def _replace_authored_tokens(value: Any) -> Any:
    if isinstance(value, str):
        for literal, name in TOKEN_MAP.items():
            value = value.replace(literal, "{$secret_file:" + name + "}")
        return value
    if isinstance(value, list):
        return [_replace_authored_tokens(item) for item in value]
    if isinstance(value, dict):
        return {key: _replace_authored_tokens(item) for key, item in value.items()}
    return value


def _redact(value: Any, secrets: dict[str, str]) -> Any:
    if isinstance(value, str):
        for secret in secrets.values():
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, secrets) for key, item in value.items()}
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _browser_hashes(output: Path | None) -> list[dict[str, Any]]:
    if output is None or not output.exists():
        return []
    if output.is_symlink() or not output.is_dir():
        raise ValueError("browser output must be a real directory")
    records = []
    for path in sorted(output.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("browser output contains link or special file")
        if path.is_file():
            records.append({"path": path.relative_to(output).as_posix(), "size": path.stat().st_size, "sha256": _hash_file(path)})
    return records


def _secret_values() -> dict[str, str]:
    values = {
        "FIXTURE_AUTH_TOKEN": "acceptance-fixture-alpha-" + uuid.uuid4().hex,
        "OTHER_TENANT_TOKEN": "acceptance-fixture-beta-" + uuid.uuid4().hex,
        "READ_ONLY_TOKEN": "acceptance-fixture-readonly-" + uuid.uuid4().hex,
    }
    env_map = {"FIXTURE_AUTH_TOKEN": "AUTOFORGE_FIXTURE_AUTH_TOKEN", "OTHER_TENANT_TOKEN": "AUTOFORGE_OTHER_TENANT_TOKEN", "READ_ONLY_TOKEN": "AUTOFORGE_READ_ONLY_TOKEN"}
    for name, env_name in env_map.items():
        if env_name in os.environ:
            values[name] = os.environ[env_name]
    return values


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.output.is_absolute():
        raise ValueError("--output must be absolute")
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("--output must be a new path; refusing overwrite")
    if not args.private_root.is_absolute() or not args.audit.is_absolute():
        raise ValueError("--private-root and --audit must be absolute")
    if args.output.resolve() == args.audit.resolve():
        raise ValueError("--output and --audit must be different files")
    if args.audit.exists() and (args.audit.is_symlink() or not args.audit.is_file()):
        raise ValueError("--audit must be a regular file when it exists")
    if args.browser_contract and not args.authorize_browser:
        raise ValueError("browser execution requires --authorize-browser")
    if args.browser_contract and args.browser_output is None:
        raise ValueError("--browser-contract requires --browser-output")
    if args.browser_output is not None and not args.browser_output.is_absolute():
        raise ValueError("--browser-output must be absolute")
    if args.browser_output is not None and (args.browser_output.exists() or args.browser_output.is_symlink()):
        raise ValueError("--browser-output must be a new directory")
    if args.browser_output is not None and (args.browser_output.resolve() == args.output.resolve() or args.browser_output.resolve() == args.private_root.resolve()):
        raise ValueError("browser output must be distinct from report and private secrets")
    authored_contract = (FIXTURES / "http_contract.json").resolve(strict=True)
    contract_path = (args.contract or authored_contract).resolve(strict=True)
    if contract_path != authored_contract:
        raise ValueError("this runner only executes the authored HTTP contract; generated contracts are reference-only")
    browser_path = args.browser_contract.resolve(strict=True) if args.browser_contract else None
    if browser_path == (FIXTURES / "generated_browser_contract.json").resolve():
        raise ValueError("generated browser contract is reference-only and is never run here")
    contract_original = _strict_json(contract_path)
    contract = _replace_authored_tokens(contract_original)
    browser_contract = _replace_authored_tokens(_strict_json(browser_path)) if browser_path else None
    source_path = FIXTURES / "http_app.py"
    authored_hash = _hash_file(source_path)
    contract_hash = _hash_file(contract_path)
    browser_contract_hash = _hash_file(browser_path) if browser_path else None
    secret_manifest = None
    detail = None
    error = None
    with tempfile.TemporaryDirectory(prefix="autoforge-container-acceptance-") as tmp:
        workspace = Path(tmp) / "candidate"
        workspace.mkdir()
        manifest = _manifest(workspace, _fixture_source())
        values = _secret_values()
        try:
            secret_manifest = application_secrets.prepare_runtime_secrets(
                values, private_root=args.private_root,
                session_id="container-acceptance-" + uuid.uuid4().hex,
                allowed_names={"FIXTURE_AUTH_TOKEN", "OTHER_TENANT_TOKEN", "READ_ONLY_TOKEN"})
            detail = application_service.run(
                manifest["workspace_manifest"], contract, args.engine, args.image,
                ["python", "-I", "/candidate/main.py"], args.timeout,
                browser_contract=browser_contract,
                browser_output=args.browser_output,
                browser_authorized=args.authorize_browser,
                secret_mounts=secret_manifest["mounts"])
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            cleanup = None
            if secret_manifest is not None:
                cleanup = application_secrets.cleanup_runtime_secrets(secret_manifest, audit_path=args.audit)
        report = {
            "schema": 1,
            "status": "PASS" if detail is not None and detail.get("test_pass") is True else "FAIL",
            "fixture": "authored",
            "service_source": {"path": str(source_path), "sha256": authored_hash, "staged_sha256": manifest["candidate_sha256"]},
            "http_contract": {"path": str(contract_path), "sha256": contract_hash},
            "browser_contract": ({"path": str(browser_path), "sha256": browser_contract_hash} if browser_path else None),
            "candidate_sha256": manifest["candidate_sha256"],
            "engine": args.engine,
            "argv": ["python", "-I", "/candidate/main.py"],
            "result": detail,
            "http_results": detail.get("http_results") if isinstance(detail, dict) else None,
            "screenshot_artifacts": (detail.get("browser", {}).get("artifacts", []) if isinstance(detail, dict) and isinstance(detail.get("browser"), dict) else _browser_hashes(args.browser_output)),
            "secret_names": secret_manifest["secret_names"] if secret_manifest else [],
            "secret_values_exposed": False,
            "secret_cleanup": cleanup,
            "error": error,
            "generated_integration": {
                "request": str(FIXTURES / "request.json"),
                "http_contract": str(FIXTURES / "generated_http_contract.json"),
                "browser_contract": str(FIXTURES / "generated_browser_contract.json"),
                "executed": False,
                "usage": "Generated request and contracts are recorded as separate integration references only; this runner intentionally stages the authored fixture and never claims generated-application success.",
            },
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        report = _redact(report, values if secret_manifest else {})
        payload = _canonical(report) + b"\n"
        if len(payload) > application_builder.MAX_REPORT:
            raise ValueError("acceptance report exceeds size limit")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            raise ValueError("refusing to overwrite existing report")
        args.output.write_bytes(payload)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-fixture", action="store_true", required=True, help="explicitly authorize a local container run")
    parser.add_argument("--engine", choices=("docker", "podman"), required=True)
    parser.add_argument("--image", required=True, help="immutable sha256:<64 hex> container image reference")
    parser.add_argument("--contract", type=Path, help="HTTP contract; defaults to authored fixture contract")
    parser.add_argument("--browser-contract", type=Path, help="optional declarative browser contract")
    parser.add_argument("--authorize-browser", action="store_true", help="explicitly authorize browser interaction")
    parser.add_argument("--browser-output", type=Path, help="absolute directory for browser artifacts")
    parser.add_argument("--output", type=Path, required=True, help="absolute new evidence report path")
    parser.add_argument("--private-root", type=Path, required=True, help="existing private directory for temporary secrets")
    parser.add_argument("--audit", type=Path, required=True, help="secret cleanup audit JSONL path")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
            raise ValueError("--image must be immutable sha256:<64 lowercase hex>")
        if not 1 <= args.timeout <= 180:
            raise ValueError("--timeout must be in 1..180 seconds")
        if not args.private_root.is_absolute() or not args.audit.is_absolute():
            raise ValueError("--private-root and --audit must be absolute")
        report = run(args)
        sys.stdout.buffer.write(_canonical({"status": report["status"], "report": str(args.output)}) + b"\n")
        return 0 if report["status"] == "PASS" else 2
    except (OSError, ValueError, application_builder.BuildFailure, application_service.ServiceFailure, application_secrets.SecretInjectionRefused) as exc:
        sys.stderr.buffer.write(_canonical({"status": "FAIL", "reason": str(exc)}) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
