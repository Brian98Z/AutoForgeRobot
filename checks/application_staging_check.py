#!/usr/bin/env python3
"""Staging, manifest, report-publication and diagnostics hardening boundaries."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
import json
import os
import tempfile
from pathlib import Path
import application_builder as builder
import applications


def refused(fn, label):
    try:
        fn()
    except (ValueError, OSError, builder.BuildFailure):
        return
    raise AssertionError(f"expected refusal: {label}")


def main():
    with tempfile.TemporaryDirectory(prefix="autoforge-staging-") as tmp:
        root = Path(tmp)
        # Strict JSON: duplicate keys, nonstandard constants and nonfinite serialization.
        duplicate = root / "duplicate.json"
        duplicate.write_bytes(b'{"a":1,"a":2}')
        refused(lambda: applications._read_json(duplicate), "duplicate JSON key")
        constant = root / "constant.json"
        constant.write_bytes(b'{"a":NaN}')
        refused(lambda: applications._read_json(constant), "nonstandard JSON constant")
        refused(lambda: applications._json_bytes({"a": float("nan")}), "nonfinite serialization")

        candidate = {"schema": 1, "goal": "staging boundary", "references": [],
                     "files": [{"path": "main.py", "content": "print(1)\n"}]}
        stage = applications.stage_candidate(candidate, root / "revision")
        workspace = Path(stage["directory"])
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        applications._verify_staged_files(workspace, manifest)

        # Manifest record bounds are enforced before any read.
        tampered = json.loads(json.dumps(manifest))
        tampered["files"][0]["size"] = -1
        refused(lambda: applications._verify_staged_files(workspace, tampered), "negative manifest size")
        tampered = json.loads(json.dumps(manifest))
        tampered["files"][0]["sha256"] = "not-a-digest"
        refused(lambda: applications._verify_staged_files(workspace, tampered), "malformed manifest digest")
        tampered = json.loads(json.dumps(manifest))
        tampered["files"].append(dict(tampered["files"][0]))
        refused(lambda: applications._verify_staged_files(workspace, tampered), "duplicate manifest path")

        # The whole inventory is checked, not only the listed files.
        extra = workspace / "extra.py"
        extra.write_text("pass\n", encoding="utf-8")
        refused(lambda: applications._verify_staged_files(workspace, manifest), "unmanifested file")
        extra.unlink()
        applications._verify_staged_files(workspace, manifest)
        if hasattr(os, "symlink"):
            link = workspace / "link.py"
            try:
                link.symlink_to(workspace / "main.py")
            except OSError:
                link = None
            if link is not None:
                refused(lambda: applications._verify_staged_files(workspace, manifest), "symlink in inventory")
                link.unlink()
        applications._verify_staged_files(workspace, manifest)

        # Report publication refuses a redirected leaf and never leaves temporary files.
        outside = root / "outside.json"
        outside.write_text("do not touch\n", encoding="utf-8")
        report = workspace / "report.json"
        if hasattr(os, "symlink"):
            try:
                report.symlink_to(outside)
                refused(lambda: applications._publish_report(workspace, {"status": "PASS"}), "symlinked report leaf")
                assert outside.read_text(encoding="utf-8") == "do not touch\n"
                report.unlink()
            except OSError:
                pass
        applications._publish_report(workspace, {"status": "PASS"})
        assert json.loads(report.read_text(encoding="utf-8"))["status"] == "PASS"
        assert not list(workspace.glob(".report.*.tmp"))

        # Windows drive-qualified components are refused on every platform.
        hostile = {"schema": 1, "goal": "x", "references": [], "workspace": str(workspace),
                   "candidate_sha256": "0" * 64,
                   "files": [{"path": "sub/C:/evil.py", "size": 0, "sha256": "0" * 64}]}
        refused(lambda: builder._manifest(hostile), "drive-qualified manifest component")
        # Diagnostics ignore drive-qualified and backslash paths instead of reading hosts.
        stderr = (b'Traceback (most recent call last):\n'
                  b'  File "/candidate/C:/Windows/win.ini", line 3, in <module>\n'
                  b'  File "/candidate/..\\\\outside.py", line 4, in <module>\n'
                  b'NameError: name SECRET is not defined\n')
        locations = builder._failure_locations(stderr, workspace)
        assert not any("win.ini" in item or "outside" in item for item in locations), locations
        # A candidate-owned coordinate is still reported.
        owned = b'Traceback (most recent call last):\n  File "/candidate/main.py", line 1, in <module>\nValueError: x\n'
        assert "main.py:1" in builder._failure_locations(owned, workspace)
    print(json.dumps({"status": "PASS", "scope": "strict JSON, manifest/inventory bounds, report publication and diagnostics path hardening; no container execution"}))
if __name__ == "__main__":
    main()
