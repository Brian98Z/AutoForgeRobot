#!/usr/bin/env python3
"""Exercise sequential offline goal evolution against the real AutoForge release binary.

This is bounded behavioral evidence for the current i64 synthesizers, not a proof of
Turing completeness or of universal self-bootstrap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import random
import subprocess
import sys
import time
import traceback
import uuid
from typing import Callable

import paths

PROJECT = pathlib.Path(__file__).resolve().parents[1]
EXE = paths.release_binary(PROJECT)
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
MASK = (1 << 64) - 1


def i64(value: int) -> int:
    value &= MASK
    return value - (1 << 64) if value >> 63 else value


def functions() -> list[tuple[str, str, Callable[[int, int], int]]]:
    return [
        ("sum", "Return the signed 64-bit wrapping sum x+y", lambda x, y: i64(x + y)),
        ("difference", "Return the signed 64-bit wrapping difference x-y", lambda x, y: i64(x - y)),
        ("reverse-difference", "Return the signed 64-bit wrapping difference y-x", lambda x, y: i64(y - x)),
        ("product", "Return the signed 64-bit wrapping product x*y", lambda x, y: i64(x * y)),
        ("minimum", "Return the smaller signed integer of x and y", min),
        ("maximum", "Return the larger signed integer of x and y", max),
        ("absolute-wrapping-difference", "Return abs(i64(x-y)) with signed 64-bit wrapping", lambda x, y: abs(i64(x - y)) if i64(x - y) != I64_MIN else I64_MIN),
        ("affine-a", "Return i64(3*x-2*y+7)", lambda x, y: i64(3 * x - 2 * y + 7)),
        ("affine-b", "Return i64(-4*x+5*y-9)", lambda x, y: i64(-4 * x + 5 * y - 9)),
        ("constant", "Return the constant signed integer -17", lambda x, y: -17),
        ("clamp-x", "Clamp x to the inclusive range [-7, 11]", lambda x, y: max(-7, min(11, x))),
        ("clamp-y", "Clamp y to the inclusive range [-13, 4]", lambda x, y: max(-13, min(4, y))),
    ]


def contract_cases(fn: Callable[[int, int], int], seed: int) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    boundaries = [I64_MIN, I64_MIN + 1, -32, -17, -1, 0, 1, 4, 7, 11, 16, 32, I64_MAX - 1, I64_MAX]
    points: list[tuple[int, int]] = [(x, y) for x, y in zip(boundaries, reversed(boundaries))]
    points += [(rng.randint(I64_MIN, I64_MAX), rng.randint(I64_MIN, I64_MAX)) for _ in range(36)]
    # More than a dozen examples in each independently frozen partition.
    public_points = points[:26]
    holdout_points = points[26:]
    assert len(public_points) >= 12 and len(holdout_points) >= 12
    to_cases = lambda rows: [{"input": [x, y], "expected": fn(x, y)} for x, y in rows]
    return to_cases(public_points), to_cases(holdout_points)


def fresh_probes(fn: Callable[[int, int], int], seed: int,
                 contract_inputs: set[tuple[int, int]]) -> list[dict]:
    rng = random.Random(seed ^ 0xB00757A9)
    probes = []
    while len(probes) < 5:
        pair = (rng.randint(I64_MIN, I64_MAX), rng.randint(I64_MIN, I64_MAX))
        if pair in contract_inputs or any(tuple(p["input"]) == pair for p in probes):
            continue
        probes.append({"input": list(pair), "expected": fn(*pair)})
    return probes


class Harness:
    def __init__(self, run_dir: pathlib.Path, timeout: int):
        self.run_dir = run_dir
        self.state = run_dir / "state"
        self.tasks = run_dir / "tasks"
        self.events = run_dir / "events"
        self.timeout = timeout
        self.commands = 0
        self.started = time.monotonic()
        self.report: dict = {
            "schema": 1,
            "status": "RUNNING",
            "run_directory": str(run_dir),
            "project": str(PROJECT),
            "executable": str(EXE),
            "proposal_rounds": [],
            "extra_probes": [],
            "checks": [],
            "errors": [],
            "claims": {
                "mode": "real release executable plus offline Python and Julia providers",
                "mocks": False,
                "independence_proven_by_hashes": False,
                "turing_completeness_proven": False,
                "scope": "finite seeded behavioral evidence only; bounded i64 ABI and fixed synthesizers",
            },
        }
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith("AUTOFORGE_"):
                self.env.pop(key)
        self.env.update({"AUTOFORGE_PROJECT": str(PROJECT), "AUTOFORGE_PROVIDER": "offline",
                         "AUTOFORGE_PYTHON": sys.executable})

    def save(self) -> None:
        self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        path = self.run_dir / "report.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)

    def record(self, label: str, passed: bool, evidence: dict | None = None) -> None:
        entry = {"check": label, "result": "PASS" if passed else "FAIL"}
        if evidence is not None:
            entry["evidence"] = evidence
        self.report["checks"].append(entry)
        print(f'{entry["result"]}: {label}', flush=True)
        if not passed:
            raise AssertionError(label)

    def call(self, *args: object, expected_code: int = 0, timeout: int | None = None) -> dict:
        self.commands += 1
        ident = f"{self.commands:04d}-{time.time_ns()}"
        cmd = [str(EXE), *(str(arg) for arg in args)]
        started = time.monotonic()
        try:
            proc = subprocess.run(cmd, cwd=PROJECT, env=self.env, capture_output=True,
                                  timeout=timeout or self.timeout, check=False)
            stdout = proc.stdout.decode("utf-8", errors="replace")
            stderr = proc.stderr.decode("utf-8", errors="replace")
            result = {"command": cmd, "returncode": proc.returncode, "expected_returncode": expected_code,
                      "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                      "stdout": stdout, "stderr": stderr}
            (self.events / f"{ident}.stdout.txt").write_text(stdout, encoding="utf-8")
            (self.events / f"{ident}.stderr.txt").write_text(stderr, encoding="utf-8")
            (self.events / f"{ident}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            if proc.returncode != expected_code:
                raise RuntimeError(f"command exit {proc.returncode}, expected {expected_code}: {cmd}\n{stdout}\n{stderr}")
            text = stdout.strip() or stderr.strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                if expected_code != 0:
                    return {"raw": text}
                raise RuntimeError(f"command returned no JSON: {cmd}\n{stdout}\n{stderr}")
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            for suffix, data in (("stdout.txt", stdout), ("stderr.txt", stderr)):
                (self.events / f"{ident}.{suffix}").write_text(data, encoding="utf-8")
            (self.events / f"{ident}.json").write_text(json.dumps({"command": cmd, "timeout": timeout or self.timeout,
                "stdout": stdout, "stderr": stderr}, indent=2) + "\n", encoding="utf-8")
            raise RuntimeError(f"command timed out after {timeout or self.timeout}s: {cmd}") from exc

    def task(self, name: str, goal: str, fn: Callable[[int, int], int], index: int) -> tuple[pathlib.Path, list[dict], list[dict]]:
        public, holdout = contract_cases(fn, 0xA170 + index)
        task = {"schema": 1, "id": name, "goal": goal, "public_cases": public,
                "holdout_cases": holdout, "max_rounds": 2}
        path = self.tasks / f"{index + 1:02d}-{name}.json"
        path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
        return path, public, holdout

    def release_evidence(self, release_id: str) -> dict:
        envelope_path = self.state / "blobs" / release_id
        raw = envelope_path.read_bytes()
        envelope = json.loads(raw)
        payload = envelope["payload"]
        blobs = {}
        for field in ("wasm", "wat", "audit_wasm", "task"):
            artifact_id = payload[field]
            data = (self.state / "blobs" / artifact_id).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest != artifact_id:
                raise AssertionError(f"artifact hash mismatch: {field}")
            blobs[field] = {"id": artifact_id, "sha256": digest, "bytes": len(data)}
            if field == "wat":
                blobs[field]["source_utf8"] = data.decode("utf-8")
        return {"release_id": release_id, "envelope_sha256": hashlib.sha256(raw).hexdigest(),
                "payload": payload, "blobs": blobs}

    def run(self) -> None:
        if not EXE.is_file():
            raise FileNotFoundError(f"release executable missing: {EXE}")
        self.report["executable_sha256"] = hashlib.sha256(EXE.read_bytes()).hexdigest()
        julia = paths.julia_binary(PROJECT)
        if julia.is_file():
            self.env["AUTOFORGE_JULIA"] = str(julia)
            self.report["julia_executable"] = str(julia)
            self.report["julia_sha256"] = hashlib.sha256(julia.read_bytes()).hexdigest()
        else:
            self.report["julia_executable"] = "PATH:julia"
        init = self.call("init", self.state)
        self.record("isolated fresh state initialized", bool(init.get("initialized")), {"state": init})
        rounds = self.functions_rounds()
        self.report["proposal_round_count"] = len(rounds)
        self.extra_scenarios()
        self.report["status"] = "PASS"

    def functions_rounds(self) -> list[dict]:
        results = []
        previous_release = None
        for index, (name, goal, fn) in enumerate(functions()):
            path, public, holdout = self.task(name, goal, fn, index)
            before = self.call("status", self.state)["active"]
            start = time.monotonic()
            outcome = self.call("evolve", self.state, path)
            elapsed = round(time.monotonic() - start, 3)
            expected_proposal_rounds = 1 if index == 0 else 2
            if outcome.get("decision") != "PROMOTE" or outcome.get("rounds") != expected_proposal_rounds:
                raise AssertionError(f"{name} did not successfully evolve in {expected_proposal_rounds} proposal rounds: {outcome}")
            release_id = outcome["release"]
            evidence = self.release_evidence(release_id)
            attempts = evidence["payload"]["attempts"]
            line_pass = {line: [a for a in attempts if a.get("line") == line and a.get("decision") == "PASS"]
                         for line in ("python", "julia")}
            for line, passed in line_pass.items():
                if len(passed) != 1:
                    raise AssertionError(f"{name}: expected exactly one {line} PASS attempt, got {len(passed)}")
            round1 = [a for a in attempts if a.get("round") == 1]
            if previous_release is not None:
                if len(round1) < 2 or any(a.get("decision") != "REJECT" for a in round1):
                    raise AssertionError(f"{name}: old candidate was not rejected by both lines in round 1")
            else:
                if len(round1) != 2 or any(a.get("decision") != "PASS" for a in round1):
                    raise AssertionError(f"{name}: initial proposals did not both pass in round 1")
            if outcome.get("previous") != before or before != previous_release:
                raise AssertionError(f"{name}: prior active pointer mismatch")
            replay = self.call("replay", self.state)
            if replay.get("verified_releases") != index + 1:
                raise AssertionError(f"{name}: replay count mismatch: {replay}")
            contract_inputs = {tuple(c["input"]) for c in public + holdout}
            probes = fresh_probes(fn, 0xB007 + index, contract_inputs)
            for probe in probes:
                actual = self.call("execute", self.state, *probe["input"])
                if actual.get("output") != probe["expected"]:
                    raise AssertionError(f"{name}: fresh execution probe mismatch: {probe}, {actual}")
                self.report["extra_probes"].append({"round": index + 1, "name": name,
                    "input": probe["input"], "expected": probe["expected"], "actual": actual["output"],
                    "in_contract": False})
                self.save()
            row = {"evolution_round": index + 1, "task_id": name, "decision": "PROMOTE",
                   "proposal_rounds": outcome["rounds"], "elapsed_seconds": elapsed,
                   "previous_release": before, "release": release_id,
                   "old_candidate_round1_rejected": previous_release is not None,
                   "pass_evidence": {line: [{"round": a["round"], "wasm": a["wasm"], "wat": a["wat"],
                                              "strategy": a.get("strategy"), "decision": a["decision"]}
                                      for a in passed] for line, passed in line_pass.items()},
                   "release_artifacts": evidence["blobs"], "replay": replay,
                   "fresh_probe_count": len(probes)}
            self.report["proposal_rounds"].append(row)
            self.record(f"round {index + 1}: {name} promoted after both offline lines passed",
                        all(line_pass.values()), {"proposal_rounds": outcome["rounds"],
                        "python_pass": len(line_pass["python"]), "julia_pass": len(line_pass["julia"]),
                        "previous": before, "release": release_id, "elapsed_seconds": elapsed})
            results.append(row)
            previous_release = release_id
            self.save()
        return results

    def extra_scenarios(self) -> None:
        previous = self.call("status", self.state)["active"]
        # Same ID task proposes a different function. Public checks fit it, but historical anchors veto it.
        regression_path, _, _ = self.task("clamp-y", "Incompatible changed behavior", lambda x, y: i64(x + y), 20)
        result = self.call("evolve", self.state, regression_path, expected_code=2)
        self.record("same-ID historical regression veto holds", result.get("decision") == "HOLD",
                    {"result": result, "active": self.call("status", self.state)["active"]})
        if self.call("status", self.state)["active"] != previous:
            raise AssertionError("same-ID HOLD changed active pointer")

        # Public sum is consistent; intentionally wrong hidden expectation must veto promotion.
        hidden_path, public, holdout = self.task("hidden-veto", "Return wrapping sum", lambda x, y: i64(x + y), 21)
        task = json.loads(hidden_path.read_text(encoding="utf-8"))
        task["holdout_cases"][0]["expected"] = i64(task["holdout_cases"][0]["expected"] + 1)
        hidden_path.write_text(json.dumps(task, indent=2) + "\n", encoding="utf-8")
        result = self.call("evolve", self.state, hidden_path, expected_code=2)
        self.record("hidden-only incorrect expectation veto holds", result.get("decision") == "HOLD", {"result": result})
        if self.call("status", self.state)["active"] != previous:
            raise AssertionError("hidden-only HOLD changed active pointer")

        # Contract validation rejects contradictory answers before any candidate proposal.
        contradictory = {"schema": 1, "id": "contradictory", "goal": "contradictory contract",
                         "public_cases": [{"input": [1, 2], "expected": 3}, {"input": [1, 2], "expected": 4}],
                         "holdout_cases": [{"input": [0, 0], "expected": 0}], "max_rounds": 2}
        cp = self.tasks / "extra-contradictory.json"
        cp.write_text(json.dumps(contradictory), encoding="utf-8")
        result = self.call("evolve", self.state, cp, expected_code=1)
        self.record("contradictory goal rejected without pointer change", "Ambiguous" in result.get("error", ""), {"result": result})
        if self.call("status", self.state)["active"] != previous:
            raise AssertionError("contradictory contract changed active pointer")

        # XOR is outside the documented synthesizer set; exact, diverse samples must remain HOLD.
        xor = lambda x, y: i64(x ^ y)
        xp, _, _ = self.task("unsupported-xor", "Return bitwise exclusive-or x xor y", xor, 22)
        result = self.call("evolve", self.state, xp, expected_code=2)
        self.record("unsupported x xor y remains HOLD", result.get("decision") == "HOLD", {"result": result})
        if self.call("status", self.state)["active"] != previous:
            raise AssertionError("unsupported XOR HOLD changed active pointer")

        # Persistent rollback must restore the previous release and execute successfully.
        last = self.report["proposal_rounds"][-1]["release"]
        prior = self.report["proposal_rounds"][-2]["release"]
        rollback = self.call("rollback", self.state)
        status = self.call("status", self.state)
        self.record("persistent rollback restores prior release", rollback.get("active") == prior and status.get("active") == prior,
                    {"rollback": rollback, "status": status, "from": last, "to": prior})
        assert self.call("execute", self.state, 100, -100)["output"] == 11
        self.call("replay", self.state)
        second_rollback = self.call("rollback", self.state)
        assert second_rollback["active"] == self.report["proposal_rounds"][-3]["release"]
        assert self.call("execute", self.state, 100, -100)["output"] == -17

        # Explicitly exercise the operator latch, refusal to execute, and resume.
        self.call("kill", self.state)
        status = self.call("status", self.state)
        refused = self.call("execute", self.state, 5, 7, expected_code=1)
        self.call("resume", self.state)
        resumed = self.call("status", self.state)
        assert self.call("execute", self.state, 5, 7)["output"] == -17
        self.record("kill/resume latch blocks then restores execution", status.get("state") == "KILLED"
                    and "KILL" in refused.get("error", "") and resumed.get("state") == "READY",
                    {"killed": status, "blocked_execute": refused, "resumed": resumed})

        # Tamper detection while restoring original bytes in finally, including on assertion failure.
        release = self.call("status", self.state)["active"]
        payload = self.release_evidence(release)["payload"]
        artifact = self.state / "blobs" / payload["wasm"]
        original = artifact.read_bytes()
        try:
            artifact.write_bytes(original + b"\x00tamper")
            tamper = self.call("execute", self.state, 5, 7, expected_code=1)
            self.record("tampered release artifact is rejected", "integrity" in tamper.get("error", "").lower(),
                        {"error": tamper.get("error"), "artifact_id": payload["wasm"]})
        finally:
            artifact.write_bytes(original)
        self.record("tamper test restored original artifact bytes", artifact.read_bytes() == original,
                    {"artifact_id": payload["wasm"], "sha256": hashlib.sha256(original).hexdigest()})
        final_replay = self.call("replay", self.state)
        self.report["extra_scenarios"] = {"same_id_regression": "HOLD", "hidden_only_veto": "HOLD",
            "contradictory_goal": "REJECT", "unsupported_xor": "HOLD", "rollback": "PASS",
            "kill_resume": "PASS", "tamper_and_restore": "PASS", "final_replay": final_replay}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path,
                        help="absent directory for this run; defaults to bootstrap-runs/<timestamp-random>")
    parser.add_argument("--timeout", type=int, default=180,
                        help="per-command subprocess timeout in seconds (default: 180)")
    args = parser.parse_args()
    base = PROJECT / "bootstrap-runs"
    run_dir = args.output if args.output is not None else base / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_dir = run_dir.resolve()
    if run_dir.exists():
        print(f"refusing existing output directory: {run_dir}", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True)
    for name in ("tasks", "events"):
        (run_dir / name).mkdir()
    harness = Harness(run_dir, max(1, args.timeout))
    harness.save()
    try:
        harness.run()
    except BaseException as exc:
        harness.report["status"] = "FAIL"
        harness.report["errors"].append({"type": type(exc).__name__, "message": str(exc),
                                         "traceback": traceback.format_exc()})
        harness.save()
        print(f"bootstrap failed; partial report: {run_dir / 'report.json'}", file=sys.stderr)
        raise
    finally:
        harness.save()
    print(json.dumps({"status": harness.report["status"], "report": str(run_dir / "report.json"),
                      "evolution_rounds": harness.report.get("proposal_round_count", len(harness.report["proposal_rounds"]))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
