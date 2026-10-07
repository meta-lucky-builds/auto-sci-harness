/-
chh-lean-check: the comparator and axiom audit of the custom-harness `lean` adapter (GOAL §4.7, tier T0).

    chh-lean-check CHALLENGE_MODULE SOLUTION_MODULE THEOREM NONCE

prints exactly one line on success:

    CHH-LEAN-RESULT <NONCE> {"axioms":[..],"kind":"theorem","problems":[..],"replayed":N,
                             "statement_match":true,"theorem":"<THEOREM>"}

What it does (environment level, never by reading Lean source text, so notation, macros, `local instance`s or
`#exit` in the proof cannot change what is compared):
  1. imports the HUMAN-APPROVED challenge module (the statement, proved by `sorry`) and the solution module;
  2. refuses solution declarations that are `unsafe` or that mention `Lean.ofReduceBool`, `Lean.reduceBool`,
     `Lean.trustCompiler` (native_decide) or `sorryAx`, BEFORE anything is replayed (the kernel would otherwise run
     compiled code for `Lean.reduceBool`);
  3. re-checks every declaration of the solution module with the Lean kernel (`Environment.replay`, as
     lean4checker does): an .olean written by compile-time code, or produced with `debug.skipKernelTC`, cannot
     smuggle in an unchecked declaration;
  4. compares the theorem's type in the challenge and in the replayed solution (`Expr` equality, universe
     parameters too), and every constant the statement depends on (transitively through types, definition bodies
     and inductive constructors), so a renamed, weakened or re-defined statement is a mismatch;
  5. collects the axioms the replayed theorem depends on (`Lean.CollectAxioms`).
The verdict (allowlist, nonce, toolchain) is decided in Python by custom_harness.kernel.formal, from this output.

Loading modules does NOT run their `initialize` declarations (initializer execution is never enabled here).

UNVERIFIED: written against the documented Lean 4 API (target: the version pinned in custom_harness/kernel/formal.py)
without a Lean toolchain; it has never been compiled. Changing this file changes its sha256, which is pinned in
formal.CHECKER_SHA256 (part of the adapter's content hash), so any change needs a new human approval.
-/
import Lean
open Lean

namespace ChhLeanCheck

/-- Constants that mean "the kernel trusted compiled code" (native_decide) or "an unproved goal". -/
def forbiddenConstants : List Name :=
  [`Lean.ofReduceBool, `Lean.reduceBool, `Lean.trustCompiler, `sorryAx]

def kindOf : ConstantInfo → String
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "definition"
  | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quot"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"

def isTheorem (ci : ConstantInfo) : Bool := kindOf ci == "theorem"

/-- Constants mentioned by a declaration's type and (unless it is a theorem: proofs are irrelevant) its body, and
an inductive type's constructors. -/
def dependencies (ci : ConstantInfo) : Array Name := Id.run do
  let mut deps := ci.type.getUsedConstants
  if !isTheorem ci then
    if let some v := ci.value? then
      deps := deps ++ v.getUsedConstants
  if let .inductInfo info := ci then
    deps := deps ++ info.ctors.toArray
  return deps

/-- Same declaration: kind, universe parameters, type, and body (theorems: the type only). -/
def sameDecl (a b : ConstantInfo) : Bool :=
  kindOf a == kindOf b && a.levelParams == b.levelParams && a.type == b.type && a.isUnsafe == b.isUnsafe &&
  (match a, b with
   | .thmInfo _, .thmInfo _ => true
   | .inductInfo x, .inductInfo y =>
       x.ctors == y.ctors && x.numParams == y.numParams && x.numIndices == y.numIndices && x.isRec == y.isRec
   | _, _ => a.value? == b.value?)

/-- The first constant reachable from `roots` whose declaration differs between the two environments. -/
def depsMismatch (a b : Environment) (roots : Array Name) (bound : Nat := 5000000) : Option String := Id.run do
  let mut todo := roots.toList
  let mut seen : NameSet := {}
  for _ in [0:bound] do
    match todo with
    | [] => return none
    | n :: rest =>
      todo := rest
      if seen.contains n then
        continue
      seen := seen.insert n
      match a.find? n, b.find? n with
      | some ca, some cb =>
        if !(sameDecl ca cb) then
          return some s!"{n} differs between the approved statement's and the proof's environment"
        todo := (dependencies ca).toList ++ todo
      | some _, none => return some s!"{n} is not defined in the proof's environment"
      | none, _ => return some s!"{n} is not defined in the approved statement's environment"
  return some "the dependency walk exceeded its bound"

/-- The constants and direct imports of one loaded module. -/
def moduleOf (env : Environment) (mod : Name) : IO (Array ConstantInfo × Array Import) := do
  let pairs := env.header.moduleNames.zip env.header.moduleData
  match pairs.find? (fun p => p.1 == mod) with
  | some (_, md) => return (md.constants, md.imports)
  | none => throw <| IO.userError s!"module {mod} is not loaded"

def run (challenge solution thm : Name) : IO Json := do
  let mut problems : Array String := #[]
  let chalEnv ← importModules #[{ module := challenge }] {} (trustLevel := 0)
  let solEnv ← importModules #[{ module := solution }] {} (trustLevel := 0)
  let (solConsts, solImports) ← moduleOf solEnv solution
  if solImports.any (fun i => i.module == challenge) then
    problems := problems.push s!"the proof imports the challenge module {challenge}"
  -- (2) refuse before replay: replaying `Lean.reduceBool` would run compiled code inside the kernel
  for ci in solConsts do
    if ci.isUnsafe then
      problems := problems.push s!"{ci.name} is unsafe"
    let used := ci.type.getUsedConstants ++ ((ci.value? (allowOpaque := true)).map Expr.getUsedConstants |>.getD #[])
    for f in forbiddenConstants do
      if used.contains f then
        problems := problems.push s!"{ci.name} references {f}"
  if !problems.isEmpty then
    return Json.mkObj [("theorem", toJson (thm.toString (escape := false))), ("kind", toJson "unchecked"),
      ("statement_match", toJson false), ("axioms", toJson ([] : List String)), ("replayed", toJson (0 : Nat)),
      ("problems", toJson problems)]
  -- (3) re-check every solution declaration with the kernel, on top of the solution's own imports
  let base ← importModules solImports {} (trustLevel := 0)
  let mut newConsts : Std.HashMap Name ConstantInfo := {}
  for ci in solConsts do
    newConsts := newConsts.insert ci.name ci
  let replayed ← Lean.Environment.replay newConsts base
  -- (4) statement fidelity
  let chalCi? := chalEnv.find? thm
  let solCi? := replayed.find? thm
  let mut kind := "missing"
  let mut statementMatch := false
  match chalCi?, solCi? with
  | some c, some s =>
    kind := kindOf s
    let typeOk := c.type == s.type && c.levelParams == s.levelParams
    if !typeOk then
      problems := problems.push s!"the type of {thm} differs from the approved statement"
    match depsMismatch chalEnv replayed c.type.getUsedConstants with
    | some why => problems := problems.push why
    | none => pure ()
    statementMatch := typeOk && problems.isEmpty
  | none, _ => problems := problems.push s!"{thm} is not declared by the approved statement"
  | _, none => problems := problems.push s!"{thm} is not declared by the proof"
  -- (5) axiom audit of the replayed theorem
  let (_, st) := ((CollectAxioms.collect thm).run replayed).run {}
  return Json.mkObj [("theorem", toJson (thm.toString (escape := false))), ("kind", toJson kind),
    ("statement_match", toJson statementMatch), ("axioms", toJson (st.axioms.map (·.toString (escape := false)))),
    ("replayed", toJson solConsts.size), ("problems", toJson problems)]

end ChhLeanCheck

def main (args : List String) : IO UInt32 := do
  match args with
  | [challenge, solution, thm, nonce] =>
    try
      initSearchPath (← findSysroot)
      let out ← ChhLeanCheck.run challenge.toName solution.toName thm.toName
      IO.println s!"CHH-LEAN-RESULT {nonce} {out.compress}"
      return 0
    catch e =>
      IO.eprintln s!"chh-lean-check: {e}"
      return 2
  | _ =>
    IO.eprintln "usage: chh-lean-check CHALLENGE_MODULE SOLUTION_MODULE THEOREM NONCE"
    return 64
