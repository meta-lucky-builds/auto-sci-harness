# Scout: evolutionary-coding and ML-engineering agents → upgrades per model call

Labels: **verified** = read in a repo/README/abstract or in our own code/liverun today; **inferred** = reasoned from
verified facts; **guess** = unchecked. "Self-reported" = the authors ran and graded it themselves.

## 0. The fact that frames everything

Every system below spends **10²–10⁴ model calls per task**. AIDE drafts 5 then iterates for 24 h. ShinkaEvolve's
"sample-efficient" headline is about 150 samples. We spend **12–13 calls per run** (liverun1, verified). Five of
those (38–42 %) go to ranking and critiquing ideas before any code runs: hypothesize 1, tournament 3, critic 1.
Two more went to improve calls that could not succeed (Run A), and three to fixes (Run B).

At our budget, a search *policy* (islands, MCTS, MAP-Elites) has almost nothing to select over. What transfers is:
- **operator economics**: what each call produces, and what is decided without a call;
- **selection hygiene**: how a winner is picked from a handful of noisy candidates.

AIRA-dojo's own conclusion points the same way: operators and search policy only matter together, and the
validation→test gap is a first-order problem (verified, abstract and HTML).

## 1. Systems table

| System | Mechanism worth stealing | Evidence | Weakness for us |
|---|---|---|---|
| **AIDE** ([WecoAI/aideml](https://github.com/WecoAI/aideml), arXiv 2502.13138) | Draft = plan + code; the tree is ranked by executed val score, not by an LLM. `num_drafts 5, debug_prob 0.5, max_debug_depth 3` (verified, `config.yaml`, `agent.py`) | MLE-bench 17.1 ± 0.6 % (o1-preview), run by OpenAI = independent-ish | Greedy on max-val, so it overfits val (see AIRA) |
| **AIRA-dojo** ([facebookresearch/aira-dojo](https://github.com/facebookresearch/aira-dojo), arXiv 2507.02554) | Operator redesign beats search-policy changes. **Scoped memory**: siblings for draft/improve (diversity), ancestors for debug. Measured val→test gap | Self-reported. Selecting the final node by test instead of val would add **9.4–16.6 pts** of medal rate (verified, HTML) | Their "+10 % from top-3 hedging" uses test scores, i.e. an oracle |
| **ShinkaEvolve** ([SakanaAI/ShinkaEvolve](https://github.com/SakanaAI/ShinkaEvolve), arXiv 2509.19349, ICLR 2026) | Parent weight = sigmoid(λ·(score − median)/MAD) × 1/(1 + n_offspring) (verified, `parents.py`). Novelty rejection by code embedding plus LLM judge (threshold 0.99). Patch mix diff/full/cross 0.6/0.3/0.1. **`headless/codex` subscription-backed mutation models** (May 2026, verified README) | Self-reported: circle-packing SOTA in 150 samples | The novelty judge costs a call. Embeddings need an API we do not have |
| **OpenEvolve** ([algorithmicsuperintelligence/openevolve](https://github.com/algorithmicsuperintelligence/openevolve)) | Defaults: `diff_based_evolution: true`, `cascade_evaluation` with thresholds 0.5/0.75/0.9, 5 islands, MAP-Elites (verified, `default_config.yaml`). Note: "feature_based diversity not implemented" | Re-implementation; no independent benchmark (inferred) | Thresholds are on absolute score, which is meaningless for our paired claims |
| **AlphaEvolve** (arXiv 2506.13131) | Evaluation cascade; diff edits; Flash/Pro ensemble (throughput vs depth); evolved prompts | Process self-reported. Math constructions are checkable artifacts | Closed source; budget not comparable |
| **FunSearch** (Nature 2023) | Programs clustered by **signature** (per-input score vector); shorter programs preferred within a cluster; best-shot prompting (from memory, guess on details) | Cap-set constructions independently checkable | Needs ~10⁵–10⁶ samples (guess) |
| **MLEvolve** ([InternScience/MLEvolve](https://github.com/InternScience/MLEvolve), arXiv 2606.06473) | Progressive MCGS with cross-branch reference edges; retrospective memory; adaptive coding modes including SEARCH/REPLACE patches (verified README) | Leaderboard 61.33 ± 1.33 (Gemini-3-Pro, 12 h). README claims 65.3 ± 0.8 (Gemini-3.1) **self-reported**, not on the official table | Hundreds of nodes per task |
| **MARS / MARS+** (Google CAIR, arXiv 2602.02660) | **Budget-aware (cost-constrained) MCTS**; comparative reflective memory ("63 % of utilised lessons come from cross-branch transfer", self-reported) | Leaderboard 62.67 ± 0.77; code not released | Closed |
| **MLE-STAR** (arXiv 2506.15692) | Ablation-guided refinement of one code block; ensembling stage; web-retrieved models | 64 % on Lite, self-reported | Ablation deltas are noise at n ≈ 124 (inferred) |
| **R&D-Agent**, **ML-Master 2.0**, Famou-Agent 2.0, AIBuildAI | Researcher/Developer split, parallel traces, memory | Leaderboard: 35.1 / 56.4 / 64.4 / 63.1 % "All" (verified table) | Mostly closed code; 12–24 h on GPU |
| **GEPA** ([gepa-ai/gepa](https://github.com/gepa-ai/gepa), arXiv 2507.19457) | Reflective mutation from traces; **Pareto front over per-instance scores** as the parent pool | Self-reported: +6 % vs GRPO with up to 35× fewer rollouts | Built for prompt programs; fits L6, not inside a run |
| **SWE-agent ACI** (arXiv 2405.15793) | Lint-gated edits: a broken edit is rejected before it executes | Self-reported ablation | Already covered by systems.md M-d |
| **TextGrad** (Nature 2025, inferred) | LLM "gradients" as text critiques | Self-reported | One extra call per step, so poor value at our budget (inferred) |

**MLE-bench caveats** (verified, [README](https://github.com/openai/mle-bench) 2026-04-24):
- Submissions are **frozen** "while we develop an improved process for ensuring submissions are fair and comparable".
- "±" is the SEM over about 3 seeds, not a CI over the 75 competitions.
- Grading is run by the submitters.
- Two entries are segregated for **"Test-set feedback"**: Disarray at 77.8 % and LoongFlow at 62.7 %.
- Rows differ in LLM and hours, so no mechanism can be attributed from the table.
- Contamination: the competitions and their public notebooks predate the models' training cutoffs (inferred).

## 2. Ranked upgrades

Common ground: all five act **before the claim**, on the dev side. The Kernel, K2 pre-registration and the vault
are untouched (invariant 3). Every new candidate (batch members, the ensemble) enters the attempt record that K4's
selection accounting reads. Every upgrade ships with an off switch for ablation (invariant 12).

---

### U1. Execution is the ranker, and calls are packed (rank 1)

**Idea.**
- Drop the 3-call LLM tournament from the default spec and order the queue by hypothesis id.
- Implement calls carry **2–3 hypotheses each** and return `{"solutions":[{id, code, notes}]}`.
- Fix calls pack all currently failing candidates.
- Saved calls buy more drafts.

**First principles.** A pre-execution ranking is worth something only when it changes *which* ideas get run.
- With `max_attempts 8` ≥ 5 hypotheses, it changes only the order (verified, `modules.py`). Dev CV is the true
  ranker and costs 0 model calls.
- The ~16 k-token, 13–15 s overhead is per call. Packing 3 scripts pays it once for about 10 k output tokens.
  Run B ran ≈ 78 tok/s (5,558 tokens in 70.9 s), so a packed call takes about 130 s, under the 600 s timeout
  (inferred).
- Run B's tournament put h2 first. h4 won by **1 of 124 rows**, and h1, h2 and h3-fix tied, so the ranking showed
  no measurable value (verified).

**Expected budget** (inferred): hyp 1 + critic 1 + implement 2 + fix ≈ 2 = **6 calls** for the coverage that
costs 13 today. Or, at 13 calls, about 10 drafts instead of 5.

**GOAL changes.** L4.5: the tournament is used only when #hypotheses > remaining attempt budget (a VOI rule).
L4.7: add a packed-operator note.

**Kernel / invariants.** Unchanged, except:
- **Invariant 7**: each packed solution is validated separately, and a malformed member fails only itself (closed).
- K4's family and attempt counts include every member.
- Never pack the **replication** agent: its independence from the author's code is the point (L4.9).

**Build.**
- A `research.pack` module and a `solutions` array schema; `implement`/`fix` prompts become batch templates.
- `run_candidate` iterates over the members and gets a per-member attempt id.
- `tournament` gains `skip_if_budget_covers: true`.
- About 150 LoC plus tests: a malformed member, a duplicate id, a member over the size cap.

**Acceptance.** 21 tasks × 2 run seeds, paired by task and seed against `research_v1`:
- (a) Model calls to the first valid dev score drop ≥ 40 %.
- (b) Calls per certified-or-honest-negative outcome drop ≥ 30 %.
- (c) Rate non-inferiority: the paired difference has a 90 % CI lower bound ≥ −10 pts.
- (d) Diversity guard: the median pairwise OOF error correlation between packed siblings is no higher than between
  unpacked drafts + 0.1.

Cost: about 2 × 21 × 2 × ~10 ≈ 840 calls. **Ask before spending this quota** (working rule).

**Failure modes.**
- Siblings written in one context share bugs and ideas (correlated drafts). (d) measures this.
- Long outputs may be truncated.
- The tournament may matter on tasks where budget < hypotheses; the VOI rule keeps it there.

**New vs copied.** AIDE/AIRA rank by execution (copied). Packing operator requests to amortise per-call overhead
appears in none of the systems read (new, inferred).

---

### U2. Parsimonious, reproducible winner selection: the one-SE rule plus a runtime screen (rank 2)

**Idea.** Today a draft becomes `best` by **point estimate** (`_better`, verified `modules.py` L353). Replace this
with:
1. The equivalence set E = candidates whose paired dev interval against the top dev score includes 0. Use the same
   `paired_example_interval` as dev racing.
2. From E, pick the minimum by (projected held-out wall per seed, code size).
3. Reject any candidate whose projected held-out wall exceeds 0.5 × the per-run limit. The projection is the
   measured fold wall × (n_train_full / n_train_fold)^1.5, a deliberately pessimistic exponent (guess, to be
   calibrated).

**First principles.**
- Within E, dev cannot tell candidates apart. Choosing the max is selecting on noise: AIRA measured a **9–17 pt**
  val→test loss from exactly this, self-reported.
- Our Kernel adds a second cost to complex winners: K3 must re-execute them inside limits. Run B picked h4
  (2,259 s dev) over h1 (21 s) on 1/124 rows, then failed K3/K4 on a seed timeout (verified).
- E = {h4, h1, h2, h3-fix} there, so the rule picks h1 (inferred).

**GOAL changes.** L4.7 "code-scored seeded selection" becomes "equivalence-set + parsimony selection". The
runtime-risk input is shared with systems.md P1's preflight (built on that, not repeated).

**Kernel / invariants.**
- Strengthens K3/K4 reliability; no check changes.
- Dev racing is unchanged for improves.
- Within L4, the held-out confirmation is the re-measure. L6's winner re-measure is not touched.
- The rule is an evolvable module parameter, bounded: the evolver may change the ordering key, but not remove the
  runtime screen. Make that field protected in `evolve/edits.py`.

**Build.** `select_incumbent(attempts, labels, task)` in `modules.py` (~80 LoC). It records E and the reason in the
attempt record, and the report shows the reason.

**Acceptance.**
- Offline, 0 calls: on stored attempts from both live runs and cassette runs, the rule picks h1 or h2 in Run B.
- Live, paired against `research_v1` on 21 tasks: K3/K4 rejections caused by timeouts or missing seeds fall to 0
  from the observed baseline count.
- Certified rate: paired difference with a 90 % CI lower bound ≥ 0.
- Held-out effect of the claimed candidate: non-inferior, with a margin of 0.01 on the metric.

**Failure modes.**
- On a large dev set, E shrinks to one candidate and the rule is a no-op (fine).
- It can systematically prefer weak-but-fast models when the dev interval is wide. This costs effect size, which
  K7 then penalises: an honest trade.

**New vs copied.** The one-SE rule is copied from Breiman's CART. FunSearch's shortest-in-cluster is the same
instinct. Tying it to certificate reproducibility is new (inferred).

---

### U3. Behavioural archive from out-of-fold predictions: dedupe plus a zero-call vote ensemble (rank 3)

**Idea.** Each attempt already stores its out-of-fold dev predictions (`dev_preds_ref`, verified). Treat that vector
as a FunSearch-style **signature**:
- **Dedupe:** a candidate agreeing ≥ 99 % with an existing one gets no fix or improve calls. This is Shinka's
  novelty rejection, but with 0 calls and no embeddings.
- **Diversity metric:** pairwise OOF error correlation, which GOAL L4.4 already asks for. This makes it a measured
  number.
- **One pre-declared ensemble:** a hard majority vote of the top-3 behaviourally distinct valid candidates, with
  ties going to the U2 pick.
  - Its OOF predictions are the **vote of the stored OOF vectors**. Members run with the same folds and seed 0, so
    this is exact for deterministic members (inferred). That makes it **0 model calls and 0 sandbox runs**.
  - It enters as a candidate that must win **dev racing** (paired lower bound > 0), not a point comparison.
  - Its projected wall is the sum of its members' walls, so it is subject to U2's screen.
  - It is materialised as a harness-written wrapper that imports the member scripts, run in the sandbox.

**First principles.**
- Ensembling is the most reliable tabular gain, and every top MLE-bench agent has an ensembling stage (MLE-STAR,
  verified abstract).
- Our model wrote h4 by hand, a soft-vote ensemble that cost 2 fixes and 5,269 s (verified).
- Exactly one pre-declared rule keeps the selection multiplicity at +1, not 2^k.

**GOAL changes.** L4.4: diversity measured by OOF error correlation, now built. L4.5: a mechanical Combine operator
alongside SciDER's LLM Combine (code-review #15), which it builds on. L4.7: dedupe.

**Kernel / invariants.**
- Dev labels stay private. Scoring happens harness-side exactly as today (invariant 2).
- The wrapper is executed only in the sandbox (invariant 1).
- K3 re-executes the wrapper. A nondeterministic member makes the stored-vote estimate wrong, and K3 then catches
  the mismatch.

**Build.** `research/archive.py` (~200 LoC): signatures, dedupe, a fixed wrapper template, an exactness test (vote
of stored vectors == wrapper OOF on a fixture).

**Acceptance.**
- Offline first: compute the vote on Run B's stored OOF vectors of h1, h2 and h3-fix. Report whether it passes dev
  racing. This is a 0-cost check on real data.
- Live on 21 tasks: the ensemble passes dev racing on ≥ 20 % of tasks with ≥ 3 valid candidates (guess threshold).
- On those tasks, the realised held-out effect beats the single-model claim, paired 90 % CI > 0.
- Fix/improve calls on duplicates fall to 0.

**Failure modes.**
- Members are often correlated: same library family, same prompt. Then the vote ties the best member and loses
  dev racing, which is correct, just no gain.
- Summed runtime.
- Hard vote only covers classification. Regression would need a mean, a separate pre-declared rule.

**New vs copied.** Signatures come from FunSearch. Ensembling is standard. Exact zero-cost scoring from stored OOF
vectors under the same-folds contract is a cheap trick I have not seen named (inferred new).

---

### U4. Headroom gate on improve and fix calls (rank 4)

**Idea.** Before an improve call, compute with the **real** `paired_example_interval` whether even a *perfect*
candidate could pass dev racing against the incumbent. If not, skip the call and spend it on a new draft.

**Worked arithmetic** (inferred; a percentile bootstrap is assumed, and the acceptance test uses the real
function):
- The best possible paired-difference vector has e ones, where e is the number of incumbent errors.
- The one-sided 90 % lower bound is > 0 only if P(a resample has no ones) ≈ e^(−e) < 0.10. That needs **e ≥ 3**.
- **Run A:** incumbent at 1.0 on 24 rows (e = 0), so both improve calls were provably futile (verified that both
  were refused).
- **Run B:** h4 at 0.9839 (e = 2), so any improve call would have been futile.

For fixes, route by error class:
- An API/import error gets a fix call. Run B: 1/1 fixed.
- A **timeout** gets a fix only if the projected wall of the *fixed* scope fits. Otherwise abandon it. Run B's
  first fix of a timeout also failed (1/2).

**GOAL changes.** L4.7 repairs: "capped" becomes "capped and VOI-gated". This extends systems.md P1 from the claim
level to the operator level.

**Invariants.** None weakened. A skipped call is ledgered with its reason (invariant 8).

**Build.** `headroom(task, labels, incumbent_preds)` in `modules.py` plus an error classifier (regex classes)
feeding `run_candidate` routing. About 120 LoC.

**Acceptance.**
- Unit test: on Run A/B-shaped fixtures, the gate reports infeasible for e ∈ {0, 1, 2} and feasible for e ≥ 3 at
  n = 124. Confirm against the real function.
- Live: ≥ 15 % fewer model calls per run with no drop in the certified-or-honest-negative rate (paired, 90 %
  CI ≥ −5 pts).

**Failure modes.**
- It makes improve nearly unusable on small dev sets. That is the truth about our dev statistics, and it argues for
  U3, or for raising dev n.

**New vs copied.** New in this form (inferred). AIDE's `debug_prob` is a blind version of it.

---

### U5. Diff-patch repairs and per-node effort routing (rank 5)

**Idea.**
- Fix and improve return SEARCH/REPLACE blocks, applied mechanically. A failed match falls back to one full
  rewrite. The patcher fails closed.
- Each node gets an `effort` parameter: low for fix, high for draft. The Codex CLI accepts a reasoning-effort
  config (guess on the exact flag; Shinka's headless driver passes `effort=low…xhigh`, verified).
- Both become L6 edit surfaces instead of an online bandit: 13 calls cannot train a bandit.

**Reason.** Output tokens drive latency (inferred from Run B timings). A 300-token patch instead of a 3–4 k
rewrite makes a fix call take about 20 s instead of 50–60 s. A patch also cannot silently drop working code.
OpenEvolve defaults to diffs; Shinka samples diffs 60 % of the time (verified).

**GOAL changes.** L4.7 operator format; L6 edit surface list.

**Invariants.** 7: the patch parser fails closed. 1: unchanged.

**Build.** A `research/patch.py` applier (~100 LoC); a schema of `{"edits":[{search, replace}]}`; a
`gateway/codex_cli.py` effort passthrough.

**Acceptance.**
- Median fix-call wall time −40 %.
- Fix success rate (next run valid) non-inferior within −10 pts on 21 tasks.
- 0 silent patch misapplications on 50 synthetic fixtures.

**Failure modes.** At under 150 lines, full rewrites are cheap enough that the gain may be small. Low effort may
raise the repeat-fix rate.

**New vs copied.** Copied.

## 3. Tempting but wrong

1. **Islands, MAP-Elites or MCTS inside one run.** Even ≥ 150-sample "efficient" systems are 10× our budget, so
   with ≤ 10 nodes the policy choice is noise. Islands belong to L7, across runs.
2. **Picking the max-dev node, or "top-k hedging".** AIRA's extra 10 % from top-3 selection is scored on test, so it
   is an oracle. Any best-of-k chosen by held-out score is test-set feedback; MLE-bench segregates such entries.
3. **More CV folds or repeated CV to break ties.** This adds selection power, so more winner's curse and more
   wall time (Run B). Ties should go to parsimony (U2).
4. **LLM novelty judges or embedding novelty.** They cost a call or an API. The OOF signature is free and
   behavioural.
5. **Ablation-guided block refinement (MLE-STAR) at n ≈ 124.** Per-block deltas are within dev noise (U4's
   arithmetic).
6. **In-run meta-prompt evolution** (AlphaEvolve/Shinka meta-recommendations). This is prompt self-editing that
   bypasses the L6 gate and LOFO (invariant 16).
7. **Cross-task retrieved memory (MLEvolve/MARS-style) fed from gate or sealed tasks.** It leaks. Evolve-split
   only, through L5's evidence floor.
8. **Citing leaderboard deltas as evidence for a mechanism.** The rows confound LLM and hours, SEM is over 3 seeds,
   the grading is self-run, and the board is frozen.
9. **Multi-vendor model ensembles for generation.** We have one subscription family. A second family is better
   spent on the critic and replicator (invariant 5).
10. **Raising thinking effort everywhere.** AIRA doubled completion tokens for its gain, which for us roughly
    doubles wall time per call. Route effort per node (U5).
