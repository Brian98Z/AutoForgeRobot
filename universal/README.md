# Universal Computation Validation Component

## Three Distinct Conclusions

- Abstract two-stack interpreter: a constructive proof of Turing-completeness under the assumptions of an arbitrary finite program description, extensible unbounded storage, and no global step limit is provided in `PROOF.md`.
- Actual Python implementation: it has budgets, stopping, and checkpoints; `verify.py` checks its finite consistency with an independent sparse-tape model. It does not claim certification by a proof assistant.
- Original AutoForge: it remains a constrained i64 WASM candidate iterator; this component is not automatically connected to release, does not expand candidate permissions, and does not prove autonomous generation of arbitrary systems.

See `VERDICT.md` for the current assessment and measured counts, `REVIEW.md` for proof-review notes, and `results/batch-summary.json` for itemized evidence from all 12 batches.

New kernel and benchmark: `python universal/kernel_check.py` checks 9 Lean formal-model theorems; `python universal/benchmark.py --repeats 11` runs the correctness hard gate and performance scoring. The former's scope is described in `formal/README.md`, and the latter's rules and measurements in `BENCHMARK.md`; do not broaden model certification into certification of the Python implementation.

Joint boundary tests for the next round: `python universal/resilience.py`, covering long tapes, recovery across 100 segments, concurrent checkpoints, stopping during execution, and Lean/scoring fault injection. Itemized results and limitations are in `RESILIENCE.md`.

Local optimization comparison: `optimization_check.py --baseline <trusted old machine.py> --repeats 21` validates old-checkpoint compatibility, two-stack ordering, and copy isolation, and alternates timings. The frozen baseline, rerun command, and measured gains are in “Next-round local optimization and same-process comparison” in `BENCHMARK.md`.

## Running It

From the AutoForge project directory, run in PowerShell:

```powershell
New-Item -ItemType Directory universal\results\manual
python universal\machine.py run universal\examples\increment.json --input '["1","1","1"]' --steps 3 --cells 100 --output universal\results\manual\paused.json
python universal\machine.py resume universal\examples\increment.json universal\results\manual\paused.json --steps 20 --cells 100 --output universal\results\manual\halted.json
```

The first command returns PAUSED, and the second HALTED. The observed tape contains 1 at position −1 and 0 at positions 0, 1, and 2—that is, binary 111 incremented to 1000. The head can move to a negative position; it is not clamped to 0.

Machine that writes 1 forever to the right:

```powershell
python universal\machine.py run universal\examples\march.json --input '[]' --steps 20 --cells 100 --output universal\results\manual\march.json
```

Its result is PAUSED, not HALTED. From the transition structure, this example can be seen never to enter a halt state; the program does not implement a general halting decision procedure.

Stop: add `--stop-file universal\results\manual\STOP`, then have the operator create a file at that path. It is checked before each transition; when found, the program returns STOPPED. The stop file is not deleted automatically; there is no hardware-level atomic-interrupt guarantee between completing a step and the file appearing. The checkpoint preserves the current position, and execution can explicitly resume after the operator removes the file.

## Budgets and Resumption

- Each CLI command is limited to 1,000,000 transitions and 100,000 storage slots; input and output checkpoint files are limited to 4 MiB.
- `--steps 0` observes only the current termination condition and does not advance a transition.
- HALTED: explicit halt state; STUCK: undefined transition; PAUSED: this step budget is exhausted; RESOURCE_LIMIT: insufficient storage quota; STOPPED: operator stopped execution.
- RESOURCE_LIMIT is returned before an out-of-bounds transition is committed, preserving the prior state.
- The output path must not already exist; old checkpoints are never overwritten. Installation is atomic using a temporary file and a hard link in the same directory. If the filesystem does not support hard links, the operation fails explicitly.
- The checksum binds the machine definition and state and detects accidental changes only; it is not an identity signature. Anyone with write access to the file can recompute the checksum; restore also does not prove that the state is reachable from the initial input.
- Long symbols may hit the checkpoint byte limit before the cell count limit. Before writing, the program rejects files too large to read back; this is not a hard process-RSS quota.
- The Python library API is for tests and trusted callers; it is not a sandbox for malicious host code. The host process outside the CLI remains part of the trusted computing base.

## Verification

```powershell
python universal\verify.py
```

Evidence is written to an independent directory under `universal/results/`; it preserves seeds, counts, results by dimension, hashes of tested files, and actual CLI output. The complete state-machine protocol is in `CONTRACT.md`.

## Extensibility Boundary

The machine's state set, symbol set, and transition table are data; adding a machine does not modify the interpreter. Accordingly, the prototype can validate variable-length inputs, persistent computational state, pause/resume, and general control flow. However, connecting it to AutoForge as a new candidate type still requires an independent target contract, two-lane proposals, and an arbitration interface; this component does not replace the existing safety core by itself.