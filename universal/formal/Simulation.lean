import Init

/-!
# Finite-stack / relative-tape simulation

This file proves a local semantic refinement for the abstract tape update used
by the Python interpreter. It does not formalize JSON, validation, resource
limits, parsing, or the universal-machine encoding in PROOF.md.

Unlike Python's `left` and `right` lists (whose nearest cell is at the end),
these lists put the nearest cell at the HEAD. Decoding therefore uses
`List.getD n` for distance `n`; this corresponds to reading Python's lists from
the end. An empty list reads as blank.
-/

namespace TapeSimulation

inductive Pos where
  | here
  | left (distance : Nat)
  | right (distance : Nat)
deriving DecidableEq, Repr

abbrev Tape := Pos → Nat

structure Source where
  state : Nat
  tape : Tape
  steps : Nat

structure Target where
  state : Nat
  left : List Nat
  current : Nat
  right : List Nat
  steps : Nat

/-- Replace the old head symbol, without depending on target representation. -/
def writeAt (t : Tape) (symbol : Nat) : Tape :=
  fun p => if p = .here then symbol else t p

def fromRight (p : Pos) : Pos :=
  match p with
  | .here => .right 0
  | .left 0 => .here
  | .left (n + 1) => .left n
  | .right n => .right (n + 1)

def fromLeft (p : Pos) : Pos :=
  match p with
  | .here => .left 0
  | .right 0 => .here
  | .right (n + 1) => .right n
  | .left n => .left (n + 1)

/-- Direct source semantics: re-origin the tape after a head move. -/
def sourceWrite (s : Source) (a : Nat) : Source :=
  { s with tape := writeAt s.tape a }

def sourceRight (s : Source) : Source :=
  { s with tape := fun p => s.tape (fromRight p) }

def sourceLeft (s : Source) : Source :=
  { s with tape := fun p => s.tape (fromLeft p) }

def decodeTape (blank : Nat) (x : Target) : Tape := fun p =>
  match p with
  | .here => x.current
  | .left n => x.left.getD n blank
  | .right n => x.right.getD n blank

def decode (blank : Nat) (x : Target) : Source :=
  { state := x.state, tape := decodeTape blank x, steps := x.steps }

inductive Direction where
  | stay
  | goLeft
  | goRight
  deriving DecidableEq, Repr

structure Action where
  nextState : Nat
  writeSymbol : Nat
  direction : Direction
  deriving Repr

def targetApply (blank : Nat) (x : Target) (a : Action) : Target :=
  match a.direction with
  | .stay => { x with state := a.nextState, current := a.writeSymbol, steps := x.steps + 1 }
  | .goRight =>
      match x.right with
      | [] => { state := a.nextState, left := a.writeSymbol :: x.left, current := blank, right := [], steps := x.steps + 1 }
      | c :: rest => { state := a.nextState, left := a.writeSymbol :: x.left, current := c, right := rest, steps := x.steps + 1 }
  | .goLeft =>
      match x.left with
      | [] => { state := a.nextState, left := [], current := blank, right := a.writeSymbol :: x.right, steps := x.steps + 1 }
      | c :: rest => { state := a.nextState, left := rest, current := c, right := a.writeSymbol :: x.right, steps := x.steps + 1 }

def sourceApply (s : Source) (a : Action) : Source :=
  let written := sourceWrite s a.writeSymbol
  let moved := match a.direction with
    | .stay => written
    | .goLeft => sourceLeft written
    | .goRight => sourceRight written
  { moved with state := a.nextState, steps := s.steps + 1 }

/-- Same state, elapsed steps, and pointwise decoded tape. -/
def Related (blank : Nat) (s : Source) (x : Target) : Prop :=
  s.state = x.state ∧ s.steps = x.steps ∧ s.tape = decodeTape blank x

theorem decode_write (blank a : Nat) (x : Target) :
    decodeTape blank (targetApply blank x ⟨x.state, a, .stay⟩) =
      writeAt (decodeTape blank x) a := by
  funext p
  cases p <;> simp [targetApply, decodeTape, writeAt]

theorem decode_right (blank : Nat) (x : Target) (a : Action)
    (h : a.direction = .goRight) :
    decodeTape blank (targetApply blank x a) =
      fun p => (writeAt (decodeTape blank x) a.writeSymbol) (fromRight p) := by
  cases a with
  | mk q w d =>
    cases d <;> simp at h
    cases hx : x.right with
    | nil =>
      funext p
      cases p with
      | here => simp [targetApply, decodeTape, fromRight, writeAt, hx]
      | left n =>
        cases n <;> simp [targetApply, hx, decodeTape, fromRight, writeAt]
      | right n => simp [targetApply, hx, decodeTape, fromRight, writeAt]
    | cons c rest =>
      funext p
      cases p with
      | here => simp [targetApply, decodeTape, fromRight, writeAt, hx]
      | left n =>
        cases n <;> simp [targetApply, hx, decodeTape, fromRight, writeAt]
      | right n => simp [targetApply, hx, decodeTape, fromRight, writeAt]

theorem decode_left (blank : Nat) (x : Target) (a : Action)
    (h : a.direction = .goLeft) :
    decodeTape blank (targetApply blank x a) =
      fun p => (writeAt (decodeTape blank x) a.writeSymbol) (fromLeft p) := by
  cases a with
  | mk q w d =>
    cases d <;> simp at h
    cases hx : x.left with
    | nil =>
      funext p
      cases p with
      | here => simp [targetApply, decodeTape, fromLeft, writeAt, hx]
      | right n =>
        cases n <;> simp [targetApply, hx, decodeTape, fromLeft, writeAt]
      | left n => simp [targetApply, hx, decodeTape, fromLeft, writeAt]
    | cons c rest =>
      funext p
      cases p with
      | here => simp [targetApply, decodeTape, fromLeft, writeAt, hx]
      | right n =>
        cases n <;> simp [targetApply, hx, decodeTape, fromLeft, writeAt]
      | left n => simp [targetApply, hx, decodeTape, fromLeft, writeAt]

theorem decode_stay (blank : Nat) (x : Target) (a : Action)
    (h : a.direction = .stay) :
    decodeTape blank (targetApply blank x a) =
      writeAt (decodeTape blank x) a.writeSymbol := by
  cases a with
  | mk q w d =>
    cases d <;> simp at h
    funext p
    cases p <;> simp [targetApply, decodeTape, writeAt]

theorem one_step (blank : Nat) (s : Source) (x : Target) (a : Action)
    (hr : Related blank s x) :
    Related blank (sourceApply s a) (targetApply blank x a) := by
  rcases hr with ⟨hs, hn, ht⟩
  constructor
  · cases a with
    | mk q w d =>
      cases d <;> cases hl : x.left <;> cases hr : x.right <;>
        simp [sourceApply, targetApply, hl, hr]
  constructor
  · cases a with
    | mk q w d =>
      cases d <;> cases hl : x.left <;> cases hr : x.right <;>
        simp [sourceApply, targetApply, hl, hr, hn]
  cases a with
  | mk q w d =>
    cases d with
    | stay =>
      funext p
      cases p <;> simp [sourceApply, sourceWrite, targetApply, decodeTape, writeAt, ht]
    | goRight =>
      have hm := decode_right blank x ⟨q, w, .goRight⟩ rfl
      simp only [sourceApply, sourceWrite, sourceRight] at *
      rw [hm]
      funext p
      simp [writeAt, ht]
    | goLeft =>
      have hm := decode_left blank x ⟨q, w, .goLeft⟩ rfl
      simp only [sourceApply, sourceWrite, sourceLeft] at *
      rw [hm]
      funext p
      simp [writeAt, ht]

abbrev Transition := Nat → Nat → Option Action

def sourceStep (δ : Transition) (s : Source) : Option Source :=
  match δ s.state (s.tape .here) with
  | none => none
  | some a => some (sourceApply s a)

def targetStep (blank : Nat) (δ : Transition) (x : Target) : Option Target :=
  match δ x.state x.current with
  | none => none
  | some a => some (targetApply blank x a)

def StepRelated (blank : Nat) (ss : Option Source) (ts : Option Target) : Prop :=
  match ss, ts with
  | none, none => True
  | some s, some t => Related blank s t
  | _, _ => False

theorem step_simulation (blank : Nat) (δ : Transition) (s : Source) (x : Target)
    (hr : Related blank s x) :
    StepRelated blank (sourceStep δ s) (targetStep blank δ x) := by
  have choice : δ s.state (s.tape .here) = δ x.state x.current := by
    rw [hr.1, hr.2.2]
    rfl
  cases h : δ x.state x.current with
  | none => simp [sourceStep, targetStep, choice, h, StepRelated]
  | some a =>
    simp only [sourceStep, targetStep, choice, h, StepRelated]
    exact one_step blank s x a hr

/-- Each machine runs independently for a bounded number of transitions. -/
def runSource (δ : Transition) : Nat → Source → Source
  | 0, s => s
  | fuel + 1, s => match sourceStep δ s with
    | none => s
    | some s' => runSource δ fuel s'

def runTarget (blank : Nat) (δ : Transition) : Nat → Target → Target
  | 0, x => x
  | fuel + 1, x => match targetStep blank δ x with
    | none => x
    | some x' => runTarget blank δ fuel x'

theorem fuel_simulation (blank : Nat) (δ : Transition) (fuel : Nat)
    (s : Source) (x : Target) (hr : Related blank s x) :
    Related blank (runSource δ fuel s) (runTarget blank δ fuel x) := by
  induction fuel generalizing s x with
  | zero => exact hr
  | succ fuel ih =>
    cases hs : sourceStep δ s with
    | none =>
      have ht : targetStep blank δ x = none := by
        cases ht : targetStep blank δ x with
        | none => rfl
        | some t =>
          have hrel := step_simulation blank δ s x hr
          simp [hs, ht, StepRelated] at hrel
      simp [runSource, runTarget, hs, ht]
      exact hr
    | some s' =>
      cases ht : targetStep blank δ x with
      | none =>
        have hrel := step_simulation blank δ s x hr
        simp [hs, ht, StepRelated] at hrel
      | some x' =>
        have hrel := step_simulation blank δ s x hr
        simp [hs, ht, StepRelated] at hrel
        simpa [runSource, runTarget, hs, ht] using ih s' x' hrel


/-- The empty-stack representation of a nonempty input has its first symbol at the head. -/
def initialTarget (blank : Nat) (q : Nat) (input : List Nat) : Target :=
  match input with
  | [] => { state := q, left := [], current := blank, right := [], steps := 0 }
  | c :: rest => { state := q, left := [], current := c, right := rest, steps := 0 }

def initialSource (blank : Nat) (q : Nat) (input : List Nat) : Source :=
  { state := q
    tape := fun p => match p with
      | .here => input.headD blank
      | .left _ => blank
      | .right n => input.drop 1 |>.getD n blank
    steps := 0 }

theorem initial_related (blank q : Nat) (input : List Nat) :
    Related blank (initialSource blank q input) (initialTarget blank q input) := by
  constructor
  · cases input <;> rfl
  constructor
  · cases input <;> rfl
  · funext p
    cases input with
    | nil => cases p <;> simp [initialSource, initialTarget, decodeTape]
    | cons c rest =>
      cases p with
      | here => rfl
      | left n => simp [initialSource, initialTarget, decodeTape]
      | right n => simp [initialSource, initialTarget, decodeTape]

theorem input_simulation (blank q : Nat) (input : List Nat)
    (δ : Transition) (fuel : Nat) :
    Related blank
      (runSource δ fuel (initialSource blank q input))
      (runTarget blank δ fuel (initialTarget blank q input)) := by
  exact fuel_simulation blank δ fuel _ _ (initial_related blank q input)

#print axioms decode_write
#print axioms decode_left
#print axioms decode_right
#print axioms decode_stay
#print axioms one_step
#print axioms step_simulation
#print axioms initial_related
#print axioms fuel_simulation
#print axioms input_simulation

end TapeSimulation
