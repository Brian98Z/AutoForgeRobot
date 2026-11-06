# Multidimensional Bootstrap and Extensibility Validation

## Criteria for Interpreting the Conclusions

This report distinguishes three objects to avoid conflating passing tests with a proof of Turing-completeness:

1. **Execution language**: WebAssembly has conditional branches, loops, recursive calls, memory reads and writes, and indirect calls. The instructions actually available remain subject to the current wasmi support range; the latest specification is not substituted for local measurements.
2. **Current deployment**: Each execution has only 100,000 fuel, a 128 KiB linear-memory limit, two i64 inputs, and one i64 output. Every run is truncated by a budget; the input space, available state, and number of execution steps are bounded, so strict Turing-completeness cannot be claimed under an unbounded computation model. Removing fuel would not automatically resolve the finite-memory and interface constraints either.
3. **Autonomous proposer**: By default this is a finite expression search; it does not automatically invent loop algorithms, extend its syntax, recompile itself, or rewrite the acceptance core. The 12 rounds are continuous evolution of the candidate system, not compiler self-hosting or recursive self-improvement.

Finite tests cannot prove correctness for arbitrary programs, arbitrary new targets, or future extensions. We verify observable behavior and failure boundaries; we make no unbounded proof or long-term stability guarantee.

Reference: the official WebAssembly execution semantics at https://webassembly.github.io/spec/core/exec/instructions.html, where loop, if, br, call, and others have independent rules. For local project limits, see `src/sandbox.rs` and `src/main.rs`.

## Test Composition

### A. Continuous Target Evolution

`python bootstrap.py` generates state, tasks, command logs, and reports in an independent `bootstrap-runs/` directory; it does not modify the existing `state/`.

Each round must satisfy:

- Use actual Rust executables and Python and Julia processes; do not fabricate proposer or signature evidence.
- Have at least 12 public samples and 12 held-out samples, with fixed seeds; cover i64 extremes and wraparound where applicable.
- Both lanes pass in the same round, and the previous-version pointer is traceable; for different targets, revise the old candidate after it fails.
- Execute new inputs not included in the contract and compare against an independent reference result.
- Replay historical versions and retain failure logs; report candidate-search rounds separately from target-evolution rounds.

Additional negative scenarios validate regression vetoes, held-out-sample vetoes, rejection of contradictory specifications, rejection of unsupported XOR targets, rollback, stop/resume, and tamper detection.

### B. Execution Capabilities Beyond the Current Proposer Syntax

`cargo test --locked` in `tests/computation.rs` directly uses the real sandbox to compile and run hand-written WAT:

- Euclid GCD: loops, conditional branches, remainder.
- Recursive factorial: base condition, call stack, return-value composition.
- Memory reads and writes and state isolation across calls.
- Function tables and indirect calls.
- Small bytecode interpreter: fetch, decode, state update, conditional backward branch, halt.
- Function composition, 64-bit wraparound, and traps.
- Division by zero, out-of-bounds access, growth limit exceeded, ABI mismatch.

These programs are computational witnesses written by the verifier. They do not count as autonomous generation capability and do not constitute proof of universal Turing-machine simulation.

### C. Extension Experiment Without Recompiling the Controller

`python extension_test.py` uses the existing `AUTOFORGE_PROJECT` protocol to switch to a proposer in a separate test directory:

- Independent Python and Julia implementations search for bitwise-operation candidates; after extension, XOR—which the original proposer cannot express—can be promoted and executed correctly.
- The controller binary SHA-256 must remain the same; the new proposer hash must be included in signed provenance.
- Recompile the connected artifact and replay acceptance.
- A missing proposer and an ABI mismatch must be rejected; they must not degrade to single-lane release.

The extension proposer is written and installed by an operator; the prototype does not create it itself. The experiment proves only that **the proposer strategy can be replaced within the same i64 ABI**; it does not prove that HTTP, databases, persistent state, multimodal inputs, or arbitrary language builders can be connected directly.

## Extensibility Thresholds

|Dimension|Currently verifiable boundary|Not yet guaranteed|
|---|---|---|
|Proposer strategy replacement|Same stdin/stdout protocol, independent two-lane proposals, hash binding|Automatic discovery/installation of arbitrary plugins|
|Computational expression|WASM branches, loops, recursion, memory, interpreter|Unbounded storage and unbounded execution|
|Target evolution|Continuous iteration across multiple targets with a fixed ABI|Automatic formalization of arbitrary natural-language targets|
|Domain input/output|Two i64 inputs, one i64 output|Variable-length documents, streaming input, persistent state|
|Acceptance|Samples, held-out set, historical regression, replay|Specification completeness and correctness for all future inputs|
|Security|Generated WASM has no imports and has resource budgets|Host-process isolation, hardware stop, protection against administrator tampering|
|Autonomous scope|Revision of candidate data plane|Self-modification and self-approval of the control plane, compiler, and acceptance system|

When expanding capabilities, add an explicitly versioned domain contract, isolated executor, independent oracle, data migration, and rollback. Do not remove existing resource and termination limits for the sake of “Turing-completeness.”

## Findings and Fixes

The first run of the 12-round suite stopped at the addition target: Python passed, Julia rejected, and the old version was not replaced. Retained evidence: `bootstrap-runs/20261030-115711-084e6e9e/report.json` and the corresponding ledger.

Direct diagnosis showed that the current JSON3 1.14.4 / Parsers 2.8.10 / Julia 1.13.3 combination decodes `-9223372036854775807` as `9223372036854775807`; both ordinary generic parsing and explicit Int64 structure parsing are affected. This does not establish that all versions have this defect.

The fix uses JSON3's RawType hook to preserve the original token, then performs strict decimal conversion with Julia `parse(Int64, token)`; it does not modify dependency source, special-case a particular number, or remove boundary cases. Inputs, expected values, public feedback, and protocol integer fields in all cases use exact decoding; overflow, decimals, scientific notation, and numeric strings are rejected. Added regression cases in `tests/protocol.jl`.

The test code's own incorrect assumption was also corrected: a missing proposer file returns an error during the provenance precheck rather than entering HOLD; the division-by-zero acceptance check examines the runtime trap and a valid-division comparison, without binding to third-party error wording. Failure reports are retained and are not counted among completed rounds.

## Measured Results (2026-10-30)

Main report: `bootstrap-runs/20261030-120505-144b067b/report.json`. Total elapsed time was 57.235 seconds, including 12 rounds of target evolution and additional negative scenarios; this is not a long-duration stability benchmark. Each target had 26 public + 24 held-out samples and additionally ran 5 new inputs not in the contract. In total there were 600 contract samples and 60 new-input probes; the two lanes and replay execute cases repeatedly, so repetitions must not be counted as independent samples.

|Round|Target|Two-lane proposal rounds|Result|
|---|---|---:|---|
|1|Wrapping addition|1|PROMOTE|
|2|Wrapping subtraction|2|PROMOTE|
|3|Reverse subtraction|2|PROMOTE|
|4|Wrapping multiplication|2|PROMOTE|
|5|Signed minimum|2|PROMOTE|
|6|Signed maximum|2|PROMOTE|
|7|Absolute value of wrapped difference|2|PROMOTE|
|8|3x−2y+7|2|PROMOTE|
|9|−4x+5y−9|2|PROMOTE|
|10|Constant −17|2|PROMOTE|
|11|Clamp x to [−7,11]|2|PROMOTE|
|12|Clamp y to [−13,4]|2|PROMOTE|

There were 23 cumulative two-lane proposal rounds, or 46 independent proposer invocations. In rounds 2–12 both lanes first rejected the old candidate, then revised and passed; this behavior was triggered by the offline proposer's first-round retained-baseline strategy and does not mean it independently discovered a higher-level requirement.

Additional scenarios: same-ID historical regression and held-out-set errors both resulted in HOLD; contradictory targets were REJECTed; XOR unsupported by the original syntax correctly resulted in HOLD; actual outputs after two rollbacks were correct; kill/resume, rejection of artifact tampering and recovery passed; replay of the final 12 signed versions passed.

Extension report: `bootstrap-runs/extension-55276ae7dfb0/report.json`, 6 checks passed; the actual XOR generalization output was correct, and the controller binary did not change as a result of connecting the extension.

Julia protocol: all 40/40 assertions in `tests/protocol.jl` passed. Rust: 23 test executions passed, of which 6 old sandbox tests were executed again in the integration module. Thus there were **17 distinct tests (6 existing + 11 new)**, not 23 independent validations.

**Conclusion: constrained multi-target evolution passed; same-ABI proposer-strategy extension passed; general control-flow execution capability is witnessed. Strict unbounded Turing-completeness does not hold; arbitrary system generation and autonomous syntax extension are not implemented; future high extensibility is confirmed only within the tested interfaces.**

All 15 scenarios in the repaired original `acceptance.py` passed re-verification (28.78 seconds); replay of the two historical versions in the old demo `state/` passed. Tests created only independent state and did not upgrade or overwrite the demo versions.