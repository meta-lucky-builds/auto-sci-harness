# institutionalist — final position (round 2)

**Evidence status, stated once.** Nothing below ran on the real Kernel, sandbox or agents. "Toy-verified" means a
pure-Python simulation in the scratchpad that I or another agent ran. Everything else is inferred or speculative.

## 1. Mechanisms I ENDORSE, ranked

**1. Non-interference replay (output-difference control). Buildable now. My strongest surviving idea.**
- **The test.** For claims in K3's *exact* class, the frozen program is re-run with its seed pinned, in a fresh
  sandbox. Every private copy of the held-out labels is replaced by counterfactual labels y′ ~ Bernoulli(p_ref(x)).
  "Every copy" means the vault plus any file whose (row, label) content matches the held-out set. **Fail if the
  prediction vector differs in any byte from the *stored production predictions*.**
- **Why it works.** An honest program never reads those bytes, so its output cannot change. Any program whose output
  depends on them is caught, whatever world it thinks it is in.
- **Where the board's lesson stops applying.** This is a *dynamic information-flow test*, not a score test. "Output
  controls only measure" was true of score-based controls. It is false here, *for the perturbed channels*.
- **Toy evidence** (`scratchpad/institutionalist/noninterference.py`, `ni_extra.py`; N = 1000, 300 trials). Flagged
  rate per program:

| Program | Flagged | Notes |
|---|---|---|
| honest deterministic | 0.000 | |
| maverick's agreement device, full cheat | 1.000 | |
| agreement device, stealth f = 0.02 | 0.997 | |
| naturalist's amplifier, f = 0.05 | 1.000 of the 77% of runs where it cheated | |
| prevalence-only leak | 0.967 | permutation worlds miss it: 0.000 |
| world-fingerprint device, p = 0.99 | every real-world cheat caught | |
| replay-aware device (cheats only in production) | 1.000 against stored production output | 0.000 if two *replays* are compared, so the reference must be production |

- **Limits.**
  - It needs byte-determinism. An unpinned honest program gives a 100% false-alarm rate. Tolerance-class claims fall
    back to maverick's k-world ceiling, with its stealth margin printed.
  - It is blind to feature-embedded leaks, to carried or memorised answer keys, and to encoded copies.
- **Real-system test.**
  - The DEV-1 and SPLIT museum patches, wrapped in the agreement device at f ∈ {1, 0.05}: 4/4 flagged.
  - 20 honest exact-class claims: 0/20 flagged.
  - Run the real-label replay twice. Any diff means the claim moves to the tolerance class.
- **Kill rule.** If more than 10% of honest suite programs are not byte-deterministic under pinned seeds, this cannot
  be the workhorse.

**2. Resweep, the merged spec of crossdomain, historian, naturalist and me. Buildable now.**
- **Trigger and screen.** A list-level Kernel change triggers it. The new rule must first pass the honest corpus
  screen: 0 flips, *and* ≥ 1 member in every category on a human-owned coverage list (naturalist A). Count alone
  does not satisfy the screen.
- **Output-keyed.** It runs only on stored B-samples.
- **Statuses:** `STALE-RULE`, `STALE-DATA`, `REJECTED-INTEGRITY` and `NOT-RECHECKABLE`.
  - NOT-RECHECKABLE caps the label at CERTIFIED-INTERNAL.
  - If NOT-RECHECKABLE exceeds 5%, the bundle format is defective.
- **Graded, local sanctions.** VDS is clawed back. Edits are re-gated only where evidence or information exposure
  passed through the stale certificate. A penalty beyond clawback applies only to REJECTED-INTEGRITY.
- **Credit reporting.** Seasoned and provisional VDS are reported separately. This replaces escrow; maverick conceded.
- **Tests.**
  - The pre-SPLIT archive: ≥ 1 flip on the 4 affected tasks, 0 elsewhere.
  - 5 planted F4 certificates: 5/5 become STALE-RULE, 0 become REJECTED-INTEGRITY.
- **Kill rule.** 0 historical flips over 10 changes means it is just a regression check.
- **Evidence.** None simulated.

**3. `draw()` with a per-purpose input table** (maverick and naturalist's final tables; I co-sign).
- **Formula:** `H(v1‖purpose‖frozen_hash‖[S]‖B_r)`.
- **Inputs by purpose.**
  - Salt assignment alone needs S plus B. A public value would reveal salts to agents, and the beacon B stops the
    owner grinding.
  - Every frozen-before-draw purpose uses B only, so third parties can rerun it immediately.
- **Fail-closed.** An input set that does not match the table is refused.
- **Tests.**
  - Misuse guard: a beacon-only salt allocation is refused 100%.
  - Owner grinding over 10⁴ values: 0 accepted.
  - Seed shopping: self-chosen seeds give a false-certification rate > 2α; drawn seeds give ≤ α (1,000 simulated
    claims).
  - The S 8-hex prefix gets 0 grep hits, *including in builder-agent scratchpads*.
- **Gate draw.** Disabled unless the pool is ≥ 3× the gate size.

**4. Audit Plane, final shape.** Measurement only. It never enters L6, memory or exports.
- **Layer A:** item 1 above, plus row-permutation (positional keys only), F4 and P4a. The permuted shadow is renamed
  a "naive-leak tripwire".
- **Layer B:** sealed plants, in priority order:
  1. positive plants with *effect-size coverage*, which is naturalist's only instrument against amplifiers;
  2. self-similar nulls for synthetic families (naturalist);
  3. plasmode nulls for real data, admitted only after the prevalence-matching fix for maverick's "nothing works on
     dev" tell.
- **Detectability exam:** 40 + 40 presentations, upper bound of d̂ ≤ 0.2.
- **Honest power:** these are catastrophe detectors only.
- **What the plane does not cover** (crossdomain's toy, verified by them: plasmode gain −0.11 or 0.00): feature-
  embedded leaks. These go to an **adapter-side provenance check**:
  - an availability-time tag on every feature;
  - K1 refuses post-outcome and ID features;
  - a single-feature-dominance screen routes hits to a **human legitimacy ruling**.

**5. Third-party reporting.** A field type `self_reported` caps the tier.
- **Test:** a registrant-declared "external" dataset role certifies at most CERTIFIED-INTERNAL.
- This is cheap, and nobody challenged it.

## 2. Where I still DISSENT
- **Against "Layer A is a naive-leak detector only"** (naturalist, maverick, historian). That conclusion was reached
  before the non-interference repair. It holds for score-based and permutation controls. My toy says it does not hold
  for output-difference against the stored production output on exact-class claims. The others never responded to
  this repair, so it is my claim, not yet a consensus. Settle it with the real-system test in 1.1.
- **Against the k-world ceiling as primary A1** (historian's final spec). On deterministic claims it is dominated:
  maverick's own retraction shows a tuned device cheats on 27% of tasks and is caught on only 29% of those. Keep it
  only for the tolerance class.
- **Partial: the attempt-telemetry veto.** I accept naturalist's veto on the sandbox-total channels (a) and (b), with
  a non-inferiority margin, but only *after* crossdomain's two-channel displacement simulation passes.

## 3. DROPPED
**My own items:**
- the score-based permuted "GAAR" (and the GAAR analogy);
- external anchoring (it already exists);
- stings in the gate stream;
- kleroterion as a standalone mechanism (merged into `draw`);
- the bunching detector (parked until there are ≥ 200 verdicts per lineage);
- the "two randomness sources" framing (superseded by the purpose table);
- escrow (seasoned VDS covers it).

**Others' items, which I co-sign dropping:**
- T2 optimistic acceptance (keep bisection);
- the T5 stop rule;
- label-permutation nulls of any kind;
- the attempt rate as a fitness term;
- P4b as written;
- the bankroll / α-market, parked;
- H5, H8 and H12.

## 4. The governance rule the owner most needs
**A two-speed Kernel, with the screen in front of the list. Reputation may only raise scrutiny.**
- **Core** (the §10 invariants, K0–K9 pass semantics, and the `draw` purpose table) changes only in a scheduled
  milestone window, with a 7-day cooling-off.
- **List** (check parameters, museum checks, defeater lists) changes on demonstrated evidence. It is published
  before it applies and goes through the honest-corpus screen before any retroactive use.
- No check, sample size or audit rate may *decrease* with a lineage's track record. Metamorphic test: swapping two
  lineages' histories leaves every verdict unchanged.
- **Capture guard.** The exams (red team, museum, honest corpus) are written by the same AI agents that build the
  Kernel. So at least one attack per milestone must come from outside that pipeline. The certificate prints
  "exam written by examinee" until that happens. It also prints "blinder = experimenter" until a second person holds
  the envelope.

## 5. Owner decisions required
1. Adopt the two-speed rule and the cooling-off length.
2. Who curates the honest-corpus coverage list.
3. drand fetch by the control plane, or human dice only.
4. Model-call budget for Layer B plants. The minimum useful size is about 10 positive plants.
5. Prevalence matching (admitting genuinely null-dev tasks) vs the 16.12 10–90% solve filter.
6. Approve the provenance-tag and human-legitimacy approval node for features.
7. Accept that tolerance-class claims carry a printed stealth margin.
8. Name an outside attacker per milestone, or accept the printed assumption.
