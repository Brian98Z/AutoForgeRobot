#!/usr/bin/env python3
"""Build one single-file Windows executable for the AutoForge controller.

The controller dispatches module CLIs dynamically, so every production module is a
hidden import. Playwright is imported lazily by application_browser and is therefore
not bundled; a frozen binary still reports it as missing at runtime instead of failing
to start.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"


def hidden_imports() -> list[str]:
    names = {path.stem for path in ROOT.glob("*.py")}
    universal = ROOT / "universal"
    names |= {"universal." + path.stem for path in universal.glob("*.py") if path.stem != "__init__"}
    return sorted(name for name in names if name not in {"autoforge_cli"})


def write_manifest() -> Path:
    target = ROOT / "build" / "bundled_modules.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(hidden_imports(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return target


def build() -> int:
    manifest = write_manifest()
    argv = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "autoforge_cli.py"),
        "--onefile", "--console", "--clean", "--noconfirm",
        "--name", "autoforge",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'fixtures'};fixtures",
        "--add-data", f"{ROOT / 'src'};src",
        "--add-data", f"{manifest};.",
    ]
    for excluded in ("numpy", "tkinter", "distutils", "setuptools", "pip", "matplotlib"):
        argv += ["--exclude-module", excluded]
    for source in sorted(ROOT.glob("*.py")):
        argv += ["--add-data", f"{source};."]
    for name in hidden_imports():
        argv += ["--hidden-import", name]
    print(f"building {len(hidden_imports())} modules -> {DIST / 'autoforge.exe'}", flush=True)
    return subprocess.call(argv, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(build())
