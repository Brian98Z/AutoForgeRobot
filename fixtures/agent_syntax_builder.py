"""Trusted syntax-only builder. Never executes candidate code or claims business correctness."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys


def main() -> int:
    request = json.load(sys.stdin)
    manifest = request["workspace_manifest"]
    workspace = Path(manifest["workspace"])
    issues = []
    python_files = []
    for item in manifest["files"]:
        relative = item["path"]
        if relative.endswith(".py"):
            python_files.append(relative)
            try:
                source = (workspace / relative).read_text(encoding="utf-8")
                ast.parse(source, filename=relative)
            except SyntaxError as exc:
                issues.append({"path": relative, "line": exc.lineno, "column": exc.offset,
                               "message": exc.msg})
    observations = ([f"SyntaxError in {x['path']} line {x['line']}: {x['message']}" for x in issues]
                   or [f"Python syntax parsed successfully in {len(python_files)} file(s)."])
    print(json.dumps({"schema": 1, "candidate_sha256": request["candidate_sha256"],
                      "build_pass": not issues, "test_pass": not issues,
                      "observations": observations,
                      "limitations": ["AST syntax parse only; no candidate source was executed.",
                                      "No behavior, dependency, security, or container correctness was tested."]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
