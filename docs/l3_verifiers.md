# L3 verifier additions: synthesis, calibrated tolerances, float-screen gate

GOAL §5 L3, §4.7 (computational class), §9 rows The Station and ScienceIDE. Branch `g-synth`.
Parameters: `src/custom_harness/verify/l3_config.py` (the one config place; UNCALIBRATED values marked).

## Verifier synthesis (`verify/synthesis.py`)
- A model (scripted backend in tests; no live calls) writes a verifier as a JSON spec in a closed vocabulary:
  property tests (`range`, `sorted`, `le`, `equals`, `one_of`), invariants (`permutation_of`, `sum_equals`) and a
  small simulator (`simulate_linear`, exact rational stepping). Trusted code interprets it; nothing model-written is
  executed. Limit: arbitrary-code verifiers (written as programs) would need the sandbox and are NOT built.
- Validation: the fixed, human-owned `SUITES[problem]` (golden cases to accept, planted faults to reject; public and
  sealed halves; the author sees only the description and the public golden cases). No caller can pass a suite.
- Tier: unvalidated -> unusable; validated -> T2 cap; ledgered human approval (`verifier_review`, bound to the verifier
  hash AND suite hash) -> T1; a human rejection -> unusable; any edit voids the review.
- Independence: a synthesized verifier whose author id or model family matches the claim's author never accepts that
  claim alone; an independent second verifier (other author and other family; a human-owned check counts) must agree.
- Ledger kind `verifier_review` is added by kstats (commit 6701279 on `s16-kstats`). Until that merges into the
  base, the tests add it with a labelled monkeypatch and `record_review` raises a clear error.
- Not wired: no CLI command for the review yet (`chh review-verifier`), no caller in the research loop yet, and the
  suites registry holds two example problems (`sort`, `decay`). New problems need human-written suites.

## Calibrated tolerances (`verify/tolerance.py`)
- Noise floor = the reported run plus honest re-runs: nominal, perturbed, rebuild, and seed spread.
- Per source, the split-conformal bound (the ceil((n+1)(1-a))-th smallest |rerun - reported|) covers a new honest re-run
  of that source with probability >= 1-a under exchangeability; the tolerance is the max over the sources the purpose
  needs (`reproduce`: nominal/perturbed/rebuild; `seed`: plus seed spread). Too few runs, non-finite values, a miss rate
  above `TOL_MAX_MISS_RATE`, or a floor above the Kernel's K1 ceiling are refused, never clipped.
- `resolve_tolerance(class, hand_set, calibration)`: data/simulated use the calibration in place of the hand-set value;
  formal/computational are exact (0, no tolerance); physical is refused.
- Not wired: `research/claims.py` (other owner) would pass the value as `AnalysisPlan.reproduce_tolerance`, and the
  runner must produce the noise-floor runs (perturbed / rebuild runs need sandbox support).

## Float screen -> exact certificate (`verify/screen.py`)
- `screen_then_certify`: the float screen may only prune (False). Every other candidate (True, a crash, a non-bool)
  goes to the exact checker; only an empty reason LIST makes a `Confirmed`, which only the gate can construct.
- LP instance reuses the `lp` adapter's exact checker (`kernel.lp.check` + `bounds`). The Kernel re-checks every
  submitted certificate exactly (K3), so the gate filters submissions and never certifies by itself.
