# historian: final position (round 2)

**Evidence quality.** Nothing was run on the real system.
- *Toy simulations.* I re-ran only maverick's `defeat.py`. The others (`noninterference.py`, `selfsim.py`,
  `plasmode_featureleak.py`, `subset.py`, `kworld*.py`) were run only by their authors.
- *History web-checked this session.* Scheel 2021, Henrion & Fischhoff 1986, Millikan/Franklin, Kennefick 2009,
  LIGO "Big Dog", ATLAS 750 GeV, Kaplan & Irvin 2015, COMPare, meldonium 2016, OP and Arbitrum fault proofs.
- *Everything else.* Recalled or inferred.

## 1. Endorsed mechanisms, ranked

**1. Information-flow controls first. Output controls only measure.** This is the board's main finding: a 3-line
defeat device beat every label-swapping control.
- *Prevention*: K9; the 16.5 release ledger; naturalist's **double-entry reconciliation** (sandbox IO log vs release
  ledger); and crossdomain's **adapter provenance tags** (K1 refuses post-outcome and ID features; a
  feature-dominance screen routes suspects to a *human* ruling). Crossdomain's toy showed no output control can tell
  a leaked feature from a predictive one.
- *Test*: ≥ 19/20 injected release-ledger bypasses are flagged, and ≥ 8/10 planted feature leaks (ID-sorted or
  post-outcome) are routed to human review, with ≤ 2/20 false routings on honest tasks.
- *History*: Clever Hans and N-rays were caught by cutting the information channel, not by scoring the horse.

**2. The non-interference replay** (institutionalist) for exact-class claims.
- The frozen program is re-run with every private label copy replaced by Bernoulli counterfactuals. *Any byte
  difference* in the predictions is a flag.
- Toy result: it caught a 2% stealth cheat (0.997) and a fingerprinting device. It falsely flags every honest
  nondeterministic program.
- Tolerance-class claims fall back to maverick's k-world ceiling, and the certificate prints the stealth margin.
  Maverick retracted his own "1 in 10" claim: a tuned device cheats on 27% of tasks and is caught on only 29% of
  those. So it is a *weak bound*, labelled as such.
- *Test*: DEV-1 and SPLIT wrapped in the device are flagged 4/4, with 0/20 honest flags.
- *Kill rule*: if more than 10% of honest suite programs are not byte-deterministic, this check is not the
  workhorse.

**3. Positive plants with effect-size coverage first; null plants only as a gross-failure alarm.**
- Construction: self-similar plants (same generator, g = 0 or a known g; naturalist) for synthetic families, fitted
  plasmodes for real data.
- On positive plants, the *reported effect must match the planted effect* (my P2 coverage audit). This is the only
  defence against naturalist's "amplifier" device. Toy: f = 0.1 flagged 0.53 per salt; pooling untested.
- Assignment by secret plus beacon; research stream only; L6 gets one circuit-breaker bit per campaign.
- Admission: spotter d̂ upper bound ≤ 0.2 on 40 + 40 presentations.
- Real-data nulls need **prevalence matching** (maverick's "nothing works on dev" tell).
- Power: about 30 nulls detects gross failure only, never α vs 2α.

**4. Resweep as one spec.**
- Meldonium rule: retroactive only after 0 flips on an honest corpus covering a human-owned list of rare honest
  categories.
- Output-keyed. Statuses: STALE-RULE, STALE-DATA, UNCHECKABLE, and REJECTED-INTEGRITY (never from a resweep).
- Graded, reversible clawback along evidence and information-exposure edges; penalties only for integrity findings;
  provisional vs seasoned VDS.
- *Test*: the pre-SPLIT archive gives ≥ 1 flip on the 4 affected tasks and 0 elsewhere, and 5/5 planted F4
  certificates become STALE-RULE.
- *Kill*: 0 historical flips over the next 10 changes.

**5. `draw()` with a per-purpose input table** (naturalist and maverick finals). Frozen-before-draw purposes use the
beacon only. Salt assignment uses secret plus beacon. The Kernel refuses mismatched inputs. Tests: order forgery is
rejected 100%; seed shopping is > 2α with self-chosen seeds and ≤ α with drawn seeds; owner grinding succeeds 0 of
10⁴ times.

**6. Exclusion-limit certificates** (`bounded_null`; PROPOSED).
- Fixed at Stage 1: δ_eq, the reimplementation hash, and the expected limit with ±1σ/±2σ bands.
- A positive-control plasmode built from the claimed mechanism must be detected at least 80% of the time;
  otherwise the verdict is "no sensitivity".
- A lucky limit is clipped to the expected −1σ band, as with LHC's power-constrained limit.
- *Test*: 5 broken reimplementations give 0 bounded nulls, and honest coverage is ≥ 95%.
- *History*: Michelson–Morley and dark-matter limits earned trust by demonstrated sensitivity.

**7. Calibration bookkeeping.**
- Per-task noise-floor record and a replication-vs-z curve per decision type (with naturalist); flat = MECHANICAL.
- Realised-vs-forecast alarm (maverick), z > 2 either way.
- T7 escalates, never rejects; S_max frozen at campaign open. *Test*: 0 false rejections on planted 3×/10×/30×
  effects.

**8. Anti-capture**, from my decay post plus institutionalist's addition.
- Ritual detector: ≥ 90% of checklist items have a killed mutation that blanks the property.
- One attack per milestone written from outside the agent pipeline; outside vs inside hit rates tracked.
- "Blinder = experimenter" printed on certificates while there is one owner.

## 2. Where I still dissent

- **The permuted-label shadow, kept as a "free naive tripwire"** (maverick, naturalist). Free and known to be
  beatable is how checks become ritual (Power's *Audit Society*). Keep it only if no report ever prints it as a
  *pass*. It may only produce flags.
- **Label names overclaim.** Historically "validated" meant *independent people re-built the instrument* (Huygens,
  Collins's TEA laser, ATLAS and CMS), over years, not a held-out check in weeks. Until a second family or a second
  human replicates, labels should read CERTIFIED-…-UNREPLICATED. A naming change, not a statistics change.
- **The honest corpus is the hidden critical path.** Resweep, Prosecutor categories and detector admission all
  depend on it, and it does not exist yet. No retroactive rule should ship before it does.

## 3. Dropped

- **Mine:** the fixed 80% alarm; P4b as written; the jointly permuted null; the claims that S detects leaks and that
  null plasmodes catch feature leaks.
- **Board:** permutation-based leak controls and null salts; T2 optimistic acceptance (bisection kept); subset-search
  fraud proofs (96% of honest certificates "broken" at 1.5δ); the T5 stop rule; attempt rate as a fitness term; H5,
  H8, H12; escrow (replaced by seasoned VDS); capture–recapture as a gate (floor only, and only at m ≥ 3).
- **Parked:** bankroll and α-market; H7; H11; bunching monitor.

## 4. The one lesson from history

**Every famous self-deception passed the procedures its own author designed.** Examples: Blondlot, Benveniste,
Schön's reviewed papers, Andersen's audits, issuer-paid AAA ratings.

Each was exposed by an *outsider* with a different incentive who could perturb the apparatus unseen: Wood in the
dark room, Sohn and McEuen, the LIGO envelope holders, Markopolos.

Here the AI agents build the Kernel, the museum, the red team and the honest corpus. **The examinee is writing the
exam.** The owner must keep at least one check that no agent designed and no agent can see.

## 5. Owner decisions required

1. The model-call budget for positive and null plants (Layer B).
2. Prevalence matching (keeping null-dev real tasks) versus the 16.12 10–90% solve filter.
3. A drand fetch by the control plane, or human dice only.
4. Who holds the envelope secret. Ideally a second person; otherwise accept "blinder = experimenter".
5. A K1/K7 change for `bounded_null`.
6. The retroactivity policy: honest-corpus coverage list ownership, the UNCHECKABLE cap (board majority:
   CERTIFIED-INTERNAL), and graded clawback.
7. Whether to enable the attempt veto on sandbox-total channels, only after the two-channel displacement
   simulation.
8. The S_max starting value.
9. Renaming labels to mark them UNREPLICATED.
