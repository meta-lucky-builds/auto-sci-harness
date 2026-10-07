# maverick — final (round 2)

**Evidence status.** Nothing here ran on the real system. Every number comes from **toy, pure-Python simulations**:
a binary task with N = 1000 and 200–300 trials. The scripts are in `scratchpad/maverick/`. Toy results show that a
mechanism *can* work or fail. They do not show that it works in the pipeline. Labels: [toy-verified], [inferred],
[speculative].

## 0. The finding that organised everything
**Output-based audits are an arms race. Only information-flow control ends it.**
- A 3-line *defeat device* holds leaked labels and uses them only when they look real. It fooled the permuted-label
  control 100% of the time (`defeat.py`).
- A k = 5 counterfactual-world ceiling control still let a tuned device cheat on 27% of tasks, and caught only 29% of
  those (`kworld2.py`).
[toy-verified]

Every audit below is therefore a *measurement*. Prevention belongs to K9, the release ledger, and sandbox
information flow.

## 1. Mechanisms I endorse, ranked

**E1. Non-interference leak control, run in an entropy-pinned sandbox, with a McNemar fallback.**
Built on institutionalist's output-difference test, with my two amendments.
- **The test.** Replay the frozen program in a fresh sandbox, with every content-matched private label copy replaced
  by Bernoulli counterfactual labels. Flag if the outputs differ by even one byte. An honest program never reads the
  labels, so its outputs cannot differ.
- **My attack on it [toy-verified, `noiseoptout.py`].** A cheater that randomises *which* rows it copies is
  nondeterministic. It fails the determinism precondition, gets classed as tolerance-class, and escapes the byte
  test. A majority-vote fallback caught only 1–18% of cases.
- **Amendment 1: the sandbox pins all entropy.** The clock and /dev/urandom are seeded by the Kernel. A program
  that is deterministic given its inputs then cannot opt out, and seeding noise from the label bytes makes it differ
  anyway. [inferred]
- **Amendment 2: a fallback for genuinely nondeterministic claims.** Use a paired McNemar test of label dependence
  on rows where the real and counterfactual runs disagree (3 + 3 runs, z > 3). In the toy (`mcnemar.py`,
  `mcnemar2.py`):
  - With noise = 0: 0% honest false flags; f = 0.02 caught 94.5%.
  - With 5% honest noise: f = 0.05 caught 49%, f = 0.10 caught 98%.
  - Against a tuned world-detecting device with 5% noise: caught 12% at f = 0.05 (about +1.3 points of gain), 70% at
    f = 0.10, 100% at f = 0.30.
  - The residual stealth margin of about 1 point is printed on the certificate.
- **Test on the real Kernel.**
  - The DEV-1 and SPLIT museum patches, wrapped in the device (deterministic and randomised): 4/4 caught.
  - 20 honest exact-class claims: 0 flags.
  - Kill criterion: if > 10% of honest suite programs are nondeterministic under pinned entropy, McNemar becomes the
    primary test.

**E2. The Audit Plane salt stream, positives first.**
This is historian's Envelope as amended by naturalist and crossdomain.
- **Positive salts report coverage of the known effect.** Naturalist's amplifier device is invisible to null salts
  and visible only here.
- **Nulls.**
  - Synthetic families: self-similar g = 0 salts (naturalist), which have no world-switch.
  - Real data: plasmodes, only after the d̂ exam passes (upper bound ≤ 0.2 at 40 per arm).
  - Label-permutation nulls are dropped.
- **Feature-embedded leaks are excluded from the plane's claims.** Crossdomain's toy shows plasmodes cannot see
  them. They go to adapter-side provenance tags plus a human legitimacy ruling.
- **Salts never enter L6.** There is one circuit-breaker bit per campaign.
- **Test.** DEV-1 wrapped in the device on 30 synthetic tasks: self-similar nulls certify ≥ 80% of the time. Amplifier
  at f = 0.1: pooled coverage flags it in ≥ 90% of runs.

**E3. `draw()` with a per-purpose input table.** Purposes that are harmless to know after the fact (target, seeds,
subsample, gate, environment pair) use the beacon only. Salt assignment uses the secret S plus the beacon B.
- The Kernel checks the hash, that the round r is named in the commit, the order anchor < r < first touch, and
  H(S) against the commitment.
- **Tests:** forged order rejected 100%; seed shopping ≤ α when drawn; grep for S finds 0 hits, builder scratchpads
  included. [inferred]

**E4. Resweep with an honest-corpus screen, reason-typed STALE, graded clawback, and provisional vs seasoned VDS.**
- A new check applies retroactively only after it shows 0 flips on ≥ 500 honest histories.
- Clawback follows evidence-margin edges, not ancestry.
- Seasoned VDS replaces my escrow idea.
- **Test:** the pre-SPLIT archive gives ≥ 1 flip on the affected tasks and 0 elsewhere.

**E5. Exclusion-limit bounded-null certificates** (my H1 plus historian and institutionalist).
- Fixed at Stage 1: δ_eq, the reimplementation spec, and the injection sizes.
- The certificate reports the expected limit with bands, the observed limit, and a clipped limit.
- The positive control is a positive plasmode on the same pipeline, tagged `draw(positive_control)` with public
  inputs, kept separate from salts.
- **Test:** 1,000 broken reimplementations yield 0 bounded nulls.

**E6–E8 (co-signed; specs on the board).**
- **E6:** the Kernel reliability diagram (crossdomain plus my H3), with cluster bootstrap.
- **E7:** per-channel attempt telemetry, as a monitor with a non-inferiority veto and a displacement index.
- **E8:** the Prosecutor, after E1–E2: a second signal, pre-registered counter-claims, and α capped at 0.2×.

## 2. Where I still dissent
1. **The k-world Bayes-ceiling control in Layer A.** Historian and naturalist keep it "as a bound". My own toy shows
   it is weak: 27% cheat and 29% caught for a tuned device. It is dominated by McNemar on the same replays. Replace
   it. [toy-verified]
2. **Real-data null plasmodes carry an inherent tell:** "nothing beats the baseline on dev". Our real tasks are
   *selected* to be improvable (16.12's 10–90% filter), so this tell is stark. Only prevalence matching fixes it, by
   adding genuine null tasks to the real pool. I expect the d̂ exam to fail on real data without it. [inferred]
3. **Capture–recapture N̂ as a release gate.** It is a floor, so report it but never gate on it. [inferred]
4. **Determinism is now a security property.** I dissent from treating it as a convenience. A claim class chosen by
   the program's own behaviour is a lever the cheater controls. The class must follow from pinned entropy, not from
   observed behaviour. [inferred]

## 3. Dropped
- **Mine:** H5 baseline blinding, H7 strong inference (parked), H8 expert priors, H9 α-market (parked), H12
  compression currency, escrowed credit, PROVISIONAL-SEALED, and my wrong p1 claim of "about 1 cheat in 10".
- **Board:** label permutation as a leak control or as a null construction, joint-permuted nulls, T2 optimistic
  acceptance (bisection kept), the T5 Gao stop rule, an attempt-rate gate, P4b as written, and any claim that an
  output control "covers the label-leak class".

## 4. Boldest idea that survived
**Negative results as a product: exclusion-limit certificates.** Today the system can only say "not confirmed".
With Stage-1-fixed positive controls and expected-limit bands, a Claim Audit can publish "the effect is < δ_eq at
95%, with demonstrated sensitivity". That is what physics publishes, and what psychology never managed to reward.

Runner-up: the defeat device itself. It deserves to become a **standing red-team primitive**. Every new audit must
publish its result against a device that conditions on that audit's own seam before it is adopted.

## 5. Owner decisions required
1. Call budget for Layer B salts and Prosecutor trials.
2. Null-task prevalence vs the 16.12 admission filter.
3. An entropy-pinned sandbox as a substrate requirement.
4. Beacon via HTTPS fetch, or dice only.
5. A second-person blinder, or print "blinder = experimenter" on certificates.
6. New statuses and kinds: STALE-RULE, STALE-DATA, REJECTED-INTEGRITY, `bounded_null`, exact vs tolerance class.
7. Seasoned VDS as the L6 objective.
8. Human legitimacy rulings on feature provenance.
9. "Defeat device first" as an adoption rule for every audit.
