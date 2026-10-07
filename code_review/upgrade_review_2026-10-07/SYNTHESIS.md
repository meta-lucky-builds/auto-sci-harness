# Two-round synthesis: naturalist, maverick, historian, institutionalist, crossdomain (2026-10-07)

Evidence: every number below comes from pure-Python TOY simulations in the scratchpad (`maverick/`, `naturalist/`,
`institutionalist/`, `crossdomain_*.py`), or from history the agents checked on the web. **Nothing ran on the real
Kernel, sandbox or agents.** Round 1 was independent. Round 2 was a many-to-many board of about 50 posts over 2
passes, followed by final files.

## What the debate established (toy-verified)
1. **The permuted-label check is broken.** A 3-line "defeat device" cheats only when its leaked labels still agree
   with the features. It scored 1.0 and was flagged 0% of the time. Three agents also found it blind to leaks inside
   the features. All five agreed to drop it.
2. **Output checks only measure; prevention is the backbone.** A program that already holds leaked information can
   tailor its behaviour to any output-based check. Each design was beaten in turn: k-world controls (27% cheating,
   29% of that caught), null salts (the "amplifier" device got through 100%), and determinism checks (opted out by
   randomising). The backbone has to be information-flow prevention: K9 + release ledger + **double-entry
   reconciliation** of that ledger against the sandbox's own IO log + adapter **feature provenance** with a human
   ruling. (Endorsed by all five; no simulation backs the reconciliation yet.)
3. **Leaks baked into the raw data are invisible to every output test.** A leak column in the raw data showed
   −0.112 gain and was never flagged. Leaks that pipeline code re-derives from the labels are caught by null
   plasmodes.
4. **The strongest per-claim check is non-interference against the STORED production output.** Re-run the frozen
   program with every copy of the hidden labels substituted; any change in its predictions fails the check. It
   caught every device built during the debate, including a 2% stealth leak (99.7%), with 0 honest false alarms.
   Two amendments are needed:
   - an **entropy-pinned sandbox**, with the clock and urandom seeded by the Kernel, so a program cannot opt out
     by randomising;
   - a **paired McNemar dependence test** for genuinely nondeterministic claims. Against a tuned device it caught
     12%, 70% and 100% at 5%, 10% and 30% of rows copied, so a cheat of about +1.3 points can still slip through.
5. **Salts measure; positive salts matter most.** Null salts miss an amplifier device that cheats only when the
   honest gain is already large. Positive salts catch it by checking the reported effect against the known planted
   effect: 0.25, 0.53 and 1.00 at f = 0.05, 0.10 and 0.30, with honest false flags at or below 0.05.
   - Null salts on real data carry a "nothing beats the baseline" tell. The only fix is to add genuinely null tasks
     to the real pool, which conflicts with the 10–90% admission filter in 16.12.
   - The detectability bound d̂ = TPR − FPR is ≤ 0.2 at 40+40 presentations; ≤ 0.1 would need about 135 per arm.
   - At our throughput, salts detect gross failure only.
6. **Seed-subset "fraud proofs" are reverse p-hacking.** They break 96% of honest certificates at an effect of
   1.5δ. Challenges must be pre-registered counter-claims on freshly drawn data.

## The converged mechanisms (ranked by group consensus)
| # | Mechanism | Endorsed by | Status |
|---|---|---|---|
| 1 | **Prevention backbone**: release ledger + double-entry IO reconciliation + adapter feature provenance with a human ruling | 5/5 | design; needs simulation, then build |
| 2 | **Non-interference replay against stored output**, plus the entropy-pinned sandbox and the McNemar test for nondeterministic claims | 5/5 (maverick's amendments accepted by the others' final files) | toy-verified; buildable now |
| 3 | **Salt stream (the Envelope)**, positive salts first with an effect-match check; null salts as a gross-failure alarm; kept out of the L6 gate; detectability exam first | 5/5 | needs a model-call budget |
| 4 | **`draw()` with a per-purpose input table**: a public beacon (drand or dice) for anything frozen before the draw; human secret + beacon for salt assignment only; the secret appears nowhere agents can read | 5/5 | buildable now |
| 5 | **Resweep**: re-check archived certificates when the rules change, but only after a screen showing 0 false flips on ≥ 500 honest histories (upper bound about 0.6%). Four typed statuses (STALE-rule / new-data / uncheckable / integrity). Graded, reversible clawback. Provisional vs seasoned VDS instead of escrow | 5/5 | blocked on building the honest corpus (not yet scoped) |
| 6 | **Exclusion-limit "bounded null" certificates**: sensitivity frozen before the data, proved by a positive plasmode, lucky limits clipped as in physics. Turns negative results into a product | 4/5 | Kernel change (K1/K7); owner decision |
| 7 | Escalate-never-reject "too good to be true" tripwire; per-channel attempt telemetry as a monitor (veto only on fully enforced channels, with a non-inferiority margin); Kernel reliability diagram; Prosecutor agent (later, within limits) | 3–5/5 | later |

## Governance (independent convergence of historian and institutionalist)
- **The examinee writes the exam.** Every famous self-deception passed the procedures its own author designed.
  Here, AI agents write the Kernel, its tests, the museum and the honest corpus. So the owner must keep **at least
  one check no agent designed or can see**, and supply **at least one attack per milestone from outside the agent
  pipeline**.
- **Two-speed Kernel.** The core (§10 invariants, K0–K9 pass conditions) changes only in scheduled windows with a
  7-day cooling-off. The check-parameter list changes on evidence, screened against honest histories first.
  Changes that only tighten checks may merge at once.
- **Reputation may raise scrutiny, never lower it.** The xz-utils long con is the counterexample to doing
  otherwise.
- **Labels must not overclaim.** Historian (dissent) proposes CERTIFIED-…-UNREPLICATED until a second family or a
  human has replicated the result.

## Dropped by consensus (with the reason)
- Permuted-label checks and nulls: defeat device; blind to in-feature leaks.
- Optimistic acceptance: correlated challengers; provisional results get used as accepted.
- Seed-subset fraud proofs: reverse p-hacking.
- Attempt rate as a gate or fitness term: rejects every honest edit, and selects for quieter cheating.
- Gao/R(D) stop rule: invalid summation; the gate is too small.
- Fixed 80% positive-rate alarm: fires on a working preflight.
- Baseline blinding: the agent can recompute the baseline.
- Expert priors for K7: overconfidence makes K7 easier to pass.
- α-market and credibility bankroll: noise at tens of resolutions.
- Compression currency.
- Escrowed credit: replaced by provisional/seasoned VDS.
- Capture–recapture as a release gate: unstable at about 5 findings per hunter; report it as a floor only.

## Owner decisions (union of the five lists, de-duplicated)
1. Model-call budget for salts.
2. Public beacon: drand fetch or human dice.
3. Who holds the offline secret and the plant envelope (ideally a second person).
4. Adding genuinely null tasks to the real pool vs the 10–90% admission filter (16.12).
5. Entropy-pinned sandbox as default; whether nondeterministic claims are allowed at all.
6. New certificate statuses (STALE variants, LEGACY, UNREPLICATED labelling).
7. Retroactivity and clawback policy.
8. Who rules on feature legitimacy (feature provenance).
9. Bounded-null certificates (a K1/K7 change).
10. Kernel change cadence (two-speed rule).
11. The detectability threshold (≤ 0.2 at 40+40, or pay for ≤ 0.1).
12. Who supplies one outside attack per milestone.
