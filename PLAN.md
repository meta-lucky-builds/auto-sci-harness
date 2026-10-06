# custom-harness — Master Design Plan

> Superseded for WHAT/WHEN by `GOAL.md` (2026-10-06). This file is kept as design history and rationale.

Date: 2026-10-05. Source of truth for the survey: `../autoresearch_survey/00_SYNTHESIS.md` (cited below as **S§n**).
Status: **design only**. No code exists. Every number below that comes from a source is "as reported by a Haiku summary" and must be re-checked in the paper before we rely on it (S§0).

---

## 0. Honest framing (read first)

- **"Better than every harness in the survey" is not one thing we can guarantee.** The sources are good at different things and are scored on different benchmarks. What we *can* commit to is:
  1. **Coverage:** every capability in S§2–S§5 has a named home in this design (feature matrix, §9).
  2. **Measured superiority on a fixed scorecard (§10):** we beat or match the best reproducible baseline on each scorecard axis, with confidence intervals, on held-out data.
  3. **Things none of them have:** the fused self-improving + research loop, verifier-gated autonomy, and the other additions in §8.
- **The verifier ceiling (S§1) still applies to us.** This design does not make open-ended scientific judgment verifiable. It (a) uses hard verifiers wherever they exist, (b) tries to *build* more of them (§8.2), and (c) labels everything else honestly.
- **Compute is the real constraint.** Stacking MILO + RRSI + MoB + ScholarEvolve multiplies evaluation calls (S§7). The plan only works with the evaluation-budget machinery in §6.4. Phases (§11) are ordered so each one is useful on its own if funding stops.
- **Several mechanisms here are untested proposals** (marked **[NOVEL]**). Each has a falsification test; if it fails, we drop it.

---

## 1. The goal, exactly

> **custom-harness is a research agent that improves its own scaffolding, where every self-modification and every published claim is gated by the strength of the verifier behind it, and where the harness is optimized for research outcomes (measured by time-split rediscovery and hard-verifier discoveries), not just agent-benchmark scores.**

Decomposed into five commitments:

| # | Commitment | Closes gap |
|---|---|---|
| G1 | **Runs real research loops end-to-end**: problem → data profiling → literature → hypotheses → experiments → verified claims → paper with claim-level provenance. | AI-scientist half (S§2.B) |
| G2 | **Improves its own harness** with trace-grounded, typed, bounded edits under held-out gates, with branching/routing for heterogeneous tasks. | Self-improving half (S§2.A) |
| G3 | **Fuses G1 and G2**: the outer loop optimizes the *research* harness against research-outcome verifiers (time-split rediscovery, hard-verifier problems). | S§6.2 — the gap nobody closed |
| G4 | **Verifier-tier policy everywhere**: autonomy, skill admission, harness edits, and published claims are all gated by verifier tier. | S§6.1, S§6.5 |
| G5 | **Statistically honest and cost-aware**: CIs, held-out splits, cost as an objective, negative results logged. | S§6.4 |

**Non-goals (v1):** robotics / physical actuation (human-in-the-loop only, like Robin); training frontier-scale models; a general chat product.

---

## 2. Design principles (each derived from a multiply-replicated finding, S§4)

| Principle | Evidence | Consequence in design |
|---|---|---|
| P1 Raw traces, not scalars | S§4.1 (34.6 → 50.0) | Everything writes full traces to the Evidence store; optimizers read traces. |
| P2 Overfitting is the default | S§4.2 | Three-way splits (evolve / gate / sealed test), leakage critic, bootstrap CIs. |
| P3 Structure beats prose | S§4.3 | Harness edits are typed (contracts, hops, control flow, context policy), not free-text prompt tweaks. Prompt edits are one type among ~10. |
| P4 Inverted U over model capability | S§4.4 | Strong model proposes edits; the executor model is a parameter; we measure the curve. |
| P5 Relative > absolute judgment | S§4.5 | All LLM judging is pairwise / K-way with position swapping; ratings via Bradley-Terry. |
| P6 Remember failures | S§4.6 | Negative memory is first-class; rejected candidates kept with lineage. |
| P7 LLM proposes, code decides | S§4.7 | Acceptance, budgets, admission, publication are deterministic code over evidence. |
| P8 One harness doesn't fit all | S§4.8 | Harness *branches* + router; router itself held-out-tested. |
| P9 Reward hacking is real | S§4.9 | Exploit quarantine before anything enters memory or skills (§8.4). |
| P10 Verifier is the cap | S§1 | Verifier ladder is a core layer; tier is attached to every object. |

---

## 3. Core data model

Everything is an immutable, content-addressed record in the Evidence store. Nothing is "true" in the system unless an Evidence record supports it.

```
Run            id, harness_version, branch_id, task_id, model_ids, seed, budget, start/end, cost{tokens,$,gpu_h,wall}
Trace          run_id, ordered steps[ {role, input, output, tool_calls, tool_results, tokens, t} ], final_state_ref
StateSnapshot  id, run_id, step, filesystem_hash, env_hash, memory_hash   ← exact, restorable (ScienceFlow)
Evidence       id, kind{log|metric|test|proof|stat|citation|wetlab|judgment}, payload_ref, produced_by_run, verifier_id
Verifier       id, tier, description, cost, known_failure_modes, validated_by(mutation-test result)
Claim          id, text, tier(min over supporting evidence chain), evidence_ids[], status{draft|verified|published|retracted}
Hypothesis     id, text, parents, novelty_score, feasibility_from_data_profile, bt_rating, status
HarnessVersion id, parent_id, diff(typed edits), spec(YAML/DSL), branch_id, gate_results, predicted_effect, actual_effect
Edit           id, type, target, diff, rationale_trace_refs[], predicted_effect{clusters_fixed, delta, at_risk_tasks}
Skill          id, code/prompt, provenance, quarantine_result, tier, usage_stats
MemoryEntry    id, kind{positive|negative|dead_end|playbook|lineage}, content, evidence_ids[], embeddings, last_read
```

**Verifier tiers** (strongest first):

| Tier | Name | Examples |
|---|---|---|
| T0 | Formal | Lean proof, SMT check |
| T1 | Executable exact | test suite, evaluator.py, BnB-PEP numerical certificate, invariant checks (ScienceIDE) |
| T2 | Statistical | multi-seed metric with CI, independent re-implementation agrees |
| T3 | Physical / external | wet lab, optical bench, human expert replication |
| T4 | Ground-truth retrospective | time-split rediscovery match against later literature |
| T5 | LLM judgment | pairwise / K-way judge panel |
| T6 | None | unsupported assertion → cannot be published |

A claim's tier = the weakest link in its evidence chain.

---

## 4. Architecture — the layer stack (extends S§7)

```
 L9  Governance & human interface   tier policy, approvals, dashboards, kill switch
 L8  Weights (slow)                 RL/SFT on doubly-verified trajectories
 L7  Search-over-search             orchestrator over L6: mutator mix, islands, stall detection
 L6  Harness evolution (outer loop) diagnose → typed edits → gate → branch/route
 L5  Memory                         positive/negative/dead-end/lineage/playbook + revivable states
 L4  Research loop (inner loop)     formulate → profile → lit → hypotheses → experiments → claims → paper
 L3  Verifier ladder                T0..T6 verifiers + verifier synthesis + exploit quarantine
 L2  Evidence plane                 immutable logs, claim graph, integrity audit
 L1  Control plane                  budgets, scheduling, admit/timebox/kill, eval allocation
 L0  Substrate                      durable graph runtime, sandboxes, exact snapshots
```

Dependency rule: layer N may call layers < N; it can only *influence* higher layers by writing Evidence.

### L0 — Substrate
- **Durable graph runtime**: nodes = agent steps, edges = control flow, checkpoint after every node, interrupts for human approval. LangGraph-style semantics (S§2.F); build or adopt — decide in Phase 0.
- **Sandboxes**: containerized, network-policy'd, resource-capped; one per run.
- **Exact state snapshots** (ScienceFlow, S§5): filesystem + environment + memory hash; restore = byte-identical resume. Required for backtracking (−13.6 pp without it, as reported) and for revivable dead-ends (§8.9).
- **Model gateway**: one interface for all LLMs; logs every call to Trace; supports open-weight models (S§6.9 reproducibility).

### L1 — Control plane (deterministic code, no LLM authority)
- Budget ledger per run / per campaign / per edit candidate: tokens, $, GPU-hours, wall clock.
- Admit / timebox / kill policies (ScienceFlow resource controller, Auto-RecSys scripts).
- **Evaluation allocator** (§6.4): decides how many rollouts each candidate gets.
- Crash recovery from last snapshot (Auto-RecSys, Prime Agent long runs).
- Cost is logged for every object (fixes S§6.4 missing-cost problem).

### L2 — Evidence plane
- Append-only store; content-addressed; every record has producer run + verifier.
- **Claim graph**: claims → evidence → runs → traces. Chain-of-Evidence by construction (ScientistOne).
- **Integrity audit** (ScientistOne I1–I4 generalized), run deterministically before anything is published:
  - I1 every number in a report resolves to an Evidence record;
  - I2 baselines conform to the task spec (catches spec-violating baselines);
  - I3 every citation resolves to a retrieved document whose text supports it;
  - I4 reported metric recomputed from raw logs matches.
- **Negative results are first-class Evidence**, published alongside positives.

### L3 — Verifier ladder
- Registry of verifiers with tier, cost, known failure modes.
- **Verifier validation**: every new T1/T2 verifier is mutation-tested (inject known-wrong candidates; it must reject them) before it can gate anything.
- **Verifier synthesis [NOVEL]** (§8.2).
- **Exploit quarantine [NOVEL]** (§8.4).
- **Judge panel (T5)**: pairwise / K-way with position swapping, multiple model families, agreement stats logged; never sufficient alone for publication of a "verified" claim.
- **Physical / human (T3)**: ticketed tasks to humans with protocol, results entered as Evidence (Robin / AHOIS pattern).

### L4 — Research loop (inner loop)
Each stage is a graph node with its own contract (input/output schema) — contracts are the edit surface for L6.

1. **Problem intake / formulation** — accepts a given problem *or* runs the Problem Formulator (§8.11) to propose problems, each tagged with the strongest verifier available for it. Problems with only T5 verifiers are flagged "soft".
2. **Data profiling first** (SciDER): schema, distributions, missingness, leakage checks, feasibility constraints → feeds hypothesis filter.
3. **Literature module**: retrieval + persistent full-text index + knowledge graph (PARNESS); citation verifier agent in parallel (Feynman); dated snapshots so time-split experiments can freeze it at T.
4. **Hypothesis generation**: diverse proposers incl. forced distance-3 cross-domain inspiration (ResearchBench) and literature-sourced mechanisms (ScholarEvolve); topic-quota round robin.
5. **Hypothesis tournament**: K-way relative ranking → Bradley-Terry ratings (Robin, SciDER, EvoScientist Elo); feasibility from step 2.
6. **Experiment design**: EIG-based selection where a model class exists (MDA); otherwise value-of-information heuristic; explicit stopping rule (§8.9).
7. **Execution**: tree search over experiment code with exact snapshots and backtracking (AI Scientist v2 tree search + ScienceFlow states).
8. **Socratic critic** (AHOIS): asks questions only — causal probes, "what would falsify this?", "which control is missing?" — answers must cite Evidence.
9. **Claim formation**: claims are created *only* from Evidence; tier computed automatically.
10. **Report / paper**: generated from the claim graph; every sentence that asserts something links to a claim; tier labels shown; integrity audit must pass.

### L5 — Memory
- **Kinds**: positive (what worked), negative (what failed and why), dead-ends (direction + reason + snapshot ref), lineage (all harness candidates incl. rejects — MILO), playbook (operational procedures with maturity stats — Auto-RecSys).
- Cross-task idea memory and experiment memory (EvoScientist).
- **Hierarchy** L0–L3 by recency/importance (Prime Agent).
- **Read-audit**: every entry tracks reads; entries never read in N campaigns are summarized or retired (fixes "memory written but rarely read", S§6.6).
- **Revivable dead-ends** (§8.9): dead-end entries point to exact snapshots and are re-scored when new literature or evidence arrives.
- **Admission rule**: nothing enters skills or positive memory without passing exploit quarantine and meeting the tier floor (§5).

### L6 — Harness evolution (outer loop)
The harness is a **typed spec** (YAML/DSL + code modules), so edits are diffable and classifiable.

**Edit types** (closed set, extendable by the system with human approval):
`contract` · `hop / information-flow` · `control-flow (retry/loop/terminate)` · `context policy (compaction, packing)` · `tool add/modify` · `subagent role` · `memory policy` · `prompt text` · `verifier wiring` · `budget policy` · `routing rule`.
(RHI contracts/hops, AutoSaddler patch taxonomy, HarnessX 9-dimension typing.)

**Pipeline per generation:**
1. **Diagnose** from full traces (Meta-Harness, Self-Harness failure signatures, AutoSaddler deep diagnosis, HarnessX digest): cluster failures; each cluster cites trace refs.
2. **Propose** typed edits. Proposer sources: (a) strong external model (Meta-Harness/MILO), (b) same model as executor (Self-Harness), (c) literature-mined mechanisms (ScholarEvolve). Every edit carries a **predicted effect** (§8.5).
3. **Socratic review of the edit** (§8.6) + **leakage critic** (RRSI): does the edit encode test-set specifics?
4. **Evaluate** via the allocator (§6.4) on the *evolve* split; compare against parent (RHI Θ(1) comparison) and against the branch frontier.
5. **Gate** (deterministic): non-regression on the *gate* split per branch (Self-Harness, resolved for MoB per S§7), bootstrap CI on improvement (ScholarEvolve), cost not above budget, exploit quarantine passed.
6. **Branch / route**: frontier-coverage voting decides whether an edit creates a new specialist branch (MoB); router trained on gate split, tested on sealed split (Adaptive Auto-Harness oracle gap is the target to close).
7. **Record** as a harness-science claim (§8.10).

**Mid-episode refinement** (Continual Harness): allowed only for low-risk edit types (context policy, prompt text) and only within a run; promoted to the harness only via the full pipeline.

**Temporal-reveal mode** (Adaptive Auto-Harness): on streaming tasks, feedback becomes visible only after a delay, to prevent label leakage.

### L7 — Search over search
- Islands with different proposer/mutator mixes (MILO); migration between islands.
- **Orchestrator** detects stalls (no gated improvement in k generations) and reallocates budget across mutators, islands, and edit types (MILO orchestrator, MoB guidance evolution).
- Anti-collapse: enforced quota of non-prompt edit types (HarnessX "under-exploration" finding).
- The orchestrator's own policy is versioned and evaluated like a harness, but on a slower cadence.

### L8 — Weights (slow timescale, optional per deployment)
- Training data = trajectories that pass **two disjoint verifier sets** (§8.8).
- Methods: SFT on verified trajectories, GRPO/RL in verifiable environments (HarnessX, SIA LoRA, ScienceIDE environments, Continual Harness DAgger + PRM), synthetic data targeted at reward-variance gaps (AWorld-RL RODS).
- Weight updates are always followed by re-running the harness gate (a new model can invalidate harness branches — the inverted U).

### L9 — Governance & human interface
- Tier policy table (§5) is configuration, changeable only by a human.
- Approval queue for: new edit types, T3 physical tasks, publishing, anything below tier floor.
- Dashboards: campaign progress, claim graph, harness lineage, cost, calibration of proposers.
- Kill switch at campaign and system level.

---

## 5. Verifier-tier policy [NOVEL — S§8.1, specified]

| Action | Minimum tier of supporting evidence | Additional requirement |
|---|---|---|
| Publish claim as "verified" | T2 (T0/T1 preferred) | Integrity audit passes |
| Publish claim as "supported" | T4 or T5 | Labeled with tier |
| Admit skill to library | T1 | Exploit quarantine passed |
| Accept harness edit (auto) | T1/T2 gate results | CI excludes zero; non-regression per branch |
| Accept harness edit evaluated only by T5 | — | Human approval required |
| Modify verifier wiring / tier policy | — | Human only |
| Weight update | trajectories pass 2 disjoint verifier sets | Post-update harness re-gate |

---

## 6. The fused loops — how self-improvement and research meet (G3)

### 6.1 Three timescales
```
fast   (minutes–hours) : L4 inner research loop on one task, mid-episode refinement
medium (hours–days)    : L6 harness generation over a task batch; L7 adjusts every ~k generations
slow   (days–weeks)    : L8 weight update; tier policy review by humans
```

### 6.2 The research-quality objective (what L6 optimizes when the harness is a research harness)
Weighted scorecard, each term computed by a non-LLM verifier where possible:

1. **Time-split rediscovery rate (T4)** [NOVEL, S§8.2]: freeze literature + data at date T (after executor model cutoff); task = open problem as stated at T; score = match of produced claims to results published after T, judged by structured matching (pairwise judge only for the final semantic match, with human audit sample). Includes a **novelty-weighted holdout** so obvious results don't dominate.
2. **Hard-verifier discovery score (T0/T1)**: problem suites with evaluator scripts/Lean (Station-style bounds, AutoOPT-style optimization, kernel speedups) — improvement over best-known value.
3. **Executable research tasks (T1/T2)**: ML-engineering / reproduction tasks with test harnesses (e.g. ScienceIDE environments; MLE-bench-like suites — verify availability).
4. **Integrity**: audit pass rate, hallucinated-citation rate, claims-without-evidence count (must be 0).
5. **Cost**: $ and wall clock per verified claim.

Gate rule: an edit must not reduce (1)–(4) on the gate split beyond CI, and must improve the weighted total.

### 6.3 Data flow between loops
- Inner-loop traces → L6 diagnosis.
- Inner-loop claims and dead-ends → L5 memory → next tasks.
- Literature module → ScholarEvolve-style mechanism mining → L6 proposals.
- L6 harness-science claims → L5 → transfer priors for new models.
- Doubly-verified inner-loop trajectories → L8.

### 6.4 Evaluation allocation [NOVEL in this corpus, S§8.3]
- Candidates compete in **racing / successive halving**: small rollout batch for all, eliminate those whose CI upper bound is below the incumbent's lower bound, give survivors more.
- Prior from proposer calibration (§8.5) sets initial budget.
- Cheap proxies first (trace replay on cached prefixes, smaller task subsets) only to *prune*, never to *accept*.
- Target: ≥3× fewer rollouts per accepted edit at equal false-accept rate vs fixed-k (falsification test for this idea).

---

## 7. Conflict resolutions (from S§7)

| Conflict | Resolution |
|---|---|
| Cost multiplies | §6.4 allocator; islands share cached rollouts; proxies prune only |
| Global non-regression vs specialists | Non-regression per branch + router tested on sealed split |
| Same-model vs external proposer | Run both as islands; proposer calibration (§8.5) decides allocation empirically |
| Decentralized vs centralized | Decentralized cognition (Station-style agents + shared archive), centralized resources and verification (L1, L3) |
| Audit assumes gold evaluator | Audit checks *provenance* everywhere; *correctness* only where T0–T2 exists; otherwise claim is labeled with lower tier |

---

## 8. Novel additions — specified

Each: mechanism → falsification test.

1. **Verifier-tier-gated autonomy** — §5. *Test:* compare exploit/regression incidents with vs. without gating over equal budgets.
2. **Verifier synthesis** (new, beyond S§8): when a problem has only T5 verification, the system tries to build T1/T2 verifiers — property tests, invariants, simulators, held-out data checks — and validates them by mutation testing before use. This is how we widen the hard-verifier territory instead of accepting the ceiling. *Test:* fraction of "soft" problems upgraded to T1/T2; false-accept rate of synthesized verifiers on injected wrong candidates.
3. **Time-split rediscovery objective** — §6.2. *Test:* does harness optimization against it improve held-out rediscovery on a later time window (T2 > T)? If not, it is overfitting to the window.
4. **Exploit quarantine** — rerun gain under perturbed evaluators (hidden invariants, stricter spec, randomized variant); drop if gain vanishes. *Test:* plant known exploits (RCON-like) and measure catch rate.
5. **Proposer calibration** — every edit predicts effect; log predicted vs actual; calibration error sets acceptance thresholds and island budgets. *Test:* does calibration-weighted allocation reach the same score with less budget?
6. **Socratic critic on harness edits** — questions only, answers must cite traces. *Test:* ablation vs RRSI-style leakage critic on OOD drop.
7. **Diversity injection** — distance-3 inspiration, literature mechanisms, topic quotas, edit-type quotas; diversity measured by error correlation across proposers, not by vendor count (SDE r > 0.8). *Test:* error correlation and frontier coverage vs. vendor-mixing only.
8. **Two-timescale co-evolution with disjoint verifiers** — harness on set A (fast), weights on set B (slow), training data must pass both. *Test:* coupled-Goodhart indicator = gap between A-score and sealed-score after co-evolution vs. single-verifier co-evolution.
9. **Persistence rule + revivable dead-ends** — continue while P(improvement) > θ under a Bayesian model of the direction's progress; abandoned directions stored as snapshots and re-scored on new evidence. *Test:* revived-direction success rate; compare to Station-style heuristic abandonment.
10. **Harness science ledger** — every accepted edit stored as a claim "edit type X fixes failure class Y for model family Z, effect ± CI"; aggregated into transfer priors. *Test:* do priors improve first-generation results on a new model?
11. **Problem Formulator** (new): mines anomalies (results that contradict consensus in the literature graph), open-problem statements, and failed-replication records; proposes problems ranked by (verifier tier available) × (estimated value) × (feasibility from data). Closes S§6.3 partially. *Test:* human-expert pairwise preference vs. given problems, and fraction of formulated problems that end in T0–T2 claims.
12. **Independent replication agent** (new): for any claim aimed at T2, a separate agent with no access to the original code re-implements from the paper text; agreement upgrades tier, disagreement opens an investigation. *Test:* catch rate on seeded bugs in experiment code.
13. **Design for the inverted U** — frontier model proposes edits; cheaper executor runs; the curve is measured per model and stored in the ledger. *Test:* cost per verified claim vs. frontier-only executor.
14. **Data profiling before ideation everywhere** — mandatory L4 step 2. *Test:* infeasible-hypothesis rate with vs. without.
15. **Red-team module** (new): an adversary agent periodically tries to make the system accept a false claim or exploit a verifier; successful attacks become regression tests in L3. *Test:* attack success rate over time should fall.

---

## 9. Feature coverage matrix — "does it contain everything?"

Every source capability → where it lives. A row with no module is a bug in this plan.

| Source | Capability taken | Module |
|---|---|---|
| Meta-Harness | Full filesystem of past code/traces/scores for proposer | L2 + L6.1 |
| RHI | Contracts & hops edit types; compare only to parent | L6 edit types, L6.4 |
| RRSI | Proposal + acceptance regularization; leakage critic | L6.3, L6.5 |
| Self-Harness | Same-model proposer; failure signatures; held-in/out non-regression | L6.1–6.2, L6.5 |
| AutoSaddler | Deep diagnosis, patch taxonomy, EvoDAG, rollout efficiency | L6.1, edit types, §6.4 |
| MILO | Islands, lineage incl. rejects, adaptive orchestrator | L5 lineage, L7 |
| Mixture of Branches | Frontier-coverage voting, branches, router | L6.6 |
| HarnessX/AEGIS | 9-dim typed harness, digest→plan→evolve→critic, deterministic gate, harness+weights | L6 typing, L6.5, L8 |
| Adaptive Auto-Harness | Git-branch harness tree, 4-role evolver, temporal reveal, solve-time routing | L6.6, temporal-reveal mode |
| ScholarEvolve | Paper → mechanism → code mutation; lifelong generations; bootstrap CI; topic quotas | L4.3, L6.2(c), L6.5, §8.7 |
| SoL-Pi | Cost objective; hypothesis-driven harness study; frozen held-out | L1, §6.2(5), splits |
| Continual Harness | Mid-episode refinement; DAgger + PRM | L6 mid-episode, L8 |
| SIA | Feedback agent picks harness vs weights lever | L7 orchestrator + L8 |
| EvoMaster | Execution / exploration / evolution loops between runs | L4 + L6 cadence |
| Weng survey | Decision observability; stage-5 optimizer optimization | §8.5, L7 |
| AI Scientist v1/v2 | End-to-end pipeline; tree search over experiments | L4 entire, L4.7 |
| EvoScientist | Cross-task idea + experiment memory incl. failures; Elo | L5, L4.5 |
| ScientistOne | Chain-of-Evidence; deterministic Ground; integrity audit I1–I4 | L2 |
| SciDER | Data profiling first; K-way ranking; open model + trajectories | L4.2, L4.5, L0 gateway |
| ScienceFlow | Recoverable exact states; separate resource controller | L0 snapshots, L1 |
| PARNESS | YAML DAG kernel; full-text index; knowledge graph | L0 graph runtime, L4.3 |
| Auto-RecSys | Cognition vs deterministic procedure; playbook with dead-ends & maturity | L1, L5 playbook |
| MM-Agent | Hierarchical method library; actor-critic modeling | L5 (method library as memory kind), L4.8 |
| MatAgent | Human-in-the-loop agents | L9 approvals, L3 T3 |
| Feynman | Parallel citation verifier | L4.3, L2 I3 |
| The Station | Decentralized agents; paper archive as coordination; evaluator + Lean | L4 multi-agent mode, L3 T0/T1 |
| AutoOPT | Numerical certificate + Lean for discoveries | L3 T0/T1 |
| MDA | Bayesian evidence; EIG experiment design; LLM only proposes models | L4.6, §6.4 |
| Robin | Lab-in-loop with humans; BTL ranking | L3 T3, L4.5 |
| AHOIS | Socratic question-only critic; physical bench | L4.8, §8.6, L3 T3 |
| ScienceIDE | Codebases → verifiable RL envs; tolerances + invariants | L3 T1, L8, §8.4 |
| AWorld-RL | Data synthesis at reward-variance gaps | L8 |
| Prime Agent | L0–L3 memory hierarchy; long durable runs; (exploit lesson) | L5 hierarchy, L0/L1, §8.4 |
| ResearchBench | Hypothesis composition eval; distance-3 inspiration | §10 eval, §8.7 |
| SDE | Discovery-grounded eval; cross-vendor error correlation | §10 eval, §8.7 metric |
| LangGraph | Graph, checkpoints, interrupts | L0 |

---

## 10. How we prove it is top-tier — the scorecard

**Rules:** three-way splits (evolve / gate / sealed); sealed split touched only at milestone ends; ≥3 seeds; bootstrap 95% CI; cost reported for every number; baselines re-run by us where code exists, otherwise cited as "reported" and marked.

| Axis | Benchmarks (verify availability/licensing in Phase 0) | Baselines to beat |
|---|---|---|
| A. Agent harness quality | Terminal-Bench 2.x, SWE-bench Verified, GAIA, AppWorld | Meta-Harness, MILO, MoB, Self-Harness numbers (re-run where possible) |
| B. Self-improvement efficiency | Gain per 1k rollouts; OOD drop after evolution | AutoSaddler (rollouts), RRSI (OOD) |
| C. Research execution | ScienceIDE envs, ML reproduction/engineering suites | AI Scientist v2, SciDER, EvoScientist |
| D. Discovery | Station-style bound problems, AutoOPT-style problems | Best known values; Station, AutoOPT |
| E. Research judgment | ResearchBench, SDE, time-split rediscovery set (built by us) | Reported numbers; frontier-model single-agent |
| F. Integrity | Hallucinated citations, unsupported claims, audit pass | ScientistOne (0/337 as reported) |
| G. Cost | $ and hours per verified claim / per benchmark point | SoL-Pi, RHI |
| H. Robustness | Planted exploits caught; red-team success rate | No baseline — we define it |

**"SOTA" definition for this project:** on the sealed split, at matched or lower cost, we are ≥ best reproducible baseline on A–D and G (CI-overlapping counts as "match"), strictly better on F and H, and we report E as the first fused-system result. Any axis where we lose is reported, not hidden.

**Required ablations** (each module must earn its place): remove each of — traces→scalars, gate split, leakage critic, Socratic critic, exploit quarantine, allocator, branching, memory negative entries, literature mutations, data profiling, tier policy.

---

## 11. Build phases (each with exit criteria; each useful alone)

| Phase | Build | Exit criterion |
|---|---|---|
| **0. Foundations** | Pick runtime (adopt LangGraph vs own); sandbox; model gateway; Evidence store + data model (§3); benchmark access checks; re-verify the four extraordinary claims (S§0) | Run any task, fully traced, snapshot+restore byte-identical; cost logged |
| **1. Baseline research agent (G1)** | L4 pipeline minimal: profile → lit → hypotheses → K-way → experiment tree search → claims → report; L2 audit I1–I4 | End-to-end on 10 ScienceIDE/ML tasks; 0 unsupported claims in reports |
| **2. Verifier ladder** | L3 registry, tiers, judge panel, mutation-testing of verifiers, exploit quarantine, tier policy | Planted exploits caught ≥ target (set in Phase 0); tier attached to every claim |
| **3. Harness evolution (G2)** | L6 typed spec, diagnosis, proposers, gate, allocator (§6.4) on agent benchmarks | Reproduce a Meta-Harness-scale gain on one benchmark with CI on sealed split; allocator falsification test run |
| **4. Branching + memory** | MoB branches, router; L5 full memory incl. dead-ends, lineage, read-audit | Router beats single best harness on heterogeneous suite (sealed) |
| **5. Fusion (G3)** | Build time-split rediscovery set; point L6 at research objective (§6.2); literature-sourced mutations | Harness evolved on window T improves rediscovery on later window T2 (the key result) |
| **6. Search-over-search** | L7 islands + orchestrator; proposer calibration; Socratic edit critic | Lower budget to reach Phase-5 score |
| **7. Discovery mode** | Station-style decentralized agents on T0/T1 problem suites; verifier synthesis; problem formulator; replication agent; red team | ≥1 independently verified improvement on a known-value problem, or documented null result |
| **8. Weights** | L8 two-timescale co-evolution with disjoint verifiers | Sealed-score gain without A-vs-sealed gap widening |

Phases 1–3 can partially overlap; 5 depends on 1–3; 8 is optional.

---

## 12. Risks and open questions

- **Time-split contamination:** models may have seen post-T results via leakage; need cutoff-verified models and contamination probes. If unsolvable, objective 6.2(1) degrades to T5.
- **Rediscovery rewards the obvious** — mitigated by novelty-weighted holdout, not eliminated.
- **Compute cost of Phase 5+** is unknown until §6.4 is measured; may force smaller task suites.
- **Verifier synthesis can produce wrong verifiers** — mutation testing reduces but doesn't eliminate; synthesized verifiers cap at T2 until human-reviewed.
- **Router generalization** — the 87.7 vs 50.2 oracle gap (as reported) suggests routing is hard; may stay a large gap.
- **Same-model vs external proposer** — open; we measure, not assume.
- **Benchmark validity** — Terminal-Bench/SWE-bench versions shift; pin versions.
- **Closed-model dependence** — keep an all-open-weights configuration runnable (lower scores expected).

---

## 13. Decisions

### Decided (2026-10-05)
1. **Runtime: own thin core, adopt everything around it.**
   - Own: a small interpreter that runs the typed harness spec (§4 L6) as a graph of steps, with event-sourced traces, deterministic replay, and exact snapshots tied into the sandbox.
   - Reason: the harness is the object being optimized. Edits must be clean typed diffs against semantics we control, not edits to LangGraph code. LangGraph checkpoints graph state only, not sandbox filesystem/env, which we need anyway.
   - Adopt (do not build): containers (Docker / gVisor), a model-gateway library or thin adapters, Postgres/SQLite + content-addressed object store, MLflow-style experiment tracking, local serving (vLLM / llama.cpp / Ollama).
   - Cost: slower Phase 0 than adopting LangGraph.
2. **Stack:** whatever the build demands; default Python for orchestration (ML/DL ecosystem), PyTorch inside sandboxes, Docker, Postgres + object store. Revisit only if profiling demands.
3. **First research domain: ML / DL.**
   - Pro: plentiful T1/T2 verifiers (metrics, seeds, CIs, held-out data).
   - Con: worst contamination risk for time-split rediscovery (ML papers dominate model training data) → cutoff-dated models + contamination probes required.
4. **Access modes: three, see §14.**

### Still open (resolved by measurement in Phase 0, not by guessing)
5. Which models per role (proposer / executor / judges) → measure capability floor per role; publish a "minimum viable configuration".
6. Budgets → expressed in provider-neutral units (§14.4); concrete numbers set after Phase 0 measurements.

---

## 14. Access modes, Terms of Service, and budgets

### 14.1 The three modes

| Mode | What the user brings | Status | Main constraint |
|---|---|---|---|
| **OW — Open weights** | Own hardware (or rented GPUs); local serving via vLLM / llama.cpp / Ollama | Fully supported | Model license terms (some, e.g. Llama, have conditions); small models may sit below the capability floor (S§4.4) → lower results expected |
| **API — Bring your own API key** | Provider API key | Fully supported; the sanctioned standard path | Provider usage policies; output-use-for-training restrictions (§14.3) |
| **BYOS — Bring your own subscription** | Consumer subscription login | **Partially restricted**: per-provider allowlist only | Only where the provider officially permits third-party use; quota windows |

### 14.2 BYOS policy (the restricted mode)

**Principle:** we support subscription use only where the provider allows it; where a provider prohibits it, the option does not exist in the product. The disclaimer is an additional layer, never the main safeguard. Shipping a working login for a provider that prohibits it would make the project itself a ToS-circumvention tool, whatever the disclaimer says.

**Provider registry** (remote-updatable config, not hard-coded):

```
provider:        <name>
byos_status:     allowed | prohibited | unknown
source_url:      official ToS / announcement link
verified_on:     date
verified_by:     person
auth_flow:       official OAuth / documented login (required for "allowed")
training_on_outputs: allowed | forbidden | unknown
notes:           quota behavior, scope limits
```

- **allowed** → option shown. Current belief to verify at build time: OpenAI permits ChatGPT-subscription use in some third-party tools.
- **prohibited** → option hidden; user is pointed to API or OW mode. Current belief to verify at build time: Anthropic prohibits Claude-subscription use in third-party tools.
- **unknown** → off by default until verified.

**Hard rules (non-negotiable):**
1. Official, documented auth flows only. Never extract session/OAuth tokens, never impersonate an official client, never route around rate limits.
2. A provider with no official third-party auth flow is effectively **prohibited**.
3. Status flips remotely without a release (kill switch per provider).
4. Every allowed entry is re-verified before each release; record who and when.

**User consent at connect time:**
- Show: provider ToS link, plain-language summary of risks, our disclaimer.
- Disclaimer intent (wording to be reviewed by a lawyer before public launch): *"Provider Terms of Service apply. You are responsible for complying with the terms of the provider you connect. custom-harness is not responsible for account suspensions or bans by providers. Check your provider's terms before use."*
- Require explicit checkbox; log the accepted ToS/disclaimer version.

**Scope limit for BYOS:** subscription quotas are rate windows, incompatible with harness evolution at scale (thousands of rollouts). BYOS is intended for interactive use and the inner research loop (L4); the allocator (§6.4) refuses to schedule L6/L7 bulk evaluation on BYOS backends unless the user explicitly overrides, and it always respects quota windows with backoff.

### 14.3 Weight training (L8) and output-use restrictions

- Several major providers' terms restrict using outputs to train competing models.
- Every trajectory is tagged with the backend(s) that produced it. L8 may only train on trajectories whose every contributing backend has `training_on_outputs: allowed` — in practice mostly OW models with permissive licenses.
- Enforced in code at dataset build time, not left to the user.

### 14.4 Budgets — provider-neutral units

Each backend has a **cost adapter** that converts its native cost into common units the control plane (L1) and allocator (§6.4) use:

| Mode | Scarce resource | Adapter converts |
|---|---|---|
| OW | GPU-hours, VRAM | measured tokens/sec → GPU-hours |
| API | Money | provider price table → $ |
| BYOS | Quota windows | messages/requests per window → quota units (marginal $ ≈ 0, hard ceiling) |

**Two separate budgets in ML/DL** (both mandatory per campaign):
1. **Inference budget** — the agents thinking (any of the three modes).
2. **Experiment compute budget** — the training runs the agent launches (always the user's own or rented GPUs, independent of login mode). This can dominate total cost; the planner must budget both and refuse experiments that exceed (2).

### 14.5 Mixing modes and comparability

- A campaign may mix backends per role (e.g. API proposer, OW executor, mixed judges).
- **Scorecard runs (§10) pin exact models and modes**; results from different configurations are reported as different experiments, never pooled.
- The harness-science ledger (§8.10) records model + mode for every edit, so transfer priors are per configuration.
- An **all-open-weights configuration** stays runnable at all times (reproducibility, S§6.9), with lower scores expected.

---

## 15. Decision register (chosen 2026-10-06, optimizing for SOTA quality)

Criterion for every choice: what maximizes the chance of a **credible, reproducible, measured** SOTA result in autonomous self-improving research harnesses. Where "fastest" and "best" conflict, best wins; where "impressive" and "credible" conflict, credible wins (an unreproducible claim is not SOTA).

### A. Product & scope
| # | Decision | Why it serves SOTA |
|---|---|---|
| A1 | Users: researchers / power users; local or self-hosted. No hosted SaaS before SOTA milestone. | All effort goes into the harness, not billing/abuse infrastructure. |
| A2 | Form: CLI + local web dashboard (claim graph, lineage, costs, approvals). | Inspectability is part of credibility. |
| A3 | License: **Apache-2.0, fully open** (code, harness specs, traces of scorecard runs). | SOTA claims in this field are only believed when reproducible; open release is the strongest evidence and attracts contributors. Revisit only if a business model requires open-core. |
| A4 | Milestones: **M1 = Phases 0–3** (internal), **M2 "SOTA release" = Phases 0–5 + selected Phase 6 pieces** (allocator, proposer calibration). Phases 7–8 after M2. | The SOTA claim *is* the fusion result (Phase 5); shipping before it would be just another harness. Phase 8 (weights) is not needed to beat the surveyed harnesses. |
| A5 | Team / timeline: **open — must be provided by the user**; effort sizing per phase follows once known. | Cannot be estimated honestly without it. |

### B. Architecture & stack
| # | Decision | Why |
|---|---|---|
| B1 | Own thin runtime core (decided §13). | Harness = optimized object; clean typed diffs. |
| B2 | Harness = **typed spec (JSON Schema, versioned) + code modules referenced by ID**; code-module edits must pass module tests. | Structural edits drive gains (S§4.3); typed diffs enable edit taxonomy, calibration, harness-science ledger. |
| B3 | Sandbox: Docker with **gVisor by default** where supported; network off + allowlist. | Self-modifying agents running arbitrary code; exploit containment is part of P9. |
| B4 | Snapshots: content-addressed filesystem snapshots + env/memory log; **documented as state-exact, not process-exact**. | Reliable backtracking/revival without fragile CRIU/GPU checkpointing. |
| B5 | Storage: Postgres + content-addressed blob store (S3-compatible API, local MinIO by default). SQLite only for a lightweight dev mode. | Scorecard campaigns are large; multi-process evaluation from day one. |
| B6 | Model gateway: own thin adapters over official SDKs; exact token/cost/provenance logging; BYOS allowlist enforced here; secret redaction here. | ToS-sensitive and measurement-critical path; no third-party dependency in it. |
| B7 | Experiment tracking: **our Evidence store is the single source of truth**; MLflow export. | One provenance chain for audit (L2). |
| B8 | GPU orchestration: local GPUs + SSH remote hosts in M1; **Slurm + one cloud GPU backend before M2**. | Scorecard needs parallel evaluation; the allocator is useless without parallel capacity. |
| B9 | Platform: Linux target (WSL2 on Windows supported for dev). | GPU/container tooling. |
| B10 | Language: Python 3.12+, typed (mypy strict on core), PyTorch in sandboxes. | ML/DL ecosystem; typed core reduces evolver-induced breakage. |

### C. Models & access
| # | Decision | Why |
|---|---|---|
| C1 | Three modes (§14): OW, API, BYOS (allowlist). | Decided. |
| C2 | **Scorecard configuration uses the strongest available models via API for proposer/critic**, plus a pinned **all-OW configuration** reported alongside. | SOTA headline needs the best proposer (Meta-Harness/MILO used frontier proposers); the OW result proves reproducibility. |
| C3 | Executor: measured in Phase 0 across 1 frontier, 1 mid-tier, 2–3 OW sizes; report the inverted-U curve. | Turns S§4.4 into a measured asset (§8.13). |
| C4 | Judges: ≥3 model families, pairwise with position swap; **judge-agreement and correlation reported**. | SDE r > 0.8 means diversity must be measured, not assumed. |
| C5 | Both proposer types (external strong + same-model) run as islands from Phase 3. | Resolves the open S§7 conflict empirically. |
| C6 | BYOS registry verified for OpenAI, Anthropic, Google in Phase 0. | Decided policy §14.2. |
| C7 | L8 weight training: **post-M2**, OW-permissive trajectories only. | Not required for SOTA; ToS-safe. |

### D. Research domain (ML/DL)
| # | Decision | Why |
|---|---|---|
| D1 | Task types for M1: fixed-dataset improvement + efficiency (speed/memory) tasks; M2 adds reproduction and open method-proposal tasks. | Hardest verifiers first (T1/T2). |
| D2 | Experiment scale: single GPU, ≤4 h/run in M1; multi-GPU ≤24 h allowed in M2 campaigns. | Iteration speed vs. realism. |
| D3 | T2 standard: ≥3 seeds (5 for headline claims), bootstrap 95% CI excluding zero vs. identically re-run baseline, I2 spec conformance, independent replication agent (§8.12) for headline claims. | Stricter than any surveyed system → credibility edge. |
| D4 | Time-split rediscovery set: designed in Phase 0, built in Phase 4; arXiv cs.LG/cs.CL/stat.ML papers after every used model's cutoff, with code and numbers; contamination probes; novelty-weighted holdout; **two windows (T, T2)** to test generalization. | This is the M2 headline experiment. |

### E. Evaluation
| # | Decision | Why |
|---|---|---|
| E1 | Benchmarks (pinned versions, availability verified Phase 0): Terminal-Bench 2.x, SWE-bench Verified, GAIA, an MLE-bench-style suite, ScienceIDE envs, ResearchBench, SDE, own time-split set, Station/AutoOPT-style hard-verifier problems. | Covers scorecard axes A–H. |
| E2 | Re-run every baseline with public code under our budget accounting; others marked "as reported". | Matched-cost comparison is the honest SOTA test. |
| E3 | Splits 60/20/20 (evolve/gate/sealed); sealed accessible only via a human-run script at milestone ends; **pre-registered** metrics and hypotheses before each milestone. | Prevents the overfitting default (S§4.2) and p-hacking. |
| E4 | Re-verify the four extraordinary claims (S§0) in Phase 0. | Targets must be real. |
| E5 | Full ablation table (§10) required for M2. | Each module must earn its place. |

### F. Safety, governance, legal
| # | Decision | Why |
|---|---|---|
| F1 | M1 default: human approval for anything beyond in-sandbox experiments. M2: tier-gated autonomy (§5) enabled once red-team & quarantine targets are met. | Autonomy earned by measured robustness. |
| F2 | Network off by default; allowlist (arXiv, package indexes, dataset hosts). | Containment. |
| F3 | Secrets in OS keychain; never in traces/logs/Evidence; redacted at gateway. | Traces are released openly (A3). |
| F4 | Disclaimer + ToS reviewed by a lawyer before public release. | §14.2. |
| F5 | No external publication by the system, ever, without human action. | Governance. |

### G. Budget & operations
| # | Decision | Why |
|---|---|---|
| G1 | Two budgets per campaign (inference, experiment GPU); user-set hard caps; confirm at 80%. Numbers set after Phase 0 measurement. | §14.4. |
| G2 | No telemetry leaves the machine by default; opt-in only. | Trust. |
| G3 | Every scorecard number reported with cost. | Cost is a scorecard axis. |

### Still open
- **A5 team & timeline** (needed to size phases).
- Concrete model names and budget numbers → Phase 0 measurements.
- Benchmark availability/licensing → Phase 0 checks.
