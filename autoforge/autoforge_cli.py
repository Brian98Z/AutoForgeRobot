#!/usr/bin/env python3
"""Single executable entry point for the AutoForge controller.

The controller is a collection of independent module CLIs. This dispatcher keeps that
surface intact in one frozen binary:

    autoforge <module> [module args...]
    autoforge --list
    autoforge --run-py <script.py> [module args...]   (internal re-exec shim)

Subprocess sites in the controller launch scripts with ``sys.executable``. When frozen,
``sys.executable`` is this binary, so the child invocation arrives here as
``autoforge <script.py> ...`` and is routed to the bundled module by basename. No source
module needs changing for that path.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import runpy
import sys
from pathlib import Path

EXCLUDED_MODULES = {"autoforge_cli"}


def known_modules() -> list[str]:
    """Bundled top-level modules, discovered from the frozen archive or the source tree."""
    names: set[str] = set()
    if getattr(sys, "frozen", False):
        archive = getattr(sys, "_MEIPASS", None)
        manifest = Path(archive) / "bundled_modules.json" if archive else None
        if manifest and manifest.is_file():
            names.update(json.loads(manifest.read_text(encoding="utf-8")))
    else:
        for entry in Path(__file__).resolve().parent.glob("*.py"):
            names.add(entry.stem)
    return sorted(name for name in names if name not in EXCLUDED_MODULES and not name.startswith("_"))


def _resolve(selector: str) -> str:
    name = Path(selector).stem if selector.lower().endswith(".py") else selector
    if not name.isidentifier():
        raise SystemExit(f"invalid module selector: {selector}")
    return name


def run(selector: str, argv: list[str]) -> int:
    name = _resolve(selector)
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise SystemExit(f"unknown module: {name} ({exc})") from exc
    entry = getattr(module, "main", None)
    if not callable(entry):
        bundled = Path(getattr(sys, "_MEIPASS", "")) / f"{name}.py"
        if getattr(sys, "frozen", False) and bundled.is_file():
            sys.argv = [str(bundled), *argv]
            runpy.run_path(str(bundled), run_name="__main__")
            return 0
        raise SystemExit(f"{name} does not expose main(); it is a library module")
    parameters = [p for p in inspect.signature(entry).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty]
    saved = sys.argv
    sys.argv = [f"{name}.py", *argv]
    try:
        result = entry(argv) if parameters else entry()
    finally:
        sys.argv = saved
    return 0 if result is None else result


def usage() -> str:
    modules = known_modules()
    lines = [
        "usage: autoforge <module> [args...]",
        "       autoforge --list",
        "",
        "Runs a bundled AutoForge module CLI in-process. Examples:",
        "  autoforge application_capabilities matrix",
        "  autoforge application_release check --candidate ... --spec ...",
        "",
        f"bundled modules ({len(modules)}):",
    ]
    lines.extend("  " + name for name in modules)
    return "\n".join(lines)


def _safe_streams() -> None:
    """Keep the interpreter's locale encoding so parent/child pipes stay symmetric.

    Only relax the error handler: controller output can contain replacement characters
    that the Windows code page cannot encode, and a strict handler would kill the process.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(usage())
        return 0 if args else 1
    if args[0] == "--list":
        print("\n".join(known_modules()))
        return 0
    if args[0] == "--run-py":
        if len(args) < 2:
            raise SystemExit("--run-py requires a script or module name")
        return run(args[1], args[2:])
    return run(args[0], args[1:])


if __name__ == "__main__":
    import multiprocessing

    # PyInstaller's Windows spawn helper must consume --multiprocessing-fork before
    # this dispatcher interprets argv as a module selector or internal script call.
    multiprocessing.freeze_support()
    raised = sys.argv[1:]
    if getattr(sys, "frozen", False):
        _safe_streams()
    if getattr(sys, "frozen", False) and raised:
        # Subprocess sites in the controller launch ``sys.executable`` with a script path
        # or ``-c``. In a frozen binary that is this executable, so route it back here.
        if raised[0].lower().endswith(".py"):
            script = raised[0]
            if os.path.isfile(script):
                # A real file (bundled source or a runtime fixture): execute it as a script.
                sys.argv = [script, *raised[1:]]
                runpy.run_path(script, run_name="__main__")
                raise SystemExit(0)
            raise SystemExit(run(script, raised[1:]))
        if raised[0] == "-c" and len(raised) >= 2:
            saved = sys.argv
            sys.argv = ["-c", *raised[2:]]
            try:
                exec(compile(raised[1], "<string>", "exec"), {"__name__": "__main__"})
            finally:
                sys.argv = saved
            raise SystemExit(0)
    raise SystemExit(main())
