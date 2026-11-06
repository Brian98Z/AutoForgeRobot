"""Explicit in-process tool capabilities and narrow application candidate policy."""
from __future__ import annotations

import re
import time
import uuid
from typing import Any

MAX_TTL_SECONDS = 24 * 60 * 60
MAX_TOOLS = 32
# Protect explicit control/runtime files only; ordinary product paths remain available.
IMMUTABLE_CORE_PATHS = frozenset({"agent_tools.py", "agent_tools_check.py", "agent_runtime.py", "agent_runtime_check.py", "agent_session.py", "agent_session_check.py", "agent_transport.py", "agent_transport_check.py", "agent_control_check.py", "application_policy.py", "application_policy_check.py", "application_secrets.py", "application_auth.py", "application_auth_check.py", "application_plan.py", "application_plan_check.py", "application_data_policy.py", "application_data_policy_check.py", "application_effects.py", "application_effects_check.py", "application_spec.py", "application_spec_check.py", "application_capabilities.py", "application_capabilities_check.py", "application_build.py", "application_build_check.py", "application_checkpoint.py", "application_builder.py", "application_evaluation.py", "application_evaluation_check.py", "applications.py", "fixtures/business/contract.json", "fixtures/business/ast_verifier.py", "trust/", "trusted/", "oracle/", "charter/", "policy/", "evaluator/"})
IMMUTABLE_CORE_PATHS = IMMUTABLE_CORE_PATHS | frozenset({'agent_reconcile.py', 'application_oracle.py', 'application_oracle_check.py'})
_SENSITIVE_EXACT = frozenset({".env", "credentials", "credential", "secret", "secrets", "password", "passwd", "private.key", "private_key", "id_rsa", "id_ed25519", "api_key", "apikey"})
_CAPABILITIES: dict[str, dict[str, Any]] = {}


class PolicyRefused(ValueError):
    """A capability or candidate policy denied an operation."""


def grant(session_id: str, tools: list[str], *, ttl_seconds: int | float = 3600) -> dict:
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise PolicyRefused("session_id must be bounded nonempty text")
    if not isinstance(tools, list) or not tools or len(tools) > MAX_TOOLS or any(not isinstance(x, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", x) for x in tools) or len(set(tools)) != len(tools):
        raise PolicyRefused("tools must be a unique bounded allowlist")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise PolicyRefused("ttl_seconds is outside the permitted range")
    token = uuid.uuid4().hex + uuid.uuid4().hex
    record = {"session_id": session_id, "tools": frozenset(tools), "expires_at": time.monotonic() + ttl_seconds, "revoked": False}
    _CAPABILITIES[token] = record
    return {"token": token, "session_id": session_id, "tools": sorted(record["tools"]), "expires_at": record["expires_at"]}


def authorize(capability: dict | str, session_id: str, name: str) -> None:
    token = capability.get("token") if isinstance(capability, dict) else capability
    record = _CAPABILITIES.get(token) if isinstance(token, str) else None
    if record is None or record["revoked"]:
        raise PolicyRefused("capability is unknown or revoked")
    if not isinstance(session_id, str) or session_id != record["session_id"]:
        raise PolicyRefused("capability belongs to another session")
    if time.monotonic() >= record["expires_at"]:
        record["revoked"] = True
        raise PolicyRefused("capability expired")
    if not isinstance(name, str) or name not in record["tools"]:
        raise PolicyRefused("tool is outside capability scope")


def revoke(capability: dict | str) -> None:
    token = capability.get("token") if isinstance(capability, dict) else capability
    record = _CAPABILITIES.get(token) if isinstance(token, str) else None
    if record is not None:
        record["revoked"] = True


def check_candidate_path(path: str, *, candidate_kind: str = "product") -> None:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise PolicyRefused("candidate path must be nonempty text")
    if candidate_kind not in {"product", "strategy"}:
        raise PolicyRefused("candidate kind must be product or strategy")
    normalized = path.replace("\\", "/").strip("/").casefold()
    parts = normalized.split("/")
    if any(part in {".", ".."} for part in parts):
        raise PolicyRefused("candidate path traversal refused")
    protected = {p.casefold() for p in IMMUTABLE_CORE_PATHS}
    if (any(normalized == p.rstrip("/") or normalized.startswith(p) for p in protected if p.endswith("/"))
            or normalized in protected or any(part in protected for part in parts)
            or any(part in _SENSITIVE_EXACT or part.startswith(".env.") for part in parts)):
        raise PolicyRefused("candidate path is protected")

def authorize_provider(provider: str, configured_provider: str | None) -> None:
    """Bind use to exact configured provider identity; performs no discovery or network RPC."""
    if not isinstance(configured_provider, str) or not configured_provider.strip():
        raise PolicyRefused("no provider is explicitly configured")
    if not isinstance(provider, str) or provider != configured_provider:
        raise PolicyRefused("provider is not the explicitly configured binding")


def allowed_source_path(path: str, allowed_roots: list[str]) -> str:
    """Require an explicit source root and return only a validated relative POSIX path."""
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/") or "\x00" in path:
        raise PolicyRefused("source path must be relative POSIX text")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PolicyRefused("source path traversal refused")
    check_candidate_path(path)
    if not isinstance(allowed_roots, list) or not allowed_roots:
        raise PolicyRefused("no source roots are explicitly allowed")
    roots = []
    for root in allowed_roots:
        if not isinstance(root, str) or not root or "\\" in root or root.startswith("/") or any(x in {"", ".", ".."} for x in root.split("/")):
            raise PolicyRefused("allowed source root malformed")
        roots.append(root.casefold().rstrip("/"))
    folded = path.casefold()
    if not any(folded == root or folded.startswith(root + "/") for root in roots):
        raise PolicyRefused("source path is outside configured roots")
    return path


def validate_staged_artifact(kind: str, value: Any, *, approval: bool = False) -> dict:
    """Validate product/strategy artifacts as distinct drafts; never approve or promote them."""
    if kind == "product":
        import application_spec
        clean = application_spec.validate_spec(value, require_approved=False)
        if approval and clean.get("approval", {}).get("status") != "approved":
            raise PolicyRefused("product specification lacks explicit approval")
        return {"kind": "product", "status": "staged", "artifact": clean}
    if kind == "strategy":
        if not isinstance(value, dict) or set(value) != {"schema", "strategy_id", "version", "goal", "steps", "approval"}:
            raise PolicyRefused("strategy artifact fields mismatch")
        if type(value["schema"]) is not int or value["schema"] != 1 or type(value["version"]) is not int or value["version"] < 1:
            raise PolicyRefused("strategy schema/version invalid")
        for key in ("strategy_id", "goal"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise PolicyRefused(f"strategy {key} must be nonempty text")
        if not isinstance(value["steps"], list) or not value["steps"] or any(not isinstance(step, str) or not step.strip() for step in value["steps"]):
            raise PolicyRefused("strategy steps must be nonempty text entries")
        record = value["approval"]
        if not isinstance(record, dict) or set(record) != {"status", "approved_by", "approved_at"} or record["status"] != "draft":
            raise PolicyRefused("strategy approval record malformed")
        if record["approved_by"] != "" or record["approved_at"] != "":
            raise PolicyRefused("candidate strategy cannot claim approval identity")
        if approval:
            raise PolicyRefused("strategy approval must be granted through external promotion")
        return {"kind": "strategy", "status": "staged", "artifact": value}
    raise PolicyRefused("kind must be product or strategy")
def evaluate_strategy(strategy: dict, *, protected_paths: list[str]) -> dict:
    """Evaluate strategy isolation without authorizing promotion."""
    import hashlib, json
    staged = validate_staged_artifact("strategy", strategy)
    if not isinstance(protected_paths, list) or not protected_paths:
        raise PolicyRefused("trusted charter/trust/oracle paths must be explicitly supplied")
    findings = []
    for raw in protected_paths:
        if not isinstance(raw, str) or not raw:
            raise PolicyRefused("protected path inventory is malformed")
        norm = raw.replace("\\", "/").strip("/").casefold()
        if any(part in {".", ".."} for part in norm.split("/")):
            raise PolicyRefused("protected path inventory contains traversal")
        for index, step in enumerate(strategy["steps"]):
            if norm and norm in step.casefold():
                findings.append({"path": raw, "strategy_step": index, "finding": "mentions_protected_path"})
    digest = hashlib.sha256(json.dumps(staged["artifact"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return {"strategy_id": strategy["strategy_id"], "strategy_sha256": digest, "isolated": not findings, "findings": findings, "promotion": "not_authorized"}


def promote_strategy(strategy: dict, *, evaluation: dict, operator: str, authorization_source: str, expected_sha256: str) -> dict:
    """Promote only exact isolated bytes with a separately supplied operator authorization."""
    import datetime, hashlib, json
    staged = validate_staged_artifact("strategy", strategy)
    actual = hashlib.sha256(json.dumps(staged["artifact"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if not isinstance(operator, str) or not operator.strip() or not isinstance(authorization_source, str) or not authorization_source.strip():
        raise PolicyRefused("external operator and authorization source are required")
    if expected_sha256 != actual or not isinstance(evaluation, dict) or evaluation.get("strategy_sha256") != actual or evaluation.get("isolated") is not True or evaluation.get("findings"):
        raise PolicyRefused("strategy evaluation is missing, unsafe, or bound to different bytes")
    result = dict(strategy)
    result["approval"] = {"status": "approved", "approved_by": operator.strip(), "approved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    return {"kind": "strategy", "status": "promoted", "authorization_source": authorization_source.strip(), "artifact": result, "strategy_sha256": actual}
