#!/usr/bin/env python3
"""Exercise finite oracle accounting and heldout feedback isolation."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
import json
import tempfile
from pathlib import Path
import application_oracle as oracle
def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
def main() -> int:
    contract = hashlib.sha256(b"independent expected contract").hexdigest()
    candidate = hashlib.sha256(b"candidate bytes").hexdigest()
    artifact = hashlib.sha256(b"immutable artifact").hexdigest()
    with tempfile.TemporaryDirectory(prefix="autoforge-oracle-") as temp:
        ledger_path = Path(temp) / "oracle-ledger.json"
        ledger = oracle.OracleLedger(ledger_path, contract_sha256=contract, max_queries=2)
        public_detail = {"status": "FAIL", "observations": ["case alpha request 123 expected 456 actual 789"], "candidate_sha256": candidate}
        calls = []
        public = ledger.run_query(candidate, "public_repair", lambda: calls.append("public") or public_detail)
        check(public["observations"] == public_detail["observations"], "public repair detail was unexpectedly altered")
        check(len(calls) == 1 and public["artifact_sha256"] == candidate, "public query did not dispatch and commit")
        hidden_input = {"status": "FAIL", "reason_codes": ["behavior_failed"],
                        "scenario": "secret-scenario", "request": {"pin": "481516"},
                        "expected": {"balance": 9}, "actual": {"balance": 0},
                        "observations": ["per-case hint"]}
        hidden = ledger.run_query(candidate, "heldout_final", lambda: hidden_input | {"artifact_sha256": artifact})
        check(hidden == {"status": "FAIL", "artifact_sha256": artifact, "reason_codes": ["behavior_failed"], "external_effect": "complete"},
              "heldout result was not reduced to coarse safe feedback")
        encoded = ledger_path.read_text(encoding="utf-8")
        for secret in ("secret-scenario", "481516", "balance", "per-case hint"):
            check(secret not in encoded, f"heldout detail persisted: {secret}")
        called = []
        try:
            ledger.run_query(candidate, "heldout_final", lambda: called.append(True) or {})
        except oracle.OracleRefused:
            pass
        else:
            raise AssertionError("exhausted query cap was not refused")
        check(not called, "cap refusal happened after dispatch")
        for changed_contract, changed_cap in (("0" * 64, 2), (contract, 3)):
            try:
                oracle.OracleLedger(ledger_path, contract_sha256=changed_contract, max_queries=changed_cap).reserve(candidate, "heldout_final")
            except oracle.OracleRefused:
                pass
            else:
                raise AssertionError("contract/policy mismatch was accepted")
        # An interrupted external dispatch leaves its reserved allowance consumed.
        second_path = Path(temp) / "interrupted-ledger.json"
        interrupted = oracle.OracleLedger(second_path, contract_sha256=contract, max_queries=2)
        unknown = interrupted.run_query(candidate, 'heldout_final', lambda: {'status': 'HOLD', 'external_effect': 'unknown'})
        check(unknown['external_effect'] == 'unknown', 'ambiguous dispatch was finalized')
        resumed = oracle.OracleLedger(second_path, contract_sha256=contract, max_queries=2)
        try:
            resumed.reserve(candidate, "heldout_final")
        except oracle.OracleRefused:
            pass
        else:
            raise AssertionError("restart reset allowance after unknown dispatch")
        state = json.loads(second_path.read_text(encoding="utf-8"))["state"]
        check(state["entries"][0]["state"] == "reserved", "interrupted reservation was not retained as unknown")
        # A committed query cannot be finalized twice under a reused query id.
        committed_id = json.loads(ledger_path.read_text())['state']['entries'][0]['query_id']
        try:
            ledger.complete(committed_id, {'status': 'PASS'}, artifact_sha256=candidate)
        except oracle.OracleRefused:
            pass
        else:
            raise AssertionError('committed query was finalized twice')
        report = oracle.heldout_result({"build_pass": True, "test_pass": False,
                                        "observations": ["case-name and expected value"]}, artifact_sha256=artifact)
        check(report == {"status": "FAIL", "artifact_sha256": artifact, "reason_codes": ["behavior_failed"]},
              "heldout adapter report leaked beyond coarse reason code")
        # Actual CLI rejects a different dispatch contract before spending or launching.
        import subprocess, sys
        bound_contract = Path(temp) / 'bound.json'; bound_contract.write_text('{}')
        foreign_contract = Path(temp) / 'foreign.json'; foreign_contract.write_text('{"foreign":true}')
        marker = Path(temp) / 'must-not-launch'
        adapter = [sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("launched")', '--contract', str(foreign_contract)]
        cli_ledger = Path(temp) / 'cli-ledger.json'
        command = [sys.executable, str(Path(oracle.__file__)), '--ledger', str(cli_ledger), '--contract', str(bound_contract), '--contract-sha256', hashlib.sha256(bound_contract.read_bytes()).hexdigest(), '--max-queries', '1', '--builder-argv', json.dumps(adapter)]
        denied = subprocess.run(command, input=json.dumps({'candidate_sha256': candidate}), capture_output=True, text=True, timeout=10)
        check(denied.returncode == 2 and not marker.exists() and not cli_ledger.exists(), 'different contract dispatched or consumed allowance')
        # The `--contract=<path>` spelling cannot smuggle a second contract option.
        marker2 = Path(temp) / 'must-not-launch-2'
        adapter2 = [sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(marker2)) + ').write_text("launched")',
                    '--contract', str(bound_contract), '--contract=' + str(foreign_contract)]
        denied2 = subprocess.run([sys.executable, str(Path(oracle.__file__)), '--ledger', str(cli_ledger),
                                  '--contract', str(bound_contract), '--contract-sha256',
                                  hashlib.sha256(bound_contract.read_bytes()).hexdigest(),
                                  '--max-queries', '1', '--builder-argv', json.dumps(adapter2)],
                                 input=json.dumps({'candidate_sha256': candidate}), capture_output=True, text=True, timeout=10)
        check(denied2.returncode == 2 and not marker2.exists() and not cli_ledger.exists(),
              '--contract= spelling bypassed the single-contract rule')
        # Generic dispatch results are validated before they can affect accounting.
        third = oracle.OracleLedger(Path(temp) / 'validated-ledger.json', contract_sha256=contract, max_queries=3)
        try:
            third.run_query(candidate, 'heldout_final', lambda: {'status': 'PASS', 'artifact_sha256': 'not-a-digest'})
        except oracle.OracleRefused:
            pass
        else:
            raise AssertionError('invalid artifact digest was accepted')
        fourth = oracle.OracleLedger(Path(temp) / 'validated-ledger-2.json', contract_sha256=contract, max_queries=3)
        coerced = fourth.run_query(candidate, 'heldout_final',
                                   lambda: {'status': 'PASS', 'artifact_sha256': artifact, 'external_effect': 'forged'})
        check(coerced['external_effect'] == 'unknown' and coerced['status'] == 'HOLD',
              'arbitrary external_effect was trusted')
        state = json.loads((Path(temp) / 'validated-ledger.json').read_text(encoding='utf-8'))['state']
        check([e['state'] for e in state['entries']] == ['reserved'],
              'invalid dispatch result was finalized')
        huge = Path(temp) / 'huge-ledger.json'
        huge.write_bytes(b'x' * (4 * 1024 * 1024 + 1))
        try:
            oracle.OracleLedger(huge, contract_sha256=contract, max_queries=1)._load()
        except oracle.OracleRefused:
            pass
        else:
            raise AssertionError('oversized ledger was read')
    print(json.dumps({"status": "PASS", "checks": ["durable-cap", "pre-dispatch-refusal", "contract-policy-binding",
          "unknown-reservation-nonreplay", "heldout-detail-redaction", "public-repair-detail-preserved", "contract-spelling-refused", "dispatch-result-validated", "ledger-read-bounded"]}, sort_keys=True))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
