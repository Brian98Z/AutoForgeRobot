#!/usr/bin/env python3
"""Focused fixture for the bounded application adapter protocol (not run by orchestrator)."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'autoforge'))

import hashlib
import json
import os
import references
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "autoforge"
RUNNER = MODULES / "applications.py"
def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
def main() -> int:
    with tempfile.TemporaryDirectory(prefix="autoforge-app-check-") as temp:
        base = Path(temp)
        proposer = base / "proposer.py"
        builder = base / "builder.py"
        request_path = base / "request.json"
        proposer.write_text('''import json,sys
p=json.load(sys.stdin)
issues=p["previous_validation"].get("issues",[])
fixed=any("SyntaxError" in str(x) for x in issues)
files=[{"path":"app.py","content":"def main():\\n    return 7\\n"},{"path":"tests/test_app.py","content":"from app import main\\ndef test_main():\\n    assert main() == 7\\n"}]
if not fixed: files[0]["content"]="def main(:\\n    return 7\\n"
else:
    files=p["previous_candidate"]["files"]
    files[0]["content"]=files[0]["content"].replace("def main(:", "def main():")
print(json.dumps({"schema":1,"goal":p["requirement"]["goal"],"references":p["requirement"]["references"],"files":files}))
''', encoding="utf-8")
        builder.write_text('''import ast,json,sys
from pathlib import Path
p=json.load(sys.stdin); m=p["workspace_manifest"]; root=Path(m["workspace"])
errors=[]
for f in m["files"]:
    if f["path"].endswith(".py"):
        try: ast.parse((root/f["path"]).read_text(encoding="utf-8"), filename=f["path"])
        except SyntaxError as e: errors.append(f"SyntaxError: {f['path']}:{e.lineno}: {e.msg}")
print(json.dumps({"schema":1,"candidate_sha256":p["candidate_sha256"],"build_pass":not errors,"test_pass":not errors,"observations":errors or ["All Python sources and tests parse with ast.parse; test body was not run."],"limitations":["AST syntax validation only; no dependency resolution, execution, runtime, integration, security, or deployment validation."]}))
''', encoding="utf-8")
        request = {"schema": 1, "goal": "Create a minimal Python app and parse-check it", "references": []}
        reference_store = base / "reference-store"
        refs = base / "reference-source"; refs.mkdir()
        (refs / "README.md").write_text("Fixture package source for reference resolution.\n", encoding="utf-8")
        request["references"] = [references.ingest(refs, reference_store, "fixture", "operator-local", "source")["package_id"]]
        request_path.write_text(json.dumps(request), encoding="utf-8")
        output = base / "cycle"
        argv = [sys.executable, str(RUNNER), "cycle", "--request", str(request_path),
                "--proposer-command", json.dumps([sys.executable, str(proposer)]),
                "--builder-command", json.dumps([sys.executable, str(builder)]),
                "--trusted-adapter", "--rounds", "2", "--output", str(output), "--reference-store", str(base / "reference-store")]
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
        require(result.returncode == 0, f"fixture cycle failed: {result.stderr}")
        report = json.loads((output / "report.json").read_text(encoding="utf-8"))
        require(report["status"] == "PASS" and report["rounds_completed"] == 2,
                "syntax correction was not reached through actual previous validation feedback")
        require(report["history"][0]["status"] == "FAIL", "initial syntax-invalid candidate was not genuinely rejected by AST builder")
        require(report["history"][1]["status"] == "PASS", "fixed candidate did not pass real adapter build/test gates")
        revisions = sorted(output.glob("revision-*"))
        require(len(revisions) == 2, "failed and passed candidate revisions were not both kept")
        manifest = json.loads((revisions[1] / "manifest.json").read_text(encoding="utf-8"))
        require(manifest["candidate_sha256"] == report["history"][1]["validation"]["candidate_sha256"], "staged manifest digest mismatch")
        negative = base / "foreign-builder.py"
        negative.write_text('''import json,sys
p=json.load(sys.stdin)
print(json.dumps({"schema":1,"candidate_sha256":"0"*64,"build_pass":True,"test_pass":True,"observations":[],"limitations":[]}))
''', encoding="utf-8")
        bad_output = base / "foreign"
        bad = subprocess.run([sys.executable, str(RUNNER), "cycle", "--request", str(request_path),
                              "--proposer-command", json.dumps([sys.executable, str(proposer)]),
                              "--builder-command", json.dumps([sys.executable, str(negative)]), "--trusted-adapter", "--rounds", "1", "--output", str(bad_output), "--reference-store", str(base / "reference-store")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
        require(bad.returncode == 2, "foreign digest adapter response was incorrectly accepted")
        bad_report = json.loads((bad_output / "report.json").read_text(encoding="utf-8"))
        require(bad_report["status"] == "HOLD" and bad_report['rounds_completed']==0 and bad_report['ambiguous_external_call']['phase']=='builder',
                "unbound builder response was committed or replayable")
        print(json.dumps({"status": "PASS", "scope": "multi-file Python AST syntax only; no runtime/deployment claim", "syntax_correction_rounds": report["rounds_completed"], "foreign_digest": "HOLD"}, indent=2))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
