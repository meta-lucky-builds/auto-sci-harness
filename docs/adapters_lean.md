# The `lean` adapter (formal claims, tier T0)

GOAL §4.7 "Formal" row, §5 "Domain adapters", L3 "T0 split", §11 M3. Branch `m3-lean-adapter`.

**Status: built and tested against deterministic fakes only. No part of it has been run against a real Lean
toolchain** (none is installed; installing one is the owner's decision). As shipped, the adapter **cannot be
approved**: its validation fails until the owner pins the Mathlib commit (see "Decisions").

## What a formal claim is

- `kind: "theorem"`, `evidence_class: "formal"`, `metric: {id: "lean_theorem", higher_is_better: true}`,
  `subject` = `comparator` = a registered `chh-lean-statement/1` JSON artifact
  `{"format", "theorem": "<Lean name>", "source": "<Lean file that states it, proved by sorry>"}`;
  no datasets, empty analysis plan, zero effect fields, declared probability in (0, 1), no risk prior, no value.
- The statement must carry a HUMAN approval: `chh approve-statement FILE --artifact-id ID --approver NAME
  [--statement-hash SHA]` prints the statement, registers it and appends a `statement_approval` ledger entry bound
  to its sha256. This CLI command is the only code that writes that entry kind (a test enforces it).
- The evidence is ONE check record (`chh-lean-check/1`), produced by the sandbox runner
  (`custom_harness.sandbox.lean.run_check`) and linked with `record_check` (= `Kernel.record_certificate`).

## What is checked, and where

| Where | What |
|---|---|
| sandbox runner (adapter side) | hygiene scan BEFORE compiling; records `lean --version`, the project's `lake-manifest.json`, the checker's sha256; compiles the proof first, lists `out/`, then writes and compiles the approved statement; runs `chh-lean-check Challenge Solution <thm> <nonce>` |
| `chh-lean-check` (Lean program, `docker/lean/ChhLeanCheck.lean`, sha pinned in `formal.CHECKER_SHA256`) | refuses unsafe declarations and any reference to `Lean.ofReduceBool`/`Lean.reduceBool`/`Lean.trustCompiler`/`sorryAx` before replay; replays every solution declaration through the Lean kernel (`Environment.replay`, as lean4checker); compares the theorem's elaborated type and universe params with the approved statement's, and every constant it depends on (types, definition bodies, inductive constructors); collects axioms (`CollectAxioms`) |
| Kernel K3 (`kernel/formal.py`, stdlib only) | record bound to these statement bytes and theorem; pinned checker; hygiene of statement and proof (stored bytes); Lean version and Mathlib `rev`+`inputRev` pins; nonce = sha256(statement, proof, salt); proof build wrote only `Solution.olean/.ilean`; both builds exit 0 with no `error`, proof without `declaration uses 'sorry'`; exactly one `CHH-LEAN-RESULT <nonce> {json}` line; theorem/kind/statement_match/problems/replayed; axioms within the allowlist (`sorryAx`, `Lean.ofReduceBool`, `Lean.trustCompiler` and anything else named in the reason) |
| Kernel K0/K2/K9 | shared with the computational class: statement and record resolve to intact registered artifacts; claim pre-registered; record registered AFTER the pre-registration; no canary/private file in statement or record |
| Kernel K8 | size bounds (sources 200 kB, outputs 2 M chars) |
| Kernel K4-K7 | n/a by the human-owned `CHECK_APPLICABILITY` table (K7: OWNER DECISION PENDING, see below) |

Certificate: label `CERTIFIED-FORMAL`, tier T0, verifier `lean:lean_theorem`, adapter hash; assumptions: the
approved statement formalises the intent (untested), Lean's kernel + pinned Mathlib build are sound (untested), the
record came from the runner (untested; the Kernel re-checks, it does not re-run Lean), comparator + axiom audit
(tested by K3), and one line per axiom used (`K3: relies on axiom propext`, untested).

## What Lean prints (from memory of the documented behaviour; NOT verified here)

- `#print axioms foo` prints `'foo' depends on axioms: [propext, Classical.choice, Quot.sound]`, or
  `'foo' does not depend on any axioms`; a `sorry` shows `sorryAx`; `native_decide` shows `Lean.ofReduceBool`
  (newer releases also `Lean.trustCompiler`). The adapter does NOT parse this text: a proof can redefine `#print`
  via macros, so the checker collects axioms at the environment level and prints its own JSON line.
- A `sorry` produces `<file>:<l>:<c>: warning: declaration uses 'sorry'`; errors are `<file>:<l>:<c>: error: ...`.
- `lean --version` prints `Lean (version 4.19.0, x86_64-unknown-linux-gnu, commit <sha>, Release)`.

## Red team (a dishonest prover) and what stops it

| Trick | Stopped by |
|---|---|
| `sorry` / `admit` / `sorryAx` | hygiene (source); axiom audit (`sorryAx`); build warning |
| new `axiom` | hygiene; axiom audit (non-allowlisted) |
| `native_decide`, `decide +native`, `(config := {native := true})` | hygiene; pre-replay scan; audit (`Lean.ofReduceBool`) |
| statement drift: weakened hypotheses, changed constants, renamed theorem, record of another statement | comparator (type + dependency identity); record/statement hash and theorem binding |
| `local instance` / `local notation` / `infix` shadowing `+` | notation: hygiene; instances: the elaborated type then names a different instance constant -> comparator mismatch |
| redefining a constant the statement uses | dependency identity walk (definition bodies, inductive constructors) |
| `macro "theorem"`, `macro_rules`, `syntax`, `elab`, `#exit`, `#eval` forging the result line | hygiene; the checker never reads proof syntax (environment level); nonce unknown to the proof at compile time |
| `set_option debug.skipKernelTC true` | set_option allowlist; kernel replay of every declaration |
| `@[implemented_by]`, `@[extern]`, `@[export]`, `unsafe`, `unsafeCast` | hygiene; checker refuses unsafe declarations |
| `initialize`, `run_cmd`, `run_tac`, `by_elab`, `addDecl` (compile-time code, e.g. writing a fake .olean) | hygiene (never compiled); `out/` listing; kernel replay |
| `import Lean`, `import Challenge`, `prelude`, `«sorry»` | import allowlist; checker refuses importing the challenge; `prelude` and guillemets forbidden |
| proof build overwrites `Challenge.lean` | the runner writes the approved statement after the proof build |
| splicing a passing result from another proof | nonce = sha256(statement, proof, salt) recomputed by the Kernel |

Hygiene scans comments and strings too (fail closed: a comment containing `sorry` is rejected).

## Decisions for the owner (policy, marked DECISION in `kernel/formal.py`)

1. **Toolchain pins**: `LEAN_VERSION = "4.19.0"`, `MATHLIB_INPUT_REV = "v4.19.0"` were chosen without checking
   that these releases/tags exist or are current. `MATHLIB_COMMIT` is a placeholder: validation check "the Lean
   version and the Mathlib commit are pinned" fails, so `chh approve-adapter lean` refuses until it is a 40-hex
   commit (take it from the built image's `lake-manifest.json`). Changing it changes the adapter hash (re-approval).
2. **Axiom allowlist** `{propext, Classical.choice, Quot.sound}`.
3. **K7 for theorems**: marked n/a with "OWNER DECISION PENDING" (GOAL §13.3: no resampling noise; a prior over
   statements is not defined). No K7 rule was invented. Formal claims are therefore certifiable without any
   riskiness assessment until you decide.
4. **New label** `CERTIFIED-FORMAL` and its VG-ladder text; tier T0.
5. Import roots (`Mathlib`, `Init`, `Std`, `Batteries`; never `Lean`), `set_option` allowlist (`maxHeartbeats`,
   `maxRecDepth`, `synthInstance.maxHeartbeats`), the forbidden-token list (scans comments too).
6. Whether to replace the synthetic golden record in `verify/lean_validation.py` with one captured from the image.

## Install note (owner only; nothing here was run)

1. `docker build -f docker/lean.Dockerfile -t custom-harness-lean:4.19.0 docker` (downloads elan from
   `raw.githubusercontent.com/leanprover/elan/master/elan-init.sh` - unpinned script, review it -, the toolchain
   and Mathlib; several GB; `lake exe cache get` fetches prebuilt Mathlib, else a long build).
2. `docker run --rm custom-harness-lean:4.19.0 cat /opt/chh-proj/lake-manifest.json` -> copy the mathlib `rev`
   into `formal.MATHLIB_COMMIT`; check `lean --version` matches `LEAN_VERSION`; rebuild with `--build-arg
   MATHLIB_REV=<commit>`.
3. Run `pytest tests/test_lean_adapter.py -k real` (two tests, skipped until the image/toolchain exist). Expect
   to fix `ChhLeanCheck.lean` API drift (it was never compiled: `importModules` arguments, `Environment.replay`,
   `CollectAxioms.collect`, `ModuleData` fields, `Std.HashMap`); each fix changes `CHECKER_SHA256`.
4. Run the sandbox with `memory="8g"` or more (two Mathlib environments are loaded).
5. Only then: `chh approve-adapter lean --approver <you>`.

## Residual risks

- The Kernel trusts that the record came from the runner (as with LP certificates); it re-checks the output, it
  does not re-run Lean. A K3 re-execution path through the Kernel's replayer is not built.
- `chh-lean-check` is unverified Lean code; `lake env` and `lean -o/-i` behaviour, LEAN_PATH handling,
  initializer behaviour of `importModules`, and memory needs are unverified. All such failures fail closed.
- The comparator trusts Mathlib's prebuilt .olean files in the image (not replayed).
- The statement's own source is human-approved but compiled in the same workspace after the proof; the proof's
  compile-time code is blocked by hygiene, not by a second sandbox.
