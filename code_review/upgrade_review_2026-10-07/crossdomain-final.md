# crossdomain — final (round 2)

## Evidence status
**Nothing below ran on the real system.** "Verified" means only that a toy pure-Python simulation ran (script named)
or that I read the code. Every test is a proposal for the real Kernel.

## 1. Endorsed mechanisms, ranked

### E1. Audit Plane (merged: historian, institutionalist, maverick, naturalist, crossdomain)
**Layer A: runs per claim, zero model calls.**
- *Exact-determinism claims:* institutionalist's **output-difference non-interference test**: replace every
  content-matched private label copy by y′ ~ p_ref(x), re-run, and flag any byte difference in the predictions. Toy:
  catches a stealth cheat at f = 0.02 (0.997), with 0 honest false alarms (`institutionalist/noninterference.py`).
- *Tolerance-class claims:* a k-world Bayes-ceiling bound, with its stealth margin printed on the certificate.
- Also: F4 check, answer-key replays, amended dispersion forensics, T7 escalation, world-fingerprint equality.

**Layer B: sealed salt stream.**
- Order: positive salts first, scored by **effect-size coverage**, which is the only defence against naturalist's
  amplifier device (`naturalist/selfsim.py`).
- Then **self-similar nulls** from the same generator family with g = 0, or fitted plasmodes for real-data tasks.
- Salts are assigned by `draw(salt_assignment)` (secret plus beacon). They stay in the **research stream only**.
- Reversing entries: salt outcomes never reach VDS, L6, the export or memory.

**Layer C: decoys plus attempt telemetry, as a monitor.** It may veto only on sandbox-total channels, using a
non-inferiority margin.

**Reporting.** d̂ = TPR − FPR (strongest spotter, 40 + 40 presentations), upper bound ≤ 0.2; report
r̂/(1 − d̂_upper). Nulls detect only gross failure.

**Test.** DEV-1 and SPLIT in the defeat device (f = 1.0, 0.05): non-interference flags 4/4, the permuted control 0;
0 flags on 20 honest exact-class claims; self-similar nulls certify DEV-1 ≥ 80%.

**Kill rule:** > 10% of honest programs non-deterministic under pinned seeds → the exact test is not the workhorse.

### E2. Feature-provenance check for leaks baked into the raw data (my fix)
**The toy result.** Null plasmodes do **not** catch a leak column baked into the raw data, such as an ID sorted by
class or a post-outcome variable. The leaky program's gain over baseline is −0.112, or 0.000 when the plasmode is
refit on all features (`crossdomain_plasmode_featureleak.py`).

**Resolving my disagreement with maverick** (`crossdomain_leak_origin.py`): a *pipeline* leak (data prep re-derives
it from the label file) **is** caught (gain +0.199, flagged 1.00), so maverick is right there. A *raw-data* leak is
not (−0.112, flagged 0.00).

**Why no output test can fix it:** by outputs alone, a raw-data leak looks exactly like a genuinely predictive
feature. Legitimacy is *semantic* (Kaufman et al. 2012, recalled).

**Spec.**
- Every registered dataset feature carries an availability-time and provenance tag.
- K1 refuses features tagged post-outcome or identifier.
- The adapter runs a train/test near-duplicate audit and a single-feature-dominance screen: a feature's dev mutual
  information with y must not exceed every other feature's by more than X.
- A flagged feature goes to a **human legitimacy ruling** at an approval node, never to an automatic verdict.

**Test.**
- 10 planted raw-data leaks (5 ID-sorted, 5 post-outcome): Layers A and B flag ≤ 2/10, which confirms the limit. The
  provenance screen routes ≥ 8/10 to a human.
- No more than 2 of 20 honest tasks routed falsely.

### E3. Resweep (merged: institutionalist, historian, naturalist, crossdomain T1)
- Trigger: a list-level Kernel change or a museum check.
- **Screen first.** The check must produce 0 flips on ≥ 500 honest histories (real archive plus 16.9-generated,
  each rare category ≥ 10%). The rule-of-three bound is about 0.6%.
- Statuses change only on an **output-keyed** re-check of the stored B-sample: `STALE-RULE`, `STALE-DATA`,
  `REJECTED-INTEGRITY` (never from a rule change alone), `NOT-RECHECKABLE` (capped at CERTIFIED-INTERNAL).
- Graded, evidence-local clawback.
- **Tests:** 5 planted F4 certificates → 5/5 STALE-RULE; pre-SPLIT archive → ≥ 1 flip on affected tasks, 0
  elsewhere; an over-broad check is blocked. Kill rule: 0 flips over 10 changes.

### E4. `draw()` purpose table (maverick, naturalist, institutionalist, crossdomain)
The mode is fixed by `purpose` in a closed enum:
- **Beacon mode** for anything frozen before the draw (seeds, subsample, target, gate tasks, positive control,
  check parameters). The result is public at once, so bundles can be rerun immediately.
- **Sealed mode, S‖B,** only for salt assignment. Its certificates are PROVISIONAL-SEALED until the reveal.

The Kernel checks the hash, that r is inside the commit, and anchor < r < first touch.

**Tests:** forged ordering and mode misuse refused 100%; over 1,000 simulated claims, self-chosen seeds give > 2α
and drawn seeds ≤ α; 0 grep hits of S in agent logs.

### E5. T7 escalate-never-reject
- S_max is frozen at campaign open: set by a human, then the 99th percentile of replicated surprisal once ≥ 30
  replications exist.
- A bunching monitor runs at S_max.
- **Test:** planted true effects at 3×, 10× and 30× give 0 false rejections.

### E6. Smaller items, all buildable
- **K3 bisection** over checkpoint hashes (deterministic CPU only).
- **T4 defeater count** as a monotone tier-lowering map. Test: opening a defeater never raises a label (10⁴ cases).
- **Capture–recapture as a floor**, plus seeded recovery s/S. Test: N̂ ≤ the true count in ≥ 8/10 repeats.
- **Fraud proofs only as pre-registered counter-claims.** Toy: subset search "breaks" 96% of honest certificates at
  1.5δ (`crossdomain_subset.py`).

## 2. Where I still dissent
- **Maverick's B3 claim** that plasmode nulls catch "in-data leaks" ≥ 8/10. That holds only for pipeline leaks. The
  spec must split the classes, or it overclaims.
- **The k-world ceiling as the primary tolerance-class control.** Maverick's own toy shows a tuned device cheats on
  about 27% of tasks and is caught on under a third. I would rather make *determinism the default*: pin seeds and
  BLAS threads, and treat tolerance class as the exception the claim must justify.
- **Prevalence matching vs the 16.12 filter:** I favour prevalence matching (owner's call).
- **Live escrow** (maverick): simulation only until H3 cells have about 30 resolutions; before that, P_surv is a
  guess multiplied into L6.

## 3. Dropped
T2 optimistic acceptance; subset-search fraud proofs; T5's R(D) stop rule (the sentinel stays); held-out-permuted
null salts and the permuted "GAAR" as a guarantee; capture–recapture as a gate; T13 (Oulipo); T6 diversity as a
priority (its machinery is reused for fingerprint equality); the attempt-rate fitness gate; bankroll and α-markets;
the fixed 80% alarm.

## 4. The deepest dot-connection that survived
**Commit before the verifier is determined.**

Three survivors are one principle in three dimensions:
- **Time:** doping B-samples and art forensics become resweep, where future checks apply to past evidence.
- **Identity:** LIGO's envelope, the IT-audit test facility and mystery shoppers become salts.
- **Value:** Hooke's anagram, risk-limiting audits and the kleroterion become `draw()`.

In each, the optimiser freezes its artifact while its verifier is still unknown, so it cannot fit that verifier.
The board also found the principle's boundary. Maverick's defeat device shows that an artifact *carrying* hidden
information can detect the verifier at run time. My raw-data-leak toy shows that some defects are *semantic*, so no
hidden output verifier can see them. Beyond that boundary only information-flow control and human judgment work. Those are invariant 2, the release
ledger, and E2. This is Thompson's "trusting trust" again.

## 5. Owner decisions required
1. Layer B call budget.
2. drand fetch vs dice.
3. Who holds S ("blinder = experimenter" otherwise).
4. Prevalence matching vs the 16.12 filter.
5. New statuses (PROVISIONAL-SEALED, STALE-RULE, NOT-RECHECKABLE).
6. Graded clawback; seasoned vs provisional VDS.
7. Feature-provenance tagging and the human legitimacy rulings; the value of X.
8. Determinism-by-default.

