# evalA final evaluation: every idea in the corpus, graded by a security and alignment reviewer

## Threat model
- **Optimiser.** L6 evolution plus the LLM agents. It is adaptive, may be deceptive, and may come to model its own
  audits (the defeat device shows this is easy).
- **Builders.** AI agents with write access to the repository, who write the Kernel, tests, museum, corpus and
  generators.
- **Owner.** One human, who is approver, blinder and experimenter at once.
- **What counts as a guarantee.** A guarantee counts only if it survives that optimiser. "Measured" is not
  "prevented".

## Scales
Scores are 1–5:
- **N** novelty against the corpus *and* prior art;
- **S** soundness against an adaptive adversary;
- **E** evidence: 1 none, 2 argument or prior art, 3 toy, 4 code read or verified, 5 run on the real system;
- **B** buildability on this laptop and codebase;
- **I** impact on the real bottlenecks: certifiable held-out signal, integrity holes, model calls, and human
  attention.

## Verdicts
| Verdict | Meaning |
|---|---|
| BREAKTHROUGH | strict bar (see the end) |
| STRONG | sound, important, buildable |
| USEFUL | worth building; limited scope |
| INCREMENTAL | small improvement |
| RESTATEMENT | of the corpus or of named prior art |
| FLAWED | broken as stated |
| DROPPED | dropped by consensus |

## Cross-check with evalB
I did this in `r3-*` mailbox posts. Disagreements that remain are recorded in §4.

Nothing in this evaluation ran on the real system. "Toy" means a pure-Python script in the scratchpad.

---

## 1. Graded list, de-duplicated

The **Origin** column credits every author. Abbreviations:
- **EE** evidence-economics, **EM** emergence, **VtV** verify-the-verifier, **SYS** systems;
- **SA** scout AI scientists, **SE** scout evolve and MLE, **CON** constraints, **PC** product capabilities;
- **nat / mav / his / ins / cd** the five thinkers (naturalist, maverick, historian, institutionalist, crossdomain);
- **A** evalA, **B** evalB.

### 1a. Findings: defects demonstrated or read in code

| id | Finding | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| F-1 | Adaptive reuse: Bonferroni is not adaptive-valid while continuous held-out values flow back (55.5% false certification in a toy) | EE | STRONG (finding) | 3 5 3 – 5 | toy, not run on the Kernel | EE P1's `test_adaptive_reuse.py` against the real K4/K6 |
| F-2 | VDS is earned by failing (p0 is measured post hoc) | EE | STRONG | 3 5 4 – 4 | none | unit test: a tiny-δ claim at q = 0.05 earns > 0 bits today |
| F-3 | `verify` mixes up stale and forged | VtV | STRONG | 3 5 4 – 3 | none | the MR7 as-of test fails today |
| F-4 | Transductive leaks (per-batch normalisation) | SA | USEFUL | 2 5 2 – 3 | known in ML practice | 3 planted fault-injection cases |
| F-5 | **Defeat device:** score-based and label-swapping controls are beatable by a program that checks its own leak | mav | STRONG | 4 5 3 – 5 | toy | re-run on DEV-1 in the museum |
| F-6 | Null plasmodes cannot see raw-data feature leaks (gain −0.11 or 0.00) | cd | STRONG | 4 5 3 – 3 | toy | 10 planted leaks |
| F-7 | Amplifier device: invisible to null salts | nat | USEFUL | 4 5 3 – 3 | toy | positive-salt coverage test |
| F-8 | Subset-search fraud proofs are reverse p-hacking (96% at 1.5δ) | mav, cd | STRONG | 3 5 3 – 3 | standard selection effect | already run |
| F-9 | **B2: the sandbox image bundles scikit-learn's labelled copies of 5 real tasks (9 of 21 tasks)** | B | **STRONG**, most urgent (a known class, answers in the agent's environment, as in SWE-bench git-history and test-file leaks; per evalB) | 4 5 4 5 5 | the exploit has not been written | a 20-line candidate that calls `load_wine()` and joins on features. Prediction: it certifies on wine-real today |
| F-10 | **A single coupled PRNG stream (seeds 1 and 2) for features, labels and split in all 12 synthetic tasks; MT state is clonable from features** | A (toy, code), B (re-verified) | **STRONG** | 4 5 4 5 4 | candidate programs cannot see the generator today | regenerate the held-out labels from the repo code: should match 12/12 |
| F-11 | Byte-diff on scores false-flags honest float code (1,144 of 2,000 rows) | A (toy) | USEFUL | 3 5 3 5 2 | pure Python, not BLAS | 20 honest `predict_proba` pipelines under row shuffle |
| F-12 | Patch-to-pass voids the "0 flips on 500" screen bound (true rate 0.94% against ≤ 0.6%) | A (toy) | STRONG | 4 5 3 5 4 | the toy population is synthetic | P4b patched on dev; count flips on a sealed half |
| F-13 | Composition hazards: resweep verdicts and defeater contents are unledgered releases; seed provenance; self-reported alarm baseline | A, B | USEFUL | 4 4 2 5 2 | it leaks only a few bits | campaign NI replay across a resweep |

### 1b. The information-flow backbone (prevention)

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| IF-1 | Release ledger and verdict embargo; transcript-counted multiplicity (16.5) | EE | STRONG; RESTATEMENT of Dwork et al. 2015 applied mechanically | 3 5 2 4 5 | rests on "the log is complete", which is unenforced | IF-4 plus EE's adaptive-reuse test |
| IF-2 | Double-entry reconciliation of the release ledger with the sandbox IO log | nat | USEFUL | 3 3 1 3 3 | needs to know which bytes are derived from the target, which fails for encodings | 20 injected bypasses (the encoded one should be missed) |
| IF-3 | Kernel process boundary, "kerneld" (VtV P6) | VtV | **STRONG**; promoted because IF-4, NI-1 and minting all depend on it | 2 5 2 3 5 | 1–2 weeks of work; Saltzer–Schroeder prior art | AST test: no host module imports `EvidenceStore`; a planted direct-store profiler is refused |
| IF-4 | Campaign-scope cassette replay with releases pinned: process non-interference | A Idea 2 = B B1 (joint) | **STRONG** | 4 4 3 3 4 | complete only relative to the perturbed roots and P6; needs byte-deterministic prompts; one realised U | Run A/B real-label replay with 0 misses, then the B3-LEAK branch with ≥ 1 miss in PAST EDITS |
| IF-5 | Feature-provenance tags, a dominance screen, and a human legitimacy ruling | cd | USEFUL | 3 4 3 4 3 | human attention; semantic leaks are unbounded | 10 planted raw-data leaks, ≥ 8 routed to the human |
| IF-6 | Third-party reporting: a `self_reported` field caps the tier | ins | USEFUL | 2 4 1 5 2 | prior art (Kleven); narrow | a registrant-set "external" role certifies at most INTERNAL |
| IF-7 | Closed-enum Kernel reasons and capability-resolved references (confused deputy) | cd T12 | INCREMENTAL | 2 4 1 5 2 | already partly mitigated | fuzz 10⁴ forged references |
| IF-8 | B2 truth-scan of image layers plus a separate exporter image | B | STRONG (as the fix for F-9) | 3 4 2 5 5 | it covers only enumerable roots | the scan finds 0 tuples after the fix; the exploit fails |

### 1c. The core design principle

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| M-1 | **Freeze, then mint:** held-out truth is generated after the program hash is anchored, with independent HMAC streams per purpose, a generator-adapter and a separate dev mint | B (principle), A (CSPRNG precondition, PRNG leak), joint | **STRONG** as engineering; the guarantee is a RESTATEMENT of fresh-sample / prospective evaluation (evalB agrees); the CSPRNG rule is known crypto practice (never use MT19937 for secrets) | 3 5 3 4 5 | claims become generator-scoped. An optimiser trained on minted families learns the generator, not the world (Goodhart toward the simulator) | the clone attack against HMAC streams falls to honest accuracy; certify 50 claims on 50 fresh mints with ≤ α + 3SE false certifications |
| M-2 | "Commit before the verifier is determined": time, identity and value | cd final | RESTATEMENT (of commit-reveal; it is M-1's ancestor) | 2 4 1 – 3 | no mechanism of its own | — |
| M-3 | Closed world: reachable bytes = declared inputs, and every truth copy is a declared root | A r2 | USEFUL; superseded by M-1 for minted targets | 3 3 1 3 3 | the root set is open-ended (weights, public data) | the B2 exploit fails under a hermetic mount with a truth-free image |

### 1d. Per-claim leak controls (measurement)

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| NI-1 | Output-difference non-interference replay against stored production output | ins | **STRONG** | 4 4 3 4 4 | needs determinism; complete only against perturbed roots and only with P6 | DEV-1 and SPLIT in the museum wrapped in the device, 4/4; 20 honest claims, 0/20 |
| NI-2 | Entropy-pinned sandbox, plus a McNemar fallback for nondeterministic claims | mav | USEFUL | 3 4 3 3 3 | the fallback leaves about a 1-point stealth margin | the noise-opt-out device under pinning |
| NI-3 | Maximally distant worlds (complement + constant), so p_ref leaves the trusted path | A | INCREMENTAL (a refinement of NI-1) | 3 5 3 5 2 | exact class only | m = 1, 2, 5 stealth devices: 0.998 against 0.632 |
| NI-4 | Compare decisions or values at a declared precision; print the stealth margin | A, B | INCREMENTAL, but necessary | 2 5 3 5 2 | the rounding is a hiding place | see F-11 |
| NI-5 | Declared-dependency contract: one replay perturbing everything undeclared, with a randomised environment | B B3 | USEFUL | 4 3 1 3 3 | needs root injection and P6 underneath; float issues | 5 planted contract violations named in ≤ 3 replays |
| NI-6 | NI unifier: about 8 mechanisms are one 2-safety hyperproperty, so one self-composition engine | A Idea 1 | USEFUL; partly a RESTATEMENT of self-composition (Barthe et al. 2004) and secure multi-execution (Devriese & Piessens 2010) | 3 3 1 3 3 | over-counted (F1-layout and K-3 are not NI); the coverage matrix is only as complete as its secret list | ≥ 7 of 8 museum information-flow findings flagged with no finding-specific code |
| NI-7 | Permuted-label "GAAR" or shadow null | ins, his, nat | DROPPED (defeat device) | – | – | – |
| NI-8 | k-world Bayes-ceiling control | mav | FLAWED as primary (a tuned device cheats on 27% of tasks and is caught on 29% of those); a bound only | 3 2 3 4 2 | the seam between worlds is detectable | — |
| NI-9 | Row-permutation and feature-dependence replays (answer keys) | nat | INCREMENTAL | 2 3 2 5 2 | a content-keyed key survives | 10 planted positional keys |
| NI-10 | F4 example-independence invariant (ML adapter) | SA | USEFUL | 2 4 2 5 3 | transductive methods must self-declare | 3 planted fault-injection cases |
| NI-11 | Dispersion and duplication forensics (P4a; P4b only with declared nondeterminism); forensics on returned physical data | his | USEFUL | 2 4 2 5 2 | GRIM-style prior art; P4b is an autoimmunity risk | 20 copied-seed runs |
| NI-12 | T7 escalate-never-reject tripwire; S_max frozen; bunching monitor | cd, mav, nat | USEFUL | 3 4 1 5 2 | an optimiser can stay just under S_max | planted true effects at 3×, 10× and 30×: 0 false rejections |
| NI-13 | Diverse double re-execution, K3′ (Wheeler DDC) | cd T6 | INCREMENTAL (it would have caught 0 of 30 past findings); its machinery is reused for fingerprint equality | 2 4 1 4 1 | substrate faults were never the problem | the planted `sitecustomize` trojan |

### 1e. Audit Plane: salts and covert tests

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| AP-1 | Positive salts with effect-size coverage | his, mav, nat, ins, cd | USEFUL; RESTATEMENT of LIGO blind injection / IT-audit test facility, with the coverage delta | 3 3 3 2 3 | detectable worlds bias it by (1 − d); d̂ ≤ 0.2 is the best affordable bound; costs model calls | 10 positive salts against an amplifier at f = 0.1, pooled flag rate ≥ 0.9 |
| AP-2 | Null salts: self-similar for synthetic families, plasmodes for real data, prevalence matching | mav, nat | USEFUL but *demoted*: mostly redundant once M-1 and IF-4 exist | 3 3 3 2 2 | power only for gross failure; the "nothing works on dev" tell | head-to-head with DEV-1 in the device |
| AP-3 | Detectability exam: d̂ = TPR − FPR on 40 + 40 presentations | cd, his, nat | USEFUL | 3 4 2 4 2 | the strongest spotter may be weaker than the optimiser | a leaked tell must fail the exam |
| AP-4 | Decoys and per-channel attempt telemetry: a monitor plus displacement index (never a fitness term) | nat, his, mav | USEFUL | 3 3 1 5 2 | the selection signal is noise at our scale; displacement | the two-channel displacement simulation |
| AP-5 | Attempt rate as an acceptance gate | nat | DROPPED | – | – | – |
| AP-6 | Salts in the L6 gate stream | ins, mav | DROPPED (it teaches salt detection) | – | – | – |

### 1f. Randomness and commitment

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| R-1 | `draw()` with a per-purpose input table: beacon for frozen items, secret + beacon for salts | nat, mav, ins, cd, his | STRONG; RESTATEMENT of commit-reveal and risk-limiting-audit seeding, with a clean purpose table | 2 5 2 5 3 | it does not defeat gaming of whatever every draw has in common; beacon authenticity is an assumption | order-forgery and seed-shopping simulations |
| R-2 | External anchoring | ins, VtV | already built (`chh ledger-anchor`); P5 Merkle inclusion is INCREMENTAL | 1 4 4 4 2 | an attacker with store access before the anchor | 1,000 tamper trials |
| R-3 | K2 for future-data claims requires an external anchor that predates the data's release | cd T9 | USEFUL | 2 5 1 5 2 | — | a forged backdated pre-registration is rejected |

### 1g. Statistics and accounting

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| ST-1 | Per-target α-account with slots and retirement (16.2) | EE | STRONG; RESTATEMENT of α-spending engineered as ledger state | 2 5 2 5 4 | rigid; unused slots waste power; partly superseded by M-1 for minted targets | 1,000 claim orderings give identical verdicts |
| ST-2 | VDS as an e-process, plus a separate skill score (16.3) | EE | STRONG | 3 5 2 5 4 | needs K4 to be conditionally valid | 10,000 null campaigns: P(sup VDS ≥ log₂ 20) ≤ 0.05 |
| ST-3 | K3 as a reliability demonstration (zero-failure run) | EE | USEFUL; RESTATEMENT (demonstration testing) | 1 4 2 5 3 | correlated failures; replay cost | planted p-flaky programs |
| ST-4 | Pre-registered risk reference per evidence class (K7) | EE P5 | USEFUL | 2 4 1 3 2 | an early ledger prior is circular | a trivial lemma gets 0 bits |
| ST-5 | Certifiability preflight and cascade (16.6) | SYS | **STRONG** (cheap, it only denies) | 2 5 2 5 5 | an optimistic SE at tiny n | a 0-call suite audit; Run A predicted to fail K7 |
| ST-6 | Small train, large test (16.21) | CON | **STRONG** | 3 5 2 5 5 | trivial effects become certifiable; needs a δ floor | minimum certifiable effect ≤ 0.02 on the regenerated suite |
| ST-7 | Pooled multi-task claims with an IUT guard (16.22) | CON | USEFUL | 2 4 2 4 4 | a Kernel change; harder task-agnostic programs | 10⁴-trial null simulation |
| ST-8 | Exclusion-limit bounded nulls (Stage 1 frozen, positive control, PCL clip) | mav, his, ins | USEFUL; RESTATEMENT of HEP CLs/PCL plus TOST, applied to audits | 2 4 2 3 3 | the positive control may not exercise the claimed mechanism | 1,000 broken re-implementations give 0 bounded nulls |
| ST-9 | Kernel reliability diagram with cluster bootstrap (H3) | mav, cd, his | USEFUL | 3 4 1 3 2 | resolutions are slow and correlated | NRSRO-style correlated-defect simulation |
| ST-10 | Replication-vs-z curve, noise-floor record, drift barrier | nat, his | USEFUL | 3 4 1 5 3 | needs re-measurement budget | the curve on L6 logs |
| ST-11 | Realised-vs-forecast alarm | mav | USEFUL, with an A caveat (the baseline is self-reported) | 3 3 1 5 2 | blind to leaks that also inflate dev | a two-split leak toy |
| ST-12 | Interval coverage audit, and anchor-blinded replication | his | USEFUL | 2 4 1 5 2 | prior art (Henrion–Fischhoff) | K9 canary on the effect digits in the replicator prompt |
| ST-13 | Blind deviation route (exclusions decided blind) | his P5 | USEFUL (nobody examined it) | 3 3 1 3 2 | the blinded view may still leak the outcome sign | the |r| < 0.05 property test |
| ST-14 | Anomaly-first Formulator restricted to a declared grammar, scanned on dev only | his, mav, nat | USEFUL | 2 5 2 3 2 | yield is low at our n | null-plasmode scan, certification ≤ α |
| ST-15 | Bunching (McCrary) monitor | ins | DROPPED/PARKED (no power) | – | – | – |
| ST-16 | Alpha-investing, Thresholdout, Holm | EE (rejected) | DROPPED | – | – | – |

### 1h. Verifier assurance and governance

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| V-1 | Loophole museum plus mutation gate (16.8) | VtV | **STRONG** | 3 4 2 5 4 | tests written knowing the findings; mutants model slips, not spec gaps | the museum kill rate, and a mutation baseline score |
| V-2 | Clean-room reference model plus metamorphic relations (16.9) | VtV | **STRONG** | 2 4 2 4 4 | a shared misreading of GOAL (Knight–Leveson) | MR7 fails today; differential test on 10⁵ histories |
| V-3 | Findings registry as code, with strict-xfail pins (16.10) | VtV | USEFUL | 2 4 1 5 3 | bookkeeping | 0 dangling ids |
| V-4 | Pure decision core plus trusted-base budget (16.11) | VtV | USEFUL; RESTATEMENT (de Bruijn criterion) | 1 4 1 3 3 | refactor risk | byte-identical certificates on a replayed store |
| V-5 | Calibrated adversarial search against frozen Kernel versions | VtV P7 | USEFUL | 3 4 1 3 3 | a same-family attacker | ≥ 50% recall on pre-fix versions |
| V-6 | **The verifier's exams wear out:** dev/sealed halves, ledgered looks | A Idea 4 (B adopted it into B4) | **STRONG** | 4 5 3 5 4 | it halves a corpus that does not exist yet | F-12 on real P4b |
| V-7 | Reverse board with a disclosed/sealed split; fix-generalisation metric | A + B | USEFUL | 4 4 1 3 3 | needs attackers from outside the agent pipeline, and the owner is one person | 5 classes × 2 sealed variants after a fix |
| V-8 | Resweep: honest-corpus screen, reason-typed STALE, graded evidential clawback, seasoned VDS | ins, cd, his, nat | USEFUL; RESTATEMENT of doping B-sample retesting | 3 4 1 3 3 | the honest corpus does not exist; the screen wears out (V-6); resweep outcomes are releases (F-13) | the pre-SPLIT archive: ≥ 1 flip on affected tasks, 0 elsewhere |
| V-9 | AIRE honest corpus with a coverage list of rare categories | nat | USEFUL (a critical-path dependency) | 3 4 1 3 3 | builders curate it (capture) | removing a rare category lets an over-broad rule through |
| V-10 | Monotone defeater bus with a PENDING element; producers as the only interface | B B4 + A | USEFUL | 3 4 1 4 3 | yield attacks; contents leak; builders need details | 10⁴ producer streams never raise a label |
| V-11 | Eliminative defeater count for tier lowering | cd T4 | INCREMENTAL (folded into V-10) | 2 4 1 5 2 | — | label(cert + defeater) ≤ label(cert) |
| V-12 | Two-speed Kernel (core and list); tighten-only merges at once | ins, mav | USEFUL; RESTATEMENT (WADA code vs list) | 2 3 1 5 2 | a "tightening" change can still steer L6 | classify 30 past fixes as core or list |
| V-13 | Reputation may raise scrutiny, never lower it (Vulcan rule, metamorphic test) | his, ins | USEFUL | 2 5 1 5 2 | — | swapping two lineages' histories leaves verdicts identical |
| V-14 | Anti-capture: one outside attack per milestone; "blinder = experimenter" printed; ritual detector | his, ins | USEFUL | 2 3 1 4 3 | the owner may never supply one | ≥ 90% of checklist items have a killed blanking mutation |
| V-15 | Capture–recapture loophole estimate as a floor | cd, his, nat | INCREMENTAL | 3 2 2 4 1 | unstable at m ≈ 1–2 | 12 seeded loopholes |
| V-16 | Designer calibration ledger (1/5 self-favouring claims held up) | B B7 | INCREMENTAL | 3 3 4 5 1 | n = 5 | 30 resolutions, Brier score |
| V-17 | Proofreading independence of the gate stages (joint false-pass rate vs Π p_i) | nat #1 | USEFUL (nobody examined it) | 3 4 1 4 2 | needs a corpus of bad edits | 200 bad edits through the gate stages |
| V-18 | Prosecutor with a two-signal licence, α capped at 0.2×, contraction | mav, nat | USEFUL; scheduled late | 3 3 1 2 2 | correlated same-family challengers | 6 defective + 14 clean certificates |
| V-19 | K3 bisection over checkpoint hashes | cd T2 | USEFUL; RESTATEMENT (Truebit / git bisect) | 2 5 2 4 2 | deterministic CPU only | a 64-step planted divergence found in ≤ 6 comparisons |
| V-20 | Optimistic acceptance, escrow, PROVISIONAL-SEALED | cd, mav | DROPPED | – | – | – |
| V-21 | Retry the evolving-attacker population (Red Queen) | nat | DROPPED (subsumed by V-5) | – | – | – |
| V-22 | UNREPLICATED labelling | his | USEFUL (naming only) | 1 5 1 5 2 | — | — |
| V-23 | Copilot mode; steered runs earn no credit (16.16) | SYS | USEFUL | 2 3 1 4 3 | the human remains an adaptive analyst | an intervention after a held-out read is refused |

### 1i. Yield, cost and capability

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| Y-1 | Cassettes as regression fixtures (16.7) | SYS | **STRONG** (an enabler of IF-4) | 2 5 2 4 4 | prompt determinism | Run A replays with 0 calls |
| Y-2 | Execution as the ranker, packed calls, quota diet (16.19) | SE, CON | STRONG on cost | 2 4 3 5 5 | correlated sibling drafts | ≤ 6 calls per run on 3 tasks |
| Y-3 | One-SE parsimony and a runtime screen (16.20) | SE | USEFUL; RESTATEMENT (Breiman) | 1 4 3 5 3 | prefers weak-but-fast | Run B picks h1 |
| Y-4 | OOF signature dedup and a zero-call vote ensemble | SE | USEFUL | 3 4 2 5 2 | correlated members | an offline vote on Run B |
| Y-5 | Headroom gate on improve calls | SE | USEFUL | 3 5 3 5 2 | — | the e ≥ 3 arithmetic against the real function |
| Y-6 | Diff patches, effort routing | SE | INCREMENTAL | 1 4 1 4 2 | — | fix wall time −40% |
| Y-7 | Second family via a local model plus role exams (Principle 4) | CON | **STRONG** (unblocks invariant 5 cheaply) | 3 4 2 4 5 | GPT-distilled open models share errors (φ measures it); role exams wear (V-6) | the 60-pair exam; one live run with `invariant_5: met` |
| Y-8 | CPU-time limits plus a contention witness | CON | USEFUL | 3 4 1 4 3 | WSL2 cgroups | a sleep program is charged 20/20 |
| Y-9 | Claim Audit of published claims on fresh external data (16.24) | PC | USEFUL | 3 4 1 4 3 | statement fidelity; legal and reputational load | 2 planted inflated claims end NOT CONFIRMED with the correct reason |
| Y-10 | Label-dependence audit of published code | A Idea 6 | USEFUL, narrowed (INCONCLUSIVE on label-dependent splits) | 4 4 1 3 2 | shim complexity; ≥ 3/10 INCONCLUSIVE | 40 planted leaks |
| Y-11 | Lab notary, kinetic pack, bound manuscripts, non-confirmation registry | PC | USEFUL / INCREMENTAL (domain products) | 2 4 1 3 2 | need adapters and approvals | per-product planted tests |
| Y-12 | Campaign world model compiled from the store (16.26) | SA | USEFUL | 3 4 1 3 3 | planner fixation | the planted conditional-effects benchmark |
| Y-13 | Blind rediscovery tasks (16.25) | SA | USEFUL | 2 3 1 3 3 | Codex cutoff unknown; literature truth is T5-ish | 30 tasks vs a no-data probe |
| Y-14 | Baseline-anchored mode (16.27) | SA | USEFUL | 2 4 1 3 3 | GPU-bound repos | ≥ 7/10 reproduce |
| Y-15 | Calibrated idea triage on dev outcomes (16.28) | SA | INCREMENTAL | 1 4 1 4 2 | needs ≥ 100 resolutions | — |
| Y-16 | BixBench code-gradable import | SA | INCREMENTAL | 1 4 1 4 2 | contamination | count of gradable items |
| Y-17 | Ledger-calibrated forecasts (16.17) | SYS | INCREMENTAL | 1 3 1 4 2 | coupled Goodhart if it feeds K7 | ≥ 30 resolutions |
| Y-18 | OpenML upload-date time split | B B6 | USEFUL, weakened (it must postdate the model cutoff). evalB: RESTATEMENT/INCREMENTAL (a LiveBench-style split). Both views recorded | 3 3 3 3 3 | re-uploads; small pool | 40-task power (0.86 at +30 points) |
| Y-19 | Certified leaderboard: release the oracle, retire then publish | B B5 | RESTATEMENT (Blum–Hardt Ladder plus retire); no users | 2 4 1 2 1 | no submitters | the F1 attacker ≤ 0.065 |
| Y-20 | Research graph, `explain`, limitations register (16.15) | SYS | USEFUL | 2 4 1 5 3 | noisy warnings | Run B `explain` resolves every number |

### 1j. Emergence and memory

| id | Idea | Origin | Verdict | N S E B I | Strongest objection | Cheapest decisive test |
|---|---|---|---|---|---|---|
| EM-1 | Generated task families, evolve-only, with a learnability filter (16.12) | EM | USEFUL; RESTATEMENT (POET/OMNI) with acceptance barred | 2 4 1 3 2 | an underpowered 3 pp test; conflicts with prevalence matching | the 3-arm, 3-seed design |
| EM-2 | MAP-Elites archive with evidential revocation (16.13) | EM | USEFUL | 3 4 1 3 2 | tens of generations on a laptop | a planted retraction evicts its descendants |
| EM-3 | Mechanical interestingness (learning progress), allocation only (16.14) | EM | INCREMENTAL | 2 4 1 5 1 | stale signal | a 10-direction simulation |
| EM-4 | FunSearch-lite on the LP adapter | EM | INCREMENTAL | 1 5 1 4 1 | LP is solved outright | 6/10 optima rediscovered |
| EM-5 | Red/blue verifier synthesis judged by a fixed mutation set | EM | USEFUL | 3 4 1 3 2 | mutation-adequate is not correct | ≥ 90% kill, ≤ 2% false reject |
| EM-6 | Memory quarantine, a "sleep" promotion pass, an imprinting audit | nat #3 | USEFUL (L5 not built) | 3 4 1 2 2 | — | memory-on vs memory-off per new family |
| EM-7 | Scheduled amnesia, long-shot quota, swarm attrition | mav, nat | PARKED / DROPPED | – | – | – |

### 1k. Dropped by consensus (all agree; no further grading)
- baseline blinding (H5);
- expert priors (H8);
- α-market and bankroll (H9, nat P4);
- compression currency (H12);
- strong-inference certificates (H7, parked);
- outside-world forecasting (H11, low priority with the "unresolved" rule);
- T5 R(D) stop rule;
- fixed 80% alarm;
- Oulipo grammar (T13);
- validity intervals (T8, parked).

---

## 2. Top 5 overall (impact × soundness × buildability)
1. **Close the two live answer keys now: F-9 (B2) and F-10 (coupled PRNG with tiny seeds), with IF-8.**
   - They are concrete, verified-in-code holes affecting 21 of 21 suite tasks between them: 9 from the image and
     12 from the generators.
   - The fix is cheap: an exporter image, a truth scan, `draw()`-seeded HMAC streams, and regeneration.
   - Until they are fixed, no M2 certificate should be trusted.
2. **M-1, freeze then mint, with the independent-stream preconditions.**
   - It is the one principle that *removes* whole classes (vault, copy, image, weights and reuse leaks) for
     generator-, simulator- and future-scoped targets, instead of measuring them.
   - It also lifts the held-out-signal bottleneck for those classes.
3. **The enforcement chain for information flow: IF-3 (P6 kerneld), then IF-1 (release ledger and embargo), then
   IF-4 (campaign-scope cassette NI with pinned releases).**
   - IF-1's theorem rests on log completeness. IF-4 is the only mechanical test of it. Both are sound only once
     IF-3 makes the vault the sole path to the truth.
4. **NI-1 + NI-2 + NI-4 (+ NI-3): byte non-interference on decisions in an entropy-pinned sandbox.** This is the
   per-claim workhorse for non-minted targets. The defeat device cannot evade it on the channels it perturbs.
5. **Cheap yield, which is the bottleneck the owner actually faces today: ST-5 preflight + ST-6 large test sets +
   Y-2 call packing + Y-7 second family.**
   - Together they turn the 300-call M2 exit test into a few dozen calls on certifiable tasks.
   - They meet invariant 5 without weakening anything.

**Honourable mentions:** V-6 (sealed exams for the verifier), V-1 and V-2 (museum, mutation, clean room), and ST-2
(VDS as an e-process).

## 3. The breakthrough list: **none**
Applying the strict bar honestly (new against the corpus *and* prior art, qualitatively better, survives its strongest
objection, decisive test specified):

- **M-1, freeze then mint.**
  - *Closest prior art:* fresh-sample evaluation, which removes adaptivity (Dwork et al.'s own remark that fresh data
    is the trivial cure); prospective blind challenges (CASP); procedurally generated, contamination-free benchmarks
    (Procgen, LiveBench / LiveCodeBench post-cutoff questions); and GOAL's own physical path.
  - *Delta:* binding the creation of the truth to the program hash and a beacon, plus the CSPRNG-independence
    precondition.
  - That is a strong engineering synthesis, but the guarantee itself, a fresh sample, is known. It also fails the
    objection that generator-scoped certificates can be Goodharted toward the generator.
  - **Not a breakthrough.**
- **IF-4, campaign-scope cassette NI.**
  - *Closest prior art:* self-composition (Barthe, D'Argenio & Rezk 2004), secure multi-execution (Devriese &
    Piessens 2010), and record/replay.
  - *Delta:* using cassettes to fix an LLM's randomness and pinning releases, so that the Dwork transcript hypothesis
    is tested in an agent pipeline. That is new in application.
  - But it is complete only relative to the perturbed roots and needs P6, so it does not survive the B2-class
    objection on its own.
  - **Not a breakthrough.**
- **F-5 (defeat device), F-9 (B2), F-10 (PRNG).** These are important findings, not new mechanisms. Known attack
  patterns (Dieselgate; state recovery for MT19937) applied well.

**No breakthrough.** The corpus's real advance is a correct *architecture*. Prevention is placed in front of
measurement. Truth is created after the freeze. The trusted base is guarded by tests of the tests. And honest
accounting runs throughout. It is not a single qualitatively new guarantee.

## 4. Disagreements with evalB (after the r3 cross-check)
**Where we agree** (r3-001 in both directions):
- no breakthrough;
- the B2 and seed fix comes first;
- M-1 and campaign NI are STRONG, with named prior art;
- Idea 4 is STRONG;
- B4, the reverse board and Idea 6 are USEFUL;
- Idea 3 is INCREMENTAL;
- B5 is a RESTATEMENT.

evalB's top 5 differs in two ways. It ranks ST-2 (VDS e-process plus embargo) at #2, and it proposes **minted large
synthetic test sets** (M-1 × ST-6), which give power and leak-proofing in one move. I endorse that combination. It is
the best single buildable step after the B2 and seed fix.

**Remaining differences:**
I record both views wherever the mailbox did not settle them; see `r3-*`.
- **Trusted base of the NI engine.** evalB wants "root set complete only if P6" printed and NI held back until P6
  exists. I agree it should be printed, but would ship NI earlier.
- **Fixed public datasets.** evalB: label them as retrospective targets and add no further instruments. Me: keep
  the truth-scan and the hermetic mount, because they close enumerable copies.
- **The defeater bus as the only interface.** Unresolved. The embargo on defeater contents conflicts with builders
  needing the details.

## 5. Security and alignment risks that remain even if everything above is built
1. **Truth outside every root set.** Model weights memorise public datasets and papers, and row identity in public
   features keys into that memory. Only minted, future or human-returned targets avoid it. All retrospective
   certificates carry this risk permanently.
2. **The examinee writes the exam, and the exam's author shares the optimiser's blind spots.**
   - AI builder agents write the Kernel, the tests, the museum, the honest corpus, the generators (now trusted
     adapters under M-1) and the NI secret list.
   - A subtly wrong or "hypocrite-commit" change in the trusted base defeats every downstream mechanism.
   - Only outside humans or outside models break this loop, and the owner is one person.
3. **A single human is approver, blinder, experimenter and the only legitimacy judge** (feature provenance, adapter
   approval, biosafety screen). Risks:
   - attention fatigue turns approvals into ritual;
   - persuasive renderings (maverick attack #2) target the human, not the Kernel;
   - the human is an adaptive analyst outside every replay.
4. **Goodhart toward what can be checked.** Minting and exact classes make generator-scoped claims plentiful and cheap.
   - An L6 optimised on them may improve at fitting generators while real-world research ability stagnates.
   - LOFO and time-split real data are the only counterweight, and they are scarce.
5. **Specification gaps.** About two-thirds of the past loopholes were spec gaps. Every mechanism here checks
   against an enumerated spec: secret classes, defeater categories, MRs. The next gap is, by definition, not
   enumerated.
6. **An evaluation-aware, deceptive optimiser.**
   - Salts are only bounded at d̂ ≤ 0.2.
   - Telemetry displaces into channels nobody logs.
   - Rare-trigger leaks evade single-world NI (0.011 per world in my toy).
   - Selection across many generations can find any non-total check's blind spot.
   - Only total, mechanical checks (sandbox denials, byte NI on enumerated roots) resist this.
7. **Side channels and the substrate.** Timing, resource use and crash patterns. Supply-chain trust in Docker, Python,
   scikit-learn and the Codex CLI ("trusting trust"). WSL2 hermeticity. Until P6 exists, host modules can read the
   store directly.
8. **Aggregate false certificates.** Unlimited one-shot targets give about α·N false certificates. If certified claims
   are later selected for publication or for memory, selection re-inflates the error. VDS bounds the track record,
   not each published item.
9. **Correlated oversight.** Invariant 5 is still not met. A local second family may be distilled from GPT outputs, so
   critic, replicator and red-team independence may be nominal (φ measures this; it cannot fix it).
10. **Dual-use.** The physical path prepares protocols. The prohibited list and the biosafety review are human
    procedures with no mechanical backstop, and that is outside the Kernel's competence.
11. **Growth of the trusted base and wear on the verifier.** Each adopted mechanism adds trusted code or human-owned
    lists. The verifier's own validation sets wear out with every look (V-6). Without a strict trusted-base budget,
    assurance can fall even as the number of mechanisms rises.
