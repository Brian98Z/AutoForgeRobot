#!/usr/bin/env python3
"""Correctness-gated, reproducible microbenchmarks for the universal machine."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
import uuid
from pathlib import Path

import machine

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"


def definition(transitions, states=("q", "halt"), symbols=("_", "0", "1")):
    return {
        "schema": 1, "states": list(states), "symbols": list(symbols),
        "blank": "_", "start": states[0], "halt": ["halt"],
        "transitions": transitions,
    }


def row(state, read, nxt, write, move):
    return {"state": state, "read": read, "next": nxt, "write": write, "move": move}


def stationary_machine():
    return definition([row("q", "_", "q", "_", "S")])


def growth_machine():
    return definition([row("q", "_", "q", "1", "R"), row("q", "1", "q", "1", "R")])


def scan_machine():
    return definition([row("right", "_", "left", "_", "R"), row("left", "_", "right", "_", "L")],
                      states=("right", "left", "halt"))


def carry_machine():
    transitions = [row("carry", "1", "carry", "0", "R"), row("carry", "_", "halt", "1", "S")]
    return definition(transitions, states=("carry", "halt"))

def nearest(values, percentile):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def distribution(samples):
    return {
        "samples_seconds": samples,
        "min_seconds": min(samples),
        "median_seconds": statistics.median(samples),
        "p95_seconds_nearest_rank": nearest(samples, 0.95),
        "max_seconds": max(samples),
    }


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_benchmark(repeats, output):
    run_id = "benchmark-" + uuid.uuid4().hex[:12]
    outdir = output if output is not None else RESULTS / run_id
    outdir.mkdir(parents=True, exist_ok=False)
    report_path = outdir / "report.json"
    policy_path = HERE / "benchmark-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    started = time.perf_counter()
    report = {
        "schema": 1, "run_id": run_id, "status": "RUNNING", "score": 0,
        "environment": {"python": sys.version, "implementation": platform.python_implementation(),
                        "platform": platform.platform(), "machine": platform.machine(),
                        "processor": platform.processor()},
        "repeats": repeats, "warmup_runs": 1,
        "policy": policy,
        "proof_kernel": {"status": "NOT_EVALUATED", "scored": False},
        "score_scope": "Fixed engineering-target attainment; not a universality, safety, or intelligence score",
        "correctness": {"verify_returncode": None, "finite_conformance": "NOT_EVALUATED",
                        "verify_stdout": "", "verify_stderr": "", "workloads": {},
                        "checkpoint_continuation": "NOT_EVALUATED"},
        "workloads": {}, "checkpoint": {}, "memory": {}, "score_computation": {},
        "elapsed_seconds": None,
        "hashes": {"machine.py": sha256(HERE / "machine.py"),
                   "verify.py": sha256(HERE / "verify.py"),
                   "benchmark.py": sha256(Path(__file__)),
                   "benchmark-policy.json": sha256(policy_path)},
    }

    def save():
        report["elapsed_seconds"] = time.perf_counter() - started
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                               encoding="utf-8")

    try:
        proc = subprocess.run([sys.executable, str(HERE / "verify.py"), "--random-cases", "600"],
                             capture_output=True, text=True, encoding="utf-8", timeout=45, cwd=HERE)
        report["correctness"].update(verify_returncode=proc.returncode,
                                     verify_stdout=proc.stdout, verify_stderr=proc.stderr)
        evidence = None
        for line in proc.stdout.splitlines():
            if line.startswith("Evidence:"):
                evidence = Path(line.partition(":")[2].strip())
                break
        if evidence and evidence.is_file():
            report["correctness"]["verify_evidence"] = str(evidence)
            report["correctness"]["verify_evidence_sha256"] = sha256(evidence)
            try:
                verify_report = json.loads(evidence.read_text(encoding="utf-8"))
                report["correctness"]["finite_conformance"] = verify_report.get("finite_conformance", "NOT_EVALUATED")
                report["correctness"]["verify_random_cases"] = verify_report.get("random_cases")
            except (OSError, json.JSONDecodeError) as exc:
                report["correctness"]["verify_evidence_error"] = str(exc)
        if proc.returncode != 0 or report["correctness"]["finite_conformance"] != "PASS":
            raise RuntimeError("verify.py correctness gate failed or finite_conformance was not PASS")

        cases = [
            ("stationary_loop", stationary_machine(), [], 30000,
             lambda n: {"state": "q", "head": 0, "steps": n, "tape": []}),
            ("right_growing_tape", growth_machine(), [], 30000,
             lambda n: {"state": "q", "head": n, "steps": n,
                        "tape": [[i, "1"] for i in range(n)]}),
            ("bidirectional_scan", scan_machine(), [], 30000,
             lambda n: {"state": "right" if n % 2 == 0 else "left",
                        "head": 0 if n % 2 == 0 else 1, "steps": n, "tape": []}),
            ("binary_carry", carry_machine(), ["1"] * 30000, 30001,
             lambda n: {"state": "halt", "head": 30000, "steps": n,
                        "tape": [[i, "0"] for i in range(30000)] + [[30000, "1"]]}),
        ]
        for name, raw, tape, steps, expected_fn in cases:
            compile_start = time.perf_counter()
            program = machine.compile_machine(raw)
            compile_seconds = time.perf_counter() - compile_start
            initial = machine.initial(program, tape)
            expected = expected_fn(steps)
            cell_limit = 100000
            warm = machine.run(program, initial, steps, cell_limit)
            observed = machine.observe(program, warm["config"])
            if warm["status"] != ("HALTED" if name == "binary_carry" else "PAUSED") or observed != expected:
                raise AssertionError(f"{name} warmup expected-output mismatch")
            samples = []
            for _ in range(repeats):
                begin = time.perf_counter()
                result = machine.run(program, initial, steps, cell_limit)
                duration = time.perf_counter() - begin
                observed = machine.observe(program, result["config"])
                if result["status"] != ("HALTED" if name == "binary_carry" else "PAUSED") or observed != expected:
                    raise AssertionError(f"{name} expected-output mismatch")
                samples.append(duration)
            stats = distribution(samples)
            stats.update({"steps": steps, "transitions_per_second_samples": [steps / x for x in samples],
                          "median_transitions_per_second": steps / stats["median_seconds"],
                          "compile_seconds": compile_seconds, "input_cells": len(tape),
                          "cell_limit": cell_limit, "program_digest": program.digest,
                          "expected_output_sha256": hashlib.sha256(json.dumps(expected, sort_keys=True,
                                                        separators=(",", ":")).encode()).hexdigest()})
            report["workloads"][name] = stats
            report["correctness"]["workloads"][name] = "PASS"
            save()

        # Measure checkpoint/restore on a large, exactly specified tape, then compare continuation.
        grow = machine.compile_machine(growth_machine())
        large_state = machine.initial(grow, [])
        prefix_steps, suffix_steps = 40000, 10000
        prefix = machine.run(grow, large_state, prefix_steps, 100000)
        if prefix["status"] != "PAUSED" or len(prefix["config"]["left"]) != prefix_steps:
            raise AssertionError("checkpoint setup did not produce expected large state")
        expected_prefix = {"state": "q", "head": prefix_steps, "steps": prefix_steps,
                           "tape": [[i, "1"] for i in range(prefix_steps)]}
        if machine.observe(grow, prefix["config"]) != expected_prefix:
            raise AssertionError("checkpoint setup expected-state mismatch")
        checkpoint_samples, restore_samples = [], []
        payload = machine.checkpoint(grow, prefix)
        for _ in range(repeats):
            begin = time.perf_counter(); payload = machine.checkpoint(grow, prefix)
            checkpoint_samples.append(time.perf_counter() - begin)
            begin = time.perf_counter(); restored = machine.restore(grow, payload)
            restore_samples.append(time.perf_counter() - begin)
            if machine.observe(grow, restored) != machine.observe(grow, prefix["config"]):
                raise AssertionError("restored state differs from checkpointed state")
        resumed_config = machine.restore(grow, payload)
        resumed = machine.run(grow, resumed_config, suffix_steps, 100000)
        uninterrupted = machine.run(grow, large_state, prefix_steps + suffix_steps, 100000)
        if resumed["status"] != uninterrupted["status"] or machine.observe(grow, resumed["config"]) != machine.observe(grow, uninterrupted["config"]):
            raise AssertionError("checkpoint continuation differs from uninterrupted run")
        expected_continuation = {"state": "q", "head": prefix_steps + suffix_steps,
                                 "steps": prefix_steps + suffix_steps,
                                 "tape": [[i, "1"] for i in range(prefix_steps + suffix_steps)]}
        if machine.observe(grow, resumed["config"]) != expected_continuation:
            raise AssertionError("checkpoint continuation expected-output mismatch")
        report["correctness"]["checkpoint_continuation"] = "PASS"
        report["checkpoint"] = {
            "state_cells": len(prefix["config"]["left"]) + len(prefix["config"]["right"]) + 1,
            "checkpoint": distribution(checkpoint_samples), "restore": distribution(restore_samples),
            "restore_correctness": "PASS",
            "continuation_expected_output_sha256": hashlib.sha256(json.dumps(expected_continuation, sort_keys=True,
                                                        separators=(",", ":")).encode()).hexdigest(),
            "payload_bytes": len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()),
            "prefix_steps": prefix_steps, "suffix_steps": suffix_steps,
        }

        # Separate, untimed memory pass; tracemalloc measures Python allocations, not RSS.
        memory_program = machine.compile_machine(growth_machine())
        memory_initial = machine.initial(memory_program, [])
        tracemalloc.start()
        try:
            memory_result = machine.run(memory_program, memory_initial, 30000, 100000)
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        if machine.observe(memory_program, memory_result["config"]) != {
                "state": "q", "head": 30000, "steps": 30000,
                "tape": [[i, "1"] for i in range(30000)]}:
            raise AssertionError("instrumented memory-pass result mismatch")
        report["memory"] = {"instrumentation": "tracemalloc", "peak_bytes": peak,
                             "current_bytes_after_run": current, "rss": "NOT_MEASURED",
                             "timed_workloads_instrumented": False, "steps": 30000}

        weights = policy["weights"]
        contributions = {}
        expected_weights = {"stationary_loop", "right_growing_tape", "bidirectional_scan", "binary_carry", "checkpoint_median", "checkpoint_p95", "restore_median"}
        if set(weights) != expected_weights or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in weights.values()):
            raise ValueError("invalid score weights")
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in policy["targets"].values()):
            raise ValueError("invalid benchmark targets")
        for name in ("stationary_loop", "right_growing_tape", "bidirectional_scan", "binary_carry"):
            target = policy["targets"][name + "_transitions_per_second"]
            throughput = report["workloads"][name]["median_transitions_per_second"]
            points = min(100.0, 100.0 * throughput / target)
            headroom = throughput / target
            contributions[name] = {"measured": throughput, "target": target,
                                   "points": points, "weight": weights[name],
                                   "weighted_points": points * weights[name]}
            contributions[name]["target_ratio"] = headroom
        for metric, key in (("checkpoint_median", "median_seconds"), ("checkpoint_p95", "p95_seconds_nearest_rank")):
            target = policy["targets"][metric + "_seconds_max"]
            measured = report["checkpoint"]["checkpoint"][key]
            points = 100.0 if measured <= target else 100.0 * target / measured
            contributions[metric] = {"measured": measured, "target_max": target,
                                     "points": points, "weight": weights[metric],
                                     "weighted_points": points * weights[metric]}
        restore_target = policy["targets"]["restore_median_seconds_max"]
        restore_measured = report["checkpoint"]["restore"]["median_seconds"]
        restore_points = 100.0 if restore_measured <= restore_target else 100.0 * restore_target / restore_measured
        contributions["restore_median"] = {"measured": restore_measured, "target_max": restore_target,
                                           "points": restore_points, "weight": weights["restore_median"],
                                           "weighted_points": restore_points * weights["restore_median"]}
        total = sum(item["weighted_points"] for item in contributions.values()) / sum(weights.values())
        report["score_computation"] = {"method": policy["scoring"]["method"],
                                       "policy_schema": policy["schema"],
                                       "score_version": policy["score_version"],
                                       "contributions": contributions,
                                       "weight_sum": sum(weights.values()), "raw_total": total,
                                       "score": round(total, 4)}
        report["score"] = round(total, 4)
        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"
        report["score"] = 0
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        save()
    print(json.dumps({"status": report["status"], "score": report["score"],
                      "report": str(report_path)}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--output", type=Path, help="New absent output directory; defaults to unique results/benchmark-<id>")
    args = parser.parse_args(argv)
    if args.repeats < 3:
        parser.error("--repeats must be at least 3")
    try:
        return run_benchmark(args.repeats, args.output)
    except FileExistsError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
