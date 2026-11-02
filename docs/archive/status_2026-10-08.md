# STATUS — what is built, against GOAL.md

Fully rewritten 2026-10-08. GOAL.md says WHAT to build; **this file is the only place that records state.** The ✅/🟡/❌
markers inside GOAL.md §5 date from 2026-10-07 morning and are historical. Where they disagree with this file, this
file is current.

**Evidence labels.** Every statement here carries one of these:
- **verified**: the orchestrator saw the output (a gate log, a test run, a git command).
- **reported**: a builder agent ran it and said so in its final report, but nobody re-ran it independently.
- **inferred**: reasoned from code or reports, never run.

**State words.** Used in the tables below:
- **on main**: merged and full-gated.
- **integrated, not gated**: on branch `int16`, without a full gate.
- **on branch**: pushed but not merged.
- **partial**: some sub-items missing.
- **not started**.
- **needs owner**: blocked on a human decision or action.

---

## 1. Where the code is (verified with git, 2026-10-08)

| Ref | Commit | What it holds | Verification |
|---|---|---|---|
| `main` | **8cd691f** | All work up to m3-int f5c394c (see §3), plus GOAL §16 and two GOAL edits | **Full gate passed on f5c394c** (verified): ruff, mypy and Kernel `mypy --strict` OK; Kernel 157 passed at 100% line+branch coverage; full suite 767 passed, 3 skipped, 2 xfailed. Commits after f5c394c are docs only. |
| `int16` | **be6ff4f** (pushed today) | `main` plus 15 branches: m3-fix-bundled-data, s16-kstats (at 7b30dd1, *not* its latest 171dd7d), s16-mint, s16-flowsb, s16-flowk, s16-yield, s16-assure, or-tree, or-compute, or-artifact, or-deleg, ph2-audit, ph2-salts, ph2-proposer, ph2-wiring. Five merge conflicts, all additive, resolved by keeping both sides. | **Fast gate** (verified): lint, types, Kernel strict types, red-team registry and sealed ledger OK; Kernel 181 passed + **1 failed** (`test_kernel_is_small`, 3498 lines) at 100% coverage; **governance check FAILED** (no record for the Kernel core change). **The full suite was not run.** Agents found 6 more failures on this base (§6.1). |
| `int16-fix` | 4f16702 (local, in progress) | The fixer agent's integration fixes on top of `int16` | Not finished; no gate yet. |
| 9 branches `g-*` | formulator f089164 · tournament a8d84c9 · router f283725 · studio 49d00e1 · synth 3b5cb73 · runtime 2aca1f8 · scorecard e244fcf · weights 1e36213 | Each built on `int16` be6ff4f | Pushed (verified). Not merged. Each agent ran only its own targeted tests (reported). |
| `s16-kstats` | 171dd7d | Three ledger-kind allowlist commits after `int16` took 7b30dd1 (`run_kill`, `tier_policy`, `verifier_review`, `fork`) | Targeted test plus ruff only (reported) |

**Nothing after f5c394c is on `main` as code yet.** Every 2026-10-08 build (§16 and later) is on branches.

**Repository health** (verified):
- `git fsck` reported no errors and every branch ref is valid.
- One ref file (`s16-kstats`) was once found zeroed and was restored by an agent. The likely cause is that the repo
  lives inside **OneDrive**, which syncs files under `.git` while git writes them (inferred). Recommendation: move the
  repo out of OneDrive.
- `git stash` is shared by all worktrees, and it once swapped two agents' stashes. Both were recovered (reported).
  Don't use stash for fail-first checks.

---

## 2. Milestones (GOAL §11)

| Milestone | State | Notes |
|---|---|---|
| **M0 Hardening** | ✅ done, on main | Exit test passed (2026-10-06). **Risk:** `test_infra::test_docker_outage_mid_campaign_changes_no_decision` fails on `int16` (reported by runtime, reproduced on be6ff4f). The fixer is restoring it. |
| **M1 Kernel v1** | ✅ done, on main | 50-claim suite plus 100% coverage. Kernel 1.2 (the §16 statistics) is integrated, not gated. |
| **M2 Research loop v1** | built, **exit test not run** | It needs ≈300 Codex calls, or ≈30–40 with the yield and pooling upgrades (inferred). No live run has certified yet. Before any run: **today's suite is not trustworthy for certification**: BUNDLED-DATA affects 9 tasks and COUPLED-PRNG 12 (§6.2), both fixed only on branches. The suite must be rebuilt once those merge (needs owner). |
| **M3 Core/adapter split + T0 + approval** | **mostly built**; not exited | On main: adapter registry, ML/LP/simulator/Lean adapter code, approval node, physical path. The static "adapter edit rejected" check is on main too (B's evolve work). Missing: Lean is not installed, so T0 is unverified; the ML-specific K3–K7 logic has not moved behind the adapter interface ("domain-agnostic core"). |
| **M4 Evolution v1** | **pieces built on branches**; no real campaign | Agentic proposer, one-mechanism rule, smoke run, embargoed score, LOFO, branch/route and islands are all on branches. The family-separated critic is interface only (needs a second family). The exit test needs a real campaign. |
| **M5 Multi-agent domain expansion** | **not started** (only the delegation driver exists, on a branch) | Needs owner: the domain list, the prohibited list and the review standard (§13.6–7). |
| **M6 Memory, verifiers, branching** | **built on branches**, not exited | L5 store on main; memory wiring, exploit quarantine, verifier synthesis and router on branches. The judge panel refuses to run (one family). |
| **M7 Literature + time-split** | **pieces built**; headline experiment not run | Literature module on main; full-text search and the time-split builder on branches. The §6.4 experiment needs post-cutoff data and budget. |
| **M8 Scorecard** | **framework on a branch** | The registry is empty: benchmarks need downloads. It honestly reports invariant-12 coverage as **FAILED**, because there are no ablation switches. |
| **M9 Discovery & weights** | **pieces on branches** | L7 islands, verifier synthesis and the L8 export are built; no training (no GPU); no known-value result. |

---

## 3. GOAL item by item

### 3.1 Evidence Kernel (§4)

| Item | State | Evidence |
|---|---|---|
| Claim Language, prereg, K0–K9, certificates, retraction, prediction ledger, bundle, checklist | **on main** | verified (gate #2) |
| Evidence classes; class-aware check table; CERTIFIED-EXACT / -SIMULATED / -FORMAL | **on main** | verified (gate #2) |
| R1 fixes: K2 two-run fix, F1 binding, F2 replay seed, F3 K3 history, F4 twins, F5 adapter import closure, F6 NaN; dataset-role human approval; ledger anchors; bundle zip caps; v2 whole-chain bundles; K9 encodings | **on main** | verified (gate #2); fail-first reported |
| §16.4 VDS as an e-process (fixes **F2**: on the old code a q = 0.05 miss scored +0.910 bits) | integrated, not gated (kstats) | reported, fail-first reported |
| §16.4 per-target α-accounts; `verify` as of issue time with a STALE status (fixes **F3**); K3 zero-failure replays (a 50%-flaky program passed 27% of the time on the old code, now ≤ 2%) | integrated, not gated | reported. No CLI for α-accounts. |
| §16.5.8 closed reason codes; §16.6 registered-vs-executed check; §16.7 self-reported fields cap the tier (narrow: external/future roles only) | integrated, not gated | reported |
| Release ledger in K4; **F1 reproduced on the real Kernel** (embargo 0.0; unlogged continuous ≥ 0.30) | integrated, not gated | reported |
| Invariant 21: replayed evidence refused | integrated, not gated | reported; the `Provenance.replayed` path is code-read only |
| `mint` ledger kind plus a 0.02 minimum-effect floor on minted targets | integrated, not gated | reported; 0.02 is UNCALIBRATED |
| **Kernel size**: 3,498 lines against the < 3,000 test | **decision made**: budget raised to 4,000, test kept as a ratchet (owner, 2026-10-08) | Being applied by the fixer, not yet merged |
| ML-specific K3–K7 logic behind the adapter interface | **not started** | — |
| Kernel as a separate process (`kerneld`) | **partial** (flowk): working boundary for private reads and certify; ratchet test | reported. Still on the old path: `research/claims.py`, `cli.py`, the in-process Kernel itself; 3 new modules break the ratchet on `int16` (§6.1) |

### 3.2 L0 Substrate (§5)

| Item | State | Evidence |
|---|---|---|
| Typed spec graph, trace, checkpoint, resume, retry policy, approval node | **on main** | verified |
| MapNode fan-out with declared reducers; `fork(step, state_patch)` | **on branch** `g-runtime` | reported: 33 + 18 tests, kill-9 resume tests. Fan-out is sequential. Gaps: a map's input isn't treated as rendered into its body prompt; a map and its body count as two mechanisms |
| Sandbox: Docker, no network, caps dropped, non-root, read-only root; safe snapshots; host-escape refusal; output, disk and file caps; LocalSandbox test-only; protected mounts | **on main** | verified |
| **BUNDLED-DATA fix**: candidate image without bundled datasets | integrated, not gated (`m3-fix-bundled-data`) | reported: 12 tests failed first; image `custom-harness-sandbox:py312-candidate` built locally |
| Sandbox that controls the program's randomness | **partial** on `int16` (flowsb) | reported: two pinned runs are byte-identical. The clock is not pinned (needs a compiler/libfaketime image). Not a security boundary |
| CPU-time limits plus contention witness | integrated, not gated | reported. Dev runs only; held-out and K3 runs are not wired |
| Compute backends with isolation classes; runs from recorded revisions | integrated, not gated (or-compute) | reported. **To fix:** it allows agent code on a non-isolated backend with approval, which contradicts invariant 1 (the GOAL wording was clarified, the fix is queued). Nothing calls `compute/` yet |
| Model gateway (3 modes, provenance, same-family detection); request-fault classification | **on main** | verified |
| Gateway pacing; cost tables (placeholders only); registry checks | **on branch** `g-runtime` | reported; not wired in |
| Cassettes (record and replay; never evidence) | integrated, not gated (yield) | reported: a full run replays with 0 calls. flowk's replay uses a mirrored format that still needs swapping |
| gVisor; per-campaign cloud-GPU backend; SSH/remote backends | **not started** (an SSH stub refuses) | — |

### 3.3 L1 Control plane

| Item | State | Evidence |
|---|---|---|
| Per-run budgets persisted; outages uncharged; infra/policy split; transient retry | **on main** | verified |
| Exclusion-asymmetry rule wired into the evolution gate | **on main** | verified (gate #2) |
| Allocator that acts as each run finishes | integrated, not gated (or-compute) | reported: same decisions as batch racing on 300 seeded worlds. **Not wired** (the Evaluator isn't thread-safe) |
| Campaign-level persisted budgets | **on branch** `g-studio` | reported: hard-kill resume without double charge |
| Value-of-information allocation; LLM controllers may only downgrade | **not started** | — |

### 3.4 L2 Evidence store & reporting

| Item | State | Evidence |
|---|---|---|
| Append-only store; per-claim and per-run bundles; `chh reproduce` / `rerun`; checklist | **on main** | verified |
| **"Claims only from evidence"**: `add_claim` accepts an empty evidence list | **open finding** | reported by scorecard. Fix queued |
| Large-artifact manifest; capped bundle reads; anchored manifest | integrated, not gated (or-artifact) | reported. Claim bundles don't list artifacts yet (Kernel) |
| Figures rendered by script and re-rendered by the audit (I6) | integrated, not gated | reported. No loop node produces figures yet |
| Evidence export for the proposer | **on main** | verified |
| Postgres backend | **not started** | — |

### 3.5 Domain adapters (§5, §4.7)

| Adapter | State | Evidence |
|---|---|---|
| ML (data class) | **on main**, registered | verified. **Needs owner:** `chh approve-adapter ml` (the hash changed) |
| LP exact solver (T1) | **on main** | verified. Duplicate-JSON-key weakness suspected (inferred) |
| Simulator (RK4 ODE: linear, oscillator, SIR) | **on main** | verified. T2 within the simulator, UNCALIBRATED thresholds |
| Lean 4 (T0) | **code on main, never run against real Lean** | Lean, lake and Mathlib are not installed; the comparator has never been compiled. Mathlib commit is a placeholder, so approval is refused. **Needs owner** |
| Physical path (protocol → approval → returned data → Kernel) | **on main**, up to K1 | verified. No physical adapter; prohibited list not set (needs owner) |
| F4 transductive check as an ML-adapter case | built (flowsb), **not registered** in the adapter | reported |
| Life-science and engineering adapters (§15) | **not started** | needs owner (domain list) |

### 3.6 L3 Verifier ladder

| Item | State | Evidence |
|---|---|---|
| Fault-injection validation for adapters | **on main** | verified |
| Calibrated tolerances from noise floors; float screen → exact certificate | **on branch** `g-synth` | reported: a planted 0.01 drift is rejected. Not wired into claims |
| Verifier synthesis (closed spec vocabulary, human-owned suites, T2 cap until human review) | **on branch** `g-synth` | reported: 18 tests. **The example suites were written by the agent in the owner's name**; a human must approve real ones |
| Judge panel (T5): ≥ 3 families, both orders, validated against human labels | **on branch** `g-tournament`, **refuses to run** | reported. **Needs owner:** 3+ model families and 50+ human-labelled pairs |
| Exploit quarantine (perturbed evaluator) | integrated, not gated (wiring) | reported. No production skill writer yet |
| Red team: museum, mutation gate, clean-room reference model, findings registry | integrated, not gated (assure) | reported: museum 37/37 killed; mutation 93.4% baseline, 100% accounted for; 56 findings registered. ~1,000 generated histories, not the 1e5 GOAL asks for |

### 3.7 L4 Research loop

| Item | State | Evidence |
|---|---|---|
| research_v1 end to end (profile → hypotheses → tournament → prereg → experiments → critic → replication → certify → report → audit) | **on main** | verified (scripted); 2 live runs, both correctly REJECTED |
| research_v2: execution as the ranker, packed calls, headroom gate, empty-dir Codex | integrated, not gated (yield) | reported. It corrected an earlier arithmetic claim: a refinement can pass dev racing at ≥ 2 incumbent errors, not ≥ 3 |
| Certifiability preflight (deny only) | integrated, not gated | reported: P(certify) = 0.000 for Run A. One acceptance number missed (marginal K7 pass 0.050, not < 0.05) |
| Tie-aware selection; out-of-fold signatures and dedup | integrated, not gated | reported: Run B picks h1 (21 s) over h4 (2,258 s) |
| Experiment-tree lifecycle (immutable nodes, run contract, counted repairs, re-measured rounds, shape diagnostics) | integrated **and wired** into v1/v2 (or-tree + ph2-wiring) | reported. Owner decision taken: accept the twin-rival relaxation |
| Tree extras: snapshot backtracking, revivable dead-ends, curriculum | **on branch** `g-weights` | reported; not wired |
| Problem Formulator; hypothesis diversity by error correlation | **on branch** `g-formulator` | reported; libraries only, not wired. Only large anomalies are findable at our dev sizes |
| Tournament: both-order pairwise, Bradley-Terry with CIs, Improve-weakest, Combine, pinned incumbent | **on branch** `g-tournament` | reported: CI coverage 0.946–0.960 over 1,000 simulations |
| Literature module (6 sources, snapshots, retrieval provenance, fixed window, three-valued citation check, I3 audit) | **on main** | verified (gate #2) |
| Full-text retrieval (Europe PMC) | integrated, not gated (or-deleg) | reported. Fixtures only, never live. alphaXiv is **not allowed** by its ToS |
| Memory recall in prompts; negatives harvested | integrated, not gated (wiring) | reported |
| Socratic critic: answers must cite evidence | **not started** | — |
| Paged recall by an agent | **not started** | — |

### 3.8 L5 Memory & skills

| Item | State | Evidence |
|---|---|---|
| Store API, quarantine, tier floor, derivation graph, cascading revocation | **on main** | verified (gate #2). Planted exploit skill blocked; revocation cascades |
| Exploit quarantine with a real perturbed evaluator | integrated, not gated | reported |
| A production skill writer | **not started** | — |

### 3.9 L6 Harness evolution

| Item | State | Evidence |
|---|---|---|
| Typed edits, static checks, protected nodes, race, re-measure, gate, LOFO/LODO, novelty gate, decline, cost term, exclusion rule, finalize | **on main** | verified |
| R2-1, R2-2, R2-4 and R2-5 fixes; adapter-edit static check; tool-node taint | **on main** | verified (gate #2). **Risk:** the tool-node test fails on `int16` (§6.1) |
| Verdict embargo (score uses dev plus verdict bits); release emission; export embargo | integrated, not gated (flowk) | reported: the old score equalled the held-out mean |
| Agentic proposer (sandboxed, read-only export, typed edits only); one mechanism per candidate; smoke run | integrated, not gated (ph2-proposer) | reported. Isolation was tested with LocalSandbox only |
| Family-separated critic | interface only | needs owner (second family) |
| Branch and route from embargoed gate bits; temporal reveal | **on branch** `g-router` | reported; not in the CLI |
| Campaign-scope cassette replay | integrated, not gated | reported; toy campaign only |
| A real evolution campaign | **not run** | needs a budget plus a second family |

### 3.10 L7–L9

| Item | State | Evidence |
|---|---|---|
| L7 islands plus an orchestrator that detects stalls, edit-type quotas, versioned policy | **on branch** `g-router` | reported; not wired. Gating policy changes on measured gain: not built |
| L8 training-data export (two disjoint verifier sets, licence filter off by default, Goodhart alarm, variance selection) | **on branch** `g-weights` | reported. No training (no GPU). The Goodhart report must never reach the evolver: not enforced yet |
| L9 `chh explain`, research graph and limitations register, `chh queue`, `chh kill`, tier policy, no telemetry | **on branch** `g-studio` | reported |
| Delegation driver (§15) | integrated, not gated (or-deleg) | reported; not wired into `chh` |
| Dashboard / GUI | **not started** (deliberately; CLI first) | — |

### 3.11 §16 items not covered above

| Item | State |
|---|---|
| §16.1 COUPLED-PRNG fix: keyed streams per purpose | integrated, not gated (mint). Reported: before the fix, held-out labels of **12/12 synthetic tasks** were regenerated exactly from repo code |
| §16.2 freeze-then-mint, large test sets, retrospective flag | integrated, not gated. Parameters UNCALIBRATED |
| §16.3 `draw()` | integrated, not gated. Beacon source: needs owner |
| §16.5.4 label-swap replay against stored output (+ McNemar) | integrated, not gated (flowsb). **Not wired into K3** |
| §16.5.6 feature provenance plus human ruling CLI | integrated, not gated. Rulings sit in an interim store, not the ledger |
| §16.9 salts | integrated, not gated (ph2-salts). **Today's suite fails the salts' own detectability exam (d̂ > 0.5)**, so no campaign can pass until the owner chooses a fix. 0 calls spent |
| §16.10 Claim Audit (`chh audit`); label-dependence audit; emergence rule | integrated, not gated. Downloads not done (needs owner) |
| §16.10 bounded-null certificates; resweep | **not started** (Kernel; the honest corpus doesn't exist) |
| §16.7 human-held check and one outside attack per milestone | **not started**: only a human can do it. **Invariant 22 NOT MET** |

---

## 4. Hard invariants (GOAL §10)

| # | State |
|---|---|
| 1 agent code only in the sandbox | met on main (verified). Open: or-compute allows approved agent code on non-isolated backends (fix queued) |
| 2 no hidden truth agent-visible | **violated on main by BUNDLED-DATA and COUPLED-PRNG** (fixes integrated, not gated) |
| 3 Kernel only, no LLM in the Kernel | met (verified) |
| 4 persistent writes need evidence at the tier floor | met for memory (verified); weights export enforces T2 (reported) |
| 5 critic/replicator from another family | **NOT MET**: one model family. Recorded override |
| 6 paired stats, never-shown gate, sealed only via finalize | met on main (verified) |
| 7 fail closed | met as tested |
| 8 numbers resolve to artifacts | met for reports (verified); figures I6 integrated |
| 9 bundle plus cost per run | met (verified) |
| 10 loops labelled | met; every loop is MECHANICAL today |
| 11 BYOS only, licence-permitted training | met; the training flags default to off |
| 12 no README-only features | **NOT MET**: the scorecard reports that none of the 37 capabilities has an ablation |
| 13 adapters human-approved and fixed | met in code; **ml re-approval pending (owner)** |
| 14 no claim above its evidence class | met (verified on main) |
| 15 only a human approves | met in code. Note: the governance record for kstats' core change names the owner by delegation in chat |
| 16 LOFO gate | met on main |
| 17 verdict inputs fixed at registration | integrated, not gated |
| 18 releases ledgered, no continuous held-out to the evolver | integrated, not gated |
| 19 every attack test kills its attack | integrated (museum 37/37), not gated |
| 20 verifier validation sets sealed | **partial**: accounting only; agents can still read them |
| 21 replayed output never evidence | integrated, not gated |
| 22 a human-held check plus an outside attack | **NOT MET** (owner) |

---

## 5. Live runs (unchanged since 2026-10-07; details in liverun1.md)
- **Rings runs:** pipeline proven. The latest is `run_e305a0283a074fbb`: 0.958 vs 0.351, audit passed, 9 calls.
- **research_v1 run 1** (`run_cd99288e65ba4a4f`, wine): REJECTED, correctly, by K7 and a K9 false positive that was
  later fixed. 12 calls.
- **research_v1 run 2** (`run_f1bff8ab659f48e6`): REJECTED, correctly, by K3/K4/K6/K7 after a timeout. 13 calls,
  8,074 s, invariant 5 override. Its bundle reproduces.
- **No live run has ever been CERTIFIED.** The loop's research quality is **unmeasured**. Both live runs used suite
  tasks that are now known to be affected by BUNDLED-DATA (wine) or are synthetic (COUPLED-PRNG).

---

## 6. Open problems

### 6.1 Breaks on `int16` found after merging (fixer working on them)
1. `test_kerneld_boundary`: 2 tests. `audit/claim_audit.py`, `research/preflight.py` and `salts/__main__.py` read
   the store or private data directly.
2. `test_evolve_hardening::test_tool_node_cannot_put_training_rows_into_a_prompt` errors: the `baseline →
   hypothesize` edge is gone.
3. **`test_infra::test_docker_outage_mid_campaign_changes_no_decision`**: the M0 guarantee.
4. `test_research::test_independent_replication…` and `::test_same_family_replication…` are REJECTED at K6 instead
   of CERTIFIED-INTERNAL. Root cause unknown: either a new rule closed a real loophole, or it over-rejects honest
   replicas.
5. Governance check failing; Kernel size test failing (decision made).

All six test failures were reported by agents reproducing them on be6ff4f; the orchestrator has not re-run them.
The `int16` full suite has not run, so more may exist.

### 6.2 Integrity findings
| Finding | State |
|---|---|
| **BUNDLED-DATA**: candidate image ships labelled sklearn datasets (9 of 21 tasks) | fixed on `int16`, not gated; live suite still affected |
| **COUPLED-PRNG**: synthetic held-out labels regenerable from repo code (12 of 21 tasks) | fixed on `int16`, not gated; live suite still affected (needs rebuild) |
| F1 adaptive reuse; F2 VDS gaming; F3 stale vs forged | fixed on `int16` (Kernel 1.2), not gated |
| F4 transductive leak | check built, not registered in the adapter |
| KD1, KD2 | fixed on `int16` (reported) |
| **KD3**: a public input file carrying its true labels gets CERTIFIED-VALIDATED | **open**, fix queued |
| **KD4**: K2 ignores early reads of a related dataset | **open**, fix queued |
| **K9 quoted-literal gap**: labels written as `["c","a",…]` in code get CERTIFIED-VALIDATED | **open**, fix queued |
| Bundle reader keeps the last of duplicate member names | **open**, fix queued |
| Confirm retry reuses the failed attempt's directory | **open**, fix queued |
| `score_predictions` crashes on an entry without `outcome` | **open**, fix queued |
| Store accepts a claim with no evidence | **open**, fix queued |
| Leak scan can be used as a membership oracle (kerneld) | **open**, needs a rate limit |
| R2 evolve gaps for maps (input rendered into body prompt) | **open** (g-runtime) |
| The full red-team ledger (56 findings) | `redteam/findings.yaml` on `int16` |

### 6.3 Accepted limitations (still true)
- Deep paraphrase passes the copy detectors; renamed copies are now caught.
- Truth memorised in model weights.
- The Kernel certifies procedure, not meaning.
- Small public test sets are nearly uncertifiable.
- One model family.
- The sandbox's randomness control can be bypassed below Python.
- The ledger is tamper-evident, not authenticated.
- About α·N false certificates in aggregate.

---

## 7. Needs owner (only a human can do these)
1. `chh approve-adapter ml --approver <name>` (and `lp`, `sim` before use). Lean needs a real Mathlib commit.
2. A second model family, or three for the judge panel. Install a local model or provide an allowlisted API.
3. Lean 4 plus Mathlib in an image; an image with a compiler or libfaketime to pin the clock.
4. Budgets: M2 exit test; salts; evolution campaign; time-split run.
5. The beacon source (drand or dice), and who holds the offline secret (ideally a second person).
6. Rebuild the live suite with the fixed generators and the candidate image, archiving the old one. Delete or keep
   `suites/m2/_export/` (it holds labelled copies).
7. Values for every UNCALIBRATED parameter (they are spread across the config modules listed in each branch's docs).
8. Downloads: OpenML for audits, pinned benchmarks for the scorecard. Licences: provider training flags, a CORE
   licence, alphaXiv permission.
9. Domain list, prohibited list and review standard (§13.6–7); a lawyer before any public audit verdict.
10. The human-held check and one outside attack per milestone (invariant 22).
11. Consider moving the repo out of OneDrive.

**Decisions made by delegation (2026-10-08):**
- Kernel budget raised to 4,000 lines.
- Governance record names the owner by delegation.
- Twin-rival relaxation accepted.
- Sealed halves kept accounting-only, with the full gate unchanged.
- Agent code never runs on a non-isolated backend.

---

## 8. Next (in order)
1. The fixer finishes `int16-fix`: the 8 queued fixes, the §6.1 breaks and the §6.2 open items.
2. Merge the latest `s16-kstats` plus the 9 `g-*` branches. Resolve the engine conflicts (runtime and studio both
   edit `runtime/engine.py`).
3. **One full gate with nothing else running.** Merge to `main` only if it passes; then update this file.
4. Owner items 1, 6 and 2 unblock the first trustworthy live run.

## Environment
- **Code:** this folder, with git pointing at github.com/meta-lucky-builds/auto-sci-harness (private).
- **Runtime data and venv:** `C:\Users\mykids\custom-harness-data`, outside OneDrive.
- **Python:** `C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe`.
- **Gate:** `python scripts/check.py`, or `--fast` to skip the full suite. Run the full gate alone on the machine: it
  takes about 40 minutes, and an out-of-memory crash happened under parallel load.
- **Model access:** Codex CLI only, so one family.
- **Docker:** Docker Desktop 29.8.1 with images `custom-harness-sandbox:py312` and `…:py312-candidate`. No GPU.

## Stage-p0 (2026-10-31): P0 Court follow-ups, on branch `p0-build`

Reported by the stage-p0 agent; ran on this Windows PC only. Details: `META/agent_worklogs/stage-p0/`.
- Failures never spend truth: all three routes; `labels_read` comes from the ledger (docs/court_failures.md).
- Lab network rule: internal network per session, recording gateway (docs/lab.md section 11). Checked on real Docker Desktop.
- Seal at project start and the program contract (docs/sealed_projects.md).
- One display path, feedback split, rehearsal, readiness, Court health run, strawman check (docs/court_rehearsal.md).
- First-discovery plan (docs/first_discovery_plan.md).
- Open: `TRUTH_INDEPENDENT` needs the owner's review. Nothing has run with a real agent in a real Lab box.

## Stage-b (2026-11-01): the Experience API, on branch `stage-b`

Reported by the stage-b agent; ran on this Windows PC only. Details: `META/agent_worklogs/stage-b/`, docs/experience_api.md.
- `custom_harness/experience` and `chh exp`: whoami, waiting/approve, seal, start, explore, steer, freeze, rehearse, promote with readiness, mint, run, case, share, paper, record, resolve.
- One full session runs through the API with real kerneld, a fake docker and scripted programs. Nothing has run with a real agent in a real Lab box.
- Second paths closed: `chh explain`, `bundle`, `graph`, `predictions`, scorecard facts. A closed list of certificate readers is a test.
- A new Court version: `chh court health --docker` is owed on it.
- Not built, as GOAL 18.5 defers them: overnight mode, public challenge, integrations, visible self-improvement. HTTPS and WebSocket are left to the UI stage.
