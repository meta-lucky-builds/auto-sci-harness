# Evidence economics: upgrade proposals for the Evidence Kernel

Lens: what does each held-out number *cost*, who can see it, and which guarantee survives an adaptive adversary.
Labels: **verified** (I read the code or ran it), **inferred** (follows from the code plus standard theory), **guess**.
I cited every reference from memory and checked none of them online in this pass. Ones I am less sure of are marked *unverified*.

**Two findings that drive the ranking:**
- **F1, verified (toy simulation, not the Kernel code path).** Held-out data was 500 random labels, so the true effect is 0. The adversary submitted 49 random classifiers, saw each one's held-out accuracy (a continuous number, like the effect/BCa interval in today's certificate observations and the `clip(relative held-out improvement)` term in `evolve/evaluate.py`), then claimed the majority vote of the ones above 0.5. Under Bonferroni over 50 claims at α = 0.05, the false-certification rate was **55.5%** (400 reps). With binary feedback only (certified/rejected) and the transcript-count correction from P1, it was **0.5%**. So Bonferroni over registered claims is a valid adaptive guarantee only if nothing continuous flows back from the target to agents.
- **F2, verified by reading `predictions.py` and `kernel.py`.** VDS can be earned with failures. `p0 = p_null` is measured *after* the outcome and is recorded for REJECTED claims too. A miss scores `log2((1−q)/(1−p0))`, which is positive whenever q < p0. Example: a tiny-δ claim (p_null ≈ 0.5) registered at q = 0.05 that fails K7 earns **+0.93 bits**. At p_null = 0.6 it earns +1.25 bits. Registering hopeless, trivially safe claims therefore *raises* VDS. This contradicts GOAL §4.5 ("can't be gamed by safe claims").

---

## P1 (rank 1). Held-out information budget: count transcripts, not claims

**Idea.** A target's false-certification risk depends on *how much information about the held-out sample has reached anything that chooses later claims*. Counting claims misses this. Every release of target-derived data to an agent-visible surface (prompt, export, memory, evolution score, report an agent reads) is logged as a typed **release** with a finite alphabet. K4's multiplicity becomes the number of distinct (claim index, transcript prefix) pairs that the release policy allows. The default policy is a **verdict embargo**: agents see "submitted" and nothing else until the target retires, and the multiplicity is exactly today's claim count.

**Guarantee (description-length / union-bound argument, Dwork et al. 2015).** Assume three things:
- The analyst's t-th claim is any function of (a) its own randomness U, independent of the held-out sample S, (b) public data, and (c) the released prefix r ∈ 𝒯_{t−1}.
- The release set 𝒯_{t−1} is fixed in advance by the policy.
- Each fixed claim's test satisfies P_S(certify | H0) ≤ α/N, where N = Σ_{t≤k} |𝒯_{t−1}|.

Then P(∃t: H0_t true and claim t certified) ≤ α. Proof: condition on U. Every claim the analyst could make is one of N fixed claims, so the union bound applies. This holds for **any** adversary, including one that is not computationally bounded. (Verified: standard argument.)

N under each release policy:
- Embargo: N = k.
- Binary verdicts with at most r certifications per target: N(k, r) = Σ_{t=1..k} Σ_{j≤min(r,t−1)} C(t−1, j). For r = 1 this is k(k+1)/2.
- Continuous release: N is unbounded, so the target is marked **adaptively exposed**.

**What it changes.**
- GOAL §4.3 (K4, K6), §L6 step 1 and the evolution score, and §10.
- It converts the STATUS limitation "adaptive test-set reuse: Bonferroni … practical defence, not a guarantee" into a theorem with a named assumption: the release log is complete. K9 and the export leak audit become the enforcement for that assumption.

**Systems view.**
- *Breaks:* the evolution score's continuous held-out term. It must become either a binary certified/replicated score or a dev-only score. Certificates whose observations an agent could read must have their numbers sealed until retirement.
- *Improves:* R2-2 together with P2. Certificates issued on evolve-split tasks during a campaign are currently *selection-contaminated* (inferred). Under this policy that status becomes explicit.

**Build.**
- `kernel/releases.py` (~120 LoC):
  - `Release{target, channel, alphabet: enum, ledger_seq}`
  - `transcript_bound(policy, k, r) -> int`
- New ledger entry kind `release`. `vault.read` with purpose `report`/`export` must cite a release.
- In K4, `m = max(n_comparisons, |family|, transcript_bound(...))`. An `exposed` target gets K6 status `fail: adaptively exposed`, and the claim is routed to a fresh target.
- `evolve/evaluate.py` drops the held-out relative term or replaces it with its sign only, counted as a release.
- Milestone: M4.x, 2–3 days. 100% branch coverage on `releases.py` is easy because it is pure counting.

**Acceptance test.**
- `test_adaptive_reuse.py` reproduces F1 against the real `k4_statistics` + `k6_held_out`: n = 500 null labels, k = 50, α = 0.05, 2,000 reps.
- Pass criteria: false-certification rate ≤ 0.05 + 3·SE (≈ 0.065) for the embargo and binary policies, *and* ≥ 0.30 for the "continuous" policy, so the test can fail and is shown to detect the attack.
- Pin F1's 55% as a regression.

**Costs.**
- Binary policy with r = 1 at k = 20: N = 210 vs 20. The one-sided z threshold goes 2.81 → 3.49, so intervals are about 24% wider.
- The embargo costs nothing statistically, but agents lose held-out feedback. That is the intent: dev/CV is the learning signal.

**Might fail:**
- Side channels (timing, crash-on-held-out-inputs, stdout length) are releases nobody logged. The assumption is then false, and silently so.
- Humans who read numbers and then steer the system are adaptive analysts outside the threat model. This must be stated as an assumption.

**Prior art.**
- Dwork, Feldman, Hardt, Pitassi, Reingold, Roth, "The reusable holdout", *Science* 2015, and "Generalization in adaptive data analysis and holdout reuse", NeurIPS 2015 (the description-length bound).
- Blum & Hardt, "The Ladder", ICML 2015.
- Russo & Zou, AISTATS 2016 (mutual-information bias bound).
- *New:* applying the bound as a mechanical, ledgered multiplicity inside a certifying kernel, with release channels as typed entries.

---

## P2 (rank 2). Per-target α-ledger: pre-allocated slots, retirement, refresh

**Idea.** Each held-out target gets an **α-account** at registration: a total budget α_T and a human-declared number of slots S_T. The account is a protected, human-authored setting. A claim *reserves* a slot when it is pre-registered and is tested at α_T/S_T, whatever its order. When the slots run out, the target **retires**: no more certificates. Further evidence needs fresh data, such as a new split, a later time window or an external dataset.

**Guarantee.** FWER per target ≤ S_T · α_T/S_T = α_T, by Bonferroni over a fixed-size family. This is valid adaptively when combined with P1, with N in place of S_T when releases are non-empty. The per-claim level is now *constant*, so two identical harnesses get identical verdicts whatever their evaluation order. That fixes R2-2 by construction, not by convention (verified: the arithmetic is trivial).

For streams (§6.4, temporal-reveal mode), a claim that is re-checked as each window arrives uses an anytime-valid test instead of spending a slot per look. One candidate is a betting-based confidence sequence (Waudby-Smith & Ramdas 2024): Ville's inequality gives P(∃t: μ ∉ C_t) ≤ α, so peeking is free within a slot.

**What it changes.**
- GOAL §4.2 `analysis_plan` gains `slot`. The K4 row changes.
- STATUS items:
  - "test-set wear": wear becomes an explicit, visible budget instead of creeping strictness;
  - "Bonferroni rather than Holm": drop Holm from the GOAL text. Holm needs all p-values at once and does not apply to an online family (inferred);
  - small test sets: see the preflight below.
- **Preflight (new, cheap).** At registration, before any slot is spent, the Kernel computes the best achievable K7 bits and K4 lower bound for this target size from public information (n, metric range, planned seeds). For a δ-improvement on accuracy with n examples, the SE is at most 0.5/√n. A claim that cannot reach the thresholds even with a perfect subject is refused as **uncertifiable here**. Example: iris, with 45 rows and 1 example of headroom. Its slot is not consumed.

**Build.**
- `kernel/alpha.py` (~150 LoC): `open_account(target, alpha, slots)` is a human-only approval node; also `reserve(claim_hash)`, `level(claim_hash)`, `retire(target)`.
- Ledger kinds `alpha_open`, `alpha_reserve`, `alpha_retire`.
- K4 reads `level()` instead of `max(n_comparisons, |family|)`. `related_targets` merges accounts for byte-identical labels (closes R1-F4 the same way).
- `preflight_power(claim, n)` is pure arithmetic.
- 2 days; it touches `checks.py` K4 and K1.

**Acceptance tests.**
1. Order invariance: certify the same claim pair in both orders and get bit-identical verdicts. Run this over 1,000 random claim orderings: 0 differences.
2. With 10,000 simulated null campaigns, each spending all S_T = 20 slots on adaptively chosen claims under the embargo, the per-target FWER is ≤ 0.05 + 3·SE.
3. Preflight refuses a perfect-subject claim on a 45-row target at δ = 0.05 and accepts one on a 1,000-row target.

**Costs.**
- Rigid: an unused slot wastes power, and a campaign that under-declares slots gets locked out (it fails closed).
- Equal splitting is suboptimal. Weighted or graphical recycling (Bretz et al. 2009) recovers power, but only under pre-specified structure, and adds complexity to the Kernel. Defer it.

**Might fail.** Humans set S_T too large "to be safe", which makes every claim weak. The run report should show power per slot.

**Prior art.**
- Bonferroni and α-spending (Lan & DeMets 1983).
- Online FWER: Tian & Ramdas 2021, "Online control of the familywise error rate" (*unverified* venue).
- Bretz et al., *Stat Med* 2009.
- Waudby-Smith & Ramdas, *JRSS-B* 2024.
- *New:* only the engineering: budget as ledger state, retirement as a first-class event, and preflight refusal.

---

## P3 (rank 3). Make VDS an e-process, split from forecasting skill

**Idea.** Define each resolved prediction's gain as `log2 LR_t`:
- p0_t is fixed **at pre-registration**: the reserved slot level α_slot from P2, an upper bound on P(certify | no effect).
- K1 requires q_t ≥ p0_t.
- LR_t = q/p0 on certification and (1−q)/(1−p0) otherwise.

Then 2^{VDS_t} = Π LR_t is a nonnegative supermartingale under the global null "no registered claim has a real effect". Separately, measure **skill** as information beyond a pinned reference forecaster.

**Guarantee.** Suppose P(certify_t | F_{t−1}, H0) = π_t ≤ p0_t (which K4 plus P1/P2 provide) and q_t ≥ p0_t. Then E[LR_t | F_{t−1}] = π_t·q/p0 + (1−π_t)(1−q)/(1−p0) ≤ 1. The derivative in π is positive iff q > p0, and at π = p0 the expectation is exactly 1. **Ville's inequality** then gives P(sup_t VDS_t ≥ log2(1/α)) ≤ α (verified: algebra).

So "VDS ≥ 10 bits" means the system's track record rejects "everything it certifies is noise" at 2^−10. Optional stopping and adaptive choice of the next claim are covered because q and p0 are fixed before each outcome (that is what pre-registration buys). Expired predictions resolve as misses, as today.

This removes F2:
- p0 is no longer measured post hoc;
- q < p0 is refused;
- a miss can never score positive.

**Skill.** Use a logarithmic market scoring rule against a reference forecast p_ref_t that the Kernel computes at registration from public/dev evidence. The natural reference is the protected claim rule's own dev-based q, written `q_dev`. Skill = Σ log2 of the ratio of the system's forecast probability to the reference's, with each side's probability of the observed outcome. Its exponential is an e-process for "no information beyond the reference" (Henzi & Ziegel 2022; Choe & Ramdas 2024 compare forecasters this way). The two quantities answer different questions:
- VDS: does the system produce real effects?
- Skill: does it know which claims will hold?

The headline experiment (§6.4) should report both, with confidence sequences.

**What it changes.**
- §4.5 and §6.3, and it closes §13 Q2: the scoring rule is log, with weighting fixed by the LR.
- K1 gains the q ≥ p0 rule.
- *Honest limit:* VDS still rewards *obvious* true improvements against a weak comparator. The defence is K5 and P5, not VDS.

**Build.**
- Rewrite `predictions.py` (~90 LoC): `p0` is read from the `alpha_reserve` entry, plus `skill()`, `ville_threshold(alpha)`.
- Add a red-team test pinning F2.
- 1 day.

**Acceptance tests.**
1. F2 regression: a tiny-δ claim at q = 0.05 is refused at K1. Any miss has gain ≤ 0.
2. Simulate 10,000 null campaigns of 200 claims each, with adversarial q choices (any q ≥ p0, chosen adaptively from past outcomes). P(sup VDS ≥ log2 20) ≤ 0.05 + 3·SE.
3. Under a planted 30% real-effect rate, mean VDS after 200 claims is > 0 (the metric has power).

**Costs.** The supermartingale property needs π_t ≤ p0_t *conditionally*. If K4's test is anti-conservative (for example, the normal approximation at small n), the guarantee leaks. Treat this as an assumption, tested by P4-style calibration of K4 itself.

**Prior art.**
- Ville 1939.
- Shafer & Vovk; Ramdas, Grünwald, Vovk & Shafer, *Stat Sci* 2023.
- Vovk & Wang, *Ann Stat* 2021.
- Hanson's LMSR (2003/2007).
- Gneiting & Raftery, *JASA* 2007.
- Henzi & Ziegel, *Biometrika* 2022 (*unverified* details).
- Choe & Ramdas, *Oper Res* 2024 (*unverified* venue).
- *New:* tying p0 to the Kernel's own slot level so the objective inherits the certification guarantee.

---

## P4 (rank 4). K3 as a reliability demonstration, not one lucky replay

**Idea.** Every execution of a claim's program is a reliability trial: planned held-out seeds, dev folds of the final subject, and replays. K1 requires a declared maximum failure rate ε_max and confidence β. K3 replays sequentially and **stops at the first failure** (reject). It passes after n_req = ⌈ln β / ln(1−ε_max)⌉ consecutive successes, with earlier evaluation runs counted towards n_req. A failure here means a crash, timeout, non-scorable output, or an out-of-tolerance score. The certificate states "failure rate ≤ 1 − β^{1/n} at 1−β".

**Guarantee.** If each run fails independently with probability p ≥ ε_max, then P(pass) = (1−p)^{n_req} ≤ β. This is the zero-failure "success-run" test, equal to the optimal SPRT boundary when no failures are allowed. A 50%-flaky program passes 5 trials with probability 3.1% (it passes today's single replay with 50%) (verified: arithmetic).

**What it changes.** K3 row; closes the R1-F3 residual. Live run 2 (a timeout in 1 of 3 seeds) becomes a principled REJECT with a stated reason.

**Build.**
- `kernel/reliability.py`: `trials_needed(eps, beta)` and `zero_failure_verdict(outcomes)`.
- K3 loops over hash-derived replay seeds (extending `replay_seed`).
- Declared ε_max is capped by the Kernel config: ε_max ≤ 0.2 for T2.
- 1 day.

**Acceptance test.** With a planted p-flaky program for p ∈ {0.5, 0.2, 0.1}, ε_max = 0.1, β = 0.05 (n_req = 29) and 10,000 trials: the pass rate is ≤ 0.05 for every p ≥ 0.1, and ≥ 0.90 at p = 0.001.

**Costs.**
- Compute: 29 replays of one seed at ε = 0.1. That is too expensive for slow experiments, so budget presets of ε = 0.3 (n = 9) should be stated in the certificate.
- Flakiness is often *correlated* with load (live run 2: "machine overloaded"), which violates independence. Spread replays across time and sandboxes, and list "independent failures" as an untested assumption.

**Prior art.** Standard reliability demonstration testing; Wald 1945 SPRT. Nothing new except its placement in K3.

---

## P5 (rank 5). Risk as pre-registered surprise against a pinned reference

**Idea.** K7 asks whether success was improbable without the claimed thing. Make that question explicit per evidence class, always evaluated **at registration** against a pinned, adapter-owned reference, never post hoc:
- **Data claims:** p_null is computed from the *dev-predicted* held-out SE (n, dev variance). This fixes the post-hoc dependence of today's K7, whose SE comes from the held-out BCa interval. Replace the normal approximation with the exact binomial for accuracy-type metrics.
- **Negative/equivalence claims (§13 Q5):** risk = −log2 P_prior(|effect| < δ), where the prior is the empirical distribution of *certified* effect estimates in the ledger for the same metric and comparator class. Only certified entries count, so agents cannot pad it. With fewer than M entries, K7 fails closed as today.
- **Computational/formal claims (§13 Q3):** a pinned cheap reference prover or solver (fixed tactic set or time budget, inside the adapter) is run on the statement. If it succeeds, the claim earns **0 VDS bits**. Certification is unaffected, because truth is T0/T1's job and importance is not the Kernel's (§4.6).
- **Simulated claims:** reference = a pinned coarse surrogate, such as a coarse mesh or prior design.

**Guarantee.** None beyond P3's: this decides *credit*, not truth. That is the honest framing. K7 for non-statistical classes is an anti-Goodhart device for the objective, not an epistemic check.

**Build.**
- `risk_reference()` hook on the adapter interface.
- A K7 split: `k7_preregistered_risk` for the data class; VDS credit only for the other classes.
- 2–4 days per class. The formal-class reference waits for the Lean adapter.

**Acceptance tests.**
- A trivially true lemma that `decide` proves gets 0 bits.
- A planted "does not improve by 0.9" claim fails K7.
- The dev-predicted p_null matches the held-out-measured one within 20% on the suite's 13 tasks (inferred to be achievable).

**Might fail.** The ledger prior is circular early on, because there are few entries. Reference provers define "obvious" narrowly.

**Prior art.**
- Popper's content/risk.
- Empirical-Bayes effect priors (Efron).
- *New:* little beyond the pairing.

---

## What NOT to do

1. **Alpha-investing / LORD / SAFFRON for certificates.** They control mFDR/FDR, which is a statement about a *portfolio*, while a certificate is an individual promise. Earning α back on discoveries lets an agent farm easy true claims to fund risky false ones. Acceptable only for an exploratory SUPPORTED tier (Foster & Stine 2008; Javanmard & Montanari 2018).
2. **Thresholdout / DP noise on these test sets.** The guarantees need n ≫ √(queries)·polylog. At n = 45–1,000 the Laplace noise needed swamps δ. Binary embargo (P1) gives a stronger, simpler guarantee at our scale (inferred).
3. **E-values for the fixed-n held-out test.** With no optional stopping, the Neyman–Pearson test is more powerful. Use e-processes only where looks are sequential (streams, VDS, replay).
4. **Holm in an online family.** It needs all p-values simultaneously. Remove it from GOAL as a "deviation".
5. **Bayesian posterior certificates or MI-based risk scores.** The prior or density estimator becomes the attack surface, and neither is stdlib-robust.
6. **Retrying K3 and averaging.** This is shopping. Only zero-failure (or a pre-registered k-of-n) rule is valid.
7. **Optimising VDS directly in L6 without the gate.** Any scalar the evolver sees is a release (P1).

---

## Proposed GOAL.md diff (top 2)

**§4.3 table, replace the K4 row:**
> | **K4 Statistically valid** | Plan was followed; seeds ≥ n; test matches design; the claim holds a reserved slot of its target's α-account and is tested at that slot's fixed level α_T/S_T (multiplied down by the target's transcript bound when releases exist, §4.3a); paired seed CI and test-example CI exclude 0 by ≥ δ | noise, winner's curse, evaluation-order dependence |

**§4.3, new subsection 4.3a "Held-out information budget":**
> Every transfer of target-derived information to an agent-visible surface (prompt, export, memory, evolution score, agent-read report) is a ledgered **release** with a finite alphabet. The default is **verdict embargo**: agents learn nothing from a target until it retires. K4's multiplicity is the number of (claim, transcript-prefix) pairs the release policy permits (k under embargo; Σ_t Σ_{j≤r} C(t−1,j) for binary verdicts with ≤ r certifications). By the union bound this caps false certification at α against any adaptive analyst whose randomness is independent of the held-out sample, *provided the release log is complete* (an assumption listed on every certificate; enforced by K9 and the export audit). A target that has released continuous values is **adaptively exposed**: K6 fails on it.

**§4.5, add:**
> Each held-out target has a human-opened **α-account** (α_T, S_T slots) in the ledger. Pre-registration reserves a slot; a target with no free slot is **retired** and further claims need fresh data (new split, later window, external set). Before reserving, a **power preflight** refuses claims that could not pass K4/K7 even with a perfect subject on this target size ("uncertifiable here"); the slot is not spent. Streams use anytime-valid confidence sequences within one slot.

**§5 L6 step 8 / evolution score, replace "0.5·clip(relative held-out improvement)":**
> The evolution score uses only released verdict bits (certified, replicated) and dev evidence; continuous held-out values never reach the evolver (§4.3a).

**§10, add invariants:**
> 17. No target-derived information reaches an agent-visible surface except as a ledgered release counted in K4's multiplicity.
> 18. A claim's significance level is fixed at pre-registration (its α-slot) and does not depend on the order in which claims are checked.

**§13:** delete "Bonferroni rather than Holm" as a deviation; Q2 answered by P3 (log rule with LR against the slot level).
