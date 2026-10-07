# Emergence under rigor: upgrade proposals for GOAL.md

Lens: how can custom-harness produce capabilities nobody scripted, while every persisted gain still passes the Kernel?
Labels: **verified** (I read it in the repo or a source), **inferred** (reasoned from what I read), **guess**.
Read: GOAL.md, STATUS.md, code_review/openresearch.md, code_review/00_CODE_REVIEW_SYNTHESIS.md, survey 00_SYNTHESIS.md,
and the headers of `evolve/multiplicity.py`, `memory/store.py`, `memory/exploit.py`, `kernel/lp.py`,
`kernel/predictions.py` and `research/suite.py`.

## First principles (the argument behind every proposal)

1. **Where novelty comes from.** A search finds what is reachable from its current state under its operators. Open-endedness
   needs three things: (a) a **population of stepping stones** that need not be best today (Go-Explore, MAP-Elites, DGM);
   (b) a **stream of new problems** that keeps the frontier moving (POET, OMNI, PAIRED); (c) **cheap exact feedback**
   (FunSearch, AlphaEvolve). This system already has (c) for ML via the Kernel, and has an exact T1 checker on a branch
   (`kernel/lp.py`). It has neither (a) nor (b). Today the L6 loop is greedy hill-climbing on one incumbent, with a
   novelty gate that only blocks re-proposals (verified, GOAL L6.6).
2. **Where it goes wrong.** Every open-ended pressure is a Goodhart target. The structural defence is one rule:
   **novelty pressures may steer *where budget goes*; only the Kernel and the human-curated gates decide *what
   persists*.** Interestingness, archives and self-made tasks live upstream of acceptance and never replace it.
   Proposals 1 to 5 all follow this rule.
3. **"Emergence" must be measurable or it is hype.** Operational definition (proposed): a capability has emerged
   if, on a human-curated task family that no objective, generator or proposer export ever targeted, the evolved
   harness's certified-claim rate rises over the seed's with a paired CI lower bound > 0, while a matched-budget null
   (random typed edits) does not show the same rise.

---

## 1. Verifiable self-generated task families, admitted only to the evolve split (rank 1)

**Idea.** A *generator agent* writes parametrised task **generators**: programs that emit (data, hidden labels,
metric) instances. Each instance's ground truth must be one of two kinds. (i) **By construction**: the labels are the
generating process, and an adapter-registered reference model must reach the planted effect, which proves the task
is learnable. (ii) **By an approved checker**: for example, LP instances whose optimum is certified by the fixed `lp`
adapter. A mechanical **learnability filter** keeps a generated family only if the seed harness's dev solve rate is
in [0.1, 0.9] (OMNI's "learnable" criterion, replacing its LLM judge with a measured number). A family is also
rejected if its error pattern correlates with an existing family's at r > 0.8, since it would add nothing new
(S§8.7 diversity measured by error correlation). Generated families feed **only the evolve split**. Gate, sealed
and LOFO families stay human-curated.
Why it helps (inferred): today the evolve split has about 15 tasks across a few families (`suite.py`, 21 tasks in
all). The proposer overfits to so few families (S§4.2). More, diverse and checkable training problems is the POET
lesson. Because acceptance happens on human families, the system cannot get credit by making easy tasks for itself.

**GOAL changes / interactions.**
- New invariant 17 (see the diff below).
- §5 L4.2/L6.1: generated tasks run through the same sandbox and vault, with hidden labels stored outside every
  agent mount (invariant 2).
- Leakage risk: the generator agent sees its own labels. So its traces are excluded from the proposer export, and
  generator source is treated like hidden data.
- Goodhart risk: the generator learns to make tasks that flatter the current harness. This is blocked because the
  acceptance signal never comes from generated tasks.
- Multiplicity: unchanged, since generated tasks are never certification targets for the campaign's gate.
  `multiplicity.py` still counts evolve-split claims.
- Invariants 1–16 are kept.

**Build plan (around 600 LoC, M4/M6).**
- `research/generate.py`: a `TaskGenerator` protocol. The generator runs in the sandbox, writes instances into the
  vault layout and records the ground-truth kind.
- `research/curate.py`: the learnability filter, error-correlation dedup, and a family descriptor.
- `suite.py`: `assign_splits` refuses any generated family outside evolve.
- `check_split_isolation` is extended to generated content: a generated instance hash-matching a gate example is
  refused.
- Tests: written to fail first (generated family in gate refused; label in export refused; filter rejects a
  trivial family).

**Acceptance test.** Three arms, each with 3 campaign seeds at matched proposer-call budgets:
- A: human evolve families only.
- B: A plus generated families.
- B0: B with labels shuffled inside the generated families (noise control).

All of the following must hold:
- (a) On the two human families never targeted (spirals plus one new family), the paired difference in certified-claim
  rate, B minus A, has a 95% lower bound > 0. This difference is pre-registered as a Kernel claim with δ = 3 pp.
- (b) B0 minus A has a CI that includes 0. Otherwise the gain comes from evolve volume, not task content.
- (c) Canary and leak audit: 0 hits.
- (d) Planted-exploit catch rate is unchanged at 100%.

**Cost / failure modes.**
- Guess: about 20 model calls per generated family, and 300–600 per campaign. The 9 campaigns come to roughly
  3–5k calls. That needs a user budget decision.
- Compute is sklearn on CPU, which fits the laptop.
- Honest risk: power. With about 6–10 gate tasks per held-out family, a 3 pp effect may be undetectable. The
  likely first outcome is "inconclusive", and it must be reported as such.
- Generated families may drift toward trivial variants of the existing ones. The correlation filter limits this but
  cannot prevent it entirely.

**Prior art.**
- POET (Wang et al. 2019, arXiv 1901.01753, inferred id).
- PAIRED regret-based environment design (Dennis et al. 2020, arXiv 2012.02096, inferred id).
- OMNI / OMNI-EPIC (Zhang et al. 2023/2024, arXiv 2306.01711 / 2405.15568, inferred ids).
- Absolute Zero, self-proposed tasks checked by a code executor (Zhao et al. 2025, arXiv 2505.03335, inferred id).
- Prioritized Level Replay (Jiang et al. 2021, unverified).
- **New here (inferred):** generated tasks are structurally barred from acceptance; ground truth comes from fixed
  adapters; and a label-shuffled null arm separates "more data" from "better data".

## 2. Certified stepping-stone archive with evidential (not genealogical) revocation (rank 2)

**Idea.** Replace the single-incumbent L6 parent with a **MAP-Elites archive** of harness variants.
- **Cells** come from mechanical descriptors computed **only from evolve-split evidence**: the per-evolve-family
  score profile (argmax family), a cost bucket, and the dominant edit type.
- **Parent selection** samples from the archive, weighted toward cells with recent improvement (DGM-style).
- **Archive status confers no acceptance.** A stone is search state: it is never routed, never deployed, and never
  rendered into L4 or L5 memory. Promotion to the product still requires the full race → re-measure → gate → LOFO
  path.
- First-principles reason (verified as reported in the paper): DGM's ablation without its open-ended archive did
  worse than with it, which supports the claim that non-greedy stepping stones matter. DGM: SWE-bench 20% → 50%
  (arXiv 2505.22954, ICLR 2026).
- **Key subtlety.** Lineage is not evidence. If a stone's supporting certificate is retracted, its archive
  descendants are evicted, because they were reached through a now-false niche claim. An *accepted* harness
  descended from it stays accepted unless its own acceptance certificate cites the retracted evidence. Revoking by
  genealogy would wrongly kill independently validated harnesses. Revoking by nothing would leave stale stones.

**GOAL changes / interactions.**
- Invariant 4 gets an explicit clarification: archive entries are search state, labelled MECHANICAL, so they are
  not "persistent writes" in the invariant's sense. This is still safe because nothing in the archive can reach a
  certificate, the router or memory without the existing gate.
- §5 L7 replaces "islands" with "islands = archive partitions".
- L6.6: the novelty gate now also measures descriptor novelty.
- Risks:
  - Descriptors computed from gate data would leak. They are built from evolve-only data, and `export.py`'s
    policy already enforces that boundary (verified).
  - Archive diversity can collapse into cells that differ only in cost. Report the QD-score and cell occupancy per
    generation.

**Build plan (around 400 LoC, M4 → M9).**
- `evolve/archive.py`: cells, insert-if-better on evolve paired means, parent sampler, and an eviction cascade that
  reuses `memory/store.py`'s derivation-graph walk.
- `loop.py`: parent is `archive.sample()` instead of the incumbent.
- Ledger: per-generation QD-score, occupancy, and the lever and cell chosen.

**Acceptance test.**
- Matched budget, 3 seeds per arm, greedy loop versus archive loop. The archive arm must:
  - (a) reach at least as many gate-plus-LOFO-accepted edits, and
  - (b) on the held-out family, show a paired difference in final-harness gate score versus greedy with a 95% lower
    bound ≥ −1 pp (non-inferiority) and a point estimate > 0.
- (c) Revocation test: retract a planted certificate supporting a depth-1 stone. 100% of its archive descendants are
  evicted, and 0 independently gate-accepted harnesses are revoked.
- (d) A planted exploit edit enters the archive, but 0 such edits are promoted.

**Cost / failure modes.**
- Compute cost is the same as the current loop, just reallocated (inferred).
- Risk: with about 15 evolve tasks, descriptors are noisy, so cells reshuffle generation to generation. Mitigation:
  insert only when the paired evolve CI excludes 0.
- Honest: on a laptop budget (tens of generations), the archive may not beat greedy. DGM ran about 80 iterations
  on API budgets (guess from memory).

**Prior art.**
- MAP-Elites (Mouret and Clune 2015, arXiv 1504.04909).
- Novelty search (Lehman and Stanley 2011).
- Go-Explore (Ecoffet et al., Nature 2021).
- DGM (verified above).
- ADAS Meta Agent Search archive (Hu et al. 2024, arXiv 2408.08435, inferred id).
- **New (inferred):** an archive that is explicitly non-certifying, plus the evidential-versus-genealogical
  revocation rule.

## 3. Mechanical interestingness used only for allocation (rank 3)

**Idea.** The L1/L7 allocator spends budget where **learning progress** is highest. Define interestingness of a
direction (task family, archive cell, or problem) as:
> I = (Δ VDS over the last k resolutions + Δ proper score on its predictions) / cost.

This is Schmidhuber's compression progress and Oudeyer's learning progress, made mechanical with the existing
prediction ledger (`predictions.py` already computes log-score information gain; verified).
- Add a coverage term: the count of claim-graph scope regions (datasets × metric × evidence class) with no
  certified claim yet.
- **Never used in acceptance, certification or VDS.**

**Interactions.**
- It changes §5 L1 (allocator) and L7 only. No invariant changes.
- Noisy-TV risk: directions whose outcomes are random look perpetually "surprising". Mitigation: progress counts
  only *resolved* predictions on claims that passed K3 reproduction, and uses the change in score, not raw surprise.
  A purely random direction has an expected change of 0.
- Goodhart risk: proposers can't move I without moving VDS, which the Kernel guards (K7).

**Build.** `control/interest.py`, about 150 LoC. Pure functions of the ledger, unit-tested against a synthetic
ledger.

**Acceptance.**
- Simulation first (no model calls): 10 synthetic directions with known learning curves, including 3 pure-noise
  ones. Over 200 allocation steps, the allocator must give the noise directions ≤ 10% of budget, versus 30% for
  uniform allocation. It must also reach ≥ 1.2× uniform's cumulative simulated VDS, CI over 100 replicate
  simulations.
- Live: report only, no claim until M7 resolves predictions.

**Cost.** Negligible.

**Failure modes.** Ledger resolution is slow (weeks), so I is stale. Honest: this matters mainly after M7.

**Prior art.**
- Schmidhuber, compression progress (arXiv 0812.4360).
- Oudeyer et al. 2007, intrinsic motivation and learning progress.
- Burda et al. 2018, RND and the noisy-TV problem.
- Teacher-student curriculum learning (Matiisen et al. 2017).

All four are well known; ids are from memory. New: tying interestingness to a proper-scored, Kernel-guarded
prediction ledger.

## 4. Exact-evaluator discovery island (FunSearch-lite on the T1 LP adapter) (rank 4)

**Idea.** An L7 island runs evolutionary program search where each program emits a **construction plus
certificate**, and the fixed `lp` checker decides (exact rationals; verified in `kernel/lp.py`).
- Targets: known-value instances from a dated snapshot. Examples are LP relaxations of combinatorial bounds, and
  Delsarte-type LP bounds for small codes (guess as to how many are in reach).
- Claims are COMPUTATIONAL, CERTIFIED-EXACT at T1, with K7 n/a (verified, `checks.py`).
- This is the only place where "discovery" is checkable with zero statistical risk on a laptop.

**Interactions.**
- New problem classes need new **human-approved** adapters (invariant 13). The island cannot widen its own domain.
- The float-screen → exact gate (L3) prunes candidates cheaply.

**Build.** `discovery/island.py`, about 350 LoC: a program database with islands, sandboxed execution, and a
problem registry. It needs `m3-b1-compute` merged first.

**Acceptance.**
- On 10 registered LP-bound instances with known optima, the island rediscovers ≥ 6 exact certified optima within
  2k proposer calls.
- 100% of 50 planted forged certificates are rejected (wrong dual, float smuggled in, infeasible x).
- Report the "improves on best known value" rate honestly; expected 0 (guess).

**Cost.** 2k calls (guess); CPU only.

**Failure modes.**
- LP problems are often solvable outright by a solver, so evolution adds little. The honest value is in the
  pipeline, not in discoveries.
- Without a second adapter (for example an exact combinatorial-witness checker), the domain is narrow.

**Prior art.**
- FunSearch (Romera-Paredes et al., Nature 2023).
- AlphaEvolve (Novikov et al. 2025, arXiv 2506.13131, verified id).
- OpenEvolve and ShinkaEvolve (open reimplementations, unverified).
- The Station's exact evaluators (survey).

New: pre-registration plus certificate re-checking by a fixed, separate trusted checker.

## 5. Red/blue verifier co-evolution judged by a fixed mutation set (rank 5)

**Idea (L3 verifier synthesis).**
- A blue agent writes property tests and invariants for a soft task. A red agent writes plausible wrong solutions.
- Co-evolution (Hillis 1990, host-parasite sorting networks) drives both sides up.
- **But persistence is judged by neither side.** A verifier persists, capped at T2 (GOAL L3), only if:
  - it kills ≥ 90% of a **fixed, adapter-owned mutation set** (mutation-testing operators applied to reference
    solutions, generated outside both agents); and
  - it falsely rejects ≤ 2% of independently produced correct solutions.
- Red's successes become regression cases. They are never the persistence criterion, because colluding red and blue
  agents could otherwise inflate each other.

**Interactions.**
- Invariant 5: red and blue are the same family today. This is recorded as NOT MET.
- Safety rests on the fixed mutation set, not on adversarial honesty (inferred).
- Never upgrades an adapter (invariant 13).

**Build.** `verify/synth.py` plus a mutation operator set inside the ML adapter (needs human approval), about
400 LoC.

**Acceptance.**
- On 5 ML-repair tasks, synthesised verifiers reach a mutation kill rate ≥ 90%, with a 95% lower bound ≥ 80%, and
  false rejects ≤ 2%.
- A planted "verifier that accepts everything" and a planted "verifier keyed to red's outputs" are both refused.

**Cost.** About 50 calls per task (guess).

**Failure mode.** Mutation sets under-represent semantic errors, so a kill rate overstates verifier strength.
Label it "mutation-adequate", not "correct".

**Prior art.**
- Mutation testing (DeMillo, Lipton and Sayward 1978).
- CodeT (Chen et al. 2022, arXiv 2207.10397, inferred id).
- ScienceIDE fault injection (survey).

---

## What NOT to do

1. Do not use any novelty, interestingness or LLM "interesting?" score as an acceptance or certification signal.
   OMNI's LLM interestingness judge is fine for exploration, never for persistence.
2. Do not let generated tasks enter gate, sealed or LOFO splits, even after they "prove useful". Promoting them would
   let the system design its own exam.
3. Do not let the evolver or the generator write verifiers that grade its own lineage, or edit adapters.
   Self-referential DGM-style editing of the evaluator is exactly the coupled Goodhart failure that invariant 13
   blocks.
4. Do not revoke by genealogy, and do not keep stones after their evidence dies (Proposal 2).
5. Do not claim "emergence" from in-distribution gains, a single campaign seed, or without the random-edit and
   shuffled-label nulls.
6. Do not chase literal open-endedness (unbounded task growth) on a laptop. Cap generated families (for example
   ≤ 10 live) and retire them by learnability.
7. Do not count archive size or QD-score as progress in reports. They are diagnostics, labelled MECHANICAL.

## GOAL.md diff for the top 2

```diff
@@ §5 L6 Harness evolution, after item 1 (Evidence export)
+1b. **Generated task families** (❌, M6): a generator agent (sandboxed) writes task generators whose ground truth
+   is by construction (an adapter-registered reference model reaches the planted effect) or certified by an
+   approved adapter checker. A family is admitted only if the seed harness's dev solve rate is in [0.1, 0.9] and its
+   error pattern correlates ≤ 0.8 with every existing family. Generated families enter the EVOLVE split only;
+   generator source, traces and labels are hidden data (invariant 2) and excluded from the proposer export.
+   Done when: arms A (human evolve only), B (+ generated), B0 (+ label-shuffled generated), 3 seeds each at matched
+   proposer budget: B − A on two never-targeted human families has a paired 95% lower bound > 0 (pre-registered
+   δ = 3 pp), B0 − A's CI contains 0, 0 leaks, planted-exploit catch rate 100%. "Inconclusive" is reported as such.
@@ §5 L7 Search over search
-Islands with different proposer and mutator mixes (MILO idea; no public code exists).
+**Stepping-stone archive** (MAP-Elites; DGM): parents are sampled from an archive whose cells are computed from
+EVOLVE-split evidence only (per-family profile, cost bucket, edit type); insertion requires a paired evolve CI
+excluding 0. Archive entries are MECHANICAL search state: never routed, deployed or rendered into memory; promotion
+only through the full L6.8 path. Revocation follows EVIDENCE edges: a retracted certificate evicts the archive
+descendants reached through it; an accepted harness is re-gated only if its own acceptance evidence cites it.
+Islands = archive partitions with different proposer and mutator mixes (MILO idea; no public code exists).
+Done when: vs the greedy loop at matched budget (3 seeds), ≥ as many accepted edits and held-out-family
+non-inferiority (lower bound ≥ −1 pp); a planted retraction evicts 100% of descendants and revokes 0 independently
+gate-accepted harnesses.
@@ §10 Hard invariants
-4. No persistent write (memory, skill, harness, weights) without evidence at the tier floor.
+4. No persistent write (memory, skill, harness, weights) without evidence at the tier floor. Search state (candidate
+   pools, the L7 archive) is not such a write only while it is labelled MECHANICAL and is never routed, deployed or
+   rendered to a non-evolution agent.
+17. Self-generated tasks enter only the evolve split; every gate, sealed and LOFO/LODO family is human-curated, and
+   no novelty or interestingness score is an acceptance or certification signal.
@@ §11 Build order
-| **M6 Memory, verifiers, branching** (was M4) | L5 store API; ...
+| **M6 Memory, verifiers, branching** (was M4) | L5 store API; generated task families (L6.1b); ...
-| **M9 Discovery & weights** (was M7, minus T0) | L7 islands; ...
+| **M9 Discovery & weights** (was M7, minus T0) | L7 stepping-stone archive + islands; ...
```

Sources checked on the web: [DGM, arXiv 2505.22954](https://arxiv.org/abs/2505.22954v2),
[AlphaEvolve, arXiv 2506.13131](https://arxiv.org/abs/2506.13131.pdf). Every other id is from memory and labelled.
