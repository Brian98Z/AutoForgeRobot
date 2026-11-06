# Lean Proof-Kernel Check

Run: `python universal/kernel_check.py`.

Toolchain pinned to Lean4.35.0; SHA256 of the official distribution archive is verified, with provenance and hash in toolchain.json. Uses only Lean's built-in library and does not depend on downloading mathlib.

## Formalized Objects

Simulation.lean defines independent:
- Source: an infinite function tape with the head as origin; positions are here/left n/right n.
- Target: finite left/right lists and the current cell; the list head is the nearest symbol.
- Source-tape write and movement: directly relocates the position function, without calling the target executor.
- Target executor: list push/pop, with an empty list reading blank.
- Independent transition lookup and bounded execution on both sides; relation preservation is proved by induction.

Python's list end is the nearest symbol, while Lean's list head is the nearest symbol; the two correspond by reverse. What is proved here is the relation in the Lean model, not formal verification of Python list reverse, JSON parsing, checkpoint, resource policy, or OS execution. The relative-coordinate model does not include Python's absolute-head metadata.

## Verdict Requirements

- Lean exits normally, with warningAsError enabled.
- No sorry/admit, custom axiom, unsafe, or native_decide bypasses.
- Print final theorem dependencies; sorryAx must not appear.
- The false proposition (0:Nat)=1 must be rejected by the same checker.
- Save complete stdout/stderr, source/binary SHA256, and compilation artifacts.

Lean standard-logic axioms such as propext and Quot.sound may arise from function extensionality and simp; list them accurately and do not call the proof entirely axiom-free.

Computability of the entire universal-machine encoding, the final universality claim for unbounded simulation, and Python refinement remain respectively within the constructive argument in PROOF.md and the scope of finite measurements; passing local/finite-step model theorems must not be described as full-stack certification.

## This Kernel Run

Lean4.35.0 passed 9 theorems: decode_write, decode_left, decode_right, decode_stay, one_step, step_simulation, initial_related, fuel_simulation, input_simulation. The final theorem quantifies over arbitrary Nat states/symbols, arbitrary finite input, arbitrary transition function δ, and arbitrary natural-number fuel; it does not check only a few fixed-step examples.

Source and target each look up δ using their own state and read symbol, then independently run runSource/runTarget, rather than manually selecting the same branch in sync. The simulation theorem's arbitrary transition function does not prove arbitrary δ computable; validity of finite-table encoding remains in PROOF.md's constructive argument.

Evidence: `../results/kernel-887c58d426db/report.json` (relative to this directory). All dependencies are the standard propext and Quot.sound, with no sorryAx. The intentionally false proposition was rejected; the official proof source prohibits sorry/admit/custom axiom/unsafe/native_decide bypasses. The toolchain archive SHA256 matches the GitHub official asset digest.
