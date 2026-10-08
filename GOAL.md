# GOAL — the harness we are building

Date: 2026-10-06; scope revised 2026-10-07 (user decision: life sciences + engineering, core/adapter split, §4.7,
§5 "Domain adapters", §11, §15). Owner: user (product decisions) · Claude (design + build).
**This is the single source of truth for WHAT custom-harness is and when it is done.**
(State: M0 ✅, M1 ✅, M2 built with exit test pending; markers in §5/§11 reflect it, details and verification state in
`STATUS.md`.)
`PLAN.md` = design history and rationale. `STATUS.md` = what is built today. Where they disagree with this file, this file wins.

Inputs this file is built on (read them only if a detail is needed):
- `../autoresearch_survey/00_SYNTHESIS.md` — 36 sources, first-principles model, 12 novel ideas (cited **S§n**).
- `code_review/00_CODE_REVIEW_SYNTHESIS.md` — the public code of 27 of those systems, read line by line (cited **CR§n**).
- Two surveys: arXiv 2508.14111 (Agentic Science) and arXiv 2608.05179 (the "Verification Gap" audit) (cited **VG**).
- alphaXiv/OpenResearch, docs, skills and part of the Rust code read 2026-10-07 (cited **OR**; comparison and
  evidence labels in `code_review/openresearch.md`).

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

**Scope: life sciences and engineering** (ML/DL is the first, already-built domain and stays the test bed). A
domain-agnostic Kernel core judges every claim; **domain adapters** (a human-approved, fixed verifier layer) supply
each domain's data formats, metrics, invariants and tools. Claims are certified at the strongest tier the available
tools allow (§4.7): **formal** claims by Lean (T0), **computational and data** claims by solvers, exact checkers and
held-out data (T1/T2), **simulated** claims only within the simulator that produced them (the gap to reality is an
untested assumption), and **physical or empirical** claims never as "done" by the system: they go through a
pre-registered protocol, a human-in-the-loop approval node, human execution, and a Kernel check of the returned
data. The harness evolves; the adapters do not.

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
| Good experiment discipline stated only in prompts: "never edit a node a run has answered", "identical run command and environment on every node" (OR: no enforcing code found) | The rules are adopted and enforced in code: answered nodes are immutable, the run contract is identical across nodes, and every repair attempt is ledgered (L4.7). |
| Repair-until-it-works loops with no record of how many attempts preceded the result (OR) | Every attempt on a node is counted; repairs are allowed only before a node has produced any metric. |
| Cited paper IDs recalled from model memory (OR forbids it in a prompt) | A citation counts only if its ID appears in this run's recorded retrieval log (L4.3), in addition to resolving. |
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
  │ Claim Language · Pre-registration commits · checks K0–K9 · Certificates (tier) · Prediction Ledger │
  │ domain-agnostic: everything domain-specific comes from a registered adapter                        │
  └────────────────────────────────────────────────────────────────────────────────────────────────┘
  DOMAIN ADAPTERS (human-approved, FIXED: never edited by L6) ── ML · Lean/formal · solvers · simulators ·
                  life sciences · engineering: formats, metrics, invariants (K8), tools, validation evidence
  L3 Verifier Ladder ── T0 formal · T1 executable · T2 statistical · T3 physical · T4 time-split · T5 judge panel · T6 none
                         + fault-injection validation · calibrated tolerances · exploit quarantine · red team · verifier synthesis
  L2 Evidence Store ── append-only, content-addressed; claims only from evidence; Reproducibility Bundles
  L1 Control Plane ── budgets (persisted) · scheduler · admit/timebox/kill · infra-vs-policy failures · eval allocator
  L0 Substrate ── typed spec interpreter · traces · checkpoints · resume · fork · fan-out · approval nodes
                  · Docker/gVisor sandboxes · exact snapshots · model gateway (OW / API / BYOS allowlist)
```

Rule: a layer may call layers below it. It influences layers above only by writing Evidence. The Kernel is the only
component allowed to mint a Certificate. Adapters are called by the Kernel and the research loop but are part of
the trusted, human-controlled base: no layer above may modify them.

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
| Small trusted kernel | Small, deterministic Kernel code (target < 4,000 non-comment lines, raised from 3k on 2026-10-08 by the owner: §16 features; core split (§16.6) deferred; 100% test coverage, no LLM inside) |
| `sorry` / axioms audit | **Assumption audit**: every untested assumption is listed explicitly and lowers the tier (v1: listed and marked tested/untested; tier lowering not yet defined) |
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
The Kernel issues `Certificate{claim, tier, checks_passed, checks_failed, assumptions, bundle_hash, verifier_ids,
evidence_class, adapters}` (`evidence_class` and `adapters` = §4.7: the class of evidence and the exact adapter
versions, by hash, that the certificate relied on).
- Tier = the weakest link of the strongest verifier chain that passed (T0 … T6, mapped to VG's ladder in reports).
- Labels: **CERTIFIED-VALIDATED** (K0–K9 including K6 on external or future data), **CERTIFIED-INTERNAL** (K6 on
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
  **Correction (2026-10-07, finding F2, verified by reading `kernel/predictions.py`).** As built, p0 is measured after
  the outcome, so a miss scores positive whenever q < p0. A hopeless tiny-δ claim at q = 0.05 earns +0.93 bits, so
  the sentence above is FALSE today. The fix is §16.4: VDS becomes an e-process with p0 fixed at registration.
- **Released benchmark**: the ledger plus the time-split set becomes the public benchmark for "discovered knowledge vs
  ground truth" that 2508.14111 says is missing.

### 4.6 What the Kernel does not do (stated in every report)
It doesn't judge importance or creativity. It doesn't detect confounders absent from the data. It doesn't validate
measurement instruments. It certifies inference, not reality. Soft qualities stay at T5 and are labelled as such.
It never certifies that a physical experiment was performed, nor that a simulator matches reality (§4.7).

### 4.7 Scope and claim classes (life sciences + engineering)
Every claim declares its **evidence class**; the class fixes the strongest tier it can reach and what the certificate
may say. A claim is certified at the strongest tier its available tools support, never higher.

| Class | Examples | Verifier (via a registered adapter) | Max tier | What the certificate states |
|---|---|---|---|---|
| **Formal** | a lemma, a bound, an algorithm's correctness | Lean 4 build + comparator against a HUMAN-APPROVED statement; axiom audit (no `sorry`, no new axioms); statement-fidelity check | T0 | the statement holds, given Lean's kernel and the listed axioms |
| **Computational** | an optimum, a feasibility or impossibility result, an exact count, a structural property | exact solvers (SMT/SAT/MILP/CAS) with **independently checkable certificates** (proof logs, duals, witnesses) checked by a small trusted checker; float screens only prune | T1 | the stated computation's result, re-checkable from the certificate |
| **Data / statistical** | a method improves a metric on held-out data; an association holds on future data | today's Kernel (K0–K9, held-out / time-split / external data) | T2 (T4 time-split) | the effect on the stated data population, with intervals |
| **Simulated** | a design meets a spec in FEM/CFD/circuit/molecular-dynamics/systems-biology simulation | a pinned simulator (version, solver settings, mesh/step convergence evidence) inside an adapter | T1/T2 **within the simulator** | the claim holds **in simulator S (version, config)**; the scope names S; "simulation matches reality" is listed as an UNTESTED assumption; never relabelled as physical |
| **Physical / empirical** | a compound's effect in cells; a prototype's measured strength | none inside the system: **protocol → approval node → human execution → returned data → Kernel check** | T3 | the returned data support the pre-registered claim under the pre-registered analysis; data provenance is a human ATTESTATION (untested by the system) |

Physical/empirical claims are **never certified as done by the system**. (Path built up to the Kernel boundary 2026-10-07, 🟡: no physical adapter yet, so K1 refuses.) The only route: (1) the claim, the protocol
and the analysis plan are pre-registered (K2); (2) an **approval node** pauses the run until a named human approves
the protocol, including a biosafety / dual-use and ethics screen; categories on the human-owned prohibited list are
refused outright and never reach approval; (3) humans execute; (4) the returned data are registered in the vault
with role `external`/`future` and a signed provenance attestation; (5) the Kernel checks them against the
pre-registered plan. Until (5), the claim exists only as a pending prediction in the ledger. The system cannot
approve its own protocol, cannot fabricate returned data (the data enter only through the human-operated path),
and the attestation is listed as an untested assumption on every such certificate.

---

## 5. Component specifications

Each component lists **Must** (requirements), **Never** (invariants), **Done when** (acceptance test), and the sources
it absorbs and surpasses. (✅ already built · 🟡 partial · ❌ missing, as of 2026-10-06, after M0; proofs in STATUS.md.)

### L0 Substrate
**Runtime** (🟡) — typed `HarnessSpec` (JSON Schema) interpreted as a graph.
- Must: full trace per step; checkpoint per step; `fork(step, state_patch)`; **`resume(run_id)` after crash** (CR P0-4) ✅;
  **MapNode fan-out** with per-branch persisted outputs, deterministic ids and declared reducers (LangGraph); **Approval
  node** (✅ 2026-10-07, M3, brought forward): the run goes PAUSED with the exact item to approve (e.g. a physical-claim
  protocol, §4.7) pinned by hash; only a human action through L9 (CLI first) records approve/reject with the
  approver's identity in the ledger and resumes the run; no model, module or evolved node can approve, and an
  approval binds only the hashed item it names; transient-only retry policy that charges every attempt ✅; unmatched
  edge = FAILED ✅; any crash leaves FAILED, never RUNNING ✅.
- Never: pickle or arbitrary deserialisation of untrusted data; silent defaults.
- Done when: kill -9 mid-run, then resume, completes with an identical trace prefix and no double-charged cost. ✅
- Absorbs: LangGraph (resume, fan-out, interrupts) without its heuristic resume detection or async-durability loss;
  PARNESS DAG kernel (minus the dead DSL); ScienceFlow's single-writer state.

**Sandbox & snapshots** (🟡)
- Must: Docker, gVisor when available, network off plus allowlist, caps dropped, non-root, read-only root, no secrets ✅.
  Snapshots: content-addressed, **preflight all blobs → archive live workspace → restore → rollback on failure**; keep
  modes and symlinks; fingerprint-skip unchanged files (ScienceFlow, CR P0-3) ✅; host-side workspace access refuses
  links/escapes ✅; per-campaign cloud-GPU backend. Every run executes a snapshot of a recorded revision in its own
  directory, never a live working tree (OR does this).
  **Compute backends** (❌; OR covers SSH, Slurm, K8s, Modal, Ray, HF Jobs): one backend interface, each backend
  declaring an **isolation class**. Only backends that meet invariant 1 (container, no secrets, network policy,
  read-only root) may run agent-written code. Any other backend runs trusted code only, and running trusted code
  there needs a recorded human approval. Agent-written code is never allowed on a non-isolated backend, with or
  without approval (invariant 1 wins; clarified 2026-10-08 after orcompute built the ambiguous earlier wording).
- Never: run agent code on the host; mount hidden data into an agent container.
- Done when: a corrupted blob mid-restore leaves the original workspace intact; a planted host-escape test fails. ✅

**Model gateway** (✅ core) — three access modes (OW / API / BYOS allowlist), provenance on every output, strict
schemas, redaction. Add: per-host pacing; cost tables; the provider registry verified each release; same-family detection
(✅ 2026-10-07, `policy.py`: enforced before any model call; a single-family run needs an explicit override that is
recorded as invariant 5 NOT MET in every run, report and certificate checklist, and earns no replication credit).

### L1 Control plane (🟡)
- Must: budgets in neutral units **persisted across crash and fork** plus a wall-clock unit (✅ per run; campaign-level
  to do); provider outages uncharged ✅ (ScienceFlow); **infrastructure failure vs policy failure** classification, with
  infra-failed units excluded from paired comparisons and retried ✅ (AutoSaddler, ScienceIDE; CR P0-1); evaluation allocator (racing, successive halving, value of
  information), driven **per completion**: it wakes on the first finished unit, re-reads the store as the source of
  truth, acts on every newly finished unit and refills the slot, with no batch barrier (OR); LLM controllers may only
  downgrade (deny, shrink, kill-with-code-checked-evidence).
- Done when: a Docker outage mid-campaign changes no accept/reject decision. ✅

### L2 Evidence store & reporting (🟡)
- Must: append-only, content-addressed ✅; claims only from evidence ✅; **Reproducibility Bundle** per run and per claim
  (VG) (✅ per certified claim; ✅ per run since 2026-10-07, `chh rerun`); **reporting checklist** auto-filled (seeds, traces, selection
  policy, human-in-the-loop points, cost) ✅ (Kernel `checklist.py`; wiring into reports is M2); Postgres
  backend for campaigns; read-only **evidence filesystem export** for the proposer (L6).
  **Large artifacts** (weights, optimizer states, datasets): the bundle lists each omitted artifact with its path,
  size, sha256 and status (bundled / referenced / available locally). `reproduce` reports any that are missing
  instead of skipping them silently (OR demo manifest).
  **Figures** are rendered by a script stored next to them. Every plotted value resolves to an artifact, and the
  audit re-renders the figure (invariant 8 extended to figures; OR keeps scripts next to figures but does not
  check them).
- Done when: `chh reproduce <bundle>` on a clean machine reproduces a headline claim. ✅ (tested with the original
  store deleted; a real headline claim from the research loop comes with M2)

### Evidence Kernel (✅ v1, 2026-10-06 — §4; `src/custom_harness/kernel/`, 1.3k LoC). The highest-priority novel component.
- Done when: (a) on a seeded set of 50 ML claims (true, p-hacked, leaked, irreproducible, strawman-baseline, trivial),
  the Kernel accepts the true ones and rejects each failure type with the correct reason; (b) Kernel code has 100% branch
  coverage and contains no LLM call. ✅ both (`tests/test_kernel_suite.py`, `scripts/check.py` coverage gate,
  `tests/test_kernel_quality.py` import-closure test). v1 deviations are listed in STATUS.md.

### Domain adapters (🟡 M3: registry, evidence classes, K1 enforcement and the ML adapter built 2026-10-07; exact-solver adapter: class-aware check table + `lp` certificate checker (T1) built 2026-10-07, no solver wrapper yet (certificates come from outside); Lean / simulator adapters not yet; §4.7)
A domain adapter is everything domain-specific the Kernel and the loop need, as a **human-approved, fixed verifier
layer**: claim-scope vocabulary, data formats and readers, registered metrics with direction and range, background
invariants for K8 (units/dimensions, conservation laws, physical bounds), tool wrappers (Lean, solvers with
certificate checkers, pinned simulators, statistical procedures), noise models for K7, and the **validation evidence**
that its verifiers reject planted faults (fault injection, golden cases, calibrated tolerances).
- Must: a stable adapter interface the Kernel core calls (the core contains no domain logic); every adapter is
  versioned, content-hashed and registered in the ledger with the approving human; every certificate records the
  adapter hashes it used (§4.4); an adapter is usable only after its validation evidence passes; the ML adapter is
  the current code, extracted; first new adapters: **Lean (T0)**, **one exact-solver adapter (T1)**, **one simulator
  adapter (simulation-scoped claims)**, then life-science and engineering adapters (§15).
- Never: the evolver (L6), the research loop or any model edits, adds or re-parameterises an adapter (adapter files
  are outside every edit surface and protected like the Kernel); an adapter change without human approval and fresh
  validation; a simulated or physical claim certified beyond its class (§4.7).
- Done when: the same Kernel core certifies an ML claim (ML adapter), a Lean theorem (T0, comparator against an
  approved statement; planted `sorry`/axiom/statement-drift cases rejected), a solver result (certificate re-checked;
  planted wrong certificates rejected) and a simulator-scoped claim (scope and untested gap listed); an edit that
  touches an adapter is rejected statically; an adapter whose validation evidence fails cannot be used.

### L3 Verifier ladder (🟡)
- Must:
  - **Verifier validation by fault injection**: each T1/T2 verifier carries evidence that it rejects planted faults
    (ScienceIDE).
  - **Calibrated tolerances** from measured noise floors (nominal / perturbed / rebuild; seed spread).
  - **Float-screen → exact-certificate** gate where exact checks exist (Station).
  - **T0 split** (❌, **brought forward to M3**, delivered as the Lean adapter): Lean build + hygiene vs Lean +
    comparator against a human-approved Challenge spec, with axiom audit and statement-fidelity gate (AutoOPT).
  - **Judge panel (T5)**: ≥ 3 families, both orders, ensemble + meta-review (AI-Sci v1 design with its bugs fixed),
    rubric decomposition (MM-Agent), BT with CIs (Robin, fixed); **validated against human labels before use**.
  - **Exploit quarantine**: any skill or edit gain must survive a perturbed evaluator (hidden invariants, stricter spec,
    randomised variant).
  - **Red team**: an adversary agent tries to get a false claim certified; successes become regression tests.
  - **Verifier synthesis**: for soft problems the system writes property tests, invariants and simulators; they count only
    after fault-injection validation; capped at T2 until a human reviews them.
- Never: an LLM verdict substring-matched as a pass (Adaptive, PARNESS, SciDER pitfalls).
- Done when: the planted-exploit suite (including an RCON-style "found an evaluator backdoor" case) is caught ≥ target rate.

### L4 Research loop (🟡 v1 built 2026-10-06, Kernel-integrated; live research quality not yet measured)
Stages (each a node with a contract, all evolvable):
1. **Problem Formulator** (❌, novel): mines anomalies, open problems and failed replications from the literature graph;
   ranks by (best available verifier tier) × value × feasibility.
2. **Data profiling** (✅ roles, outliers, association ranking, redundancy, flags; no multi-table key graph yet): full-file profile plus outliers, cardinality, key graph, column roles; **ideation is
   conditioned on it** (SciDER promised this, its code doesn't do it).
3. **Literature** (❌): arXiv / Semantic Scholar / OpenAlex / Crossref, plus **PubMed and bioRxiv** for the
   life-science scope and full-text keyword search where the terms of service allow it (OR). Pacing and fallback,
   query splitting (Feynman, Robin), dated snapshots plus cache.
   - **Retrieval discipline** (OR, enforced here in code): one publication window per retrieval loop, inherited by
     every follow-up call. Under time-split the window ends at the cutoff T and is never widened. A hard cap on
     follow-up rounds. Deduplication by id, then DOI/arXiv id, then normalised title.
   - **Citation check:** retrieval provenance (the cited ID appeared in this run's recorded retrieval log), plus ID
     resolution (resolves / doesn't / lookup failed), plus title/year/author match, plus **quote-span support**
     → K0/I3.
4. **Hypotheses** (🟡): diverse proposers; forced distance-3 cross-domain inspiration; literature-mined mechanisms;
   topic quotas; diversity measured by error correlation, not vendor count (SDE r>0.8).
5. **Tournament** (✅): shuffled K-way rounds → BT; add both-order pairwise mode, BT CIs, Improve-weakest and Combine
   operators (SciDER), pinned incumbent.
6. **Pre-registration** (✅): typed Claim + analysis plan committed to the Kernel ledger before execution.
7. **Experiment tree** (🟡 draft/debug/improve + dev racing; no snapshot backtracking or revivable dead-ends yet): draft / debug / **improve** nodes, code-scored seeded selection, dev racing against dev
   overfitting, stop when certified; tune → creative → ablation curriculum (AI-Sci v2, done properly); exact backtracking
   via snapshots; revivable dead-ends with a persistence rule (P(improvement) > θ).
   **Node lifecycle and tree shape** (❌; design from OR, which states it only in prompts; here it is code):
   - A node is *provisional* until a run produces its metric, then it is **immutable**: content-hashed, and edits
     are refused, so a new idea becomes a child.
   - The **run contract** (command, environment, data inputs, budget) is identical across nodes. Only the
     committed code/config diff differs; a run with a different contract is refused. This is K5's "identical
     conditions" applied inside the tree.
   - **Repairs:** fixing an implementation failure (crash, OOM, timeout, missing dependency) is allowed only while
     the node is provisional. Every attempt is ledgered and counts toward the selection record, and repairs are
     capped.
   - **Rounds:** a round is a small set of sibling options for ONE decision. The next round descends from the
     round's winner only after that winner is confirmed by dev racing and a fresh re-measure (OR promotes on one
     run).
   - **Shape diagnostics:** "flat fan" (everything off the root) and "noodle" (single-child chains) are detected
     and logged in the run report.
8. **Socratic critic** (✅ question-only enforced in code; answers citing evidence not yet checked): questions only, answers must cite evidence (AHOIS).
9. **Replication agent** (✅, novel; same model family as the author today): re-implements from the claim text without access to the code; agreement is
   required for CERTIFIED headline claims.
10. **Claims → Kernel → Report** (✅): reports generated from certificates; checklist; bundle; VG-ladder mapping.
- Context management (✅ archive + handle + head/tail, verified-quote log reducer; paged recall by an agent not yet): archive large outputs to blobs, keep a handle plus head/tail in context, provide paged recall
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
1. **Evidence export** (✅ built 2026-10-07, M4 piece; no agentic proposer reads it yet): read-only filesystem of all
   candidates, full traces (including successes), scores, ledger, Kernel certificates and rejects. Policy: evolve-split
   runs only; gate/sealed outcomes only as aggregate accept/reject (+ rejecting stage); leak audit over the exported
   tree fails closed (`evolve/export.py`).
2. **Agentic proposer** (Meta-Harness, done safely): a tool-using agent inside a sandbox with that export, read-only;
   output is **typed edits only** (JSON-pointer ops on the spec) with a predicted effect.
3. **Structured diagnosis** (AutoSaddler + Self-Harness; 🟡 deterministic part built 2026-10-07): first-divergence causal
   procedure; causal signature (cause, criticality, mechanism) with verifier evidence; the claimed diff is checked against
   the real one. Built: first divergence in node/route/error vs a better run of the same task, signature with step refs,
   in the proposer prompt; claimed cluster ids checked (recorded, not enforced). Not built: verifier-evidence linkage,
   claimed-diff check, content-level divergence.
4. **Static checks** ✅: protected nodes (setup/verify/audit/Kernel); audit-only terminal; leak patterns; plus a
   deterministic denylist of evolve/gate task ids.
5. **Critic from a different model family** (RRSI, fixed): leakage, degeneracy, grader gaming, unbounded work; reasons
   are fed back for ≤ k repairs; Socratic mode.
6. **Decline / no-op allowed** ✅; one mechanism per candidate (❌); novelty gate against re-proposing reverted signatures
   (HarnessX) ✅ (static- or gate-rejected signatures; race losers are not blocked).
7. **Smoke run**: one task through the real loop.
8. **Race** ✅ (paired CI successive halving) → **re-measure the winner on fresh seeds and units** (CR P0-2) → **gate** ✅
   on the never-shown split, with a unit-alignment check and a cost term (RRSI; ✅ built 2026-10-07: total cost on the
   paired gate units ≤ (1 + `max_cost_increase`) × parent, default 1.0 not calibrated, fail closed without cost data).
   **Leave-one-family-out / leave-one-domain-out gates** (✅ built 2026-10-07, M4 piece; no real campaign yet): the campaign's gate split contains at least one
   ENTIRE task family (and, once ≥ 2 domains have adapters, at least one entire domain) that never appears in the
   evolve split nor in any evolver-visible export; a candidate is accepted only if, on EACH held-out family/domain
   separately, the paired-CI lower bound of its difference to the parent is ≥ −ε (non-regression), in addition to the
   pooled gate; the per-family/domain OOD drop is recorded in the ledger. Across campaigns the held-out family and
   domain rotate (each one is left out at least once before `finalize`), and `load_suite`/the driver refuse a
   campaign whose held-out families/domains leak into evolve (same content-level isolation as `check_split_isolation`).
9. **Branch / route**: if a candidate wins on a cluster but not globally, create a branch; route by descriptors
   **generated from gate evidence**; a select/materialize adaptation seam (Adaptive Auto-Harness, fail-closed); the router
   is tested on the sealed split.
10. **Ledger** ✅: per-task flipped/regressed ✅, `regressed_unpredicted` ✅, predicted vs actual (calibration) ✅, yield ✅,
    prune ✅, stall (❌), annealed op budget (RRSI, ❌); proposer-written generation report (❌).
11. **Temporal-reveal mode** for streams: labels sealed outside every evolver mount; watermark rule; undated = unrevealed.
12. **Finalize**: a one-shot freeze; the sealed split is touched once, by a human-run command.
- Objective: §6.3 (VDS-centred for research harnesses; benchmark score for agent harnesses).
- Done when: on a real campaign the loop finds and certifies ≥ 1 improvement that holds on the sealed split AND passes
  the leave-one-family-out gate (and leave-one-domain-out once ≥ 2 domains exist), with OOD drop ≤ RRSI's reported
  regularised level.
- Never: edit, add or re-parameterise a domain adapter (adapters are not an edit surface).

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

### 6.4 The headline experiment (M7; needs the literature module and dated snapshots)
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
| Simulation or model output presented as a real-world finding (common in AI-for-science) | Evidence classes with hard tier ceilings; simulated claims scoped to the simulator (§4.7) | a simulated claim cannot be certified as physical (M3 test) |
| Lab results claimed without a human loop or with self-approved protocols (MatAgent prose gates) | Approval node: only a human approves a hash-bound protocol; returned data checked by the Kernel | model/evolved-node approval refused; uncertified until data return (M3 test) |
| Verifiers changed by the optimiser that is graded by them (coupled Goodhart) | Domain adapters fixed and human-approved; certificates pin adapter hashes | adapter edit rejected statically (M3 test) |
| Gains that do not transfer across task families or domains (RRSI OOD drop) | Leave-one-family/domain-out gates | a candidate that regresses on the held-out family is rejected (M4 test) |

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
21. **Evidence classes with tier ceilings** (formal → physical), and a **human-approved, hash-bound approval node** as
    the only path for physical claims (§4.7).
22. **Fixed, validated domain adapters** under an evolving harness: the core/adapter split keeps the verifier out of
    the optimiser's reach while the system expands across life sciences and engineering (§15).

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
| OpenResearch (alphaXiv) | Experiment-tree lifecycle and shape, fixed run contract, per-completion loop, compute backends, PubMed/bioRxiv/full-text retrieval with fixed windows, large-artifact manifest, figure scripts, delegation hygiene, bring-your-coding-agent front-ends | L4.7, L1, L0, L4.3, L2, §15, gateway |

---

## 10. Hard invariants (never violated; each has a test)

1. No agent-written code outside the sandbox.
2. No hidden label, grader or sealed-split artifact inside any agent-visible mount, prompt or trace. This includes
   copies that sit in the execution environment (packages or datasets bundled in the sandbox image) and truth that
   can be regenerated from anything agent-visible (a generator's random stream or seed). §16.1.
3. No certificate without the Kernel; no Kernel path with an LLM call.
4. No persistent write (memory, skill, harness, weights) without evidence at the tier floor.
5. No critic (or independent replicator) from the same model family as the proposer (author) it judges.
6. No acceptance without paired statistics and a never-shown gate; the sealed split is touched only via `finalize`.
7. Every parser and gate fails closed.
8. Every number in a report resolves to an artifact and is recomputed by the audit.
9. Every run has a Reproducibility Bundle and a cost record.
10. Every loop is labelled MECHANICAL or VALIDATED.
11. BYOS only for allowlisted providers through official auth; training only on licence-permitted outputs.
12. No README-only features: every claimed capability has code, a test and an ablation.
13. Domain adapters are human-approved and fixed: no model, module or evolved harness edits them; every certificate
    records the adapter versions (hashes) it relied on; an adapter without passing validation evidence is unusable.
14. No claim is certified above its evidence class (§4.7): simulated claims are scoped to their simulator, with the
    gap to reality listed as untested; physical/empirical claims are never certified as done by the system and
    reach the Kernel only as human-returned data on a pre-registered, human-approved protocol.
15. Only a human approves an approval node, and an approval binds only the hashed item it names.
16. No harness edit is accepted without passing the leave-one-family-out gate (and leave-one-domain-out once ≥ 2
    domains exist).

Added 2026-10-07 (§16; each needs its test before it counts as met):
17. Everything a verdict depends on is fixed when the claim is pre-registered: its significance level, its scoring
    baseline p0 and its reliability requirement. Later events never change a verdict silently. They can only make a
    certificate STALE, which is reported apart from "forged", or lead to an explicit retraction.
18. No target-derived information reaches an agent-visible surface except as a ledgered release that K4 counts.
    Continuous held-out values never reach the evolver.
19. Every attack test kills its attack. Each red-team finding has a re-introduction patch that at least one test
    fails on, and a test that has stopped exercising its attack is a gate failure.
20. The verifier's own validation sets are used like held-out data. The honest corpus, the museum and the claim
    suite each have a development half and a sealed half, and every look at a sealed half is ledgered.
21. Replayed or simulated model output is never evidence. Cheap proxies (preflight, screens, cassettes) may only
    deny or prune; they never grant.
22. At least one check exists that no AI agent designed or can see. It is held by a human, and an attack from
    outside the agent pipeline is run at least once per milestone.

---

## 11. Build order — vertical slices, each ending in a working, measurable system

Principle: always keep one end-to-end path working. Build the Kernel early so everything after optimises against it.

| Milestone | Builds | Exit test |
|---|---|---|
| **M0 Hardening** (P0) ✅ 2026-10-06 | infra/policy split; winner re-measure; safe snapshot restore; resume; gate unit-alignment; persisted budget; canary + leak audit | all §7 tests that apply to existing code pass |
| **M1 Kernel v1** ✅ 2026-10-06 | Claim Language (ML); pre-registration commits; K0–K9 (K6 on sealed internal data); certificates; Reproducibility Bundle; reporting checklist; prediction ledger | seeded 50-claim suite classified correctly; `chh reproduce` works |
| **M2 Research loop v1** (built; exit test pending a live-run budget) | Kernel-integrated loop; experiment tree; improve operator; replication agent; Socratic critic; richer profiling; ObservationPack context; ML task suite ≥ 20 tasks (rings-style + real datasets + ML-repair family) | ≥ X% CERTIFIED-or-NEGATIVE, 0 integrity failures |
| **M3 Core/adapter split + T0 + approval node** (new 2026-10-07; T0 and the approval node brought forward from M7/L9) | domain-agnostic Kernel core + adapter interface; adapter registry (versioned, hashed, human-approved, validation evidence required); ML adapter extracted from today's code; **Lean adapter (T0)**: build + comparator against a human-approved statement, axiom audit, statement-fidelity gate; one exact-solver adapter (T1, re-checkable certificates); one simulator adapter (simulation-scoped claims); evidence classes in claims and certificates (§4.7); **approval node** (PAUSED → human approve/reject via CLI, hash-bound, ledgered) and the physical-claim path (protocol prereg → approval → returned-data registration with attestation → Kernel check) | the §5 "Domain adapters" done-when; a physical-claim dry run with synthetic returned data: no certificate without a human approval, a model/evolved node cannot approve, the attestation is listed as untested; adapter edits rejected statically |
| **M4 Evolution v1** (was M3) | evidence export; agentic proposer; structured diagnosis; family-separated critic; smoke; ledger upgrades; cost term; **leave-one-family-out / leave-one-domain-out gates** | first real campaign: certified improvement holding on sealed split AND on the held-out family |
| **M5 Multi-agent domain expansion** (new; plan §15) | life-science and engineering adapters built by parallel agents under human approval; per-domain task suites with held-out families; cross-domain LODO gate live | each new adapter passes its validation suite and human review; one certified claim per new domain at its evidence class; LODO gate exercised |
| **M6 Memory, verifiers, branching** (was M4) | L5 store API; exploit quarantine; red team; judge panel validated; fault-injection verifiers; branching/routing | planted-exploit suite caught; router beats single best on sealed |
| **M7 Literature + time-split** (was M5) | literature module + citation check; dated snapshots; time-split prediction benchmark; Problem Formulator | §6.4 headline experiment run (VALIDATED loop) |
| **M8 Scorecard** (was M6) | pinned benchmarks A–H, re-run baselines, ablations, bundles | SOTA criteria §2 evaluated and published honestly |
| **M9 Discovery & weights** (was M7, minus T0) | L7 islands; verifier synthesis; L8 two-timescale | ≥ 1 independently verified result on a known-value problem; coupled-Goodhart gap not widening |

Each milestone updates `STATUS.md` and is committed with its tests.

---

## 12. Resources and constraints (honest)
- Today: ChatGPT subscription via the official Codex CLI (BYOS, allowlisted), Docker, no local GPU.
- M0–M2 fit this setup (slow, but enough for development).
- M4+ campaigns and M8 need an API budget and/or rented GPUs with open-weights models. Each campaign is budgeted before
  it starts, with the two budgets (inference, experiment compute).
- Invariant 5 needs a second model family (API key or open-weights model); until then every live run is recorded as
  "invariant 5 NOT MET".
- M3 needs a Lean 4 toolchain (+ Mathlib) and at least one exact solver; they run inside the sandbox image (installing
  them on the user's machine or image is the user's decision). Simulators for M5 may carry licences; only
  licence-compatible, pinnable simulators qualify for adapters.
- Physical/empirical claims need human operators and labs; the system only prepares protocols and checks returned
  data.
- The time-split set needs cutoff-dated models; contamination probes may reduce usable windows.

## 13. Open questions (decide when reached)
1. The risk prior for K7: baseline-model predictive distribution vs empirical prior from the ledger (likely both, max).
2. The VDS weighting between information gain and miss penalty (pick a proper scoring rule; simulate before fixing).
3. Kernel scope beyond ML: decided 2026-10-07 (life sciences + engineering via adapters, §4.7); open: which K8
   invariants each first adapter registers, and the K7 risk models for formal/computational/simulated claims (a
   theorem has no resampling noise: its "risk" needs a prior over statements, e.g. from the literature graph).
4. Whether to release the prediction ledger publicly as it accrues or at milestones.
5. A risk model for negative-result/equivalence claims (K7 fails them in v1, fail closed).
6. The human-owned prohibited list and review standard for life-science protocols (biosafety / dual use) before
   any life-science adapter is approved.
7. Adapter validation standards per evidence class (minimum fault-injection suite, golden cases, tolerances).
8. Parameters for the §16 upgrades. The upgrades themselves are adopted; these values are still open:
   - the slot count S_T per target and α_T (§16.4);
   - the preflight threshold θ (§16.8);
   - ε_max and β presets for the K3 reliability demonstration (§16.4);
   - the δ floor for large minted test sets (§16.2);
   - the model-call budget for salts (§16.9) and the detectability bound (≤ 0.2 at 40 + 40 presentations, or pay
     for ≤ 0.1);
   - drand fetch or human dice for the public beacon (§16.3);
   - who holds the offline secret (ideally a second person);
   - whether nondeterministic claims are allowed at all (§16.5);
   - who rules on feature legitimacy (§16.5);
   - the clawback policy (§16.10);
   - adding genuinely null tasks to the real pool vs the 10–90% admission filter.

## 14. Glossary
- **Certificate**: Kernel output stating which checks a claim passed, at what tier.
- **VALIDATED loop**: a loop whose acceptance comes from evidence the system did not generate or see during optimisation.
- **VDS**: Validated Discovery Score, the proper-scored information gain of confirmed risky predictions.
- **Reproducibility Bundle**: everything needed to rerun a result with one command.
- **Evolve / gate / sealed**: task splits. The proposer sees evolve; acceptance uses gate; humans alone touch sealed.
- **Infra vs policy failure**: the environment broke vs the harness's behaviour failed.
- **Evidence class**: formal · computational · data/statistical · simulated · physical/empirical (§4.7).
- **Domain adapter**: the human-approved, fixed, versioned verifier layer for one domain or tool (§5).
- **Approval node**: a run step that only a human can complete; binds to one hashed item.
- **LOFO / LODO gate**: acceptance also requires non-regression on an entire task family / domain never shown to the
  evolver.

---

## 15. Plan: multi-agent domain expansion (after the M3 core/adapter split)

Goal: grow from ML to life sciences and engineering without weakening the trusted base. The adapter interface (M3)
is the contract; domain work happens outside the Kernel, and nothing an agent writes becomes trusted without a human.

**Order.** (1) M3 delivers the core/adapter split, the Lean, solver and simulator reference adapters and the approval
node. (2) The user fixes the domain list, the prohibited list and the review standard (§13.6-7). (3) Expansion starts.

**Roles (separate agents, separate model families where invariant 5 applies):**
- *Domain scout* (one per domain): proposes the domain's evidence classes, metrics, K8 invariants, candidate tools and
  task sources, with citations; output is a written adapter proposal, never code in the trusted base.
- *Adapter builder*: implements the adapter in a sandboxed branch against the interface, with its validation suite
  (golden cases, fault injection, tolerance calibration).
- *Adapter red team* (a different family from the builder): tries to get a false claim certified through the adapter
  (wrong units, unconverged simulations, forged solver certificates, scope creep from simulated to physical); every
  success becomes a regression test before review.
- *Task-suite builder*: builds the domain's task suite with held-out families, content-level isolation
  (`check_split_isolation`) and recorded sources.
- *Human reviewer* (the user or delegate): approves or rejects each adapter version through the approval node;
  approval registers its hash in the ledger. Without it the adapter cannot be used.

**Parallelism and isolation.** Domains proceed in parallel, each in its own branch/worktree and sandbox; one writer
per branch; the Kernel core and existing adapters are read-only to all of them. Merging requires: the full gate green,
the adapter's validation suite green, the red-team report attached, and the human approval recorded.
Delegation briefs are self-contained and state exactly which compute and model-quota spends the helper may make;
everything else is forbidden. Helpers cannot spawn helpers, the number in flight is capped, and a helper never
receives a branch another session holds (OR's delegation rules, enforced here by the driver, not by the prompt).

**First candidates (to be confirmed by the user):** engineering: structural/thermal FEM with mesh-convergence
evidence (simulated class), circuit/ODE models with exact solvers (computational); life sciences: computational
biology on public data (statistical class: e.g. expression/association analyses on held-out cohorts), molecular or
systems-biology simulation (simulated class); wet-lab claims only as protocols through the approval node (physical
class), after the prohibited list exists.

**Exit (M5):** see §11; additionally each domain's LODO gate is live, and the scorecard (§2, axis D) gains one
hard-verifier problem per domain.

---

## 16. Architecture upgrade (2026-10-07): prevention before measurement, truth minted after the freeze

**Where this comes from.**
- Fifteen agents reviewed the design: four single-lens reviews, two scouts of real systems, a constraint solver, a
  product review, five creative thinkers who debated over two rounds, and two competing evaluators who graded
  about 95 ideas as security and alignment reviewers.
- The texts, toy simulations and grades are summarised in `code_review/upgrade_review_2026-10-07/` (README.md indexes it).
- **Honest status:**
  - Every number below comes from toy simulations or from reading code. Nothing has been run on the real system.
  - Both evaluators independently found **no breakthrough**. The real advance is an architecture: prevention in
    front of measurement, truth created after the freeze, tests of the tests, and honest accounting throughout.
- The items were adopted under the owner's delegation of 2026-10-07 ("incorporate the ones which upgrade"). The
  parameters stay open (§13.8).

### 16.1 Close the answer keys first (findings; their preconditions verified by reading code)
- **BUNDLED-DATA.** The 5 real tasks are exported with scikit-learn's bundled datasets (`research/suite.py`), and
  the candidate sandbox image ships the same labelled copies. That is 9 of 21 suite tasks.
  - **Fix:** a candidate image with every bundled dataset removed. The full image is used only by the trusted
    exporter. A truth scan checks the image, and a missing candidate image is refused.
  - Fix in progress on branch `m3-fix-bundled-data`.
- **COUPLED-PRNG.** Every synthetic generator draws features, labels and the split from one `random.Random`
  stream with fixed seeds. That is 12 of 21 tasks.
  - Anyone who can read the generator code can regenerate the held-out labels. In a toy, the stream state could
    also be cloned from the features alone; whether that works on our real files is untested.
  - **Fix:** independent keyed streams (HMAC per purpose). Seeds come from `draw()` after the freeze, and the
    generators are treated as human-approved adapters.
- Until both are fixed, no M2 certificate on the affected tasks is trusted.
- Invariant 2 (§10) now names this class: truth can sit in the environment or be regenerable, not only in mounts.

### 16.2 Freeze, then mint the truth (the strongest principle; a known guarantee applied systematically)
- **What it is.** For synthetic, simulated and future targets, the held-out truth is created only after the claim,
  edit, Kernel version or screening rule has been hashed and anchored. It is drawn from independent keyed streams.
  The guarantee is the classic fresh-sample one, as in CASP, Procgen and LiveBench.
- **What it removes, for those targets:**
  - label canaries;
  - copy-hunting;
  - label-swap replays;
  - the leak role of null salts;
  - test-set wear (every target is fresh).
- **Combined with large test sets.** Pair it with **small train / large test**: about 20k minted test rows takes the
  minimum certifiable effect from about 0.135 to about 0.005 (computed). It is the cheapest way to more certifiable
  signal, and it needs a δ floor (§13.8) so that trivial effects do not become certifiable.
- **Limits:**
  - Fixed public datasets cannot be minted. They are flagged as **retrospective targets** and keep the §16.5
    controls.
  - Minted claims are generator-scoped, which makes the generator family the main Goodhart surface. Families rotate
    under LOFO.

### 16.3 One commitment primitive: `draw(purpose, ...)`
- Every choice made after pre-registration comes from one function: held-out subsample, seeds, target, gate tasks,
  minting, salt assignment and check parameters.
- A per-purpose table fixes the inputs:
  - purposes whose object is frozen before the draw use a public beacon only, and can be re-run at once;
  - salt assignment uses an offline human secret plus the beacon.
- The secret must appear nowhere agents can read, scratchpads included; a grep check enforces this.
- Restatement of commit-reveal; adopted because it replaces four ad-hoc mechanisms.

### 16.4 Statistics fixed at registration (invariant 17)
- **VDS as an e-process (fixes F2).** p0 is the claim's fixed level, set at registration, and K1 refuses q < p0.
  Ville's inequality then bounds P(sup VDS >= log2 1/alpha) <= alpha under the global null. Forecasting skill is
  scored separately against a pinned reference forecaster. Answers §13.2.
- **Per-target alpha-account** for retrospective targets:
  - a human opens slots for each target;
  - each slot is tested at a fixed level, whatever the order of claims;
  - a target retires when its slots run out.

  This makes test-set wear an explicit budget and fixes R2-2 by construction. The current declared
  `n_comparisons` bound stays until it is built.
- **`verify` as of the certificate's ledger prefix (fixes F3).** Later events lead to STALE, never to "forged".
- **K3 as a zero-failure reliability demonstration.** The claim declares eps_max and beta, and K3 passes only after
  ceil(ln beta / ln(1 - eps_max)) consecutive clean replays. In the toy, a 50%-flaky program passes about 3% of the
  time instead of 50%. Closes the R1-F3 residual.

### 16.5 Information-flow prevention, in build order (invariant 18)
1. **Hermetic, entropy-pinned sandbox.** The Kernel seeds the clock and urandom, so a program cannot leave the
   exact-check class by behaving randomly.
2. **Kernel process boundary ("kerneld").** Only the Kernel process mounts the store and private blobs. Host
   modules get narrow per-claim tokens. This makes the vault the sole path to the truth, which every check below
   assumes.
3. **Release ledger and verdict embargo.** Every transfer of target-derived information is a typed release, and K4
   counts the transcripts the release policy allows. The evolution score drops its continuous held-out term.
   - This makes Bonferroni a real adaptive guarantee, provided the log is complete. The log's completeness is tested
     by step 5.
   - It closes F1 (simulated: 55.5% false certification at nominal 5% under continuous feedback).
4. **Non-interference replay against the stored production output.** Re-run the frozen program with every declared
   copy of the truth replaced, using maximally different worlds: flipped labels plus a constant world.
   - Any change in its **decisions** fails the check. Score bytes are not compared, because float accumulation order
     changes them for honest code.
   - In toys this caught every evasion device built during the review (defeat device, 2% stealth leak, amplifier,
     test-world detector), with 0 honest false alarms.
   - Nondeterministic claims fall back to a paired McNemar dependence test, which has a printed stealth margin of
     about 1 point.
5. **Campaign-scope cassette replay.** Replay a whole recorded campaign with fake labels, the releases pinned and the
   model calls served from cassettes. Any cassette miss or decision change means held-out information reached an
   agent. This tests the release ledger's completeness at zero model calls.
6. **What output checks cannot see.**
   - Leak columns already in raw data: a provenance tag on each feature, a dominance screen, and a human ruling.
   - Truth memorised in model weights: only minted, future or human-returned targets avoid it.
7. **Transductive check (F4).** Predictions must be invariant to permuting or subsetting the other test rows. This is
   an ML-adapter validation case.
8. **Kernel reasons are a closed set of codes.** Free-text exception messages could carry data out of the Kernel.

### 16.6 Tests of the tests (invariants 19, 20)
- **Loophole museum plus mutation gate.**
  - Every past finding gets a patch that re-introduces it, and at least one test must fail on it.
  - Mutation score >= 90% on the vault, the ledger and the K2/K4/K6 code, with equivalent mutants <= 10% and each one
    justified.
  - The gate's Kernel test list is derived from the files, never listed by hand. It held merge-conflict markers
    during the 2026-10-07 merge.
- **Clean-room reference model plus metamorphic relations.**
  - The reference model is a pure, <= 400-line model of K2/K4/K6 and the label lattice, written from this document
    without reading `checks.py`. It is differentially tested against the Kernel on 1e5 generated ledger histories.
  - The relations: irrelevance, permutation, renaming, fail-closed monotonicity, honest composition, twin
    equivalence, as-of stability and arm swap.
- **The verifier's exams wear out (the most novel item; toy evidence only).** A rule patched until it passes a
  "0 flips on 500" screen ships every time, with a true flip rate of 0.94% against the claimed <= 0.6%. So every
  verifier validation set gets a dev half and a sealed half, and every look is ledgered.
- **Plan-code divergence.** The test actually executed must equal the registered test (the COMPare lesson). Make
  this explicit in K4.

### 16.7 Governance (invariant 22)
- **The examinee writes the exam.** AI agents wrote the Kernel, its tests and its reviews. So at least one check is
  held by a human and not visible to agents, and one attack per milestone comes from outside the agent pipeline.
- **Two-speed Kernel.**
  - The core (the §10 invariants and the K0-K9 pass conditions) changes only in scheduled windows, with a 7-day
    cooling-off.
  - The parameter list changes on evidence.
  - Changes that only tighten checks may merge at once.
- **Reputation may only raise scrutiny.** It never lowers it (the xz-utils lesson).
- **Self-reported fields** (for example a dataset role not approved by a human) cap the certificate tier.

### 16.8 Yield: more certified signal per model call (none of this weakens a check; invariant 21)
- **Certifiability preflight.** The real K4/K6/K7 functions run on outcomes simulated from public numbers before
  pre-registration, and a claim below theta is not registered. It also gives a zero-call audit of the suite before
  the M2 exit budget is spent.
- **Ranking and generation.**
  - Execution is the ranker: there is no tournament by default.
  - One call writes 2-3 scripts.
  - Improve calls are skipped when dev racing provably cannot pass.
  - Codex runs from an empty working directory.
  - Estimate: 13 calls become about 5-6 per run (inferred).
- **Tie-aware selection.** Among statistically tied candidates, pick the one with the lowest projected runtime, then
  the smallest code. Stored out-of-fold predictions give dedup and one pre-declared exact-dev ensemble.
- **Cassettes** for development: recorded runs become zero-call regression tests.
- **CPU-time limits with a contention witness**, so machine load stops causing policy failures. A program that only
  sleeps is still charged.
- **Second model family, admitted by role exams** (unblocks invariant 5).
  - Integrity rests on the Kernel, so a weaker critic, judge or replicator costs yield, not integrity.
  - Candidates: a local 3-7B open-weights model, or a free official API within its terms.
  - Admission requires a role exam on dev and public data only, with miss correlation against Codex phi < 0.8.

### 16.9 Measurement, budgeted (salts)
- **Positive salts first**, scored on whether they recover the known planted effect. They are the only defence found
  against the "amplifier" device.
- **Null salts** serve only as a gross-failure alarm. On real data they carry a tell, so they need genuinely null
  tasks in the pool (§13.8).
- A detectability exam must pass before any salt is used (d = TPR - FPR <= 0.2 at 40 + 40). Salts stay out of the L6
  gate and out of VDS.
- They need a model-call budget, so they are an owner decision.

### 16.10 Capabilities that fall out of the architecture
- **Bounded-null certificates in exclusion-limit form.** The sensitivity is frozen before the data, proved by a
  positive plasmode control, and lucky limits are clipped as in physics. This turns honest negative results into a
  product and answers §13.5. It is a K1/K7 change, built after §16.4.
- **Claim Audit (`chh audit`).**
  - A published sklearn-scale claim becomes a typed claim, approved by a human as faithful to the paper.
  - It is pre-registered before fresh external data is touched, and the baseline is re-run under an identical
    contract.
  - The verdict is a certificate, "not confirmed" (naming the failing check), "inconclusive", or "could not run".
  - No verdict about a named paper is published without human approval and a right of reply.
- **Label-dependence audit of published code.** Run a pipeline twice with its test labels flipped. Changed
  predictions show it reads test labels. Pipelines with label-dependent splits end INCONCLUSIVE.
- **Resweep.** Re-check archived certificates when the rules change.
  - It applies only after the rule passes a screen on the honest corpus's sealed half.
  - Statuses are typed: STALE under a new rule, new data, uncheckable, or integrity failure.
  - Clawback is graded and reversible, and VDS is split into provisional and seasoned.
  - Blocked until the honest corpus exists. Resweep verdicts are releases (§16.5).
- **Emergence rule.** Novelty may steer budget, never acceptance. Generated task families enter the evolve split
  only.

### 16.11 Not adopted (reason)
- **Dropped:**
  - Permuted-label leak checks and nulls: beaten by a defeat device, and blind to in-feature leaks.
  - Seed-subset fraud proofs: reverse p-hacking (96% of honest certificates broken at 1.5 delta).
  - Optimistic acceptance.
  - Attempt rate as a gate or fitness term: it rejects honest edits and selects for quieter cheating.
  - Escrowed credit, the alpha-market and credibility bankrolls: noise at our scale.
  - Expert priors for K7: overconfidence makes K7 easier to pass.
  - Baseline blinding: the agent can recompute the baseline.
  - Capture-recapture as a release gate: unstable; reported as a floor only.
  - Thresholdout, alpha-investing and Holm in an online family.
- **Deferred as incremental for now** (the gain is real but small next to its cost):
  - Merkle inclusion proofs;
  - validity intervals;
  - diverse double execution;
  - designer-calibration ledger;
  - Kernel reliability diagram, until salts exist;
  - ledger-calibrated forecasts, until >= 30 resolutions exist;
  - MAP-Elites archive, interestingness and FunSearch-lite on LP;
  - BixBench import;
  - certified leaderboard;
  - copilot mode;
  - OpenML upload-date time split (contested: useful vs a restatement);
  - campaign world model, blind rediscovery and baseline-anchored mode.

### 16.12 Remaining risks even if all of §16 is built (stated, not solved)
- Truth memorised in model weights.
- The Kernel certifies procedure, not meaning; claims can be true but trivial or generator-scoped.
- Goodhart onto generator families and small LOFO pools.
- An evaluation-aware optimiser that sandbags; detectability is bounded only at <= 0.2.
- Correlated designers (one AI family).
- One human acting as approver, blinder and judge.
- Supply chain and trusted base: the image, pip packages, the Codex CLI, WSL2, SQLite.
- Side channels.
- About alpha*N false certificates in aggregate.
- If honest pipelines cannot be made byte-deterministic, the strongest checks fall back to bounded-margin statistics.
