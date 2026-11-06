"""Controller-owned finite query ledger for independent application validation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

import application_checkpoint as checkpoint


class OracleRefused(ValueError):
    """An oracle query violates its immutable policy or ledger state."""


_QUERY_KINDS = {"public_repair", "heldout_final"}
_HELDOUT_REASONS = {"build_failed", "behavior_failed", "environment_blocked", "output_invalid", "unknown_outcome", "query_limit", "unknown"}

def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise OracleRefused(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical(value: Any) -> bytes:
    try:
        return checkpoint.canonical(value)
    except (TypeError, ValueError) as exc:
        raise OracleRefused("oracle ledger data is not canonical JSON") from exc


class OracleLedger:
    """Crash-safe allowance bound to one exact hidden contract and query cap.

    A reservation is durably spent before dispatch. Reserved entries are never
    reclaimed or replayed: a crash makes their outcome unknown, not reusable.
    """

    def __init__(self, path: Path, *, contract_sha256: str, max_queries: int):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.contract_sha256 = _sha256(contract_sha256, "contract_sha256")
        if type(max_queries) is not int or not 1 <= max_queries <= 10000:
            raise OracleRefused("max_queries must be an operator-configured integer in 1..10000")
        self.max_queries = max_queries
        checkpoint.regular_chain(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        checkpoint.regular_chain(self.path)
        if not self.path.exists():
            return {"schema": 1, "contract_sha256": self.contract_sha256,
                    "max_queries": self.max_queries, "entries": []}
        try:
            raw = self.path.read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                raise OracleRefused("oracle ledger exceeds size limit")
            envelope = json.loads(raw.decode("utf-8"))
            state = envelope["state"]
            if (set(envelope) != {"schema", "state", "sha256"} or envelope["schema"] != 1
                    or envelope["sha256"] != checkpoint.digest(_canonical(state))):
                raise OracleRefused("oracle ledger integrity failure")
            if not isinstance(state, dict) or set(state) != {"schema", "contract_sha256", "max_queries", "entries"}:
                raise OracleRefused("oracle ledger schema mismatch")
            if state["contract_sha256"] != self.contract_sha256 or state["max_queries"] != self.max_queries:
                raise OracleRefused("oracle contract or query policy differs from persisted ledger")
            entries = state["entries"]
            if not isinstance(entries, list) or len(entries) > self.max_queries:
                raise OracleRefused("oracle ledger entry inventory invalid")
            ids = set()
            for entry in entries:
                if (not isinstance(entry, dict) or entry.get("query_id") in ids
                        or entry.get("kind") not in _QUERY_KINDS
                        or entry.get("state") not in {"reserved", "complete"}):
                    raise OracleRefused("oracle ledger entry invalid")
                ids.add(entry["query_id"])
            return state
        except OracleRefused:
            raise
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
            raise OracleRefused("oracle ledger unreadable or malformed") from exc

    def _save(self, state: dict[str, Any]) -> None:
        payload = _canonical(state)
        envelope = {"schema": 1, "state": state, "sha256": checkpoint.digest(payload)}
        checkpoint._atomic(self.path, _canonical(envelope) + b"\n")

    def reserve(self, candidate_sha256: str, kind: str) -> str:
        _sha256(candidate_sha256, "candidate_sha256")
        if kind not in _QUERY_KINDS:
            raise OracleRefused("query kind must be public_repair or heldout_final")
        try:
            with checkpoint.ExclusiveLock(self.lock_path):
                state = self._load()
                if any(entry["state"] == "reserved" for entry in state["entries"]):
                    raise OracleRefused("unresolved prior query; external outcome unknown")
                if len(state["entries"]) >= self.max_queries:
                    raise OracleRefused("oracle query allowance exhausted")
                query_id = os.urandom(16).hex()
                state["entries"].append({"query_id": query_id, "candidate_sha256": candidate_sha256,
                                         "kind": kind, "state": "reserved"})
                self._save(state)
                return query_id
        except checkpoint.ResumeRefused as exc:
            raise OracleRefused("oracle ledger is locked or unsafe") from exc

    def complete(self, query_id: str, result: dict[str, Any], *, artifact_sha256: str) -> dict[str, Any]:
        if not isinstance(query_id, str) or len(query_id) != 32 or any(c not in "0123456789abcdef" for c in query_id):
            raise OracleRefused("invalid query id")
        artifact = _sha256(artifact_sha256, "artifact_sha256")
        try:
            with checkpoint.ExclusiveLock(self.lock_path):
                state = self._load()
                entry = next((item for item in state["entries"] if item["query_id"] == query_id), None)
                if entry is None or entry["state"] != "reserved":
                    raise OracleRefused("query is unknown or already finalized; dispatch cannot be replayed")
                if not isinstance(result, dict):
                    raise OracleRefused("oracle result must be an object")
                if entry["kind"] == "public_repair":
                    # Public repair intentionally retains the existing detailed validation contract.
                    public = dict(result)
                    public["artifact_sha256"] = artifact
                    stored = {"status": str(result.get("status", "HOLD")), "artifact_sha256": artifact,
                              "public_result": public}
                    response = public
                else:
                    status = result.get("status")
                    if status not in {"PASS", "FAIL", "HOLD"}:
                        status = "HOLD"
                    reasons = result.get("reason_codes", [])
                    if not isinstance(reasons, list):
                        reasons = []
                    reasons = sorted({r for r in reasons if isinstance(r, str) and r in _HELDOUT_REASONS})[:8]
                    if status != "PASS" and not reasons:
                        reasons = ["unknown"]
                    response = {"status": status, "artifact_sha256": artifact, "reason_codes": reasons}
                    stored = dict(response)
                entry["state"] = "complete"
                entry["result"] = stored
                self._save(state)
                return response
        except checkpoint.ResumeRefused as exc:
            raise OracleRefused("oracle ledger is locked or unsafe") from exc

    def run_query(self, candidate_sha256: str, kind: str, dispatch: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Reserve first, dispatch once, and never finalize an ambiguous external effect."""
        query_id = self.reserve(candidate_sha256, kind)
        result = dispatch()
        if not isinstance(result, dict):
            raise OracleRefused("dispatch returned no report; reservation remains spent and unknown")
        if result.get("external_effect") == "unknown":
            return {"status": "HOLD", "artifact_sha256": result.get("artifact_sha256", candidate_sha256),
                    "reason_codes": ["unknown_outcome"], "external_effect": "unknown"}
        artifact = result.get("artifact_sha256", candidate_sha256)
        completed = self.complete(query_id, result, artifact_sha256=artifact)
        return {**completed, "external_effect": result.get("external_effect", "complete")}


def heldout_result(builder_report: dict[str, Any], *, artifact_sha256: str) -> dict[str, Any]:
    """Reduce a trusted adapter report to bounded, non-case-specific feedback."""
    artifact = _sha256(artifact_sha256, "artifact_sha256")
    if not isinstance(builder_report, dict):
        return {"status": "HOLD", "artifact_sha256": artifact, "reason_codes": ["unknown"]}
    if builder_report.get("status") == "BLOCKED":
        status, reasons = "HOLD", ["environment_blocked"]
    elif builder_report.get("build_pass") is True and builder_report.get("test_pass") is True:
        status, reasons = "PASS", []
    elif builder_report.get("build_pass") is False or builder_report.get("test_pass") is False:
        status, reasons = "FAIL", ["behavior_failed"]
    else:
        status, reasons = "HOLD", ["unknown"]
    return {"status": status, "artifact_sha256": artifact, "reason_codes": reasons}



def _adapter_main(argv: list[str] | None = None) -> int:
    """Trusted subprocess boundary used by application_build's heldout lane."""
    import argparse
    import subprocess
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--max-queries", type=int, required=True)
    parser.add_argument("--builder-argv", required=True, help="exact trusted adapter argv encoded as JSON")
    args = parser.parse_args(argv)
    try:
        checkpoint.regular_chain(args.contract)
        if checkpoint.digest(args.contract.read_bytes()) != _sha256(args.contract_sha256, "contract_sha256"):
            raise OracleRefused("hidden contract digest differs from operator-bound contract")
        command = json.loads(args.builder_argv)
        if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item for item in command):
            raise OracleRefused("builder argv must be a nonempty JSON string array")
        if '--contract' not in command or command.count('--contract') != 1:
            raise OracleRefused('trusted adapter must bind one exact contract')
        contract_index = command.index('--contract') + 1
        if contract_index >= len(command) or Path(command[contract_index]).resolve() != args.contract.resolve():
            raise OracleRefused('trusted adapter contract differs from oracle binding')
        payload = sys.stdin.buffer.read(4_194_305)
        if len(payload) > 4_194_304:
            raise OracleRefused("adapter manifest exceeds byte limit")
        manifest = json.loads(payload.decode("utf-8"))
        candidate = _sha256(manifest.get("candidate_sha256") if isinstance(manifest, dict) else None, "candidate_sha256")
        ledger = OracleLedger(args.ledger, contract_sha256=args.contract_sha256, max_queries=args.max_queries)
        try:
            report = ledger.run_query(candidate, "heldout_final",
                lambda: _dispatch(command, payload, candidate))
        except OracleRefused as exc:
            if str(exc) == "oracle query allowance exhausted":
                report = {"status": "HOLD", "artifact_sha256": candidate, "reason_codes": ["query_limit"], "external_effect": "not_dispatched"}
            else:
                report = {"status": "HOLD", "artifact_sha256": candidate, "reason_codes": ["unknown_outcome"], "external_effect": "unknown"}
        safe = {"schema": 1, "candidate_sha256": candidate,
                "build_pass": report["status"] == "PASS", "test_pass": report["status"] == "PASS",
                "oracle_status": report["status"], "reason_codes": report["reason_codes"],
                "external_effect": report.get("external_effect", "complete"),
                "observations": ["Heldout validation " + report["status"] + "."],
                "artifact_sha256": report["artifact_sha256"],
                "limitations": ["Heldout feedback is coarse and does not expose per-case inputs, expected values, outputs, or scenario details.",
                                "Finite queries do not establish resistance to hardcoding or general correctness."]}
        sys.stdout.buffer.write(_canonical(safe) + b"\n")
        return 0
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        sys.stderr.write(json.dumps({"status": "HOLD", "reason": str(exc)}, ensure_ascii=True) + "\n")
        return 2


def _dispatch(command: list[str], payload: bytes, candidate_sha256: str) -> dict[str, Any]:
    import applications
    try:
        manifest = json.loads(payload.decode("utf-8"))
        report = applications._run_json_command(command, manifest)
    except Exception:
        return {"status": "HOLD", "reason_codes": ["unknown_outcome"], "external_effect": "unknown"}
    if not isinstance(report, dict) or report.get("candidate_sha256") != candidate_sha256:
        return {"status": "HOLD", "reason_codes": ["unknown_outcome"], "external_effect": "unknown"}
    # Validate the full trusted adapter envelope before deriving coarse heldout feedback.
    try:
        applications._adapter_report(report, candidate_sha256)
    except Exception:
        return {"status": "HOLD", "reason_codes": ["output_invalid"]}
    return {**heldout_result(report, artifact_sha256=candidate_sha256), "external_effect": "complete"}



if __name__ == '__main__':
    raise SystemExit(_adapter_main())
