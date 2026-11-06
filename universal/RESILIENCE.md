# Next-Round Boundary and Fault-Injection Tests

Run: `python universal/resilience.py`. Outputs a separate `results/resilience-*/report.json`; failure reports are retained, and SHA256 of the original machine.py, Lean proof, and scoring policy is checked before and after the run.

## Acceptance Targets

1. Right-writing tape reaches 100000 slots: the limit is reached after transition 99999; the next expansion transition must not be committed.
2. Run 100 segments of 1000 steps each, with actual JSON serialization/deserialization and checkpoint restore between segments; the final state and cumulative step count must match a single run.
3. CLI resume of a large state must retain the resource limit and must not reset tape capacity on resume.
4. Even when the checksum is recomputed, reject negative steps, a Boolean head, a head beyond history, undeclared states/symbols, and extra fields. This test does not treat the checksum as authentication.
5. Separately validate the 4MiB input-file limit and 4MiB output-checkpoint limit. Use long symbols to create a state exceeding the output-byte limit without exceeding the slot count; it must reject an unrecoverable file.
6. Two real CLI processes submitting the same checkpoint path with the same content may both succeed idempotently; the final file must be restorable. Different content must be rejected without overwrite. Add cases for cleanup warnings after commit, idempotent retry, conflict rejection, and publish/fsync failure before commit.
7. After the child process completes its first transition and emits READY, request stop; record the time from STOP creation until child-process exit. This value includes process shutdown/serialization/scheduling and is not a hard real-time guarantee.
8. Change Lean's source right-shift mapping from right0 to right1 only in an evidence-directory copy; the kernel must reject the genuine semantic error, then the original 9 theorems must pass.
9. In an independent Python copy, change steps +1 to +2; the benchmark correctness gate must fail and score 0. Benchmark on the original implementation should pass.
10. Independently recompute the final weighted score from raw measurements and rules, rather than trusting only the report's score field.

## Evidence and Limitations

- Mutation copies and failure logs are audit evidence; retain them in their corresponding result directories and do not overwrite official files.
- This round is not a 24-hour endurance test, a malicious-OS attack, or a full-stack formal proof.
- Stop uses file polling; no claim is made that an already-running single step can be atomically interrupted.
- The sparse-tape oracle and original 18-dimensional acceptance are rerun as benchmark prerequisite gates; no new random-machine coverage count is claimed here.
- Windows uses MoveFileExW WRITE_THROUGH publication without overwrite; POSIX uses hardlink and syncs the directory. Do not infer behavior on all network filesystems from local results. If Windows symlink creation lacks privileges, explicitly record NOT_EVALUATED.

## Measured Results

Complete report: `results/resilience-b18d97ee9ce5/report.json`, 14/14 items passed, 7.719 seconds. This duration does not represent endurance or long-duration stress validation.

- The 100000-slot boundary rejected the next expansion after 99999 steps; 100-segment JSON resume matched a single run. A 500302-byte large checkpoint was restored through the CLI and still obeyed the limit.
- Two processes contended for the output path, with exit codes 0/1; the successful file was correct and restorable.
- This run's STOP request-to-worker-exit time was 6.647ms; the worker completed 8 steps. The measurement includes scheduling, checks, and process shutdown; it cannot guarantee worst-case timing or establish atomic interruption.
- The actual Lean right-shift semantic mutation was rejected; the original 9 theorems passed.
- The faulty step-count copy failed the benchmark and scored 0; the official 11-sample benchmark scored 100, matching an independent formula recomputation.
- Official machine.py, Simulation.lean, and policy hashes were unchanged.

Median throughput in this round: in-place loop 3096902, rightward growth 1574935, adjacent-cell shuttle 1284456, binary carry 1383867 transitions/second; checkpoint median 4.205ms, restore 4.280ms. Some throughput values are below the previous round but remain above fixed engineering targets, and the passing score remains 100; background load was not controlled, so the variation is not identified as a code performance regression, and targets were not adjusted.

The first run stopped on a report-field conflict (worker status and test status had the same name); after repairing the test script, the full rerun passed. Failure evidence retained at `results/resilience-3ff55bea5e01/report.json`; no defect requiring changes to the official runtime was found.

## Retest After Atomicity Fix

`results/resilience-433443af2889/report.json`: 17 items passed; 1 symlink-creation item was not run due to insufficient Windows privileges. A cleanup error after commit is no longer misrepresented as not committed; retry with identical bytes can succeed, and different content is rejected. The historical report's statement “only one side succeeds” describes pre-fix semantics and remains as originally recorded.

