# naturalist — final (round 2)

**Status of every number below.** Nothing has been run on the real system. Simulation numbers come from small
pure-Python toys in `scratchpad/`: mine is `naturalist/selfsim.py`; the others are maverick's `maverick/defeat.py` and
`kworld*.py`, institutionalist's `institutionalist/noninterference.py`, and crossdomain's
`crossdomain_plasmode_featureleak.py` and `crossdomain_subset.py`. Toys show possibility, not pipeline behaviour.

## 1. Endorsed mechanisms, ranked

**1. Information-flow prevention first: double-entry reconciliation** (my P11, unchallenged).
- **Why first.** Output controls only *measure* leakage (defeat device); prevention rests on K9 + the 16.5 release ledger, which is only as good as its completeness.
- **Spec.** The sandbox's own IO log (written by L0) is a second book, reconciled both ways against the release ledger:
  - every target-derived byte that reaches an agent-visible surface must match a release entry;
  - every release entry must match an observed transfer.
- **Test.** 20 injected bypasses, such as a direct copy into a mount or a score placed in an error string: ≥ 19
  flagged, and 0 flags on 50 honest runs.
- **Evidence.** No simulation. Buildable now.

**2. Output-difference non-interference replay** (institutionalist's repair).
- **Spec.** For exact-class (byte-deterministic) claims, replay the frozen program in a fresh, fingerprint-identical
  sandbox. Every content-matched private label copy is replaced with y′ ~ p_ref(x). Flag *any* byte difference in the
  predictions.
- **Toy evidence** (noninterference.py). It caught the defeat device at stealth f = 0.02 in 99.7% of trials, with 0
  honest flags. It gives 100% false alarms on nondeterministic programs.
- **Precondition.** A double run with real labels must be byte-identical. If not, the claim falls back to the k-world
  ceiling, with its stealth margin printed. That fallback is weak: maverick's kworld toy shows a tuned device is
  caught on only 29% of the tasks where it cheats.
- **Kill.** If > 10% of honest suite programs are not byte-deterministic, it stops being the workhorse.

**3. Audit Plane salts, with positive salts and effect-size coverage first.**
- **The amplifier device.** My toy (selfsim.py) found a second device that nobody had named: it cheats only when its
  honest gain is already large. Every null salt misses it (0.000 certified). On positive salts it inflates the gain
  from 0.054 to 0.126.
- **Coverage check.** Flag a positive salt when the gain's lower 95% bound exceeds the known planted gain. That flags
  0.25 / 0.53 / 1.00 of single salts for f = 0.05 / 0.10 / 0.30, with honest false flags ≤ 0.05.
- **Spec.**
  - Positive salts are assigned by `draw(salt_assignment)`, which uses S + B (the human secret plus a beacon).
  - They stay in the research stream only, with reversing entries.
  - L6 receives one bit per campaign at envelope opening.
  - Null salts come second, as a gross-failure alarm only. They are self-similar (same generator, g = 0) for
    synthetic families, and fitted plasmodes for real data. They need maverick's prevalence matching, because
    "nothing works on dev" is otherwise a tell. That tell is exactly what the amplifier conditions on.
- **Test.** 10 positive salts against an amplifier at f = 0.1: pooled coverage must flag it in ≥ 90% of 200
  repetitions, with honest false flags ≤ 5%. This is not yet run, even in the toy.

**4. `draw()` with a per-purpose input table** (converged across all five).
- **Spec.** `H(v1‖purpose‖frozen_hash‖inputs(purpose))`.
  - Beacon only for target, held-out subsample, replication seeds, gate tasks, and check_params when the program is
    frozen before round r.
  - S + B only for salt assignment.
  - The Kernel checks anchor < round r < the first touch of the drawn object, and refuses any mismatched input set.
- **Tests.**
  - Misuse refused 100%.
  - Seed shopping: false-certification rate > 2α with self-chosen seeds, ≤ α with drawn seeds, over 1,000 simulated
    claims. Not yet simulated.
  - 0 grep hits for S in agent-readable surfaces.

**5. Resweep with an honest-corpus screen and graded sanctions** (merged spec, co-signed).
- **Screen.** 0 flips on ≥ 500 histories covering every category on a human-owned rare-honest list (deterministic,
  n = 45, resumed, honest failures, exact-solver, physical, steered). Rule of three: false flips ≲ 0.6%; absent
  categories unbounded.
- **Sanctions.** Output-keyed, local (evidence and exposure edges only), graded and reversible. A penalty beyond
  clawback applies only to REJECTED-INTEGRITY.
- **Tests.**
  - The pre-SPLIT archive: ≥ 1 flip on the 4 affected tasks, 0 elsewhere.
  - 5 planted F4 certificates become STALE-RULE.
  - Graded propagation sanctions ≥ 3× fewer units than ancestry propagation under a 5%-over-broad rule.

**6. Attempt telemetry.**
- **Spec.** Targeted attempts per 100 actions, counted per channel. Historian's displacement index is reported after
  every museum fix.
- **Test.** Crossdomain's two-channel simulation (one logged channel, one unlogged) runs before any veto is enabled.

**7. Replication-vs-z curve** (joint with historian).
- **Spec.** Every acceptance records z against a pre-measured noise floor. Later re-measurements build sign-replication
  as a function of z, per decision type.
- **Test.** The curve must reach ≥ 0.8 at z > 3. A flat curve near 0.5 relabels that decision type MECHANICAL.

**8. Prosecutor limits** (my three, adopted by maverick).
- **Spec.** A second free signal before any α is spent; α capped at 0.2 of the attacked targets' α; contraction when
  hits stop. Build it after the Audit Plane.
- **Test.** ≥ 4 of 6 defects found, and ≥ 80% of attacks on clean certificates refused before any α is spent.

## 2. Where I still dissent

- **Detectability.** d̂ ≤ 0.1 at 40 per arm is unreachable (SE ≈ 0.11 at d = 0, upper bound ≈ 0.18). Fund ~135 per
  arm, or accept ≤ 0.2 and report r̂/(1 − d̂_upper); otherwise the exam is always waived (ritual capture).
- **Null salts are over-valued on the board.** By construction they cannot see the amplifier. Positive-salt coverage
  is the more informative instrument and should be funded first.
- **Plasmode nulls vs in-data leaks.** Crossdomain's toy says they miss them (gain −0.11 / 0.00); I side with it.
  Real-data feature leaks need adapter provenance tags, a dominance screen and a human ruling.
- **Unverified sub-claim of mine.** Self-similar synthetic salts may catch leaks made by our own data-prep code
  (the salt regenerates the leak). Speculative; needs a test.
- **Telemetry veto.** Others say never a gate; I hold a narrow non-inferiority veto on sandbox-enforced channels,
  dropped if the two-channel simulation shows displacement.
- **check_params.** Beacon-only suffices once the program hash is anchored before r; S only delays rerun.

## 3. Dropped

- My own: the credibility bankroll (parked until at least 400 resolutions), swarm attrition, the evolving attacker
  population, "replay salts as a GAAR", AUC over 200 runs, and attempt rate as a fitness term.
- Shared: label-permutation null salts and shadow controls as leak detectors, kept only as a free tripwire;
  optimistic acceptance; fraud proofs by subset search (crossdomain's toy: 96% of honest claims "broken" at 1.5δ);
  the R(D) stop rule; the fixed 80% positive-rate alarm; P4b unconditioned; H12; escrow, replaced by
  provisional/seasoned VDS.

## 4. The single most important thing for the owner

Against a program that already holds leaked information, **every output-based check is an arms race**: toys broke
or bounded each within a day (permuted control, held-out nulls, k-world ceiling, nulls vs amplifiers, plasmodes vs
feature leaks). The backbone must be **prevention**: information flow (K9, release ledger, double-entry
reconciliation), non-interference on deterministic claims, and human data-provenance rulings. Salts are
thermometers. Certificates should say "leakage measured by …", never "prevented".

## 5. Owner decisions required

1. A model-call budget for positive salts, then null salts. Roughly 5–13 calls each.
2. Whether drand may be fetched by the control plane, or human dice only.
3. Prevalence matching (admitting genuinely null-dev real tasks) versus the 16.12 10–90% filter.
4. Whether nondeterministic claims are allowed (they get only the weak tolerance-class leak control).
5. Who owns the rare-honest coverage list, and at least one externally written attack per milestone (the
   "exam written by the examinee" risk). The agents must not own both.
6. Kernel change cadence: tightening-only changes merge immediately; loosening changes wait for a scheduled window.
7. The detectability threshold and sample size.
