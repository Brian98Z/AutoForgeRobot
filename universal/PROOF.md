# Universality of the Two-Stack Interpreter: Constructive Proof and Implementation Boundary

## 0. Exact Claim and Proof Level

**Claim U**: Under abstract semantics where the program description may have any finite length, the two stacks may grow by any finite amount, and there is no global step limit, the fixed interpretation algorithm A defined below can simulate the computation of any deterministic two-way single-tape Turing machine on any finite input, preserving step-by-step configurations, output, and termination/divergence behavior. Therefore, this abstract computational model is Turing-complete in the standard sense.

This document gives a mathematical construction and an inductive proof; it does not infer U from the number of tests. The subsequent `formal/Simulation.lean` has been checked by the Lean 4.35.0 kernel. What passes is writing, movement, independent one-step simulation, initial configuration, and simulation for any finite number of steps (including a composition theorem starting from the input) for a relative-position tape model. It is not end-to-end certification of all of U in this document. Formalization of complete encoding universality, Python, serialization, and operating-system refinement remains incomplete; finite differential tests can provide evidence for the actual implementation only.

**Claims not made**: The actual Python process, with a 100,000-slot storage limit and a 1,000,000-step limit per command, or the original fixed-i64 WASM automatic-release system, can perform arbitrary unbounded computation. The added component also does not prove that the original proposer can autonomously invent arbitrary algorithms.

## 1. Source Machine

Let M=(Q,Gamma,b,q0,H,delta): Q and Gamma are finite nonempty sets, b belongs to Gamma, q0 belongs to Q, H is a subset of Q; delta is a partial function from (Q\\H)×Gamma to Q×Gamma×{L,R,S}.

A source configuration is C=(q,T,h,n), where T:Z→Gamma is nonblank at only finitely many positions, h belongs to Z, and n belongs to the natural numbers. Input w=w0…wk−1 is placed at positions 0…k−1, with all other positions b; h=n=0.

When q belongs to H, the status is HALTED. When q does not belong to H and delta(q,T(h)) is undefined, the status is STUCK. Neither has a successor, but the implementation uses distinct labels. Otherwise let delta(q,T(h))=(q',a,d); first write T'(h)=a, with all other positions unchanged, then update h'=h−1/h+1/h according to d, and n'=n+1.

This model with explicit halt states, a partial function, and L/R/S includes conventional LR Turing machines. To use a unified halting convention, add, for each undefined non-halt state/symbol pair, an edge that writes back the original symbol, moves S, and enters a new halt state; this adds only a computable finite description and at most one step. If the standard model disallows outgoing edges from halt states, retain the same convention directly.

## 2. Computable Encoding; The Machine Is Not Hidden in the Interpreter

Assign each member of Q and Gamma a nonnegative number in declaration order. For a natural number i define code(i)=1^i0. Use the fixed finite alphabet {0,1,#,;}. The machine encoding consists of:

- The number of states, number of symbols, blank index, and start index;
- The list of halt indices;
- The transition list, with five natural numbers per row: state,read,next,write,move, where L/R/S are numbered 0/1/2.
- The input as a list of symbol indices.

Separate fields with # and records with ;. Prefix each list with its length, followed by exactly that many elements/records. The numeric codes contain only 0/1, not separators, and 0 terminates each unary number, so empty lists, index zero, and adjacent records can all be uniquely decoded. Given any valid finite M,w, the encoding algorithm enumerates a finite number of items and outputs the string above, so it necessarily terminates. The encoding length may grow with M,w and has no global upper bound. JSON files are only a human-readable outer format and do not constrain the mathematical encoding.

The fixed interpretation algorithm A has only this loop: read the current state and symbol index, scan the transition records for a match, copy the next/write/move fields, update the two stacks, and repeat. **A's code does not change with M; all differences in M are only in finite input data.** Hash tables are a concrete implementation optimization, not an oracle in the proof; abstract lookup can scan the finite records sequentially, so every lookup terminates in finite time.

## 3. Target Configuration and Decoding Map

A's configuration is X=(q,L,c,R,h,n). L and R are finite sequences over Gamma, with the last element as stack top; let |L|=l and |R|=r, and let array indices start at 0.

Decode D(X)=(q,T,h,n):

- T(h)=c;
- For 1≤j≤l, T(h−j)=L[l−j];
- For 1≤j≤r, T(h+j)=R[r−j];
- All other T(i)=b.

Extra blank symbols at the bottoms of stacks do not affect decoding, so the representation need not be unique. h may be negative and has no fixed equality with |L|; the initial invariant guarantees only |h|≤n.

Any source configuration with finite support can be represented: choose a finite interval containing the head and all nonblank cells, put the cells to the left of the head in L in order from farthest to nearest, and put the cells to the right in R in reverse order from farthest to nearest. Thus this representation omits no source configuration reachable at any finite time.

## 4. Fixed Update Rules

Let L·a denote pushing a onto the end of L; pop(empty) returns (empty,b). After looking up delta(q,c)=(q',a,d):

- **S**: X'=(q',L,a,R,h,n+1).
- **R**: Let pop(R)=(R0,c0); X'=(q',L·a,c0,R0,h+1,n+1).
- **L**: Let pop(L)=(L0,c0); X'=(q',L0,c0,R·a,h−1,n+1).

HALTED/STUCK have no update rule. There is no model call, user Python invocation, external file access, or step that “guesses the next state.”

## 5. Initial-Preservation Lemma

For empty input: L=R=[], c=b, h=n=0, so the entire tape decoded by D(X0) is blank, as required.

For nonempty input: L=[], c=w0, R=[wk−1,…,w1], h=n=0. For j=1…k−1, R[(k−1)−j]=wj, so decoding gives exactly the source input tape; every other cell is blank. The state is q0. Therefore D(X0)=C0.

## 6. One-Step Simulation Lemma

Assume D(X)=C and the source has a transition. The states and symbols under the head match, so lookup obtains the same delta(q,c)=(q',a,d).

### S

The head position is unchanged, and the target only replaces c with a; both stacks remain unchanged, so exactly T(h)=a is written. The state and step count also match the source.

### R (right stack nonempty)

The original R=R0·c0, and the new head h+1 scans c0, which is the original T(h+1). New L=L·a: the cell at distance 1 from the new head is the old h, with value a; each cell at distance j≥2 on the left corresponds to the cell at distance j−1 in the old left stack, with its value unchanged. The new R0 makes each cell at distance j to the new head's right correspond to the cell at distance j+1 to the old head's right. All other cells are blank. Therefore D(X') is exactly the source configuration after writing and moving right.

### R (right stack empty)

By the decoding definition, all cells to the right of the old head are blank; pop returns b, so the new head reads b. L·a stores the old head cell after the write, and all other position correspondences are as above. The claim follows.

### L

If L=L0·c0, the new head h−1 reads c0=old T(h−1). New R=R·a: the cell at distance 1 to the right of the new head is the old h, now holding a; each cell at distance j≥2 to the right corresponds to the old head's cell at distance j−1, unchanged. Each cell at distance j to the left in the new L0 corresponds to the old head's cell at distance j+1 to the left, unchanged. If L is empty, all cells to the old head's left were blank and pop returns b; the other correspondences remain unchanged. Thus decoding gives exactly the source configuration after writing and moving left.

In every branch q'=the source next state, h'=the source next position, and n'=n+1. Therefore **D(step_A(X))=step_M(D(X))**.

The converse also holds: A can update only after finding a source transition, and applies only this rule; there is no extra target computation step that could be mistaken for a source transition. “One step of A” here means an interpreter macro-step; it does not claim that its underlying runtime equals one atomic Turing-machine step.

## 7. Finite-Prefix Induction and Termination/Divergence

By the initial lemma and one-step lemma, for every natural number n, if the source executes n steps then the target can execute n macro-steps and D(Xn)=Cn. The reverse simulation rules out spurious additional successors.

If the source enters H at step n, the target state also enters H, and both are HALTED. If the source has no transition at step n, target lookup also fails, and both are STUCK. The observed state, head, and nonblank output tape are exactly equal.

If the source runs forever, the target can simulate every finite prefix; each lookup and stack operation is itself finite, and the abstract model has no total budget limit, so the target cannot stop spontaneously at any prefix. Conversely, infinitely many target macro-steps yield infinitely many source steps. This argument **does not provide an algorithm to decide whether arbitrary M halts**.

## 8. The Interpreter Has No Hidden Hypercomputational Operation

To rule out circularly proving universality by “using universal capability,” A's storage representation can be a finite string over a fixed alphabet: the program table, current index, left-stack symbol-index sequence, current symbol, right-stack sequence, and separators.

The required finite program consists only of these mechanical operations:

1. Scan to a separator to locate a field; copy/compare unary encodings in order, with equality iff both strings reach 0 together, otherwise unequal;
2. Search the table sequentially, copying its output fields when a matching record is found; every scan is over a finite table;
3. Push by writing a finite encoding at the end of the stack region; pop by scanning backward to the preceding separator and extracting the last item; test an empty stack by checking adjacent separators;
4. If the contiguous storage region must grow, move the finite suffix from right to left, then insert the new field; deletion works by shifting left similarly;
5. Control flow depends only on finite characters, match success/failure flags, L/R/S, and finite procedure state; temporary markers can be selected from a fixed extended alphabet.

All these scans, marking operations, and moves have fixed finite control and terminate for every finite operand; no real-number oracle, uncomputable function, or arbitrary function call is needed. Thus A can be implemented as an ordinary single-tape Turing machine (though it may be very slow). Together with Section 7, A simulates at least all source Turing machines and does not exceed Turing-computable power; therefore the abstract model is Turing-complete.

## 9. Resource Budgets and Resumption: The Implementation Does Prefixes, Not Counterexamples

The actual `run` has step and storage quotas. If it returns PAUSED after completing k steps, the returned configuration should be Xk; resuming from the same machine description continues as in Section 6, and segmented execution is equivalent to one run for the same total number of steps. The checkpoint hash checks whether data changed; it is **not a signature and does not prove the state is reachable**.

RESOURCE_LIMIT returns the original configuration before committing a transition that would exceed the limit; STOPPED executes no new transition. HALTED/STUCK labels are not conflated with budget exhaustion. The memory limit remains in effect across multiple resumes; allowing resumption does not justify claiming that the actual program is unbounded.

For any given finite source execution prefix, the tape interval and number of steps it uses are finite; the prefix can be completed in an abstract implementation with sufficient resources. The current deployment's hard limits cover only a subset. The quantifier “for every prefix there exist sufficient resources” is not equivalent to “there exists a machine with fixed resources that can run all prefixes.”

## 10. Proof Obligation Status

|Obligation|Basis in this document|Level|
|---|---|---|
|Any finite M,w can be encoded; interpreter is fixed|Section 2|Constructive argument|
|Configuration representation covers all finite-support configurations|Section 3|Constructive argument|
|Initial-state preservation|Section 5|Mathematical proof|
|L/R/S one-step simulation in both directions|Section 6|Case-based mathematical proof|
|Correspondence of all finite prefixes and termination/divergence|Section 7|Induction and extension argument|
|Basic operations are valid, with no hidden oracle|Section 8|Fixed-alphabet implementation construction|
|Python implementation fully refines abstract semantics|Differential tests and source review|Finite empirical evidence, not formal proof|
|Proof-assistant kernel certification|9 theorems in formal/Simulation.lean; relative-position source tape and finite-list target tape|Lean 4.35.0 passed; depends on propext, Quot.sound; no sorryAx; does not include complete U or Python refinement|
|Original AutoForge autonomously generates arbitrary systems|Outside the scope of U|Not implemented, not verified|

## Sources

- Stanford Encyclopedia of Philosophy, *Turing Machines*, revised 2025-05-21: https://plato.stanford.edu/entries/turing-machine/ . Section 1 gives the assumptions of finite control, a two-way infinite tape, and unbounded time/storage; Section 2 discusses universal machines. Used to establish the source model and theory/practice boundary, not as proof that this project's code has been verified.
- Local implementation contract: `CONTRACT.md`; local consistency evidence is produced by `verify.py`. The proof does not depend on whether randomized tests pass.