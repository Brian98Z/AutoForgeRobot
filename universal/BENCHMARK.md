# Benchmarking and the Proof Kernel Separately

## Usage

From the project directory:

```powershell
python universal\kernel_check.py
python universal\benchmark.py --repeats 7
```

The kernel check uses the project-pinned Lean4.35.0, runs formal/Simulation.lean and outputs a theorem dependency audit, then requires an intentionally false theorem, 0=1, to be rejected. The proof kernel checks only the Lean model; it does not certify the Python source code or the entire Turing-completeness encoding argument.

The benchmark does not pass off a historical kernel report as a kernel check for this run: it reports that item as NOT_EVALUATED and excludes it from the performance score. Kernel evidence is provided in a separate report.

## Correctness Hard Gate

Every benchmark first runs the real verify.py and requires exit code 0 and finite_conformance=PASS in the report. Every timed sample verifies the expected state, head position, step count, and all nonblank cells individually outside the timed interval. After checkpoint resume, the result must match both uninterrupted execution and the independently specified 50000-cell result.

Any correctness gate failure, runtime exception, or result mismatch: total score 0, exit code 1, and the failure report is retained. Performance cannot offset a correctness failure. Passing the gates does not prove the absence of security vulnerabilities.

## Workloads and Measurements

- In-place loop: 30000 steps, with no tape growth.
- Right-writing tape: 30000 steps, producing 30000 ones.
- Bidirectional shuttle: 30000 steps, with the head alternating between two adjacent blank cells; this is not long-tape scan throughput.
- Binary carry: 30000 ones, least-significant bit first; executes 30001 steps, flips them to 30000 zeros, and writes a 1 at the end.
- Checkpoint/resume: 40001 storage slots, followed by 10000 steps.

Default: 1 warmup + 7 samples. Save each duration in seconds and min/median/p95/max; p95 uses nearest-rank and equals the maximum with 7 samples, so it is not presented as a large-sample tail latency. Throughput is actual transitions divided by run duration. Compilation duration is recorded separately and is not included in execution throughput; initial input preparation is excluded from run, while run's own configuration validation/copy is included.

Checkpoint timing is in-memory construction + canonical encoding hash; restore timing is in-memory object validation + hash + copy, **excluding disk fsync or file reads**. This value should not be interpreted as persistence RTO.

tracemalloc runs once as a separate untimed sample and reports the peak Python-tracked allocation; it is not RSS and does not include all interpreter/native memory.

## Scoring Rules v1

`benchmark-policy.json` defines operator-set engineering targets, not an industry benchmark:

- In-place loop target: 100000 transitions/second; each of the other 3 classes: 10000 transitions/second.
- Checkpoint median≤50ms, p95≤100ms; restore median≤50ms.
- Score per throughput item: min(100,100×actual/target).
- Score per latency item: 100 if actual≤target, otherwise 100×target/actual.
- Each of the 4 throughput items has weight 0.2 and each of the 3 latency items has weight 0.1; weighted mean normalized by total weight 1.1.
- Score capped at 100; also report uncapped throughput target multiples and all raw samples.

**A score of 100 means only that all engineering targets in this set were met; it does not mean Turing-completeness, intelligence, self-iteration capability, security, or future extensibility is at 100%.** Do not raise or lower targets for the current results; any stricter targets added must use a new score_version and keep old reports interpretable.

The report saves machine/CPU/Python information, source and rules hashes, a complete policy snapshot, samples, and score calculations. Cross-machine comparisons must show the environment, repetition count, version, and raw values together; this benchmark is not a production-load or long-duration stability test.

## Local Measurements (11 Samples, score_version=1)

Report: `results/benchmark-814195d75887/report.json`. Correctness gates passed, total score 100/100. The targets are predefined conservative thresholds; a capped score must not be interpreted as relatively optimal.

|Workload|Median throughput (transitions/second)|Median execution time|p95 execution time|
|---|---:|---:|---:|
|In-place loop|3,341,725|8.977 ms|9.254 ms|
|Rightward growth|2,269,117|13.221 ms|16.577 ms|
|Adjacent-cell shuttle|2,022,108|14.836 ms|15.534 ms|
|Binary carry|2,034,739|14.744 ms|19.679 ms|

40001-slot state: in-memory checkpoint median 5.223ms, restore median 4.586ms. Separate tracemalloc sample peak 246832 bytes (not RSS). Environment: Windows, CPython3.11.6; complete CPU and samples are in the JSON.

Negative scoring control: in an independent temporary copy, changed the step counter from +1 to +2 on each step. The correctness gate rejected it, benchmark exited 1 and forced a score of 0. Evidence: `results/score-negative-8c6d19b4e57a/report.json`. Production machine.py was not changed.

The temporary path in the negative-control original report refers to the source used at that time; its exact bytes are retained in `negative-verifier-report.json` in the same directory, and the faulty copy is saved as `machine-mutant.py.txt`. The temporary copy was cleaned up; the historical report hash was not rewritten.

## Next Local Optimization and Same-Process Comparison

`machine.py` combines stack-symbol validation/copy, generates observations directly in absolute-coordinate order without sorting, and constructs the STOP path once per run (while still checking for its existence on every step). Input-copy isolation, resource limits, checkpoint format, and checksum are preserved; the scoring policy and Lean model are unchanged.

First freeze the original implementation at `results/optimization-dc6080e19239/baseline_machine.py`; profiling evidence is in `profile-before.txt` in the same directory. Use 21 alternating-order runs, untimed by a profiler; sample memory separately. Results: `results/optimization-check-9d8c093eb7a7/report.json`.

|Operation|Median ms before optimization|Median ms after optimization|Speedup|
|---|---:|---:|---:|
|80001-slot observation|18.427|8.005|2.302|
|80001-slot checkpoint|7.905|5.398|1.464|
|80001-slot restore|8.058|5.402|1.492|
|30000-step execution (without STOP path)|13.276|13.451|0.987|
|5000-step execution (per-step STOP check)|158.763|131.254|1.210|

Observation tracemalloc peak decreased from 5525100 to 5205084 bytes; save/restore peaks did not change, so an overall memory decrease cannot be claimed. No improvement was measured in ordinary execution; the approximately 1.3% difference is not attributed on this basis. CPU frequency was not fixed and all background load was not excluded. This is a comparison on fixed local workloads, not a cross-machine performance guarantee.

Targeted regression validation covered old-checkpoint compatibility, tape ordering on both sides of a negative head, copy isolation, rejection of 60 invalid members, and equivalence of double-stack states before and after across 500 fixed seeds. All 14 items in the complete joint acceptance passed; the original 9 Lean theorems and semantic mutation rejection also passed.

Rerun (from the project root; baseline is trusted local Python code—do not accept files from unknown sources):

```powershell
python universal/optimization_check.py --baseline universal/results/optimization-dc6080e19239/baseline_machine.py --repeats 21
python universal/resilience.py
```
