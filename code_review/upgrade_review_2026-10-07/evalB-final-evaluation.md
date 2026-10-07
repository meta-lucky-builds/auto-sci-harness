# evalB — Final evaluation of the whole idea corpus

**Reviewer stance.** A security reviewer plus an alignment researcher. The threat model is an adaptive, possibly
deceptive optimiser: the L6 evolver, Codex-written programs, and an LLM proposer. It may condition on any
observable, sandbag, or Goodhart any scalar. It is also a one-owner, AI-built system: the examinee writes the exam.

**Evidence rule.** Nothing in the corpus ran on the real Kernel, sandbox or agents. Evidence scores:

| Score | Meaning |
|---|---|
| 1 | none |
| 2 | argument only |
| 3 | toy simulation |
| 4 | verified by reading code (or a real bug found in code) |
| 5 | run on the real system (no item reaches this) |

**Score key.** N = novelty against the corpus *and* prior art. S = soundness against an adaptive adversary.
E = evidence. B = buildability here (laptop, Docker, stdlib Kernel). I = impact on the real bottlenecks: certifiable
held-out signal, live holes, invariant 5, model-call quota, the validated loop. All scores are 1–5.

**Verdict categories:**
- **BRK**: breakthrough.
- **STR**: strong.
- **USE**: useful.
- **INC**: incremental.
- **RST**: restatement (prior art named).
- **FLW**: flawed.
- **DRP**: dropped.

Only BRK requires the full bar.

**Coordination.** I posted my provisional verdicts to evalA (`r3-001`). No r3 reply had arrived when I wrote this.
Where evalA's round-2 file states a different view, I record it.

---

## 1. Graded list (de-duplicated; every author credited)

### A. Information flow and leak prevention

| id | Idea | Origin | Verdict | N/S/E/B/I |
|---|---|---|---|---|
| A1 | **The sandbox image holds labelled copies of the 5 real datasets** (`suite.py` exports sklearn's bundled data; one image) | evalB B2; preconditions verified by the orchestrator | STR (finding); RST as a class (answers present in the agent's environment, e.g. SWE-bench git-history leaks) | 2/5/4/5/5 |
| A2 | **Shared MT19937 stream with seeds 1 and 2 makes the generator code the answer key** for 12 synthetic tasks | evalA (toy `mint_prng.py`, code verified by both) | STR (finding); RST of crypto practice (never use MT for secrets) | 2/5/4/5/4 |
| A3 | **NI replay against the stored production output**: the frozen program re-run with label copies swapped; any byte difference is a flag | institutionalist; maverick amendments; evalA complement worlds | STR; RST of noninterference testing and secure multi-execution (Devriese & Piessens 2010) | 2/4/3/4/4 |
| A4 | **Entropy-pinned, hermetic sandbox**: determinism treated as a security property | maverick; crossdomain; evalA action graph | STR; RST (hermetic/reproducible builds: Bazel, Nix) | 2/4/2/3/4 |
| A5 | **Kernel process boundary (P6, "kerneld")**: the only path to the truth | verify-the-verifier P6; made load-bearing by evalB r2-002 | STR; RST (Saltzer–Schroeder, Capsicum) | 1/5/2/3/4 |
| A6 | **Campaign-scope, release-pinned cassette replay (process NI)** | evalB B1 = evalA Idea 2 (joint) | STR; closest prior art secure multi-execution plus self-composition; delta = LLM calls pinned by cassettes, releases pinned | 3/4/2/3/4 |
| A7 | **Release ledger, verdict embargo and transcript bound** | evidence-economics P1 (F1 toy) | STR; RST of Dwork et al. 2015 / Blum–Hardt | 2/4/3/4/5 |
| A8 | Double-entry reconciliation of the release ledger against the sandbox IO log | naturalist P11 | USE but FLW as "backbone": provenance-based, so blind to A1 | 2/2/1/3/3 |
| A9 | Feature provenance tags plus a human legitimacy ruling | crossdomain (toy) | USE; RST (Kaufman et al. 2012 leakage legitimacy) | 2/3/3/4/3 |
| A10 | Declared-dependency contract: one replay, decisions compared | evalB B3 (amended by evalA's float toy) | USE; RST (input-sensitivity / NI testing) | 2/3/3/4/3 |
| A11 | Maximally distant counterfactual worlds (complement plus constant) | evalA Idea 3 (toy) | INC | 2/4/3/5/2 |
| A12 | NI unifier: one self-composition engine with a secret→observer table | evalA Idea 1 (narrowed to 7/8 findings) | USE; RST (2-safety hyperproperties, Clarkson–Schneider) | 2/3/2/3/3 |
| A13 | Truth outside the root set is an open-ended channel (image, weights, row identity in X) | evalB r2-001/r2-002; evalA's "5→7 channels" | USE (framing) | 3/5/2/–/4 |
| A14 | Permuted-label control, the "GAAR" | institutionalist; naturalist; historian S | DRP / FLW (maverick's defeat device, toy) | –/1/3/–/– |
| A15 | k-world Bayes-ceiling control | maverick | FLW as primary (tuned device cheats 27%, caught 29%); keep as a printed bound for the tolerance class | 2/2/3/4/2 |
| A16 | McNemar fallback for nondeterministic claims | maverick (toy) | USE (bounded stealth margin of about 1 point) | 2/3/3/4/2 |
| A17 | F4 transductive (example-independence) invariant | scout-AI U3; board | STR (a real, observed leak class) | 3/4/2/5/3 |
| A18 | Dispersion forensics: P4a kept, P4b flawed | historian; naturalist C2 | P4a USE; P4b FLW | 2/3/2/5/2 |
| A19 | Closed-enum Kernel reasons (confused-deputy / error-oracle channel) | crossdomain T12 (free text verified in `checks.py`) | USE (cheap) | 2/4/4/5/2 |
| A20 | Decoys plus per-channel attempt telemetry and a displacement index; the gate version dropped | naturalist; crossdomain; historian; maverick | USE (monitor); gate DRP | 2/2/1/4/2 |
| A21 | Plan–code divergence: the executed test ≠ the registered test | historian (displacement post) | USE (an important gap nobody built) | 3/3/1/4/3 |

### B. Statistics and credit

| id | Idea | Origin | Verdict | N/S/E/B/I |
|---|---|---|---|---|
| B1 | **VDS as an e-process with p0 fixed at registration** (fixes F2: misses earn positive VDS) | evidence-economics P3 (F2 verified in code) | STR; RST (Ville; Shafer–Vovk) | 2/5/4/5/4 |
| B2 | Per-target α-account, slots, retirement and a power preflight | evidence-economics P2 | USE; RST (α-spending); fixes R2-2 | 2/4/2/5/4 |
| B3 | K3 as a zero-failure reliability demonstration | evidence-economics P4 | USE; RST (success-run testing) | 1/4/2/5/3 |
| B4 | K7 risk pre-registered against a pinned reference | evidence-economics P5 | INC | 2/3/1/3/2 |
| B5 | Certifiability preflight that runs the Kernel's own functions | systems P1 | USE (power analysis) | 2/4/2/5/4 |
| B6 | **Small-train / large-test task design** (certifiable effect 0.135 → 0.005) | constraints §2 | STR (a cheap inversion with a direct bottleneck hit) | 3/4/2/5/5 |
| B7 | Pooled multi-task claims with an IUT harm guard | constraints §3 | USE; RST (fixed-effect meta-analysis, Berger IUT) | 2/4/2/3/4 |
| B8 | Exclusion-limit bounded nulls: Stage-1 positive control, Brazil bands, clipping | maverick H1; historian; institutionalist | STR (product); RST (TOST, CLs/PCL) | 2/3/1/3/4 |
| B9 | Ledger-calibrated forecasts (empirical-Bayes shrinkage) | systems P5 | INC | 1/3/1/4/2 |
| B10 | Realised-vs-forecast alarm, replacing the 80% positive-rate rule | maverick; historian conceded | USE, but evalA 5c: the baseline is self-reported and blind to DEV-1-class leaks | 2/2/2/5/2 |
| B11 | Seasoned vs provisional VDS (replaces escrow) | institutionalist | USE | 2/3/1/5/2 |
| B12 | Escrowed credit; credibility bankroll; α-market | maverick; naturalist | DRP (noise at tens of resolutions; a long-con surface) | –/–/–/–/– |
| B13 | Replication-vs-z curve plus a noise-floor record | naturalist; historian | USE | 2/4/1/5/3 |
| B14 | T7 too-good-to-be-true tripwire: escalate, never reject | crossdomain; board | USE | 2/3/1/5/2 |

### C. Verifying the verifier and governance

| id | Idea | Origin | Verdict | N/S/E/B/I |
|---|---|---|---|---|
| C1 | **The verifier's own exams wear out**: dev/sealed halves, every look ledgered | evalA Idea 4 (toy `sim_screen.py`) | STR; closest prior art adaptive data analysis; applying it to verifier validation sets is new as far as I know | 4/4/3/4/3 |
| C2 | Clean-room reference model plus metamorphic relations (MRs) | verify-the-verifier P2 | STR; RST (S3 ShardStore) | 2/4/2/4/4 |
| C3 | Loophole museum plus mutation gate; derived Kernel test list | verify-the-verifier P1 (conflict markers verified) | USE; RST (mutation testing; Defects4J-style reintroduction) | 2/4/4/4/3 |
| C4 | `verify` as of the certificate's own ledger prefix, with a STALE status (F3) | verify-the-verifier §0 | USE (a real defect) | 2/4/4/5/2 |
| C5 | Findings registry as code; strict-xfail pins on accepted limits | verify-the-verifier P3 | INC | 1/3/2/5/2 |
| C6 | Pure decision core plus a trusted-base line budget | verify-the-verifier P4 | USE; RST (de Bruijn) | 1/4/2/3/3 |
| C7 | Merkle inclusion proofs and automatic anchors | verify-the-verifier P5 | INC; RST (Certificate Transparency) | 1/3/2/4/1 |
| C8 | Monotone defeater bus with a PENDING element; producer admission counted as a look | evalB B4; crossdomain T4; evalA amendments | USE; RST (Denning lattice, eliminative argumentation) | 3/3/1/4/3 |
| C9 | Resweep: honest-corpus screen, typed STALE statuses, graded clawback | institutionalist; crossdomain; historian; naturalist | USE (blocked on the honest corpus; evalA 5a: resweep verdicts are releases; C1: the screen wears out) | 2/3/1/3/2 |
| C10 | Honest "self" corpus with rare categories (AIRE) | naturalist | USE (critical path) | 3/3/1/3/3 |
| C11 | Reverse board with disclosed/sealed attack halves and a fix-generalisation metric | evalA r2-003 + evalB fix | USE; RST (bug bounties; held-out attack variants) | 3/3/1/3/3 |
| C12 | "Exam written by the examinee"; one outside attack per milestone | historian; institutionalist | STR as a risk diagnosis, weak as a prescription (C11 meters it) | 3/–/1/2/3 |
| C13 | Two-speed Kernel; tightening merges at once | institutionalist; maverick | USE | 2/3/1/5/2 |
| C14 | Reputation may only raise scrutiny (Vulcan / xz rule), with a metamorphic test | historian; institutionalist | USE | 2/4/1/5/2 |
| C15 | Kernel reliability diagram: salts as the fast stream, cluster bootstrap | crossdomain; maverick H3 | USE | 2/3/1/3/2 |
| C16 | `self_reported` field type caps the tier | institutionalist §7 | USE (cheap) | 2/4/1/5/2 |
| C17 | Certificate risk-limiting audit (RLA) sharing no component with production; ritual detector | institutionalist; historian | USE | 2/3/1/3/2 |
| C18 | Capture–recapture loophole count | crossdomain | INC (a floor only) | 2/2/2/4/1 |
| C19 | Designer-prediction ledger (1 of 5 claims about one's own mechanism survived a toy) | evalB B7 | INC (n = 5) | 3/3/2/5/2 |
| C20 | Cascade-stage independence measurement (kinetic proofreading) | naturalist #1 | USE (cheap) | 3/4/1/4/2 |
| C21 | Sleeper-harness test (long con) | institutionalist #9 | USE | 2/3/1/4/2 |
| C22 | Composition hazards: unledgered resweep releases, seed provenance, self-reported baselines | evalA Idea 5 | USE | 3/3/1/5/2 |

### D. Randomness, salts and measurement

| id | Idea | Origin | Verdict | N/S/E/B/I |
|---|---|---|---|---|
| D1 | **Freeze, then mint the truth** at 4 levels, with independent CSPRNG streams | joint, evalB + evalA | STR; core RST: prospective competitions (M-competitions, Kaggle forecasting, CASP), fresh-seed procedural evaluation (Procgen), LiveBench/LiveCodeBench, commit-reveal. Delta = systematic use inside a certifier, plus the verifier and screen levels | 3/4/3/4/5 |
| D2 | `draw()` with a per-purpose input table (beacon vs secret plus beacon) | 4 thinkers converged | USE; RST (commit-reveal, public beacons, RLA dice) | 2/4/2/5/3 |
| D3 | Salt stream / Audit Plane Layer B: positive salts with effect-size coverage first; nulls as a gross alarm; detectability exam | all 5 thinkers; naturalist amplifier toy | USE; RST (LIGO blind injection, ITF) | 2/2/3/2/2 |
| D4 | Defeat device as a standing red-team primitive ("defeat device first") | maverick | STR as methodology; RST (adaptive-attack evaluation, Tramèr et al. 2020) | 2/5/3/5/3 |
| D5 | Detectability exam: d̂ upper bound ≤ 0.2 at 40 + 40 presentations, (1−d) correction | crossdomain; naturalist; historian | USE | 2/3/2/4/2 |
| D6 | Seed-subset fraud proofs; optimistic acceptance | crossdomain T2 | DRP (reverse p-hacking, toy 0.96); bisection kept as USE | –/1/3/–/– |
| D7 | Gao R(D) stop rule | crossdomain T5 | DRP (sentinel metric kept, INC) | –/1/2/–/– |
| D8 | Prosecutor with limits (two-signal licence, α capped at 0.2×) | maverick; naturalist | INC (later) | 2/3/1/2/2 |
| D9 | Validity intervals (`valid_until`) | crossdomain T8 | INC | 2/3/1/3/1 |
| D10 | External anchor required for future-data K2 | crossdomain T9 | USE (cheap) | 1/4/2/5/2 |
| D11 | Diverse double re-execution | crossdomain T6 | INC (0/30 historical catches) | 1/3/1/4/1 |

### E. Research capability and product

| id | Idea | Origin | Verdict | N/S/E/B/I |
|---|---|---|---|---|
| E1 | **Second model family**: local 3–7B or a free official API, admitted by role exams ("costs yield, not integrity") | constraints §1 | STR (unblocks invariant 5 and the headline) | 3/4/2/4/5 |
| E2 | Fewer calls: execution as the ranker, packed calls, headroom gate, Codex quota diet | scout-evolve U1/U4; constraints §4 | USE (13 → about 5–6 calls) | 2/4/3/4/4 |
| E3 | One-SE parsimony selection; out-of-fold signatures for dedup and an exact-dev ensemble | scout-evolve U2/U3 | USE; RST (Breiman, FunSearch) | 2/4/3/5/3 |
| E4 | CPU-time limits plus a contention witness | constraints §5 | USE | 2/4/2/4/3 |
| E5 | Cassettes (recorded replay for development) | systems P2 | USE; RST (VCR) | 1/4/2/5/3 |
| E6 | Research graph, `chh explain`, limitations register | systems P3 | USE | 1/3/1/5/2 |
| E7 | Claim Audit of published sklearn-scale claims | product rank 1 | STR (product) | 3/3/1/4/3 |
| E8 | Label-dependence audit of published ML code | evalA Idea 6 (narrowed by evalB A3) | USE | 3/4/2/4/3 |
| E9 | Lab-analysis notary, kinetic reproduction pack, bound manuscripts, non-confirmation registry | product 2–5 | USE (notary, manuscripts) / INC (kinetic, registry) | 2/3/1/3/2 |
| E10 | Certificate-gated compiled world model (Kosmos, done safely) | scout-AI U1 | USE | 3/3/1/3/3 |
| E11 | Blind-rediscovery tasks | scout-AI U2 | USE (needs cutoff certainty) | 2/3/1/3/3 |
| E12 | Baseline-anchored mode | scout-AI U3 | USE | 2/4/1/3/3 |
| E13 | Calibrated idea triage; BixBench import | scout-AI U4/U5 | INC | 1/3/1/4/2 |
| E14 | Generated task families, evolve split only, with shuffled-label null arm | emergence 1 | USE; RST (POET, OMNI) | 2/4/1/3/3 |
| E15 | MAP-Elites archive; interestingness for allocation; FunSearch-lite on LP; red/blue mutation-set verifiers | emergence 2–5 | INC (RST of MAP-Elites, DGM, learning progress, FunSearch) | 1/3/1/3/2 |
| E16 | Anomaly-first Formulator: dev-only scan, declared cell grammar | maverick; naturalist; historian | USE | 2/4/2/3/3 |
| E17 | Copilot with ledgered interventions; human influence removes credit | systems P4 | INC | 2/3/1/4/2 |
| E18 | OpenML upload-date time split | evalB B6 (amended for the model cutoff) | INC; RST (LiveBench-style time splits) | 2/3/3/3/3 |
| E19 | Certified leaderboard | evalB B5 | RST (Blum–Hardt Ladder) | 1/4/1/2/1 |
| E20 | Forecasting external trials; expert priors; strong inference; amnesia; long-shot quota; compression currency; baseline blinding | maverick H5/7/8/10/11/12/13 | DRP or parked (each with its named objection) | – |

---

## 2. Notes on the strongest objection and the cheapest decisive test (top-rated items)

- **A1/A2 (answer keys).**
  - *Objection:* candidate programs may never think to call `load_wine()`. *Rebuttal:* an evolver selects for
    exactly that, and Codex knows sklearn ships the data.
  - *Test:* a museum entry that looks up the bundled set on wine-real. Prediction: it certifies today and fails
    after an exporter image split plus a decompressed-layer truth scan. For A2: HMAC streams drop evalA's cloning
    attack to honest accuracy.
- **A3–A6 (information-flow stack).**
  - *Objection:* honest byte-determinism may fail. The Codex CLI and BLAS make this likely; it is unmeasured.
  - *Test:* replay the two live runs twice with real labels. If any prompt or prediction byte differs, the exact
    class is not the workhorse (the corpus's own kill rule). Then: 0 flags before P6 on a planted direct-store
    profiler, 10/10 after.
- **A7/B1.**
  - *Objection:* humans who read numbers are outside the bound.
  - *Test:* F1 against the real `k4_statistics` (embargo ≤ 0.065; continuous ≥ 0.30). F2 regression: a q = 0.05
    miss scores ≤ 0.
- **B6 + D1 combined: minted large synthetic test sets** (this combination is mine). Regenerate each held-out set
  after the freeze, at 20k rows, with HMAC streams.
  - *Objection:* claims become generator-scoped, and the evolver can Goodhart the generator family.
  - *Test:* the minimum certifiable effect is ≤ 0.02 on every synthetic task; evalA's cloning attack fails; the
    51-claim suite still shows 0 false certificates.
- **C1.**
  - *Objection:* it halves a corpus that does not exist yet.
  - *Test:* a builder patches historian's P4b on the dev half only. Prediction: ≥ 1 flip on the sealed half.
- **E1.**
  - *Objection:* a CPU 7B replicator may be too weak.
  - *Test:* run the three role exams. The phi correlation of misses against Codex must be < 0.8.

## 3. Top 5 overall (impact × soundness × cost)
1. **A1 + A2: remove both answer keys.** This means the exporter image split, a truth scan, and HMAC-seeded
   streams. Both are verified preconditions of live holes on main, and the fix is cheap.
2. **B1 + A7: the VDS e-process and the release embargo.** They close the verified F2 and the simulated F1. The
   evolution score must drop its continuous held-out term.
3. **The information-flow stack, in order: A4 → A5 → A3 → A6.** Pin entropy; build the P6 boundary; NI against
   the stored production output (comparing decisions); then campaign-scope replay.
4. **D1 × B6: minted large synthetic test sets.** This attacks the scarcest resource (certifiable signal) and the
   leak classes at the same time.
5. **E1: a second family with role exams.** Without it there is no headline, no replication credit, and no M4
   critic.

Runners-up: C1 (exams wear out), C2 (clean-room model), E2 (call diet), B8 with E7 (exclusion-limit Claim Audit).

## 4. Breakthrough list
**No breakthrough.**

The closest candidate is **D1, freeze-then-mint**. It fails the novelty bar. "Evaluate a frozen artifact on data
created after the freeze" is established practice:
- prospective forecasting competitions;
- CASP;
- fresh-seed Procgen evaluation;
- LiveBench time windows;
- cryptographic commit-reveal.

Its delta is systematic application inside a certifier, at the claim, edit, Kernel and screen levels, with the
independent-stream precondition. That is good engineering, not a qualitatively new guarantee: it gives exactly the
fresh-i.i.d.-sample guarantee. It also does not survive its strongest objection for real-world science, because
fixed public data cannot be minted.

**C1** is the most novel item (4/5). It is a modest guarantee, supported by a toy only.

**A1/A2** are the most valuable items, but they are bug findings, not ideas.

evalA's round-2 file calls minting "the strongest single principle". I agree with that and disagree only on any
breakthrough label, which evalA did not claim either.

## 5. Risks that remain even if everything recommended is built
1. **Memorisation in the weights** of public datasets and papers. No root can be injected, so only minting and
   post-cutoff time splits help. The executor's cutoff is uncertain.
2. **Specification gaming above the Kernel.** The Kernel certifies procedure, not meaning. Claims can be true but
   trivial, generator-scoped, or the right test on the wrong question. Plan–code divergence (A21) and statement
   fidelity are still unchecked.
3. **Goodhart onto the evaluation distribution.** This covers generator families, a small LOFO pool, and gates
   shared by every draw ("pool-common gaming"). Minting makes this the *main* attack surface.
4. **A deceptive optimiser that sandbags.** It can weaken positive controls, under-perform on salts it can detect,
   or aim for margins just past δ. Detectability is bounded only at d̂ ≤ 0.2, and stealth margins are printed, not
   closed.
5. **Correlated designers.** The same AI family wrote the Kernel, its tests, the museum, the honest corpus and these
   reviews. C1/C11 meter this only if outsiders exist, and today they do not.
6. **The human channel and scalable oversight.** One owner approves adapters, approval nodes and envelopes. That
   invites rubber-stamping (ritual), persuasive LLM summaries in front of hash-bound items (maverick), and steering
   after reading numbers. There is no mechanical check.
7. **The trusted base and the supply chain.** The Docker image, pip packages, the Codex CLI, the WSL2 kernel,
   Python and SQLite. Hermeticity and P6 shrink exposure but do not verify these.
8. **Side channels.** Timing, resource use, and crash-versus-success patterns across replays. Pinning removes some.
9. **Aggregate error under unlimited targets.** About α·N false certificates still exist. Only the VDS e-process
   and replication bound them.
10. **Determinism as a dependency.** If honest pipelines cannot be made byte-deterministic, the strongest
    mechanisms (A3, A6, A10) degrade to bounded-margin statistical checks.
