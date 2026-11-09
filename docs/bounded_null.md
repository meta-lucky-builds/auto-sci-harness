# Bounded-null certificates (exclusion-limit form)

GOAL §16.10, answers §13.5. Code: `kernel/ml.py` (`risk` -> `bounded_null`, `plasmode`, `plant`, `Exclusion`),
`kernel/checks.py` (K1 `K1_POWER`, `KernelConfig.min_power/max_power/plasmode_gamma`, `Context.material`),
`kernel/kernel.py` (`resolution["exclusion"]`, re-derived by `verify`), `kernel/claim.py` (`AnalysisPlan.power`).
Tests: `tests/test_kernel_nullbound.py`. Monte Carlo study: `scripts/sim_bounded_null.py`.

**Status (2026-10-14): built, NOT test-verified.** Tests, the fail-first run, coverage and the Monte Carlo study
were not run in this round (owner rule). Only ruff, `mypy --strict` on kernel/ and small smoke checks of `ml.plant`
ran. Touching `kernel/ml.py` changes the ML adapter's identity: the ML adapter needs a new human approval
(`chh approve-adapter`).

## Problem
Before this change K7 failed every `negative_result` and `equivalence` claim (fail closed, `K7_NO_RISK_MODEL`). At
the claimed bound delta, P(certified | true effect = delta) <= alpha for ANY delta, so no noise model can tell a
vacuous bound ("does not improve by 0.9 accuracy") from a tight one. The missing ingredient is the experiment's
**sensitivity**: could this procedure, on this data, have seen an effect of delta at all?

## The claim declares its sensitivity before the data
`AnalysisPlan.power` (1 - beta): the declared probability that the registered test detects an effect of size
delta = `effect.min_effect` on the claim's targets. It is part of the claim, so it is frozen by the pre-registration
(K2: claim hash). Omitted from every dump when unset, so existing claims keep their hashes.

K1 refuses (`K1_POWER`) a declaration outside `[KernelConfig.min_power, KernelConfig.max_power]` = [0.5, 0.95]
(both UNCALIBRATED, owner decision §13.8), and any declaration on a kind other than `negative_result` or
`equivalence` (TOST, below).

## Hypotheses and test (unchanged K4/K6 machinery)
Per target, theta = the sign-corrected effect subject - comparator in metric units (positive = subject better),
averaged over the planned seeds; the population is the target's test examples (iid).

- H0: theta >= delta (a real improvement of at least delta). H1 (the claim): theta < delta.
- **One-sided.** The registered test (`paired_t_seeds+bca_examples`) gives two-sided (1 - 2 alpha_eff) intervals;
  their upper limits are one-sided (1 - alpha_eff) upper confidence limits U.
- **Level alpha_eff** = the claim's fixed K4 level on that target (plan alpha / multiplicity, or the alpha-account
  slot level; §16.4), not the plan's nominal alpha.
- Reject H0 (criterion met, K6) iff U_examples < delta and, when the seed interval is not degenerate, U_seeds < delta.
  This is an intersection-union test: its size is <= alpha_eff per target, and requiring every target keeps it so.

## The sensitivity is proven by a positive plasmode control (K7)
For each target, with N = ceil(ln gamma / ln power) plantings (gamma = `KernelConfig.plasmode_gamma` = 0.1,
UNCALIBRATED; power 0.8 -> N = 11, 0.95 -> 45), planting j = 1..N:

1. **Randomness.** `random.Random(sha256(sha256(claim_hash | "plasmode" | target | material) || j))`, where
   `material` = the sorted prediction-blob hashes of the plan-matched records (`Context.material()`, the same
   material that seeds K3's replay choice). Fixed once the evidence is fixed, unknown before; `verify()` re-derives it.
2. **Plant.** In each seed's stored subject predictions, take the wrong predictions in a random order and replace
   the fewest of them by the true label so that the sign-corrected metric gain is >= delta (bisection on the count;
   exact for accuracy/MAE/RMSE, whose gain is monotone in the count). If even replacing every wrong prediction
   gains less than delta on some seed, **no effect of delta can exist** on this target: K7 fails `K7_NOT_PLANTABLE`
   (e.g. delta = 0.9 accuracy for a subject at about 0.33 accuracy: it can gain at most about 0.67).
3. **Resample.** Draw a bootstrap resample (n examples with replacement) of the target's examples, applied to the
   labels and to both arms on every seed: a fresh pseudo-dataset in which the true effect is (observed + delta).
4. **Run the registered procedure** on it: the BCa example interval and the seed t interval at level 2 alpha_eff,
   `n_boot` from the plan (BCa internal seed 0, as K4).
5. **Detected** iff the example interval is not degenerate, its lower limit exceeds the observed (unplanted)
   example estimate, and, when the observed seed interval is not degenerate, the seed lower limit exceeds the
   observed seed estimate. "Lower limit > observed estimate" is the bootstrap analogue of "a level-alpha_eff
   one-sided test rejects 'no shift' when the shift is delta": by the bootstrap principle the planted resample's
   estimate sits around observed + delta as the real estimate sits around theta + delta.

K7 passes iff all N plantings are detected (zero-failure demonstration, the same design as K3's §16.4 reliability
demonstration). If the plasmode's detection probability pi were below the declared power, P(all N detected) =
pi^N < power^N <= gamma. So a pass proves pi >= power at confidence 1 - gamma; the certificate records
`proven_power` = gamma^(1/N) (the one-sided 1 - gamma lower confidence bound on pi after N/N detections), which is
>= the declared power. One miss fails K7 (`K7_INSENSITIVE`).

## Lucky limits are clipped
`observed_limit` = the largest U over targets and over the intervals the criterion uses. `reported_limit` =
max(observed_limit, delta). The only effect size whose detection was proven is delta, so the certificate never
states a limit tighter than delta, however lucky the observed limit (a downward fluctuation can make U far below
delta, even negative). This is the power-constrained-limit form of the physics practice (CLs and PCL both refuse to
exclude signals the experiment was not sensitive to). A tighter statement needs a new claim with a smaller delta,
pre-registered and plasmode-proven. Since a certified claim has observed_limit < delta, reported_limit = delta.

## The certificate
When (and only when) the certificate is CERTIFIED, `resolution["exclusion"]` carries the typed statement
(`ml.Exclusion`): `excluded_effect` (delta), `sided` ("one-sided (upper limit)"), `level` (the largest per-target
alpha_eff), `observed_limit`, `reported_limit`, `declared_power`, `proven_power`, `power_confidence` (1 - gamma),
`plantings` (N). K7's observation renders it in words. `verify()` re-derives the plasmode and compares the
statement: a recorded statement that differs from its re-derivation is reported as forged.

## Equivalence claims (TOST; s4-kernel, 2026-10-15, owner-delegated statistics decision)
**Status: built, NOT test-verified** (tests deferred by the owner; `tests/test_kernel_s4.py`, item 3). Before this
change every equivalence claim failed K7 (`K7_NO_RISK_MODEL`) and K1 refused a declared power (`K1_POWER`). The
statistical procedure below was chosen by the s4-kernel agent under the owner's delegation; it is recorded in
`redteam/kernel_core_changes.yaml` as a `delegated` record (cooling-off not observed) and needs the owner's review.

**The claim.** `kind: equivalence`, margin delta = `effect.min_effect` (> 0, K1), declared power `AnalysisPlan.power`
in `[KernelConfig.min_power, max_power]` (K1). Both are fields of the claim, so they are part of the claim hash that
the pre-registration commits (K2 checks that the registered content hashes to the claim hash): the margin and the
power are **fixed at registration** (invariant 17); nothing after the data can move them. The level alpha_eff of each
target is K4's fixed per-target level (plan alpha / multiplicity, or the alpha-account slot level, §16.4).

**Hypotheses, per target** (theta = sign-corrected subject - comparator, metric units, averaged over the planned seeds):
H0: |theta| >= delta (the arms differ by at least the margin). H1 (the claim): -delta < theta < delta.

**The test: two one-sided tests (TOST), each at one-sided level alpha_eff.** K4 computes the registered intervals
(BCa over examples, Student t over seeds) at two-sided level 2 alpha_eff (`ml.statistics`, unchanged), so their lower
limit L and upper limit U are each a one-sided (1 - alpha_eff) confidence limit. K6's criterion (`ml.criterion`,
unchanged since v1) is

    -delta < L_examples  and  U_examples < delta, and the same for the seed interval when it is not degenerate.

That rejects H0- (theta <= -delta) with the one-sided test L > -delta at level alpha_eff AND H0+ (theta >= delta) with
U < delta at level alpha_eff. Stated exactly: **each of the two one-sided tests has level alpha_eff (not alpha_eff / 2);
the equivalence conclusion has size <= alpha_eff** by the intersection-union principle (under H0 at least one of H0-,
H0+ is true, and falsely rejecting that one alone has probability <= alpha_eff). Requiring every target (and both
intervals) keeps the size <= alpha_eff per target. Assumptions: those of the K4 intervals (iid test examples; BCa is
asymptotic, so the size is approximate at small n, as for every data claim).

**Sensitivity: the positive plasmode control, on both sides** (`ml.bounded_null`, `ml.plasmode(side=...)`). With
N = ceil(ln gamma / ln power) plantings per side (gamma = `KernelConfig.plasmode_gamma` = 0.1, UNCALIBRATED):
- **+delta side** (`side = 1`): exactly the negative-result plasmode above (same seed string, so a negative result's
  plasmode is bit-identical to before): plant an effect of +delta into the SUBJECT's stored predictions (fewest wrong
  predictions replaced by the truth, random order), bootstrap-resample the examples, run the registered intervals at
  2 alpha_eff; detected iff each used interval's LOWER limit exceeds the observed estimate.
- **-delta side** (`side = -1`): an effect of -delta on subject - comparator is planted by IMPROVING the COMPARATOR by
  delta in the same way (its fewest wrong predictions replaced by the truth). This avoids the problem the earlier
  note raised: degrading the subject has no unique form (which wrong label, which shift), while improving an arm is
  unique up to the random order. Randomness: `sha256(claim_hash | "plasmode" | target | material | "|-")` per
  planting. Detected iff each used interval's UPPER limit is below the observed estimate.
- If either arm cannot gain delta on some seed (all its wrong predictions fixed still gain less), no effect of that
  sign and size exists on the target: `K7_NOT_PLANTABLE` (vacuous margin). One undetected planting on either side:
  `K7_INSENSITIVE`.

K7 passes iff all 2N plantings are detected. If the detection probability on EITHER side were below the declared
power, passing would need all N plantings of that side detected, probability < power^N <= gamma: so a pass proves both
detection probabilities >= power jointly at confidence 1 - gamma (one-sided lower bound; `proven_power` = gamma^(1/N)
per side).

**The statement** (`resolution["exclusion"]`, CERTIFIED only): `sided` = `ml.SIDED_TOST`; `excluded_effect` = delta;
`level` = the largest per-target alpha_eff (each limit one-sided at this level); `observed_limit` = the largest |limit|
(max(-L, U)) over targets and used intervals; `reported_limit` = max(observed_limit, delta) (lucky limits clipped, as for
negative results; a certified claim has observed_limit < delta, so reported_limit = delta); `plantings` = N (per side).
K7's observation: "|effects| >= delta excluded (TOST: each side one-sided, level ...)". `verify()` re-derives it.

**Not proven (stated):** as for negative results (bootstrap world, shape of the planted effect: oracle corrections
concentrated on an arm's wrong examples, seed-level variance); in addition the -delta side's planted effect is
"the comparator gets better", which for a metric whose gain is not monotone in the count of corrections (none of the
registered metrics today; f1_macro is approximately so) could be detected with another probability than "the subject
gets worse". The Monte Carlo study (`scripts/sim_bounded_null.py`) does not yet cover equivalence: to add (not run).

## What the plasmode proves, and what it does not
Proves (at confidence 1 - gamma, over the plasmode's own randomness, given the stored data): on bootstrap
pseudo-datasets drawn from this target, the registered procedure at the claim's level detects a planted effect of
delta with probability >= the declared power. Hence the bound delta is not vacuous: an effect of that size is
possible on this target (step 2) and the procedure could have seen it.

Does NOT prove:
- the **frequentist power over new datasets**; the bootstrap world approximates it (part C below measures how well);
- anything about **effects of a different shape**: the planted effect is concentrated on examples the subject got
  wrong (oracle replacement); an effect spread differently can have a different detection probability;
- **seed-level variance**: the resample redraws examples, not training seeds; the seed interval enters only as
  computed on the resample;
- the **validity** of the exclusion itself: that comes from the K4/K6 intervals (size <= alpha_eff, approximate for
  BCa at small n), not from the plasmode;
- anything beyond the targets (populations, other datasets).

Assumptions: iid test examples; the stored predictions are the programs' real outputs (K0/K3); BCa and t coverage
(asymptotic for BCa). Cost: N extra BCa runs per target (11 at power 0.8), repeated by `verify()`; a
non-decomposable metric (f1_macro) is much slower.

## Monte Carlo study
`scripts/sim_bounded_null.py` (run through the machine limiter) uses the Kernel's own `ml.criterion`,
`ml.plasmode` and the registered intervals on synthetic paired binary classification: n iid examples, each arm
independently correct with its accuracy, 3 seeds with identical predictions (seed-invariant programs, so the seed
interval is degenerate and the example interval decides), alpha_eff = 0.05 one-sided, n_boot = 1000, gamma = 0.1,
declared power 0.8 (N = 11). Rates are given +- one Monte Carlo SE, sqrt(p(1-p)/R).

**Status: the script is built and ready but has NOT been run** (owner rule for this round: no tests and no long
runs). No Monte Carlo number is claimed here. To run (through the machine limiter, on a snapshot worktree):

    python C:\SERS\<USER>\custom-harness-data\tools\locked_run.py <SNAPSHOT_WORKTREE> <LOG> script scripts/sim_bounded_null.py 200

Acceptance criteria, fixed before running:

- **A. False exclusion** (true effect = delta = 0.1; accuracy 0.70 vs 0.80; n = 100 and 400; R = 2000 each): the
  rate of "effect < delta" certified (criterion; criterion + plasmode) must be <= alpha = 0.05 within Monte Carlo
  error (BCa is approximate, so a small excess at n = 100 would be reported, not hidden).
- **B. Vacuous delta** (true effect 0): n = 20, delta = 0.3 (true power to exclude 0.3 about 0.66 by the normal
  approximation, below the declared 0.8): the criterion alone (the v1 rule without K7) certifies often (about 0.66
  expected); with the plasmode the certified rate must drop to about gamma or below. n = 60, delta = 0.9: no effect
  of 0.9 can be planted, so the certified rate must be exactly 0.
- **C. Power** (true effect 0, delta = 0.15, n in 40, 80, 120, 200, 400): the true power (P(delta excluded |
  effect 0) and P(lower limit > 0 | effect delta), from fresh datasets) against the plasmode pass rate at declared
  power 0.8. Where the true power is below 0.8 the pass rate should be small (about gamma; the guarantee holds in
  the bootstrap world, so the study measures how close that is to the real one); where it is well above 0.8 the
  pass rate should be high.
