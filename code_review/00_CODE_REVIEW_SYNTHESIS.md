# Code-review synthesis: what the existing systems' code actually does

Date: 2026-10-06. Built from 28 per-repo reviews in this folder (27 Sonnet agents; clone + read, nothing executed),
plus `_CODE_AVAILABILITY.md`. Six high-impact citations were re-checked by hand against the cloned code and all
matched (Station `executor_sandbox.py:709` pickle.load + no Lean; Meta-Harness `claude_wrapper.py:129`
`--dangerously-skip-permissions`; SIA `bypassPermissions`; AutoSaddler `MatchedValidStrictImprovement`;
EvoMaster `evolution/manager.py:132` unconditional overlay acceptance). Other citations are as reported by the agents.

**Important caveat:** "not in the repo" ≠ "the paper is false". Several groups may have unreleased internal code.
It does mean the claim cannot be reproduced or audited from public code.

## 1. Coverage

| Had official code — reviewed (27 repos / 28 reports) | No official framework code — skipped |
|---|---|
| AI Scientist v1, AI Scientist v2, The Station, EvoMaster, MM-Agent, MatAgent, AWorld-RL, Feynman, LangGraph, ScienceIDE (preview), Meta-Harness (+TB2 artifact), Self-Harness (partial), HarnessX, RRSI, AutoSaddler, SoL-Pi, Adaptive Auto-Harness, Continual Harness, SIA, Prime Agent, EvoScientist, SciDER, ScienceFlow, PARNESS, AutoOPT (skills + Lean only), Robin, SDE, ResearchBench (official-ness ~65%) | AHOIS, RHI, Auto-RecSys, Mixture of Branches, MILO (project page only), MDA (benchmark repos only), ScientistOne (generated artifacts only), ScholarEvolve (empty repo), Weng blog (no code) |

## 2. The headline finding: paper-vs-code gaps are the norm

In most repos, the mechanism the paper is famous for is missing, weaker, or different in code:

| System | Paper/summary claim | What the public code does |
|---|---|---|
| The Station | evaluator.py **+ Lean** verification | No Lean anywhere; exact Fraction/sympy checks in some evaluators; one LLM reviewer for "peer review" |
| HarnessX/AEGIS | digest→plan→evolve→critic, "seesaw" gate, routed variants, GRPO co-training | One meta-agent; "seesaw" absent; post-hoc best-so-far revert (accepts regressions < 3 passes); static per-category split; stock GRPO over fixed harness; no held-out |
| Meta-Harness | proposer + selection over full history | Proposer is `claude -p` with full workspace; **no accept/reject gate, no CI**; TB2 searched and evaluated on the same 89 tasks; discovered TB2 harness = essentially one change (env snapshot in first prompt) |
| Self-Harness | held-out non-regression | Strict float comparison on 2 repeats, no CI; held-out protection by prompt only; no license; single-step driver |
| AutoSaddler | ~10× fewer rollouts, generalization-aware selection | "10×" = traces at peak (147 vs 1,400); total executions ~2.8×; accept = strict mean on 3-case batch; dev never un-accepts |
| RRSI | regularized proposals + acceptance, leakage critic | Real and close to paper (regex precheck + Opus critic, cosine edit budget, cost-aware admission); but winner never re-measured, critic = same model as proposer |
| EvoMaster | persistent self-evolution | Single-task wrapper; overlay accepted unconditionally; nothing persists across runs; headline benchmarks not reproducible |
| SIA | agent chooses harness vs weights; co-evolution | Lever is a CLI flag; no co-evolution loop; same evaluator = reward = feedback = report; test labels readable by host agent |
| Continual Harness | refiner + DAgger/PRM co-learning | Refiner edits live stores with no eval/gate/rollback; **no DAgger/PRM code**; several bugs |
| Adaptive Auto-Harness | temporal reveal; router vs oracle 50.2 vs 87.7 | Reveal gate fails open (undated = revealed; scores leak via git messages); router is zero-shot prompt; **no oracle code** |
| EvoScientist | idea + experiment memory, Elo | Not in repo; generic keyword-ranked memory product; Elo is one prompt sentence |
| SciDER | data profiling before ideation; K-way ranking | Profiling = describe() on first 10k rows; **ideation never sees the data report**; one unshuffled ranking call; grader source inlined in MLE-Bench prompts |
| ScienceFlow | exact recoverable states, delta snapshots | File-level CAS + manifest (real, good); "delta" = whole-file dedup; agent memory rebuilt from summary, not restored |
| PARNESS | YAML DAG kernel, FTS index, self-improvement | DAG kernel real but several DSL fields dead; FTS table never populated; **no self-improvement**; no tests dir |
| Robin | BTL tournament over mechanisms, positional-bias handling | Tournament ranks assays; no order swapping; one model; agents are closed remote APIs |
| AutoOPT | BnB-PEP certificates + Lean | No BnB-PEP code; Lean projects (no root license); human gates are prose |
| Prime Agent | L0–L3 memory, 85h runs, RCON lesson | No experiment code; no skill admission check — RCON-as-skill failure reproduces by construction |
| AWorld-RL | RODS reward-variance synthesis | RODS dir has README + images only |
| AI Scientist v1/v2 | sandboxed experiments, tree search | No sandbox (host `subprocess` / `exec`); v2 "tree search" = greedy best-first with random debug retries; winner picked before seeds run |
| Feynman | 40+ sources, parallel verifier | 8 sources; verifier is a prompt, runs after drafting, skipped in direct mode |
| MM-Agent | "MLE-Solver" | Undefined in code; generate-run-debug loop; success = no traceback substring; MM-Bench judge never sees the problem background |
| MatAgent | multi-agent materials discovery | Transcripts + generated scripts; reports contain **numbers never computed** (fabrication) |

**Consequence for our goal:** "beat every system in 00_SYNTHESIS" is partly a comparison against paper claims that
their own released code doesn't support. Our scorecard (PLAN §10) must compare against **re-run public code** where
possible and mark everything else "as reported (not reproducible from public code)".

## 3. Where custom-harness is already ahead (consistent across reviews)

Every reviewer, independently, listed some of these:
- **Real isolation**: Docker, no network, non-root, read-only rootfs, caps dropped. Most systems run LLM code on the host
  (AI Scientist v1/v2, Station, SciDER, MM-Agent, MatAgent, Prime, PARNESS, EvoScientist); several pass API keys into it.
- **Statistics**: paired bootstrap CIs, racing, a never-shown gate split, sealed split. Almost all others use single
  noisy means or strict float comparisons; none re-measure the winner.
- **Private-data guard + hidden labels**: others leak via workspace files (Meta-Harness, SIA), prompts (SciDER grader),
  git messages (Adaptive), or permissive tools.
- **Claims only from evidence + recomputation audit**: MatAgent and others publish uncomputed numbers.
- **Typed, statically-checked, protected edits; audit-only terminal**: others edit free code with no protection.
- **Budget ledger + costed failed attempts**: almost nobody tracks cost in code.
- **Lineage with rejects + predicted-vs-actual calibration**: only RRSI has something comparable (scoreboard).

## 4. Where custom-harness is behind — ranked adoption backlog

All "reimplement", not copy (license notes in §6). Effort S/M/L.

### P0 — correctness/robustness fixes in our own code (found by reviews)
| # | Fix | Source | Our file |
|---|---|---|---|
| 1 | Separate **infra failure** from policy failure; exclude infra-failed units from paired diffs (don't score 0) | AutoSaddler, ScienceIDE | `evolve/evaluate.py` score_state, `selection.py` |
| 2 | **Winner's curse**: re-measure the race winner on fresh seeds/units before the gate | RRSI, AI-Sci v2, AutoSaddler pitfalls | `evolve/loop.py` |
| 3 | Snapshot restore: preflight all blobs, archive live workspace, rollback on failure; keep exec bits/symlinks; fingerprint to skip re-hashing | ScienceFlow | `sandbox/snapshot.py` |
| 4 | `Engine.resume(run_id)`; transient-only retry policy that charges budget | LangGraph | `runtime/engine.py` |
| 5 | Gate denominator/unit alignment check | Self-Harness | `evolve/selection.py` |
| 6 | Budget persisted across crash/fork; wall-clock unit; uncharged provider outages | ScienceFlow | `budget.py` |

### P1 — the biggest capability gaps (self-improvement core)
| # | Adoption | Source | Layer / file | Effort |
|---|---|---|---|---|
| 7 | **Agentic proposer**: read-only export of the evidence store (all candidates, full traces incl. successes, scores, ledger) + tool-using sandboxed agent; output still typed edits | Meta-Harness | L6 `evolve/propose.py` | M |
| 8 | **Structured diagnosis session**: first-divergence causal procedure; LLM causal signature (cause, criticality, mechanism) with verifier evidence; claimed diff checked vs real | AutoSaddler, Self-Harness | L6 | M |
| 9 | **Leakage/degeneracy critic** from a *different model family* + deterministic denylist of evolve/gate task ids, before racing; reasons fed back for repair | RRSI | L6 | S–M |
| 10 | Ledger upgrades: per-task flipped/regressed attribution + `regressed_unpredicted`; novelty gate blocking re-proposal of reverted signatures; tried/yield/prune/stall + annealed op budget | HarnessX, RRSI | L6 ledger | S–M |
| 11 | Decline/no-op path; one mechanism per candidate; smoke-run pre-gate (1 task through real loop) | Self-Harness, Meta-Harness, HarnessX | L6 | S |
| 12 | Cost term in acceptance (relative cost bound) | RRSI, SoL-Pi | `selection.py` gate | S |
| 13 | Proposer-written per-generation reports; one-shot `finalize` that freezes selection | Meta-Harness | L6 | S |

### P1 — research loop quality
| # | Adoption | Source | File | Effort |
|---|---|---|---|---|
| 14 | Draft/debug/**improve** node tree with code-scored, seeded selection; stop when verified; tune→creative→ablation curriculum; dev racing to avoid dev overfitting | AI Scientist v2 | `research/` + spec | M |
| 15 | Improve-weakest-dimension and Combine operators; pin incumbent | SciDER | tournament | S |
| 16 | Richer profiling (outliers, cardinality, key graph, column roles) | SciDER (paper, not code) | `research/data.py` | S |
| 17 | Context: archive big tool outputs to blobs + handle + paged recall (replace head/tail truncation); verified-quote log reducer with raw fallback | SoL-Pi | `runtime/template.py`, L0 | S–M |

### P2 — verifiers, memory, literature, branching
| # | Adoption | Source | Layer |
|---|---|---|---|
| 18 | Verifier fault-injection evidence (each verifier must reject planted faults) + calibrated tolerances from measured noise floors; floor-normalized reward; oracle/nop/floor gates before admitting a task; canaries + leak audit | ScienceIDE | L3 |
| 19 | Float-screen then exact-certificate gate | Station | L3 |
| 20 | T0 split: Lean build+hygiene vs Lean+comparator against human-approved Challenge spec; axiom audit; statement-fidelity gate ("blocked, do not weaken") | AutoOPT | L3 / Phase 7 |
| 21 | Judge panel: ensemble + meta-review (with v1's bugs fixed), both orderings, BT CIs, rubric decomposition; validate judge vs human labels | AI Sci v1, Robin, MM-Agent | L3 T5 |
| 22 | **Single memory/skill store API**: requires evidence + tier floor, quarantine before readable, derivation graph for revocation, render as quoted data; outcome-driven negative memory with evidence ids; relation edges; read audit | Prime Agent (anti-pattern), EvoScientist, Robin | L5 |
| 23 | Literature: per-host pacing, S2/OpenAlex fallback, query splitting; citation check = ID resolution (3 outcomes) + title/year/author match + quote-span support → audit I3; dated snapshots + cache | Feynman, AI Sci v1 | L4, L2 |
| 24 | Branching: select/materialize adaptation seam; fail-closed temporal reveal (labels sealed outside evolver mounts); routing descriptors generated from gate evidence; capacity/routability branching trigger | Adaptive Auto-Harness | L6.6 |
| 25 | Engine: MapNode fan-out with per-branch persisted outputs; explicit approval/PAUSED node | LangGraph | L0, L9 |
| 26 | Controller pattern: LLM may only downgrade; kills need code-checked evidence | ScienceFlow | L1 |

### P3 — weights (post-M2)
Reward-variance task selection (AWorld idea, no code), stateful T1 verifier pattern (AWorld EnvTuning), SFT trajectory
format filtered by outcome (SciDER format, fixed), RL zero-variance/reward-centering checklist (SIA).

## 5. Implications for benchmarks and the time-split set
- **Neither SDE nor ResearchBench can seed the time-split rediscovery set** (no dates; retrospective gold). Build it
  from dated arXiv papers ourselves.
- ResearchBench: retrieval/ranking = cheap deterministic axis-E probes; data CC BY-NC → eval-only, never in repo.
- SDE: question sets = no-regression sentinel only (measure the model more than the harness); 4 projects have
  computational oracles (heavy deps).
- MM-Bench problems usable as a task set with our own judge; MatAgent's 6 datasets as a cheap materials pack
  (licenses unchecked).
- ScienceIDE tasks not directly reusable (Fortran/C++/Julia, no license on metadata) → borrow the task *format* for an
  "ML-pipeline repair" family (inject defect, oracle, floor, mutation).
- Scorecard rule: **no grader visibility** to the agent (SciDER inlined grader source).

## 6. Licensing summary (for copying code)
- Copy OK with attribution (MIT/Apache-2.0): Station, EvoMaster, AWorld-RL, Feynman, LangGraph, Meta-Harness (main),
  HarnessX, RRSI, AutoSaddler, SoL-Pi, Adaptive Auto-Harness, Continual Harness, SIA, Prime Agent, EvoScientist,
  SciDER, ScienceFlow, Robin, MatAgent, SDE (template license unfilled — treat cautiously).
- Ideas only: AI Scientist v1/v2 (custom RAIL-derived license with downstream restrictions), MM-Agent (GPL-3.0),
  Self-Harness (no license), ScienceIDE own code (no license), Meta-Harness TB2 artifact (no license), PARNESS (no
  LICENSE file; vendored AGPL), AutoOPT Lean projects (no root license).
- Policy: we reimplement everything anyway (cleaner fit with our typed core); attribution in docs for ideas taken.
