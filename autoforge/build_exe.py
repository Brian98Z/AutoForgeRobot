#!/usr/bin/env python3
"""Build the AutoForge controller executable.

Two layouts are supported and differ only in start-up cost:

``onefile``  one self-contained .exe; every run unpacks the payload to a temporary
             directory first, which costs roughly 700 ms for this module set.
``onedir``   a folder whose executable starts without unpacking (about 90 ms measured);
             the folder must be shipped as a whole.

The controller dispatches module CLIs dynamically, so every production module is a
hidden import. Playwright is imported lazily by application_browser and is therefore
not bundled; a frozen binary still reports it as missing at runtime instead of failing
to start.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_EXE = "autoforge.exe" if os.name == "nt" else "autoforge"


def hidden_imports() -> list[str]:
    names = {path.stem for path in (ROOT / "autoforge").glob("*.py")}
    universal = ROOT / "universal"
    names |= {"universal." + path.stem for path in universal.glob("*.py") if path.stem != "__init__"}
    return sorted(name for name in names if name not in {"autoforge_cli"})


def write_manifest() -> Path:
    target = ROOT / "build" / "bundled_modules.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(hidden_imports(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return target


def build(mode: str = "onefile", dist_base: str = "dist") -> int:
    manifest = write_manifest()
    onedir = mode == "onedir"
    distpath = ROOT / (f"{dist_base}-onedir" if onedir else dist_base)
    platform = "win" if os.name == "nt" else "posix"
    workroot = ROOT / "build" / f"pyinstaller-{platform}"
    argv = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "autoforge" / "autoforge_cli.py"),
        "--onedir" if onedir else "--onefile",
        "--console", "--clean", "--noconfirm",
        "--name", "autoforge",
        "--distpath", str(distpath),
        "--workpath", str(workroot / ("onedir" if onedir else "onefile")),
        "--specpath", str(workroot),
        "--add-data", f"{ROOT / 'fixtures'}{os.pathsep}fixtures",
        "--add-data", f"{ROOT / 'src'}{os.pathsep}src",
        "--add-data", f"{manifest}{os.pathsep}.",
        "--paths", str(ROOT / "autoforge"),
    ]
    for excluded in ("numpy", "tkinter", "distutils", "setuptools", "pip", "matplotlib", "playwright"):
        argv += ["--exclude-module", excluded]
    for source in sorted((ROOT / "autoforge").glob("*.py")):
        argv += ["--add-data", f"{source}{os.pathsep}."]
    for name in hidden_imports():
        argv += ["--hidden-import", name]
    target = distpath / "autoforge" / WINDOWS_EXE if onedir else distpath / WINDOWS_EXE
    print(f"building {len(hidden_imports())} modules ({mode}) -> {target}", flush=True)
    return subprocess.call(argv, cwd=ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("onefile", "onedir"), default="onefile")
    parser.add_argument("--dist", default="dist", help="output directory name under the project root")
    parsed = parser.parse_args()
    raise SystemExit(build(parsed.mode, parsed.dist))
