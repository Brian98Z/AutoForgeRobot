#!/usr/bin/env python3
"""Focused no-network and truthful-status checks for application_capabilities."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import os
from unittest.mock import patch
import application_capabilities as c
import hashlib
import json
import tempfile
from pathlib import Path
import references
def main() -> int:
    # Readiness is a filesystem/configuration inspection: all network-capable APIs are forbidden.
    with patch.dict(os.environ, {}, clear=True), \
         patch.object(c.shutil, "which", return_value=None), \
         patch("urllib.request.urlopen", side_effect=AssertionError("readiness attempted HTTP")):
        ready = c.check()
    assert ready["status"] == "PARTIAL"
    assert ready["container"]["docker_or_podman"] is False
    assert "container engine is unavailable" in ready["blockers"]
    assert ready["model"]["reachability"] == "NOT_CHECKED"
    # Mixed evidence must never become an overall pass.
    def fake_popen(argv, **kwargs):
        import json
        output = json.dumps({"status": "BLOCKED"}) if argv[1].endswith("application_database_check.py") else "status=PASS"
        class Proc:
            def wait(self, timeout=None): return 0 if argv[1].endswith("agent_tools_check.py") else 1
            def kill(self): pass
        kwargs["stdout"].write(output.encode("utf-8"))
        return Proc()
    with patch.object(c.subprocess, "Popen", side_effect=fake_popen), \
         patch.object(c.Path, "is_file", return_value=True):
        report = c.verify(live_model=False)
    db_row = next(row for row in report["checks"] if row["check"] == "application_database_check.py")
    assert db_row["status"] == "BLOCKED", db_row
    assert report["status"] == "PARTIAL"
    assert report["counts"]["blocked"] >= 1
    assert report["counts"]["fail"] >= 1
    matrix = c.matrix()
    assert matrix["status"] == "PARTIAL"
    assert all(row["status"] == "unsupported" for row in matrix["capabilities"])
    assert all(row["adapter_availability"] == "implemented_unverified" for row in matrix["capabilities"])
    # Evidence packages bind source bytes and refuse stale/tampered content on load.
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        source = base / "evidence.json"
        source.write_text(json.dumps({"evidence_id": "fixture-evidence", "limitations": []}), encoding="utf-8")
        package = references.ingest(source, base / "store", "fixture", "MIT", "rea-evidence")
        package_path = Path(package["path"])
        raw = json.loads(package_path.read_text(encoding="utf-8"))
        raw["files"][0]["sha256"] = hashlib.sha256(b"stale").hexdigest()
        package_path.write_text(json.dumps(raw), encoding="utf-8")
        try:
            references.export(base / "store", package["package_id"], base / "export.json")
        except ValueError:
            pass
        else:
            raise AssertionError("stale evidence hash accepted")
    assert matrix["denominator"]["obligations"] == len(c.KINDS)
    assert matrix["denominator"]["verified"] == 0
    print("application_capabilities_check: PASS (no-network readiness, missing engine blocked, mixed checks remain PARTIAL, explicit denominator)")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
