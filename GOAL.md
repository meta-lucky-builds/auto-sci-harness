# GOAL — the harness we are building

Date: 2026-10-06. Owner: user (product decisions) · Claude (design + build).
**This is the single source of truth for WHAT custom-harness is and when it is done.**
`PLAN.md` = design history and rationale. `STATUS.md` = what is built today. Where they disagree with this file, this file wins.

Inputs this file is built on (read them only if a detail is needed):
- `../autoresearch_survey/00_SYNTHESIS.md` — 36 sources, first-principles model, 12 novel ideas (cited **S§n**).
- `code_review/00_CODE_REVIEW_SYNTHESIS.md` — the public code of 27 of those systems, read line by line (cited **CR§n**).
- Two surveys: arXiv 2508.14111 (Agentic Science) and arXiv 2608.05179 (the "Verification Gap" audit) (cited **VG**).

---

## 0. The one-paragraph goal

Build **custom-harness**: an autonomous research system whose research is trustworthy by construction and which
gets better at research by itself. At its centre is an **Evidence Kernel**: a small, mechanical, Lean-like checker
for scientific claims. Lean checks "does this follow from the axioms". The Kernel checks "was this claim stated
precisely, committed to before the data was seen, tested in a way that could have failed, reproduced from raw
artifacts, statistically valid, and confirmed on data the system had never touched". Every claim the system publishes,
every skill it keeps and every change it makes to itself must pass the Kernel at a stated strength. The harness
improves itself only through **validated loops**, driven by external evidence, never by its own metric alone. It does
everything the 36 surveyed systems do combined. It fixes what all of them failed to do. It proves its superiority on a
pinned, held-out, cost-matched scorecard that anyone can rerun from a single bundle.

---

## 1. What we learned, and the commitment each lesson forces

### 1.1 From the survey (S)
| Lesson | Commitment |
|---|---|
| Capability is capped by the verifier (S§1) | Verification is the product's core, not a feature. The Evidence Kernel (§4) sits at the centre. |
| Real discoveries happen only where checking is cheap and exact (S§1, S§3) | Maximise the share of work under hard verifiers; build new verifiers (§5.L3 verifier synthesis). |
| Full traces beat scalar scores (S§4.1) | Every step is traced; optimisers read traces, including successful ones. |
| Overfitting to the evolution set is the default (S§4.2) | Task splits are evolve/gate/sealed; winner re-measured; leakage critic; time-split checks. |
| Structure beats prose (S§4.3) | Edits are typed and structural; prompt rewording is one edit type among many. |
| Gains follow an inverted U over model strength (S§4.4) | The capability curve is measured per role; strong models propose, cheaper models execute. |
| Relative judgment beats absolute (S§4.5) | All soft judging is pairwise or K-way, in both orders, with Bradley-Terry fits and CIs. |
| Remembering failures pays (S§4.6) | Negative memory is first-class and generated automatically from verified outcomes. |
| The LLM proposes, deterministic code decides (S§4.7) | Every gate, admission, budget and kill decision is code. An LLM may only downgrade, never grant. |
| One harness doesn't fit all (S§4.8) | Harness branches plus a router, with routing decided from gate evidence. |
| Reward hacking really happens (S§4.9) | Exploit quarantine and red-teaming are mandatory before anything persists. |
| The field is split: self-improving harnesses vs AI scientists (S§6.2) | Fuse them: optimise the research harness against research-outcome verifiers. |

### 1.2 From reading the code (CR)
| Finding in the public code | Commitment |
|---|---|
| In most repos the headline mechanism is missing, weaker or different (CR§2) | Every capability we claim exists in code, with a test, and appears in an ablation. No README-only features. |
| LLM code runs on the host, often with API keys (AI Scientist v1/v2, Station, SciDER, PARNESS, Prime…) | Agent code runs only in an isolated sandbox: no network by default, no secrets, non-root, read-only root filesystem. |
| Graders, test labels or scores are visible to the agent (SciDER grader in prompt, SIA labels, Meta-Harness test in workspace, Adaptive git messages) | Hidden data lives outside every agent mount. `_private` state can never be rendered into a prompt. A canary check and leak audit run on every campaign. |
| Accept on one noisy mean or a strict float comparison; winners never re-measured (AutoSaddler, Self-Harness, HarnessX, RRSI, AI-Sci v2) | Paired CIs, racing, a fresh re-measurement of the winner, a held-out gate, and a sealed split. |
| Self-approval: the same LLM reviews and approves persistent writes (Prime, Continual, EvoMaster, PARNESS) | Persistence requires Kernel evidence at a tier floor. Any critic must be a different model family from the proposer. |
| Fail-open defaults (Adaptive reveal gate, SciDER rank fallback 0.5, Robin parse-error strings, AI-Sci "missing = 0") | Everything fails closed. Parse failure means reject. A missing date means unrevealed. An infrastructure failure means excluded, not scored. |
| Prompt-only invariants ("do not hard-code", "keep held-out separate") | Every invariant is enforced in code and covered by a test. Prompts only explain the rules. |
| Fabricated numbers in reports (MatAgent) | Reports are generated from the claim graph; every number resolves to an artifact, and the audit recomputes it. |
| Strong pieces exist but are scattered (Meta-Harness proposer workspace, AutoSaddler diagnosis, RRSI critic + budget, ScienceIDE fault injection, ScienceFlow restore, SoL-Pi ObservationPack, AutoOPT comparator, Feynman retrieval…) | We reimplement the best version of each inside one typed, gated core (§5). |

### 1.3 From the two papers (VG)
| Finding | Commitment |
|---|---|
| 83% of systems release code, but only 38% release seeds or traces (VG) | Every run ships a **Reproducibility Bundle**: code, spec hash, seeds, traces, data hashes, environment lock and a one-command rerun. |
| Only 38% report any novelty verification (VG) | Novelty is a first-class, mechanically checked field: a literature-distance check against a dated snapshot, plus the risky-prediction requirement (§4). |
| Of 9 closed-loop systems, 7 are "mechanical" (re-optimise an internal metric) and only 1 is externally validated (VG) | **Every loop is labelled MECHANICAL or VALIDATED, and the product's self-improvement must be VALIDATED** (§6). |
| An eight-tier verification ladder, from formal proof down to model opinion (VG) | Our T0–T6 tiers stay as the Kernel's strength scale and are mapped to VG's ladder in reports. |
| LLM-as-judge circularity; panel novelty correlates poorly with execution success (VG) | LLM judgment can never certify a claim above T5. The judge must be validated against human labels before use. |
| Contamination; few held-out benchmarks (VG) | Time-split evaluation, cutoff-dated models, contamination probes, sealed splits. |
| No formal benchmark for discovered knowledge vs ground truth (2508.14111) | Our prediction ledger and time-split rediscovery set *are* that benchmark, and we release it. |
| Reporting checklist: seeds, traces, selection policy, human-in-the-loop points (VG Table 10) | Every report auto-fills the checklist; a missing item blocks publication. |

---

## 2. Definition of "best" — the win conditions

We are best only when all of these hold, measured on the **sealed split, at matched or lower cost, with CIs**:

1. **Coverage:** every capability in the coverage contract (§9) exists in code, with a test, and appears in an ablation.
2. **Validated loop:** our self-improvement is VALIDATED. At least one externally validated improvement result:
   a harness evolved on time window T improves risky-prediction success on a later window T2 (§6.4).
   In the VG audit, only one closed-loop system (pre-LLM, physical) achieved this.
3. **Scorecard:** on each axis below we match or beat the best **re-run** baseline (or the "as reported" number where
   the code can't reproduce it, labelled as such):
   - A. Agent-harness quality: Terminal-Bench 2.x, SWE-bench Verified, GAIA (pinned versions).
   - B. Self-improvement efficiency: gain per 1k rollouts; OOD drop after evolution (must be ≤ baseline's).
   - C. Research execution: MLE-bench-style suite, our ML task suite, ScienceIDE-format ML-repair family.
   - D. Discovery under hard verifiers: known-value bound problems; ML records with T2 certificates.
   - E. Research judgment: ResearchBench retrieval/ranking (eval-only), SDE sentinel, **our time-split prediction benchmark**.
   - F. Integrity: 0 claims without evidence, 0 unverifiable citations, 100% reports with checklist and Reproducibility Bundle.
   - G. Cost: $ / GPU-h / quota per Kernel-certified claim.
   - H. Robustness: planted-exploit catch rate; red-team success rate trending to 0; leak-audit pass rate 100%.
4. **Reproducibility:** a third party reproduces every headline number from the bundles with one command.
5. **Honesty:** any axis where we lose is reported, not hidden.

"Better than the surveyed systems" therefore means better than **what their public code demonstrably does**, plus
matching their reported numbers wherever we can test the same thing.

---

## 3. System picture

```
                         ┌─────────────────────── L9 Governance & Studio ─────────────────────────┐
                         │ tier policy · approvals · dashboards · kill switch · finalize/freeze      │
                         └─────────────────────────────────────────────────────────────────────────┘
  L8 Weights (slow) ── trains only on trajectories certified by TWO disjoint verifier sets
  L7 Search-over-search ── islands · orchestrator · stall detection · lever choice (harness/weights/budget)
  L6 Harness Evolution ── agentic proposer → typed edits → critic (other family) → smoke → race → re-measure → gate → branch/route
  L5 Memory & Skills ── ONE store API: evidence + tier floor + quarantine + derivation graph + read audit
  L4 Research Loop ── formulate → profile → literature → hypotheses → PRE-REGISTER → experiment tree → critic → replicate → claims → report
  ┌──────────────────────────────── EVIDENCE KERNEL (trusted core) ────────────────────────────────┐
  │ Claim Language · Pre-registration commits · checks K1–K9 · Certificates (tier) · Prediction Ledger │
  └────────────────────────────────────────────────────────────────────────────────────────────────┘
  L3 Verifier Ladder ── T0 formal · T1 executable · T2 statistical · T3 physical · T4 time-split · T5 judge panel · T6 none
                         + fault-injection validation · calibrated tolerances · exploit quarantine · red team · verifier synthesis
  L2 Evidence Store ── append-only, content-addressed; claims only from evidence; Reproducibility Bundles
  L1 Control Plane ── budgets (persisted) · scheduler · admit/timebox/kill · infra-vs-policy failures · eval allocator
  L0 Substrate ── typed spec interpreter · traces · checkpoints · resume · fork · fan-out · approval nodes
                  · Docker/gVisor sandboxes · exact snapshots · model gateway (OW / API / BYOS allowlist)
```

Rule: a layer may call layers below it. It influences layers above only by writing Evidence. The Kernel is the only
component allowed to mint a Certificate.

---

## 4. The Evidence Kernel — the novel centre (the "Lean for science")

### 4.1 Principle
Science has no axioms, so truth can't be derived. What *can* be checked mechanically is **how a claim earned trust**.
The Kernel certifies the procedure, not the world. Its ground truth is **reality the system has not yet looked at**:
held-out data, future data, later literature, new experiments, and hard background laws.

Lean analogy, kept deliberately:
| Lean | Evidence Kernel |
|---|---|
| Theorem statement | **Typed Claim** (Claim Language) |
| Proof | **Evidence Bundle** (pre-registration + code + data hashes + runs + statistics) |
| Small trusted kernel | Small, deterministic Kernel code (target < 3k LoC, 100% test coverage, no LLM inside) |
| `sorry` / axioms audit | **Assumption audit**: every untested assumption is listed explicitly and lowers the tier |
| Proof checks or fails | Certificate issued at tier T*, or rejected with machine-readable reasons |

### 4.2 Claim Language (ML/DL first)
A claim is a typed object, never prose:
```
Claim {
  kind: improvement | equivalence | scaling_law | ablation_effect | negative_result | rediscovery
  subject: method/artifact ref (content hash)
  comparator: baseline ref (content hash), re-run under identical conditions
  metric: registered metric id (+ direction)
  effect: { minimum_effect δ, predicted value or interval }
  scope: { datasets[], conditions{}, populations } — where the claim is asserted to hold
  generalization_targets: datasets/splits/time-windows NOT used in development
  analysis_plan: test, seeds n, alpha, multiple-comparison method, stopping rule
  assumptions: explicit list (iid, no leakage, etc.)
  risk: prior/baseline predictive distribution for the outcome (makes "risky" measurable)
}
```
Prose in reports is generated from Claims. A sentence that asserts something must link to a Claim.

### 4.3 Kernel checks (all mechanical, all fail closed)
| Check | Passes iff | Kills |
|---|---|---|
| **K1 Well-formed** | Claim parses; scope, metric and comparator resolve; δ > 0 is stated | vague claims |
| **K2 Pre-registered** | Claim + analysis plan hash was committed to the ledger *before* any run touched the generalization targets (event order checked in the trace) | p-hacking, HARKing |
| **K3 Reproduced** | Re-execution from the bundle yields the reported numbers exactly (deterministic) or within calibrated tolerance | fabrication, irreproducibility |
| **K4 Statistically valid** | Plan was followed; seeds ≥ n; test matches design; multiple-comparison correction; paired seed CI and test-example bootstrap CI exclude 0 by ≥ δ | noise, winner's curse |
| **K5 Comparator integrity** | Baseline re-run under identical data, seeds, budget and contract; baseline conforms to the spec (I2) | strawman baselines |
| **K6 Held-out confirmation** | Prediction confirmed on generalization targets never seen during development (sealed data, new datasets, later time window) | overfitting, leakage |
| **K7 Risk** | Outcome was improbable under the declared baseline/prior; information gain ≥ threshold (proper scoring rule) | trivially safe claims (Goodhart) |
| **K8 Background consistency** | Registered invariants hold (unit/dimension checks, conservation laws, sanity bounds, metric range) | physically or numerically impossible results |
| **K9 Leak & canary audit** | No canary string or hidden-data hash appears in any agent-visible artifact or trace; grader not visible | grader gaming, label leaks |

Plus **K0 Provenance (always)**: every number and citation resolves to an artifact (current audit I1/I3/I4).

### 4.4 Certificates and tiers
The Kernel issues `Certificate{claim, tier, checks_passed, checks_failed, assumptions, bundle_hash, verifier_ids}`.
- Tier = the weakest link of the strongest verifier chain that passed (T0 … T6, mapped to VG's ladder in reports).
- Labels: **CERTIFIED-VALIDATED** (K1–K9 including K6 on external or future data), **CERTIFIED-INTERNAL** (K6 on
  sealed internal data only), **SUPPORTED** (judge or retrospective only), **REJECTED** (with reasons).
- Certificates are immutable. New contrary evidence creates a **retraction** that cascades through the derivation graph
  (claims, skills and memory built on it).

### 4.5 The Prediction Ledger — the system's track record
Every pre-registered prediction is logged with its declared probability or interval. When outcomes resolve
(held-out runs, later windows, replication), they are scored with a proper scoring rule (log or Brier) against the
baseline prior. This gives:
- **Calibration**: does the system know what it knows?
- **Validated Discovery Score (VDS)**: the sum of information gain from confirmed risky predictions, minus penalties for
  confident misses. VDS is the main objective that L6 optimises (§6.3). It can't be gamed by safe claims (K7) or by
  hiding failures, because every pre-registration is counted, resolved or not.
- **Released benchmark**: the ledger plus the time-split set becomes the public benchmark for "discovered knowledge vs
  ground truth" that 2508.14111 says is missing.

### 4.6 What the Kernel does not do (stated in every report)
It doesn't judge importance or creativity. It doesn't detect confounders absent from the data. It doesn't validate
measurement instruments. It certifies inference, not reality. Soft qualities stay at T5 and are labelled as such.

---

## 5. Component specifications

Each component lists **Must** (requirements), **Never** (invariants), **Done when** (acceptance test), and the sources
it absorbs and surpasses. (✅ already built · 🟡 partial · ❌ missing, as of 2026-10-06.)

### L0 Substrate
**Runtime** (🟡) — typed `HarnessSpec` (JSON Schema) interpreted as a graph.
- Must: full trace per step; checkpoint per step; `fork(step, state_patch)`; **`resume(run_id)` after crash** (CR P0-4);
  **MapNode fan-out** with per-branch persisted outputs, deterministic ids and declared reducers (LangGraph); **Approval
  node** (PAUSED → human); transient-only retry policy that charges every attempt; unmatched edge = FAILED ✅; any
  crash leaves FAILED, never RUNNING ✅.
- Never: pickle or arbitrary deserialisation of untrusted data; silent defaults.
- Done when: kill -9 mid-run, then resume, completes with an identical trace prefix and no double-charged cost.
- Absorbs: LangGraph (resume, fan-out, interrupts) without its heuristic resume detection or async-durability loss;
  PARNESS DAG kernel (minus the dead DSL); ScienceFlow's single-writer state.

**Sandbox & snapshots** (🟡)
- Must: Docker, gVisor when available, network off plus allowlist, caps dropped, non-root, read-only root, no secrets ✅.
  Snapshots: content-addressed, **preflight all blobs → archive live workspace → restore → rollback on failure**; keep
  modes and symlinks; fingerprint-skip unchanged files (ScienceFlow, CR P0-3); per-campaign cloud-GPU backend.
- Never: run agent code on the host; mount hidden data into an agent container.
- Done when: a corrupted blob mid-restore leaves the original workspace intact; a planted host-escape test fails.

**Model gateway** (✅ core) — three access modes (OW / API / BYOS allowlist), provenance on every output, strict
schemas, redaction. Add: per-host pacing; cost tables; the provider registry verified each release; same-family detection
(needed for the "critic ≠ proposer family" rule).

### L1 Control plane (🟡)
- Must: budgets in neutral units **persisted across crash and fork** plus a wall-clock unit; provider outages uncharged
  (ScienceFlow); **infrastructure failure vs policy failure** classification, with infra-failed units excluded from paired
  comparisons and retried (AutoSaddler, ScienceIDE; CR P0-1); evaluation allocator (racing, successive halving, value of
  information); LLM controllers may only downgrade (deny, shrink, kill-with-code-checked-evidence).
- Done when: a Docker outage mid-campaign changes no accept/reject decision.

### L2 Evidence store & reporting (🟡)
- Must: append-only, content-addressed ✅; claims only from evidence ✅; **Reproducibility Bundle** per run and per claim
  (VG); **reporting checklist** auto-filled (seeds, traces, selection policy, human-in-the-loop points, cost); Postgres
  backend for campaigns; read-only **evidence filesystem export** for the proposer (L6).
- Done when: `chh reproduce <bundle>` on a clean machine reproduces a headline claim.

### Evidence Kernel (❌ — §4). The highest-priority novel component.
- Done when: (a) on a seeded set of 50 ML claims (true, p-hacked, leaked, irreproducible, strawman-baseline, trivial),
  the Kernel accepts the true ones and rejects each failure type with the correct reason; (b) Kernel code has 100% branch
  coverage and contains no LLM call.

### L3 Verifier ladder (🟡)
- Must:
  - **Verifier validation by fault injection**: each T1/T2 verifier carries evidence that it rejects planted faults
    (ScienceIDE).
  - **Calibrated tolerances** from measured noise floors (nominal / perturbed / rebuild; seed spread).
  - **Float-screen → exact-certificate** gate where exact checks exist (Station).
  - **T0 split**: Lean build + hygiene vs Lean + comparator against a human-approved Challenge spec, with axiom audit
    and statement-fidelity gate (AutoOPT).
  - **Judge panel (T5)**: ≥ 3 families, both orders, ensemble + meta-review (AI-Sci v1 design with its bugs fixed),
    rubric decomposition (MM-Agent), BT with CIs (Robin, fixed); **validated against human labels before use**.
  - **Exploit quarantine**: any skill or edit gain must survive a perturbed evaluator (hidden invariants, stricter spec,
    randomised variant).
  - **Red team**: an adversary agent tries to get a false claim certified; successes become regression tests.
  - **Verifier synthesis**: for soft problems the system writes property tests, invariants and simulators; they count only
    after fault-injection validation; capped at T2 until a human reviews them.
- Never: an LLM verdict substring-matched as a pass (Adaptive, PARNESS, SciDER pitfalls).
- Done when: the planted-exploit suite (including an RCON-style "found an evaluator backdoor" case) is caught ≥ target rate.

### L4 Research loop (🟡 v0 live)
Stages (each a node with a contract, all evolvable):
1. **Problem Formulator** (❌, novel): mines anomalies, open problems and failed replications from the literature graph;
   ranks by (best available verifier tier) × value × feasibility.
2. **Data profiling** (🟡): full-file profile plus outliers, cardinality, key graph, column roles; **ideation is
   conditioned on it** (SciDER promised this, its code doesn't do it).
3. **Literature** (❌): arXiv / Semantic Scholar / OpenAlex / Crossref with pacing and fallback, query splitting
   (Feynman, Robin); dated snapshots plus cache; citation check = ID resolution (resolves / doesn't / lookup failed) +
   title/year/author match + **quote-span support** → K0/I3.
4. **Hypotheses** (🟡): diverse proposers; forced distance-3 cross-domain inspiration; literature-mined mechanisms;
   topic quotas; diversity measured by error correlation, not vendor count (SDE r>0.8).
5. **Tournament** (✅): shuffled K-way rounds → BT; add both-order pairwise mode, BT CIs, Improve-weakest and Combine
   operators (SciDER), pinned incumbent.
6. **Pre-registration** (❌): typed Claim + analysis plan committed to the Kernel ledger before execution.
7. **Experiment tree** (❌): draft / debug / **improve** nodes, code-scored seeded selection, dev racing against dev
   overfitting, stop when certified; tune → creative → ablation curriculum (AI-Sci v2, done properly); exact backtracking
   via snapshots; revivable dead-ends with a persistence rule (P(improvement) > θ).
8. **Socratic critic** (🟡 example only): questions only, answers must cite evidence (AHOIS).
9. **Replication agent** (❌, novel): re-implements from the claim text without access to the code; agreement is
   required for CERTIFIED headline claims.
10. **Claims → Kernel → Report** (🟡): reports generated from certificates; checklist; bundle; VG-ladder mapping.
- Context management: archive large outputs to blobs, keep a handle plus head/tail in context, provide paged recall
  (SoL-Pi ObservationPack); verified-quote log reducer with raw fallback.
- Done when: on the ML suite, ≥ X% of runs end with a CERTIFIED or honest NEGATIVE claim, 0 fabricated numbers,
  0 leaks. X is set after the M1 baseline.

### L5 Memory & skills (❌)
- Must: **one store API** for memory, skills and playbooks. Writes require evidence ids and a tier floor; quarantine
  happens before an entry is readable; a **derivation graph** enables cascading revocation; entries are rendered as quoted
  data, never as instructions (Prime Agent's failure, inverted). Kinds: positive, negative (generated automatically from
  verified failures), dead-end (with snapshot ref), lineage, playbook (with maturity stats), method library (MM-Agent
  hierarchy plus outcome stats). Relations: complements / contradicts / supersedes, with demotion of superseded
  entries (EvoScientist, fixed). Read audit: unused entries are summarised or retired.
- Never: an agent writes directly to global memory; LLM self-approval.
- Done when: a planted exploit-skill is blocked; revoking one claim removes every dependent entry.

### L6 Harness evolution (🟡 core built, proposer weak)
Pipeline per generation:
1. **Evidence export**: read-only filesystem of all candidates, full traces (including successes), scores, ledger,
   Kernel certificates and rejects.
2. **Agentic proposer** (Meta-Harness, done safely): a tool-using agent inside a sandbox with that export, read-only;
   output is **typed edits only** (JSON-pointer ops on the spec) with a predicted effect.
3. **Structured diagnosis** (AutoSaddler + Self-Harness): first-divergence causal procedure; causal signature (cause,
   criticality, mechanism) with verifier evidence; the claimed diff is checked against the real one.
4. **Static checks** ✅: protected nodes (setup/verify/audit/Kernel); audit-only terminal; leak patterns; plus a
   deterministic denylist of evolve/gate task ids.
5. **Critic from a different model family** (RRSI, fixed): leakage, degeneracy, grader gaming, unbounded work; reasons
   are fed back for ≤ k repairs; Socratic mode.
6. **Decline / no-op allowed**; one mechanism per candidate; novelty gate against re-proposing reverted signatures
   (HarnessX).
7. **Smoke run**: one task through the real loop.
8. **Race** ✅ (paired CI successive halving) → **re-measure the winner on fresh seeds and units** (CR P0-2) → **gate** ✅
   on the never-shown split, with a unit-alignment check and a cost term (RRSI).
9. **Branch / route**: if a candidate wins on a cluster but not globally, create a branch; route by descriptors
   **generated from gate evidence**; a select/materialize adaptation seam (Adaptive Auto-Harness, fail-closed); the router
   is tested on the sealed split.
10. **Ledger** ✅: per-task flipped/regressed, `regressed_unpredicted`, predicted vs actual (calibration), yield, prune,
    stall, annealed op budget (RRSI); proposer-written generation report.
11. **Temporal-reveal mode** for streams: labels sealed outside every evolver mount; watermark rule; undated = unrevealed.
12. **Finalize**: a one-shot freeze; the sealed split is touched once, by a human-run command.
- Objective: §6.3 (VDS-centred for research harnesses; benchmark score for agent harnesses).
- Done when: on a real campaign the loop finds and certifies ≥ 1 improvement that holds on the sealed split, with OOD drop
  ≤ RRSI's reported regularised level.

### L7 Search over search (❌)
Islands with different proposer and mutator mixes (MILO idea; no public code exists). An orchestrator detects stalls and
reallocates budget across islands, edit types and **levers** (harness edit / weights / more evaluation), and logs the
lever chosen plus its evidence (what SIA claimed but doesn't do). Edit-type quotas prevent collapse into prompt-only
edits. The orchestrator policy is itself versioned and gated on a slower cadence.

### L8 Weights (❌, after M2)
Two-timescale co-evolution: the harness evolves against verifier set A (fast); weights train against a disjoint verifier set
B (slow) on trajectories certified by **both**; coupled-Goodhart indicator = gap between A-score and sealed score.
Task selection by reward variance (AWorld idea); SFT format filtered by outcome (SciDER format, fixed); the trajectory
licence filter is enforced (only outputs whose terms allow training).

### L9 Governance & Studio (❌, CLI only today)
Tier policy as human-only config; approval queue; dashboard (claim graph, certificates, prediction ledger and calibration,
lineage, cost, live runs); kill switch; `finalize`; "explain this certificate" view. Telemetry is opt-in only.

---

## 6. The loops

### 6.1 Labels (VG)
Every loop instance is tagged **MECHANICAL** (optimises an internal metric) or **VALIDATED** (its acceptance signal comes
from Kernel checks K6 on unseen or future data, or from external measurement). Reports state the label. The product
claim "self-improving research harness" refers only to VALIDATED loops.

### 6.2 Timescales
- Fast: the inner research loop on one task (minutes to hours).
- Medium: harness generations over task batches; L7 adjusts every k generations (hours to days).
- Slow: weights; tier-policy review by humans (days to weeks).
- Retrospective: the prediction ledger resolves as time-split windows and new data arrive (weeks), feeding VDS back into L6.

### 6.3 Objectives
- **Research harness objective** = VDS (confirmed risky-prediction information gain) + rate of hard-verifier discoveries,
  with hard constraints (0 integrity failures, 0 leaks) and cost as a gate term.
- **Agent harness objective** = pinned benchmark pass rate, gated identically.
- No objective ever uses an LLM judge as its only signal.

### 6.4 The headline experiment (M2)
Freeze literature and data at date T (after all executor models' cutoffs). Evolve the research harness on tasks whose
answers resolve in window (T, T1]. Then, with no further changes, run it on window (T1, T2]. **Success = the evolved
harness has higher VDS and calibration on (T1, T2] than the seed harness, with CIs.** This makes the self-improvement
VALIDATED. Contamination probes and a novelty-weighted subset are mandatory.

---

## 7. What everyone else failed to do → our requirement → proof

| Field failure (source) | Our requirement | Proof (test) |
|---|---|---|
| Seeds/traces unreleased (VG 38%) | Reproducibility Bundle per run | `chh reproduce` on a clean machine |
| No novelty verification (VG 38%) | Literature-distance check + K7 risk | seeded duplicate-claim test |
| Mechanical loops only (VG 7/9) | VALIDATED loop | §6.4 experiment |
| Host execution (CR) | Sandbox only | host-escape test fails |
| Grader/label visibility (CR) | Hidden data off-mount, canaries, K9 | canary leak test |
| Single-noisy-mean acceptance, winner's curse (CR) | Paired CI, racing, re-measure, gate, sealed | simulation: false-accept rate ≤ α |
| LLM self-approval of persistence (CR) | Kernel evidence + family-separated critic | planted exploit-skill blocked |
| Fail-open defaults (CR) | Fail-closed everywhere | fuzz tests on parsers and gates |
| Prompt-only invariants (CR) | Code-enforced invariants | invariant test suite |
| Fabricated report numbers (CR) | Reports from certificates; recompute audit | inject a fake number → blocked |
| Infra failure scored as policy failure (CR) | Infra/policy split | outage simulation |
| Evaluate on the training tasks (Meta-Harness TB2, EvoMaster) | Disjoint splits by construction | split-overlap check in the driver |
| Claims without dates, so no time-split (SDE, ResearchBench) | Dated snapshots + time-split benchmark | released benchmark |
| No cost accounting (most) | Cost in every number | scorecard axis G |

---

## 8. Novel contributions (what no surveyed system has)

1. **Evidence Kernel**: a mechanical, LLM-free checker for scientific claims (K0–K9) with certificates.
2. **Claim Language**: typed, falsifiable scientific claims as the unit of research output.
3. **Prediction Ledger + Validated Discovery Score**: proper-scored, risk-weighted track record, used as the self-improvement objective.
4. **VALIDATED self-improvement**: harness evolution accepted only through external or future-data evidence (§6.4).
5. **Verifier-tier-gated autonomy** across claims, skills, memory, edits and weights (S§8.1).
6. **Time-split rediscovery/prediction benchmark**, released (S§8.2; the gap noted by 2508.14111).
7. **Value-of-information evaluation allocation**: racing plus winner re-measurement (S§8.3).
8. **Exploit quarantine + red team + derivation-graph revocation** (S§8.4).
9. **Proposer calibration** steering acceptance and island budgets (S§8.5).
10. **Socratic critics** for both hypotheses and harness edits, from a different family (S§8.6).
11. **Diversity by measured error decorrelation**, not vendor count (S§8.7).
12. **Two-timescale co-evolution on disjoint verifiers** (S§8.8).
13. **Persistence rule + revivable dead-ends** on exact snapshots (S§8.9).
14. **Harness-science ledger**: accepted edits as transferable claims, Kernel-certified (S§8.10).
15. **Inverted-U-aware role assignment**, measured per model (S§8.11).
16. **Verifier synthesis with fault-injection validation**.
17. **Problem Formulator**, ranked by available verifier tier.
18. **Independent replication agent** as a certification requirement.
19. **Reproducibility Bundle + auto-filled VG reporting checklist** on every output.
20. **MECHANICAL / VALIDATED loop labelling** as a first-class property.

---

## 9. Coverage contract — every surveyed capability has a home

| Source | Capability (as it should work, not as their code does) | Home |
|---|---|---|
| Meta-Harness | Proposer with full history filesystem | L6.1–2 |
| RHI | Contracts and hops as edit types; compare to parent | L6 edit types, race |
| RRSI | Leakage critic, regularised proposals/acceptance, cost-aware admission | L6.5, L6.8, L6.10 |
| Self-Harness | Causal failure signatures, held-in/out non-regression, decline path | L6.3, L6.6, gate |
| AutoSaddler | Deep diagnosis, patch taxonomy, mini-batch pre-filter, composition DAG | L6.3, edit types, L6.8 |
| MILO | Islands, lineage with rejects, adaptive orchestrator | L7, ledger |
| Mixture of Branches | Coverage-based branching, router | L6.9 |
| HarnessX | Typed multi-dimension harness, deterministic gate, per-task attribution, novelty gate | spec, gate, ledger |
| Adaptive Auto-Harness | Branch tree, temporal reveal, solve-time routing, research logs | L6.9, L6.11 |
| ScholarEvolve | Literature → mechanism → code mutation; bootstrap CI; topic quotas | L4.3 → L6.2, gate, L4.4 |
| SoL-Pi | Cost objective, ObservationPack, verified-quote reducer, frozen held-out | L1, L4 context, gate |
| Continual Harness | Mid-episode refinement, adaptive cadence | L6 (low-risk edit types, in-run only) |
| SIA | Lever choice harness vs weights | L7 lever, L8 |
| EvoMaster | Skills, progressive disclosure, context manager | L5, L4 context |
| Weng survey | Decision observability, optimiser-of-optimiser | calibration, L7 |
| AI Scientist v1/v2 | End-to-end pipeline, reviewer ensemble, experiment tree, LaTeX loop | L4, L3 T5, L4.7, L4.10 |
| EvoScientist | Cross-task idea and experiment memory, failed directions | L5 |
| ScientistOne | Chain-of-Evidence, deterministic grounding, integrity audit | Kernel K0, L2 |
| SciDER | Data-first profiling, K-way ranking, trajectory data | L4.2, L4.5, L8 |
| ScienceFlow | Recoverable states, safe restore, resource controller | L0 snapshots, L1 |
| PARNESS | DAG kernel, full-text index, KG, provider adapters | L0, L4.3, gateway |
| Auto-RecSys | Cognition vs procedure split, playbook with dead-ends | L1, L5 playbook |
| MM-Agent | Hierarchical method library, actor-critic, rubric decomposition | L5, L4.8, L3 T5 |
| MatAgent | Human-in-the-loop gates | L9 approval nodes |
| Feynman | Multi-source retrieval, citation verifier | L4.3, K0 |
| The Station | Decentralised agents, archive coordination, exact evaluators, stagnation lanes | L4 multi-agent mode, L3, L7 |
| AutoOPT | Lean + comparator, numerical certificates, statement fidelity | L3 T0/T1 |
| MDA | Bayesian evidence, EIG experiment design | L4.7 (EIG mode), L1 allocator |
| Robin | Lab-in-loop, BTL tournament, replicated analysis + consensus | L3 T3, L4.5, L4.9 |
| AHOIS | Socratic question-only critic, physical bench | L4.8, L6.5, L3 T3 |
| ScienceIDE | Codebase → verifiable environments, fault injection, calibrated tolerances, floor-normalised reward | L3, task suite, L8 |
| AWorld-RL | Reward-variance task selection, stateful verifier | L8, L3 T1 |
| Prime Agent | Memory hierarchy, long durable runs, recursive spawn ledger | L5, L0/L1 |
| ResearchBench / SDE | Research-judgment evaluation | Scorecard E |
| LangGraph | Checkpoint, resume, fan-out, interrupts | L0 |
| VG audit (2608.05179) | Verification ladder, reporting checklist, mechanical vs validated | Kernel tiers, L2, §6.1 |

---

## 10. Hard invariants (never violated; each has a test)

1. No agent-written code outside the sandbox.
2. No hidden label, grader or sealed-split artifact inside any agent-visible mount, prompt or trace.
3. No certificate without the Kernel; no Kernel path with an LLM call.
4. No persistent write (memory, skill, harness, weights) without evidence at the tier floor.
5. No critic from the same model family as the proposer it judges.
6. No acceptance without paired statistics and a never-shown gate; the sealed split is touched only via `finalize`.
7. Every parser and gate fails closed.
8. Every number in a report resolves to an artifact and is recomputed by the audit.
9. Every run has a Reproducibility Bundle and a cost record.
10. Every loop is labelled MECHANICAL or VALIDATED.
11. BYOS only for allowlisted providers through official auth; training only on licence-permitted outputs.
12. No README-only features: every claimed capability has code, a test and an ablation.

---

## 11. Build order — vertical slices, each ending in a working, measurable system

Principle: always keep one end-to-end path working. Build the Kernel early so everything after optimises against it.

| Milestone | Builds | Exit test |
|---|---|---|
| **M0 Hardening** (P0) | infra/policy split; winner re-measure; safe snapshot restore; resume; gate unit-alignment; persisted budget; canary + leak audit | all §7 tests that apply to existing code pass |
| **M1 Kernel v1** | Claim Language (ML); pre-registration commits; K0–K9 (K6 on sealed internal data); certificates; Reproducibility Bundle; reporting checklist; prediction ledger | seeded 50-claim suite classified correctly; `chh reproduce` works |
| **M2 Research loop v1** | Kernel-integrated loop; experiment tree; improve operator; replication agent; Socratic critic; richer profiling; ObservationPack context; ML task suite ≥ 20 tasks (rings-style + real datasets + ML-repair family) | ≥ X% CERTIFIED-or-NEGATIVE, 0 integrity failures |
| **M3 Evolution v1** | evidence export; agentic proposer; structured diagnosis; family-separated critic; smoke; ledger upgrades; cost term | first real campaign: certified improvement holding on sealed split |
| **M4 Memory, verifiers, branching** | L5 store API; exploit quarantine; red team; judge panel validated; fault-injection verifiers; branching/routing | planted-exploit suite caught; router beats single best on sealed |
| **M5 Literature + time-split** | literature module + citation check; dated snapshots; time-split prediction benchmark; Problem Formulator | §6.4 headline experiment run (VALIDATED loop) |
| **M6 Scorecard** | pinned benchmarks A–H, re-run baselines, ablations, bundles | SOTA criteria §2 evaluated and published honestly |
| **M7 Discovery & weights** | L7 islands; T0 Lean comparator; verifier synthesis; L8 two-timescale | ≥ 1 independently verified result on a known-value problem; coupled-Goodhart gap not widening |

Each milestone updates `STATUS.md` and is committed with its tests.

---

## 12. Resources and constraints (honest)
- Today: ChatGPT subscription via the official Codex CLI (BYOS, allowlisted), Docker, no local GPU.
- M0–M2 fit this setup (slow, but enough for development).
- M3+ campaigns and M6 need an API budget and/or rented GPUs with open-weights models. Each campaign is budgeted before
  it starts, with the two budgets (inference, experiment compute).
- The time-split set needs cutoff-dated models; contamination probes may reduce usable windows.

## 13. Open questions (decide when reached)
1. The risk prior for K7: baseline-model predictive distribution vs empirical prior from the ledger (likely both, max).
2. The VDS weighting between information gain and miss penalty (pick a proper scoring rule; simulate before fixing).
3. Kernel scope beyond ML (physics invariants for K8 come with the first non-ML domain).
4. Whether to release the prediction ledger publicly as it accrues or at milestones.

## 14. Glossary
- **Certificate**: Kernel output stating which checks a claim passed, at what tier.
- **VALIDATED loop**: a loop whose acceptance comes from evidence the system did not generate or see during optimisation.
- **VDS**: Validated Discovery Score, the proper-scored information gain of confirmed risky predictions.
- **Reproducibility Bundle**: everything needed to rerun a result with one command.
- **Evolve / gate / sealed**: task splits. The proposer sees evolve; acceptance uses gate; humans alone touch sealed.
- **Infra vs policy failure**: the environment broke vs the harness's behaviour failed.
