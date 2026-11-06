# Universal Computation Model and Implementation Contract

This component is independent of the original i64 WASM release pipeline and grants no new candidate permissions.

## Abstract Model

Input consists of an arbitrary finite deterministic single-tape, two-way infinite Turing machine M=(Q,Gamma,blank,start,H,delta) and any finite input string. delta is a finite partial function over non-halt states and symbols, with value (next,write,move), where move is L/R/S. Explicit halt states have no outgoing edges; an undefined transition yields STUCK (finite termination, but not explicit HALTED).

The target interpreter configuration is (state,left,current,right,head,steps), where left/right are finite symbol sequences with the stack top at the end, representing the nearest symbols to the left/right of the head, respectively. All cells beyond the stacks are blank. The abstract semantics imposes no bound on stack length, transition count, or integer head/steps. Each step first writes current, then updates the two stacks and head according to L/R/S; it does not execute user code.

## File Format and Python API (Shared Contract for Implementers/Verifiers)

Machine JSON has strict fields: schema=1, states:[str], symbols:[str], blank:str, start:str, halt:[str], transitions:[{state,read,next,write,move}]. All states/symbols must be declared; duplicate definitions/states/symbols, unknown fields, and outgoing transitions from halt states are rejected.

`machine.py` API:
- compile_machine(raw:dict) -> Program, validates and freezes the definition; Program.raw preserves the canonical structure; Program.blank/start/halt/table/digest are readable.
- initial(program, tape:list[str]) -> dict configuration, with head=steps=0, first input as current, and the remainder reversed into right; empty input uses blank.
- step(program, config:dict) -> dict new configuration (does not mutate the original configuration); halted or missing transition raises ValueError. Each step increments steps by 1.
- observe(program, config) -> {state,head,steps,tape:[[absolute_index,symbol],...]}, containing only nonblank cells, sorted by position.
- run(program,config,step_budget:int,cell_limit:int,stop_path:Path|None=None) -> {status,config,executed}. status is HALTED/STUCK/PAUSED/RESOURCE_LIMIT/STOPPED; HALTED/STUCK take precedence over PAUSED; budget exhaustion returns PAUSED only and is not misreported as non-halting. STOPPED takes precedence over all. If storage would exceed the limit, that step is not committed. cell_limit is the upper bound on left+right+1 storage slots; if the initial state already exceeds it, return RESOURCE_LIMIT; steps is cumulative, executed is for this invocation. step_budget>=0,cell_limit>=1. CLI hard limits: 1,000,000 steps per command, 100,000 storage slots, 4MiB for input and output checkpoint files; trusted library callers supply their own budgets, which do not constitute a host sandbox.
- checkpoint(program,result) -> dict: schema=1,machine_digest,config,status,checksum; checksum is the canonical JSON SHA256 excluding checksum, encoded canonically as UTF-8, sorted keys, compact separators, ensure_ascii=False, and NaN rejected. It detects accidental changes only and is not authentication.
- restore(program,payload) -> config: validates checksum, machine_digest, configuration fields and members, integer head/steps, nonnegative steps, and abs(head)<=steps; head may be negative and is not equal to left-stack length. Must not claim that the checkpoint is necessarily reachable.

CLI:
`python universal/machine.py run MACHINE.json --input '["1","1"]' --steps 100 --cells 1000 --output CHECKPOINT.json`
`python universal/machine.py resume MACHINE.json CHECKPOINT.json --steps 100 --cells 1000 --output NEXT.json`
Optional `--stop-file PATH` returns STOPPED immediately if present; it does not delete the stop file. Output JSON contains status/executed/observation/checkpoint path. Existing output with different content is rejected; an existing ordinary file with identical bytes is treated as idempotent success, while links and reparse targets are rejected. Normal HALTED/STUCK/PAUSED/RESOURCE_LIMIT/STOPPED statuses return 0; format or file errors before commit return 1. First write and fsync a temporary file in the same directory; on Windows publish with no-overwrite MoveFileExW WRITE_THROUGH, and on POSIX publish using a hard link and sync the directory. Publication is the commit point; if temporary-file cleanup or directory-persistence confirmation fails after commit, report a JSON warning on stderr rather than falsely claiming nothing was committed. Power-loss durability is not certified; budget exhaustion and halting are explicitly distinguished.

## Proof and Acceptance Scope

Proof: provide a computable finite encoding for arbitrary M, a decoding map, initial-state preservation, one-step simulation lemmas for all three movement directions, induction, and termination/divergence correspondence; the fixed interpreter's table lookup can be implemented by scanning, copying, and comparing finite strings, without assuming another “universal-machine oracle.” This is a constructive paper proof assuming unbounded extensible storage, not Lean/Coq certification.

Finite verification: stepwise differential comparison against an independent sparse-tape implementation; fixed-seed random machines/inputs; exhaustive small-machine transitions and local windows; known machines (bit flip, unary addition, scan and return, etc.); equality of budget-sliced resume and a single run; negative head, empty tape, blank writes, L/R/S, explicit halt, stuck, infinite-loop pause, memory rejection, stopping, and malformed/tampered checkpoint checks.