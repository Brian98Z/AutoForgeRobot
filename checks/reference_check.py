"""Local-only regression checks for reference package integrity and safe ingestion."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import json
from pathlib import Path
import tempfile

import references

ROOT = Path(__file__).resolve().parents[1]


def expect_reject(call, phrase):
    try:
        call()
    except (ValueError, OSError) as exc:
        assert phrase.lower() in str(exc).lower(), str(exc)
        return
    raise AssertionError(f"expected rejection containing {phrase!r}")


def main():
    with tempfile.TemporaryDirectory(prefix="autoforge-reference-") as temp:
        base = Path(temp)
        store = base / "store"
        # Genuine checked-in source corpus; no network or candidate execution.
        result = references.ingest(ROOT / "src", store, "local corpus regression", "operator-declared: internal", "source")
        package = references._load_package(store, result["package_id"])
        found = references.query(store, "protocol", 10)
        assert found and all(row["package_id"] == result["package_id"] for row in found)
        exported = base / "export.json"
        references.export(store, result["package_id"], exported)
        compact = json.loads(exported.read_text(encoding="utf-8"))


        fixture = ROOT / "bootstrap-runs" / "rea-controller-evidence.json"
        if fixture.is_file():
            evidence_store = base / "evidence-store"
            evidence_result = references.ingest(fixture, evidence_store, "local REA export", "operator-declared", "rea-evidence")
            evidence_package = references._load_package(evidence_store, evidence_result["package_id"])
            real_export = json.loads(fixture.read_text(encoding="utf-8"))
            evidence = evidence_package["upstream_evidence"][0]["record"]
            assert evidence == real_export["records"][0]
            assert "unverified" in evidence_package["limitations"][-1]
            assert all(item in evidence_package['limitations'] for item in evidence['limitations'])
        # Attaching a reference cannot replace an existing operator task.
        task_path=base/'task.json'
        task_path.write_text('{"schema":1}',encoding='utf-8')
        ref_file=min(package['files'],key=lambda f:len(f['content']))['path']
        references.attach(store,result['package_id'],ref_file,task_path,base/'attached.json')
        expect_reject(lambda: references.attach(store,result['package_id'],ref_file,task_path,task_path),'exists')
        assert task_path.read_text(encoding='utf-8')=='{"schema":1}'
        # Tamper content while keeping the immutable manifest ID: export must fail closed.
        stored = store / "packages" / f"{result['package_id']}.json"
        damaged = json.loads(stored.read_text(encoding="utf-8"))
        damaged["files"][0]["content"] += "tampered"
        stored.write_text(json.dumps(damaged), encoding="utf-8")
        expect_reject(lambda: references.export(store, result["package_id"], base / "bad.json"), "manifest hash mismatch")

        # Rebuild a well-hashed malicious package manifest with traversal entry; loader rejects it.
        payload = {k: v for k, v in package.items() if k != "package_id"}
        payload["files"][0]["path"] = "../escape.py"
        pid = references.digest(references.canonical(payload))
        malicious = dict(payload, package_id=pid)
        (store / "packages" / f"{pid}.json").write_bytes(references.canonical(malicious) + b"\n")
        expect_reject(lambda: references.export(store, pid, base / "traversal.json"), "unsafe package path")

        # Reject direct symlink/reparse traversal; platform may disallow creating links.
        outside = base / "outside.py"
        outside.write_text("not imported", encoding="utf-8")
        link = base / "link.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        else:
            expect_reject(lambda: references.ingest(link, base / "link-store", "local", "operator", "source"), "symlink/reparse")

        # Sensitive files are forbidden even when their suffix is otherwise allowed.
        secret_dir = base / "sensitive"
        secret_dir.mkdir()
        (secret_dir / "private_key.py").write_text("opaque", encoding="utf-8")
        expect_reject(lambda: references.ingest(secret_dir, base / "secret-store", "local", "operator"), "sensitive")
    print(json.dumps({"status": "PASS", "checks": ["local-corpus-ingest-query-export", "content-tamper-rejected", "traversal-rejected", "symlink-rejected-when-supported", "sensitive-file-rejected"]}, sort_keys=True))


if __name__ == "__main__":
    main()
