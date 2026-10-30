#!/usr/bin/env python3
"""A fixed, data-driven interpreter for deterministic two-way Turing machines."""

from __future__ import annotations

import argparse
import hashlib
import json
import hmac
import os
import stat
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Mapping

MAX_STEPS = 1_000_000
MAX_CELLS = 100_000
MAX_INPUT_BYTES = 4 * 1024 * 1024
_MACHINE_FIELDS = {"schema", "states", "symbols", "blank", "start", "halt", "transitions"}
_TRANSITION_FIELDS = {"state", "read", "next", "write", "move"}
_CONFIG_FIELDS = {"state", "left", "current", "right", "head", "steps"}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads(data: str | bytes) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_no_duplicate_pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"invalid JSON number: {x}")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def _exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    missing = fields - value.keys()
    extra = value.keys() - fields
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unknown " + ", ".join(sorted(extra)))
        raise ValueError(f"{label} fields: {'; '.join(detail)}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _unique_strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    result = tuple(_string(item, f"{label} member") for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"duplicate {label} member")
    return result


class Program:
    """Validated immutable machine definition."""

    __slots__ = ("_raw", "_blank", "_start", "_halt", "_table", "_digest", "_states", "_symbols", "_sealed")

    def __init__(self, raw: dict[str, Any], states: tuple[str, ...], symbols: tuple[str, ...], halt: frozenset[str], table: Mapping[tuple[str, str], tuple[str, str, str]], digest: str) -> None:
        object.__setattr__(self, "_raw", _freeze(raw))
        object.__setattr__(self, "_blank", raw["blank"])
        object.__setattr__(self, "_start", raw["start"])
        object.__setattr__(self, "_halt", halt)
        object.__setattr__(self, "_table", MappingProxyType(dict(table)))
        object.__setattr__(self, "_digest", digest)
        object.__setattr__(self, "_states", frozenset(states))
        object.__setattr__(self, "_symbols", frozenset(symbols))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("Program is immutable")
        object.__setattr__(self, name, value)

    @property
    def raw(self) -> dict[str, Any]:
        return _thaw(self._raw)

    @property
    def blank(self) -> str:
        return self._blank

    @property
    def start(self) -> str:
        return self._start

    @property
    def halt(self) -> frozenset[str]:
        return self._halt

    @property
    def table(self) -> Mapping[tuple[str, str], tuple[str, str, str]]:
        return self._table

    @property
    def digest(self) -> str:
        return self._digest


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def compile_machine(raw: dict) -> Program:
    obj = _exact_fields(raw, _MACHINE_FIELDS, "machine")
    if type(obj["schema"]) is not int or obj["schema"] != 1:
        raise ValueError("schema must be integer 1")
    states = _unique_strings(obj["states"], "states")
    symbols = _unique_strings(obj["symbols"], "symbols")
    state_set, symbol_set = set(states), set(symbols)
    if not states or not symbols:
        raise ValueError("states and symbols must not be empty")
    blank = _string(obj["blank"], "blank")
    start = _string(obj["start"], "start")
    if blank not in symbol_set:
        raise ValueError("blank is not a declared symbol")
    if start not in state_set:
        raise ValueError("start is not a declared state")
    halts = _unique_strings(obj["halt"], "halt")
    if not set(halts) <= state_set:
        raise ValueError("halt contains an undeclared state")
    transitions = obj["transitions"]
    if not isinstance(transitions, list):
        raise ValueError("transitions must be an array")
    table: dict[tuple[str, str], tuple[str, str, str]] = {}
    clean_transitions: list[dict[str, str]] = []
    for index, item in enumerate(transitions):
        row = _exact_fields(item, _TRANSITION_FIELDS, f"transition {index}")
        state = _string(row["state"], f"transition {index} state")
        read = _string(row["read"], f"transition {index} read")
        nxt = _string(row["next"], f"transition {index} next")
        write = _string(row["write"], f"transition {index} write")
        move = _string(row["move"], f"transition {index} move")
        if state not in state_set or nxt not in state_set:
            raise ValueError(f"transition {index} references an undeclared state")
        if read not in symbol_set or write not in symbol_set:
            raise ValueError(f"transition {index} references an undeclared symbol")
        if state in halts:
            raise ValueError(f"halt state {state!r} has an outgoing transition")
        if move not in ("L", "R", "S"):
            raise ValueError(f"transition {index} move must be L, R, or S")
        key = (state, read)
        if key in table:
            raise ValueError(f"duplicate transition for state/read {key!r}")
        table[key] = (nxt, write, move)
        clean_transitions.append({"state": state, "read": read, "next": nxt, "write": write, "move": move})
    normalized = {"schema": 1, "states": list(states), "symbols": list(symbols), "blank": blank, "start": start, "halt": list(halts), "transitions": clean_transitions}
    digest = hashlib.sha256(_canonical_bytes(normalized)).hexdigest()
    return Program(normalized, states, symbols, frozenset(halts), table, digest)


def _integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an integer")
    return value


def _copy_symbols(values: list, symbols: frozenset[str], label: str) -> list[str]:
    copied = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{label} member must be a string")
        if value not in symbols:
            raise ValueError("config tape contains an undeclared symbol")
        copied.append(value)
    return copied


def _validate_config(program: Program, config: Any) -> dict[str, Any]:
    obj = _exact_fields(config, _CONFIG_FIELDS, "config")
    state = _string(obj["state"], "config state")
    current = _string(obj["current"], "config current")
    if state not in program._states:
        raise ValueError("config state is undeclared")
    if current not in program._symbols:
        raise ValueError("config current is undeclared")
    left = obj["left"]
    right = obj["right"]
    if not isinstance(left, list) or not isinstance(right, list):
        raise ValueError("config left and right must be arrays")
    left_copy = _copy_symbols(left, program._symbols, "config left")
    right_copy = _copy_symbols(right, program._symbols, "config right")
    head = _integer(obj["head"], "config head")
    steps = _integer(obj["steps"], "config steps")
    if steps < 0 or abs(head) > steps:
        raise ValueError("config requires steps >= 0 and abs(head) <= steps")
    return {"state": state, "left": left_copy, "current": current, "right": right_copy, "head": head, "steps": steps}


def initial(program: Program, tape: list[str]) -> dict:
    if not isinstance(tape, list):
        raise ValueError("tape must be an array")
    symbols = [_string(x, "tape member") for x in tape]
    if any(x not in program._symbols for x in symbols):
        raise ValueError("tape contains an undeclared symbol")
    if symbols:
        current, right = symbols[0], list(reversed(symbols[1:]))
    else:
        current, right = program.blank, []
    return {"state": program.start, "left": [], "current": current, "right": right, "head": 0, "steps": 0}


def _next(program: Program, config: dict) -> tuple[str, str, str]:
    if config["state"] in program._halt:
        raise ValueError("machine is halted")
    transition = program._table.get((config["state"], config["current"]))
    if transition is None:
        raise ValueError("machine is stuck: no transition")
    return transition


def _apply_in_place(program: Program, config: dict, transition: tuple[str, str, str]) -> None:
    nxt, written, move = transition
    config["current"] = written
    if move == "L":
        config["right"].append(written)
        config["current"] = config["left"].pop() if config["left"] else program.blank
        config["head"] -= 1
    elif move == "R":
        config["left"].append(written)
        config["current"] = config["right"].pop() if config["right"] else program.blank
        config["head"] += 1
    config["state"] = nxt
    config["steps"] += 1


def step(program: Program, config: dict) -> dict:
    checked = _validate_config(program, config)
    transition = _next(program, checked)
    _apply_in_place(program, checked, transition)
    return checked


def observe(program: Program, config: dict) -> dict:
    checked = _validate_config(program, config)
    nonblank: list[list[Any]] = []
    # Emit left-to-right directly: no temporary reversed output or sort needed.
    head, blank = checked["head"], program.blank
    left_start = head - len(checked["left"])
    for index, symbol in enumerate(checked["left"]):
        if symbol != blank:
            nonblank.append([left_start + index, symbol])
    if checked["current"] != blank:
        nonblank.append([head, checked["current"]])
    for offset, symbol in enumerate(reversed(checked["right"]), 1):
        if symbol != blank:
            nonblank.append([head + offset, symbol])
    return {"state": checked["state"], "head": checked["head"], "steps": checked["steps"], "tape": nonblank}


def run(program: Program, config: dict, step_budget: int, cell_limit: int, stop_path: Path | None = None) -> dict:
    budget = _integer(step_budget, "step_budget")
    limit = _integer(cell_limit, "cell_limit")
    if budget < 0 or limit < 1:
        raise ValueError("step_budget must be >= 0 and cell_limit must be >= 1")
    current = _validate_config(program, config)
    stop_file = Path(stop_path) if stop_path is not None else None
    executed = 0
    while True:
        if stop_file is not None and stop_file.exists():
            status = "STOPPED"
            break
        if len(current["left"]) + len(current["right"]) + 1 > limit:
            status = "RESOURCE_LIMIT"
            break
        if current["state"] in program._halt:
            status = "HALTED"
            break
        transition = program._table.get((current["state"], current["current"]))
        if transition is None:
            status = "STUCK"
            break
        if executed >= budget:
            status = "PAUSED"
            break
        move = transition[2]
        slots = len(current["left"]) + len(current["right"]) + 1
        grows = (move == "L" and not current["left"]) or (move == "R" and not current["right"])
        if grows and slots >= limit:
            status = "RESOURCE_LIMIT"
            break
        _apply_in_place(program, current, transition)
        executed += 1
    return {"status": status, "config": current, "executed": executed}


def checkpoint(program: Program, result: dict) -> dict:
    obj = _exact_fields(result, {"status", "config", "executed"}, "run result")
    status = _string(obj["status"], "status")
    if status not in {"HALTED", "STUCK", "PAUSED", "RESOURCE_LIMIT", "STOPPED"}:
        raise ValueError("invalid run status")
    config = _validate_config(program, obj["config"])
    executed = _integer(obj["executed"], "executed")
    if executed < 0:
        raise ValueError("executed must be nonnegative")
    payload = {"schema": 1, "machine_digest": program.digest, "config": config, "status": status}
    payload["checksum"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def restore(program: Program, payload: dict) -> dict:
    obj = _exact_fields(payload, {"schema", "machine_digest", "config", "status", "checksum"}, "checkpoint")
    if type(obj["schema"]) is not int or obj["schema"] != 1:
        raise ValueError("checkpoint schema must be integer 1")
    if obj["machine_digest"] != program.digest:
        raise ValueError("checkpoint machine_digest mismatch")
    status = _string(obj["status"], "checkpoint status")
    if status not in {"HALTED", "STUCK", "PAUSED", "RESOURCE_LIMIT", "STOPPED"}:
        raise ValueError("invalid checkpoint status")
    checksum = _string(obj["checksum"], "checkpoint checksum")
    unsigned = {key: value for key, value in obj.items() if key != "checksum"}
    expected = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    if not hmac.compare_digest(checksum, expected):
        raise ValueError("checkpoint checksum mismatch")
    return _validate_config(program, obj["config"])


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError(f"input file exceeds {MAX_INPUT_BYTES} bytes")
    return data


def _existing_matches(path: Path, data: bytes) -> bool:
    """Accept an existing destination only when it is the exact regular file."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise FileExistsError(f"output exists and is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise FileExistsError(f"output changed or is not a regular file: {path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            return stream.read(len(data) + 1) == data
    finally:
        os.close(fd)


def _publish_no_replace(temp_name: str, path: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        if not move(temp_name, str(path), 0x00000008):  # MOVEFILE_WRITE_THROUGH; no replace flag
            error = ctypes.get_last_error()
            if error in (80, 183):
                raise FileExistsError(error, os.strerror(error), str(path))
            raise OSError(error, os.strerror(error), str(path))
    else:
        os.link(temp_name, path)


def _atomic_create(path: Path, data: bytes) -> list[str]:
    """Return warnings for post-commit durability or temporary cleanup failures."""
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.is_dir():
        raise ValueError(f"output directory does not exist: {parent}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(parent))
    committed = False
    warnings = []
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _publish_no_replace(temp_name, path)
            committed = True
        except FileExistsError:
            if _existing_matches(path, data):
                committed = True
            else:
                raise FileExistsError(f"output already exists with different content: {path}")
        if os.name != "nt":
            try:
                dir_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError as exc:
                warnings.append(f"publication committed; directory durability could not be confirmed: {exc}")
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            message = f"temporary-file cleanup failed: {exc}"
            warnings.append(("publication committed; " if committed else "publication not committed; ") + message)
    return warnings


def _cli_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="Run a frozen deterministic two-way Turing machine")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_ArgumentParser)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("machine", type=Path)
    run_parser.add_argument("--input", required=True, help="JSON array of input symbols")
    resume_parser = commands.add_parser("resume")
    resume_parser.add_argument("machine", type=Path)
    resume_parser.add_argument("checkpoint", type=Path)
    for sub in (run_parser, resume_parser):
        sub.add_argument("--steps", type=int, required=True)
        sub.add_argument("--cells", type=int, required=True)
        sub.add_argument("--output", type=Path, required=True)
        sub.add_argument("--stop-file", type=Path)
    return parser


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _main(argv: list[str] | None = None) -> int:
    try:
        args = _cli_parser().parse_args(argv)
        if args.steps < 0 or args.steps > MAX_STEPS:
            raise ValueError(f"--steps must be between 0 and {MAX_STEPS}")
        if args.cells < 1 or args.cells > MAX_CELLS:
            raise ValueError(f"--cells must be between 1 and {MAX_CELLS}")
        program = compile_machine(_loads(_read_bounded(args.machine)))
        if args.command == "run":
            if len(args.input.encode("utf-8")) > MAX_INPUT_BYTES:
                raise ValueError(f"input exceeds {MAX_INPUT_BYTES} bytes")
            tape = _loads(args.input)
            config = initial(program, tape)
        else:
            payload = _loads(_read_bounded(args.checkpoint))
            config = restore(program, payload)
        result = run(program, config, args.steps, args.cells, args.stop_file)
        saved_checkpoint = checkpoint(program, result)
        checkpoint_bytes = _canonical_bytes(saved_checkpoint) + b"\n"
        if len(checkpoint_bytes) > MAX_INPUT_BYTES:
            raise ValueError(f"checkpoint exceeds {MAX_INPUT_BYTES} bytes; cell and byte limits are independent")
        warnings = _atomic_create(args.output, checkpoint_bytes)
        for warning in warnings:
            sys.stderr.write(json.dumps({"warning": warning}, ensure_ascii=False, sort_keys=True) + "\n")
        output = {"status": result["status"], "executed": result["executed"], "observation": observe(program, result["config"]), "checkpoint": str(args.output)}
        sys.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        sys.stderr.write(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True) + "\n")
        return 1

if __name__ == "__main__":
    raise SystemExit(_main())
