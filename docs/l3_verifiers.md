# L3 verifier additions: synthesis, calibrated tolerances, float-screen gate

GOAL §5 L3, §4.7 (computational class), §9 rows The Station and ScienceIDE. Built on branch `g-synth`; wired into the
claim path on branch `s3-verifiers` (2026-10-09).
Parameters: `src/custom_harness/verify/l3_config.py` (the one config place; UNCALIBRATED values marked).

## Verifier synthesis (`verify/synthesis.py`, `research/verifier_synthesis.py`)
- A model (scripted backend in tests; no live calls) writes a verifier as a JSON spec in a closed vocabulary:
  property tests (`range`, `sorted`, `le`, `equals`, `one_of`), invariants (`permutation_of`, `sum_equals`) and a
  small simulator (`simulate_linear`, exact rational stepping). Trusted code interprets it; nothing model-written is
  executed. Limit: arbitrary-code verifiers (written as programs) would need the sandbox and are NOT built.
- Validation: the fixed `SUITES[problem]` (golden cases to accept, planted faults to reject; public and sealed halves;
  the author sees only the description and the public golden cases). No caller can pass a suite.
- **Authorship (corrected 2026-10-09).** The two example suites (`sort`, `decay`) were written by an AI agent and were
  first labelled `owner (custom-harness)`. That was false. Every suite in code is now `AGENT-AUTHORED`
  (`FaultSuite.authorship`, a property no code can change; `author = "agent:g-synth (...)"`). A suite counts as
  human-approved only through a ledgered HUMAN suite review (`record_suite_review`: ledger kind `verifier_review`,
  body `subject: "suite"`, bound to the suite hash); `suite_status` reports `AGENT-AUTHORED` / `human-approved` /
  `human-rejected`.
- Tier: unvalidated -> unusable; validated -> T2 cap. T1 needs BOTH a ledgered human approval of the verifier (bound to
  verifier hash AND suite hash) AND a ledgered human approval of the suite: an agent-authored suite cannot lift the cap
  (tests/test_synthesis_wiring.py). A human rejection of the verifier or of its suite -> unusable; any edit voids a
  review. A reviewer named like an author or starting with "agent" is refused (accountability-level: the review is
  meant for the human-operated CLI, which does not exist yet).
- Independence: a synthesized verifier whose author id or model family matches the claim's author never accepts that
  claim alone; an independent second verifier (other author and other family; a human-owned check counts) must agree.
- Wired: `research.synthesize_verifier` (params `problem`, `role` default `verifier_author`) calls the model through
  `ModuleContext.llm` (budgeted, traced), stores the spec as a blob and returns `{problem, ref, verifier_hash,
  suite_authorship, validated, tier, error}`. State is a hint only: `usable_tier(ctx, ref)` re-parses the stored
  spec, re-validates it and recomputes the tier from the ledger. Only the validation BIT is returned (which sealed case
  failed is never shown; no suite hash either, since it covers the sealed cases). OPEN (review M4, invariants 18/20):
  repeated synthesis can learn one bit per attempt about the sealed half, these looks are not ledgered, and the module
  is not pinned to human-only nodes.
- Not built: a `chh review-verifier` / `chh review-suite` CLI; a consumer that uses synthesized verdicts in a claim.

## Calibrated tolerances (`verify/tolerance.py`, `research/noise_floor.py`)
- Noise floor = the reported run plus honest re-runs: nominal, perturbed, rebuild, and seed spread.
- Per source, the split-conformal bound (the ceil((n+1)(1-a))-th smallest |rerun - reported|) covers a new honest re-run
  of that source with probability >= 1-a under exchangeability; the tolerance is the max over the sources the purpose
  needs (`reproduce`: nominal/perturbed/rebuild; `seed`: plus seed spread; `replay_same_env`: nominal/perturbed, for a
  replay in the same environment as the original run). Too few runs, non-finite values, a miss rate above
  `TOL_MAX_MISS_RATE`, or a floor above the Kernel's K1 ceiling are refused, never clipped.
- `claim_tolerance(class, floor)` is the claim path's one decision: no hand-set value enters; without a floor, or when
  calibration refuses, the tolerance is 0.0 (identical predictions on replay, the strictest K3); exact classes are 0.
  When every nominal re-run equals the reference (the program is deterministic on identical inputs, which is what a
  same-environment replay gives it) the tolerance also stays 0.0, whatever the shuffled-rows source shows: a tolerance
  above 0 switches K3 from comparing predictions to comparing scores (kernel/checks.py), so it is used only for programs
  that are measurably non-deterministic on identical inputs (review finding H1).
- Wired: `research.claim` with `calibrate_tolerance: true` (the node is protected from harness edits) measures the
  candidate's noise floor on PUBLIC dev data (`_dev_predict`: reference + n nominal re-runs + n re-runs with each fold's
  training rows shuffled, n = `min_runs(TOL_CLAIM_MISS_RATE)` = 9), and freezes `claim_tolerance(DATA, floor).value`
  into `AnalysisPlan.reproduce_tolerance` before pre-registration; `state["tolerance"]` records the floor. Without the
  param the behaviour is unchanged (0.0). The example specs do not set the param.
- Test (tests/test_tolerance_wiring.py): with a measured floor the Kernel's K3 rejects a planted 0.01 drift between the
  recorded score and the replay, honest re-runs (and a drift inside the measured noise) pass, and the same drift passes
  the Kernel's hand-set ceiling (0.02).
- Assumptions/limits: exchangeability of dev and held-out replay deviations; the rebuild source is not measured (the
  research loop replays in the same sandbox environment); a program deliberately noisy on dev can widen its tolerance up
  to the K1 ceiling, which a hand-set claim could already declare. Kernel hook (not built, s3-kernel owns kernel/): K1
  could require a non-zero `reproduce_tolerance` to cite a noise-floor record (see the s3-verifiers report).

## Float screen -> exact certificate (`verify/screen.py`)
- `screen_then_certify`: the float screen may only prune (False). Every other candidate (True, a crash, a non-bool)
  goes to the exact checker; only an empty reason LIST makes a `Confirmed`, which only the gate can construct.
- LP instance reuses the `lp` adapter's exact checker (`kernel.lp.check` + `bounds`). The Kernel re-checks every
  submitted certificate exactly (K3), so the gate filters submissions and never certifies by itself.
- Wired submission: `certify_confirmed_lp(kernel, confirmed, candidates, claim_for)` submits ONLY `Confirmed` results
  (a screen survivor, a bare candidate or an id raises TypeError before anything is registered) and refuses a candidate
  whose content differs from the confirmed one. No loop produces LP candidates yet; this is the path one must use.
