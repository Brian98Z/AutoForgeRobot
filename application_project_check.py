"""Real filesystem/process transaction checks; candidate source is never executed."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import application_project as project

ROOT = Path(__file__).resolve().parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def candidate(text: str, extra: str = "") -> list[dict[str, str]]:
    files = [{"path": "main.py", "content": text}]
    if extra:
        files.append({"path": "docs/notes.txt", "content": extra})
    return files


def main() -> int:
    checks: list[str] = []
    with tempfile.TemporaryDirectory(prefix="autoforge-project-check-") as td:
        root = Path(td)
        source = root / "source"
        source.mkdir()
        (source / "main.py").write_text("value = 1\n", encoding="utf-8")
        (source / "unrelated.txt").write_text("user change retained\n", encoding="utf-8")
        imported = project.import_project(source, goal="test", mode="existing")
        require(imported["candidate"]["schema"] == 1, "candidate schema did not interoperate")
        require(len(imported["baseline"]["files"]) == 2, "import omitted unrelated user file")
        out = root / "transactions"
        first_files = candidate("value = 2\n", "note\n") + [{"path": "unrelated.txt", "content": "user change retained\n"}]
        first = project.write_files(source, first_files, expected_baseline=imported["baseline"], output=out)
        require(first["status"] == "COMMITTED" and {x["change"] for x in first["diff"]} == {"modified", "added"}, "first diff/commit incorrect")
        require((source / "main.py").read_text(encoding="utf-8") == "value = 1\n", "commit overwrote user's source")
        require(project.status(out)["candidate_sha256"] == first["candidate_sha256"], "active pointer not bound to staged revision")
        active_main = out / "revisions" / first["revision"] / "main.py"
        saved_payload = active_main.read_bytes()
        active_main.write_bytes(saved_payload + b"tampered")
        try:
            project.status(out)
        except project.ProjectRefused as exc:
            require("payload verification failed" in str(exc), "status rejected tamper for wrong reason")
        else:
            raise AssertionError("status accepted tampered active revision payload")
        active_main.write_bytes(saved_payload)
        checks.append("active revision payload tampering refused")
        checks.append("schema-1 import, exact digests, user-file preservation, diff and immutable active revision")

        (source / "unrelated.txt").write_text("new user work\n", encoding="utf-8")
        try:
            project.write_files(source, candidate("value = 3\n"), expected_baseline=imported["baseline"], output=out)
        except project.ProjectRefused as exc:
            require("baseline conflict" in str(exc), "wrong source conflict reason")
        else:
            raise AssertionError("concurrent unrelated source edit did not conflict")
        require(project.status(out)["revision"] == first["revision"], "conflict mutated active pointer")
        entered = threading.Event()
        release = threading.Event()
        outcomes: list[str] = []
        def hold_lock() -> None:
            with project._locked_transaction(out):
                entered.set()
                release.wait(2)
        worker = threading.Thread(target=hold_lock)
        worker.start()
        require(entered.wait(2), "concurrent lock owner did not start")
        try:
            project.write_files(source, candidate("value = 6\n"), expected_baseline=imported["baseline"], output=out)
        except project.ProjectRefused:
            outcomes.append("conflict")
        finally:
            release.set()
            worker.join(2)
        require(outcomes == ["conflict"] and not worker.is_alive(), "concurrent transaction was not refused")
        checks.append("concurrent transaction lock conflict")
        checks.append("concurrent source conflict includes unrelated user-file edit")

        fresh = project.import_project(source, goal="test", mode="existing")
        before = (out / "active.json").read_bytes()
        def interrupt(stage: str) -> None:
            if stage == "after_revision_stage":
                raise RuntimeError("simulated process interruption")
        try:
            project.write_files(source, candidate("value = 4\n"), expected_baseline=fresh["baseline"], output=out, fault=interrupt)
        except RuntimeError:
            pass
        else:
            raise AssertionError("injected stage interruption did not propagate")
        require((out / "active.json").read_bytes() == before, "interrupted stage changed active pointer")
        require(project.status(out)["revision"] == first["revision"], "interrupted commit appeared successful")
        checks.append("interrupted multi-file stage leaves pointer unchanged and no success")

        def pointer_interrupt(stage: str) -> None:
            if stage == "before_pointer_replace":
                raise RuntimeError("simulated pointer interruption")
        try:
            project.write_files(source, candidate("value = 5\n"), expected_baseline=fresh["baseline"], output=out, fault=pointer_interrupt)
        except RuntimeError:
            pass
        else:
            raise AssertionError("injected pointer interruption did not propagate")
        require(project.status(out)["revision"] == first["revision"], "pre-replace interruption exposed partial transaction")
        checks.append("pointer interruption atomicity")

        (source / "unrelated.txt").write_text("new user work\n", encoding="utf-8")
        second_baseline = project.import_project(source, goal="test", mode="existing")["baseline"]
        second = project.write_files(source, candidate("value = 9\n"), expected_baseline=second_baseline, output=out)
        restored = project.rollback(out, expected_revision=second["revision"])
        require((source / "unrelated.txt").read_text(encoding="utf-8") == "new user work\n", "rollback altered source")
        checks.append("atomic pointer rollback preserves all source files")

        new_root = root / "new-project"
        new = project.import_project(new_root, goal="new", mode="new")
        empty = project.write_files(new_root, [], expected_baseline=new["baseline"], output=root / "unused-empty-output")
        require(empty["status"] == "EMPTY" and not (root / "unused-empty-output").exists(), "empty candidate created a fake revision")
        require(new["candidate"]["files"] == [], "new mode must start with no placeholder files")
        require(new["baseline"]["exists"] is False, "new mode baseline does not capture absent source")
        initialized = project.write_files(new_root, candidate("x=1\n"), expected_baseline=new["baseline"], output=root / "new-transactions")
        require(initialized["status"] == "COMMITTED", "empty baseline rejected first real file")
        try:
            project.write_files(new_root, candidate("x=1\n"), expected_baseline=new["baseline"], output=new_root / "transactions")
        except project.ProjectRefused:
            pass
        else:
            raise AssertionError("transaction output inside source tree was accepted")
        checks.append("explicit new project mode and output/source separation")

        secret = source / ".env"
        secret.write_text("PRIVATE_TOKEN=abcd123456789\n", encoding="utf-8")
        try:
            project.import_project(source, goal="test")
        except project.ProjectRefused:
            pass
        else:
            raise AssertionError("secret-bearing source was imported")
        checks.append("credential-bearing file rejection")
        secret.unlink()
        config = source / "config.json"
        config.write_text('{"database": {"password": "not-a-placeholder-123"}, "api_key": "real-looking-key-123456"}\n', encoding="utf-8")
        try:
            project.import_project(source, goal="test")
        except project.ProjectRefused:
            pass
        else:
            raise AssertionError("quoted JSON credential properties were imported")
        config.write_text('{"pass\\u0077ord":"abc123"}\n', encoding="utf-8")
        try:
            project.import_project(source, goal="test")
        except project.ProjectRefused:
            pass
        else:
            raise AssertionError("escaped short JSON password property was imported")
        checks.append("escaped JSON key and short populated credential rejection")
        config.write_text('{"service": "inventory", "retry_count": 3, "password": ""}\n', encoding="utf-8")
        clean_import = project.import_project(source, goal="test", mode="existing")
        require(any(record["path"] == "config.json" for record in clean_import["baseline"]["files"]), "empty credential field or ordinary JSON caused false positive")
        checks.append("ordinary JSON and empty credential values remain importable")
        checks.append("quoted JSON credential property rejection")
        require(project.status(out)["status"] == "ACTIVE", "restored revision did not verify")

        config.unlink()
        # Real subprocess entrypoint status path; this does not run any candidate.
        proc = subprocess.run([sys.executable, str(ROOT / "application_project.py"), "status", str(out)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False)
        require(proc.returncode == 0 and json.loads(proc.stdout)["status"] == "ACTIVE", "CLI status subprocess failed")
        checks.append("real process CLI status")
    print(json.dumps({"status": "PASS", "scope": "filesystem and process transaction behavior; no generated source executed", "checks": checks}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
