#!/usr/bin/env python3
"""Resolve build artefacts whose names were fixed to the Windows layout.

The controller's own drivers (bootstrap, iterate, acceptance, extension/multilingual tests,
external evaluation) referenced ``target/release/autoforge.exe`` and
``runtime/julia-1.13.3/bin/julia.exe`` directly. A Linux build produces ``autoforge`` and
``julia`` instead, so resolution tries both names and keeps the Windows spelling as the
fallback for error messages.
"""
from __future__ import annotations

from pathlib import Path


def _first(*candidates: Path) -> Path:
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def release_binary(project: Path) -> Path:
    base = Path(project) / "target" / "release"
    return _first(base / "autoforge.exe", base / "autoforge")


def julia_binary(project: Path) -> Path:
    base = Path(project) / "runtime" / "julia-1.13.3" / "bin"
    return _first(base / "julia.exe", base / "julia")
