"""Focused A04 real-filesystem project transaction smoke; never executes candidate code."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import application_project as project

ROOT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cli(*args: str) -> dict:
    result = subprocess.run([sys.executable, str(ROOT / "autoforge" / "application_project.py"), *args],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=15, check=False)
    if result.returncode != 0:
        raise AssertionError(f"project CLI failed ({result.returncode}): {result.stderr.decode('utf-8', 'replace')}")
    return json.loads(result.stdout.decode("utf-8"))


def candidate(version: int) -> list[dict[str, str]]:
    contract = {"schema": 1, "entity": "item", "fields": {"sku": "string", "quantity": "integer"},
                "required": ["sku", "quantity"]}
    return [
        {"path": "main.py", "content": f"# candidate revision {version}\n"},
        {"path": "requirements.lock", "content": f"# locked dependencies for revision {version}\n"},
        {"path": "data-contract.json", "content": json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n"},
    ]


def main() -> int:
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="autoforge-project-live-") as td:
        base = Path(td)
        source, output = base / "source", base / "transactions"
        source.mkdir()
        (source / "main.py").write_text("# user source v0\n", encoding="utf-8")
        (source / "requirements.lock").write_text("# original lock\n", encoding="utf-8")
        (source / "data-contract.json").write_text('{"schema":1,"entity":"item"}\n', encoding="utf-8")
        sentinel = source / "sentinel.txt"
        sentinel.write_text("operator-owned source remains untouched\n", encoding="utf-8")
        imported = project.import_project(source, goal="atomically revise app, lock and data contract", mode="existing")
        baseline = imported["baseline"]
        before_pointer = (output / "active.json").read_bytes() if (output / "active.json").exists() else None

        def interrupt(stage: str) -> None:
            if stage == "after_revision_stage":
                raise RuntimeError("injected interruption after immutable payload staging")
        try:
            project.write_files(source, candidate(1), expected_baseline=baseline, output=output, fault=interrupt)
        except RuntimeError as exc:
            if "injected interruption" not in str(exc):
                raise
        else:
            raise AssertionError("interruption injection did not abort commit")
        if ((output / "active.json").read_bytes() if (output / "active.json").exists() else None) != before_pointer:
            raise AssertionError("interrupted transaction changed active pointer")
        if project.status(output)["status"] != "EMPTY":
            raise AssertionError("interrupted transaction exposed an active partial commit")
        if sentinel.read_text(encoding="utf-8") != "operator-owned source remains untouched\n":
            raise AssertionError("interruption lost source sentinel")
        checks.append("injected post-stage interruption leaves pointer empty and source intact")

        def concurrent_source_edit(stage: str) -> None:
            if stage == "after_revision_stage":
                sentinel.write_text("concurrent operator edit retained\n", encoding="utf-8")
        try:
            project.write_files(source, candidate(1), expected_baseline=baseline, output=output,
                                fault=concurrent_source_edit)
        except project.ProjectRefused as exc:
            if "source baseline conflict" not in str(exc):
                raise
        else:
            raise AssertionError("source edit during staging did not conflict")
        if project.status(output)["status"] != "EMPTY":
            raise AssertionError("source conflict changed active pointer")
        if sentinel.read_text(encoding="utf-8") != "concurrent operator edit retained\n":
            raise AssertionError("concurrent source sentinel was lost")
        checks.append("concurrent source edit during staging refuses commit, keeps pointer empty and preserves edit")

        baseline = project.import_project(source, goal="atomically revise app, lock and data contract", mode="existing")["baseline"]
        baseline_path = base / "baseline.json"
        baseline_path.write_text(json.dumps(baseline, sort_keys=True), encoding="utf-8")
        candidate_path = base / "candidate.json"
        candidate_path.write_text(json.dumps(candidate(1), sort_keys=True), encoding="utf-8")
        committed = cli("commit", str(source), str(baseline_path), str(candidate_path), str(output))
        if committed["status"] != "COMMITTED":
            raise AssertionError("CLI first commit did not commit")
        first_revision = committed["revision"]
        first_state = project.status(output)
        first_records = {row["path"]: row["sha256"] for row in first_state["manifest"]["files"]}
        expected_first = {row["path"]: digest(row["content"].encode()) for row in candidate(1)}
        if first_records != expected_first:
            raise AssertionError(f"first revision digests differ: {first_records!r}")
        if {row["path"] for row in committed["files"]} != set(expected_first):
            raise AssertionError("first commit did not stage source, lock and data contract as one payload")
        if sentinel.read_text(encoding="utf-8") != "concurrent operator edit retained\n":
            raise AssertionError("successful commit overwrote source sentinel")

        candidate_path.write_text(json.dumps(candidate(2), sort_keys=True), encoding="utf-8")
        second = cli("commit", str(source), str(baseline_path), str(candidate_path), str(output))
        if second["status"] != "COMMITTED":
            raise AssertionError("CLI second commit did not commit")
        second_records = {row["path"]: row["sha256"] for row in project.status(output)["manifest"]["files"]}
        expected_second = {row["path"]: digest(row["content"].encode()) for row in candidate(2)}
        if second_records != expected_second:
            raise AssertionError(f"second revision digests differ: {second_records!r}")
        restored = cli("rollback", str(output))
        if restored["status"] != "ROLLED_BACK" or restored["revision"] != first_revision:
            raise AssertionError("CLI rollback did not select exact prior revision")
        final = project.status(output)
        final_records = {row["path"]: row["sha256"] for row in final["manifest"]["files"]}
        if final["revision"] != first_revision or final_records != expected_first:
            raise AssertionError("rollback did not restore exact first committed file bytes")
        if sentinel.read_text(encoding="utf-8") != "concurrent operator edit retained\n":
            raise AssertionError("rollback altered concurrent source sentinel")
        checks.extend(["actual CLI commits bind source, lock and data-contract file digests per immutable revision",
                       "actual CLI rollback restores exact prior revision and preserves source sentinel"])
        evidence = {"first_revision": first_revision, "second_revision": second["revision"],
                    "active_after_rollback": final["revision"], "rollback_sha256_by_file": final_records,
                    "rollback_source_sentinel_sha256": digest(sentinel.read_bytes()),
                    "candidate_code_executed": False}
    print(json.dumps({"status": "PASS", "scope": "actual CLI plus real filesystem import/write_files/rollback; no generated candidate executed",
                      "checks": checks, "evidence": evidence}, ensure_ascii=True, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
