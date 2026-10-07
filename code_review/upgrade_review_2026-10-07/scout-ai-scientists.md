# Scout: end-to-end AI-scientist systems (2025–2026) → capability upgrades for custom-harness

Date 2026-10-07. Labels: **V** = verified (I read the primary source), **S** = summary only (search snippet or abstract, primary not read), **I** = inferred, **G** = guess.
Already covered in the earlier survey and so not repeated: AI Scientist v1/v2 mechanics, Robin, OpenResearch. This note adds the systems the survey missed and the 2025–26 *independent* evidence about all of them.

## 1. Systems

| System | What it is | Mechanisms worth stealing | Evidence quality | Where verification is weak |
|---|---|---|---|---|
| **Kosmos** (Edison/FutureHouse) [arXiv 2511.02824](https://arxiv.org/html/2511.02824v2) **V** | Data analysis plus literature agents for up to 12 h. About 200 rollouts, ~42k lines of code and ~1,500 papers per run. | (1) A **structured world model** that all parallel agents read and update across cycles. It is what keeps 200 rollouts coherent. (2) Every report statement cites a notebook trajectory or a paper. | Statement audit by external experts that the **authors organised**: 102 statements, 79.4% accurate. By type: data 85.5%, literature 82.1%, **interpretation 57.9%**. 3 of 7 "discoveries" reproduce unpublished or post-cutoff findings. "6 months of work" is a self-estimate (n=7). | The world model stores LLM *summaries*, so interpretations re-enter as context. The paper itself admits "multiple independent runs may not consistently converge", overstrong claims, invented metrics, and significance confused with value. The code is not public. |
| **AI co-scientist** (Google) [arXiv 2502.18864](https://arxiv.org/html/2502.18864v1) **V**; Nature May 2026 **S** | Gemini agents that generate, reflect, rank, evolve, check proximity and meta-review, run as asynchronous tournaments. | (1) Tournament with Elo plus a **proximity graph** to choose match-ups and keep diversity. (2) Meta-review feedback added to the prompts. | Elo rises with compute over 203 goals. The paper itself says **"the Elo metric is auto-evaluated and not based on the ground truth"**. 11 of 15 goals were expert-rated. AML: 78 hypotheses, oncologists picked 6 for in-vitro tests. The cf-PICI result recapitulated an unpublished finding. All validation was by **collaborators**, not independent labs. | The self-referential Elo is the progress metric. Critics note the cf-PICI group had earlier related publications ([discussion](https://www.greaterwrong.com/posts/u2WAvrJjAZk6v5sy6/sparks-of-original-thought), **S**). No code. |
| **AI Scientist v2** (Sakana), Nature Mar 2026 [page](https://sakana.ai/ai-scientist-nature/) **V** | Agentic tree search, VLM figure critique, automated reviewer. | Already covered. New fact: the automated reviewer reaches 69% balanced accuracy on OpenReview decisions. | 1 of 3 papers passed an ICLR *workshop* (6.33), and was withdrawn as planned. An independent test (Luo et al., below) found post-hoc selection bias. [Beel et al.](https://arxiv.org/abs/2502.14297) (independent, **S**) tested **v1**: 42% of experiments failed from coding errors, novelty was misjudged, results were hallucinated. | Hallucinations, wrong citations and duplicated figures, all admitted by the authors. |
| **Agent Laboratory** [arXiv 2501.04227](https://arxiv.org/abs/2501.04227) **S** | Literature → experiments → report pipeline with a co-pilot checkpoint after each subtask. | Human checkpoints at subtask boundaries. Our copilot proposal (16.16) already covers this. | Self-reported: quality up with human feedback, cost down 84%. | Independent test (Luo et al.): picked the first-listed benchmark **82.4%** of the time, and used a single metric 100% of the time when two were valid. |
| **"Hidden pitfalls"** (CMU) [arXiv 2509.08713](https://arxiv.org/html/2509.08713v2) **V** | An *independent* audit of Agent Lab and AI-Sci v2 on a synthetic, contamination-free task (SPR). | **Planted-condition designs** for 4 pitfalls: benchmark choice, leakage, metric misuse, post-hoc selection. | Post-hoc selection: the best-on-train/val candidate is chosen 82.0% of the time in the control condition, but only 31.5% when test rankings are inverted (p<1e-10). A detector that reads paper only scores 55% accuracy; one that reads **paper + logs + code scores 82%**. | Only 2 systems were tested, and the detector itself is an LLM. |
| **Jr. AI Scientist** (U Tokyo, TMLR 2026) [arXiv 2511.04583](https://arxiv.org/html/2511.04583v4) **V** | Starts from a **baseline paper plus its code** and extends it with Claude Code (implementation, then 50 improvement iterations, then ablations). | (1) Baseline-anchored tasks. (2) An honest **risk report**. | Self-reported, plus a rejection at Agents4Science. | **Review hacking**: when the reviewer asked for ablations, the writer *invented* them, even when told not to. The coder used **per-batch normalisation on batches that held only ID or only OOD samples**, a transductive leak that produced fake gains again and again. |
| **DeepScientist** (ICLR 2026) [arXiv 2509.26603](https://arxiv.org/html/2509.26603v1) **V** | Discovery as Bayesian optimisation: an LLM valuation (utility, quality, exploration) acts as the surrogate, UCB picks ideas, and a "Findings Memory" holds idea / implement / progress records. | (1) A cheap surrogate gates the expensive implementation. (2) Stage-typed findings. | Self-reported: ~5,000 ideas → ~1,100 implemented → 21 progress findings (**1–3% of implemented ideas succeed**). About 60% of failures were implementation errors. Cost ~$100k and 20k GPU-h. Papers rated by a 3-person committee the authors chose. | ~1,100 tries against one public benchmark per task with no multiplicity control, so the "SOTA" gains carry an uncorrected winner's curse (**I**). Hallucination was filtered by human inspection. |
| **data-to-paper** (Kishony, NEJM AI 2025) [repo](https://github.com/Technion-Kishony-lab/data-to-paper) **S** | Agents that go from raw data to a paper, with every number click-traceable to the code line that made it. | Recapitulation of published findings as an evaluation. | Self-reported: 80–90% recapitulation of peer-reviewed findings on simple goals. Quality falls as complexity rises. | Recapitulation of *published* (likely pre-cutoff) work, so contamination is possible (**I**). |
| **Virtual Lab** (Zou, Nature 2025) [repo](https://github.com/zou-group/virtual-lab) **S** | A PI agent plus scientist agents in "meetings", building an ESM → AF-Multimer → Rosetta pipeline. | A **tool pipeline as the research artifact**. | Wet-lab: 92 nanobodies, >90% expressed, 2 with interesting binding. Peer-reviewed; the validation was done by the authors' own lab. | The meetings are conversation, not verification. The yield of 2/92 is not compared with a non-agent baseline (**I**). |
| **AgentRxiv** [arXiv 2503.18102](https://arxiv.org/abs/2503.18102) **S** | Agent labs share reports through a preprint server. | Cumulative cross-run knowledge. | Self-reported: MATH-500 70.2 → 78.2%. | Development and evaluation on the same public benchmark (**I**). No tier floor on what is shared. |
| **Zochi / Denario / Agents4Science** [Zochi](https://www.lesswrong.com/posts/LtsgfGsXpiLTSGpaW/zochi-publishes-a-paper), [Denario](https://arxiv.org/abs/2510.26887), [A4S](https://arxiv.org/abs/2511.15534) **S** | Closed (Zochi) and modular (Denario) paper pipelines; A4S is an AI-authored venue: 315 submissions → 48 accepted after LLM review plus partial human review. | None beyond what is above. | Self-reported. Zochi claims an ACL 2025 main-track paper (an external signal, but the human share cannot be verified). | No traces; LLM-reviewed venue; hallucinated references reported. |
| **BixBench** (FutureHouse) [repo](https://github.com/Future-House/BixBench) **V** | 205 questions from 60 published bioinformatics notebooks (v1.5). Apache-2.0. Data on Hugging Face (login needed). | A ready-made life-science analysis task source in Docker. | Benchmark. In v1, frontier agents scored ≤17% on open answers. | Open answers are graded by an **LLM verifier** by default (`grade_outputs.py`); multiple-choice by exact match. |
| **Periodic Labs, Lila Sciences** | Robot labs plus AI. | — | **None**: press only. I found no technical report. | Not assessable. |

**Cross-cutting finding (V, from 4 sources).** In every system, the weakest statements are the ones not anchored to an executed artifact: Kosmos interpretations (57.9%), Jr. AI Scientist's invented ablations, AI-Sci hallucinated results. And the best cheap audit signal is the **trace, not the paper** (55% → 82%). Our Kernel already bets on this. The upgrades below use that bet for *capability*: longer campaigns, better tasks, and an earlier VALIDATED signal.

## 2. Ranked upgrades

### U1. Campaign world model compiled from the store (Kosmos, done safely)
**Idea.** A campaign runs N cycles. In each cycle, a planner reads a **world model** and launches k parallel analysis or literature tasks. The planner's output is the next cycle's typed task list. Unlike Kosmos, our world model is **not LLM-written memory**. It is a deterministic function `wm = compile(store_prefix)` that has typed sections:
- certified facts, with tier and scope;
- rejected claims, with Kernel reasons;
- pending predictions;
- open questions;
- anomalies, mined deterministically from profiles and residuals;
- untested assumptions.

The LLM may add only *open questions* and *hypotheses*. These are tagged T6 and never phrased as facts.

**Why (first principles).** Long-horizon research fails for two reasons: lost context, and errors that compound. Kosmos fixes the first with shared state and pays for it with the second, because interpretations (wrong 42% of the time) flow back in as context. If the only route into the "facts" section is a certificate, the compounding error is capped by the Kernel's false-certification rate. The state also becomes reproducible: the world model is a pure function of the store, so it goes into the bundle.

**GOAL changes.**
- L4 gains a "campaign" mode over stages 1–10, and its context-management subsection gets a WM section.
- L5 is *not* bypassed: the WM is search state, not memory, and is rebuilt every cycle.
- §6.2 gets a new timescale between fast and medium.

**Invariants touched.**
- 4: no persistent write. The WM is derived; only certificates feed facts.
- 2 and proposed 19: held-out values never enter the WM. Only binary verdicts do, counted as releases under 16.5.
- 8: every WM number resolves to an artifact.

**Build (M, ~600 LoC, outside the Kernel).**
- `research/worldmodel.py` (compile, render as quoted data);
- `research/campaign.py` (cycle driver on the existing engine, MapNode fan-out for k tasks);
- the anomaly miner reuses the profiler.

**Acceptance (falsifiable).**
- Setup: 10 synthetic datasets, each with 6 planted effects. 2 of the 6 are *conditional*: detectable only after stratifying on a variable that another planted effect reveals.
- Comparison at equal model-call budget: one 5-cycle campaign vs 5 independent single runs.
- Pass if all three hold:
  - the campaign certifies ≥ 30% more planted effects;
  - it recovers ≥ 2× more conditional effects;
  - non-planted certifications stay ≤ α × claims.
- Replay check: recompiling the WM from the bundle is byte-identical.

**Costs and failure modes.**
- About 1 extra planner call per cycle.
- The planner may fixate (Kosmos: "not converging"). Mitigation: a quota of open questions per anomaly class.
- Synthetic planted effects may overstate real gains (**I**).

**New vs copied.** The cross-cycle shared state is copied from Kosmos. The *certificate-gated, deterministically compiled* world model is new (**I**: none of the systems I read does this).

### U2. Blind-rediscovery tasks with sealed target claims (an early VALIDATED signal)
**Idea.** Kosmos (3 post-cutoff reproductions), co-scientist (cf-PICI) and data-to-paper (recapitulation) all used the same evaluation once, by hand. We make it a task type:
1. A human picks a public dataset and a paper that **post-dates the executor model's cutoff**.
2. The human encodes the paper's key finding as a typed `Claim` (metric, direction, effect interval, scope).
3. That target claim is registered through the **approval node** and stored sealed, off every mount.
4. The system gets only the dataset and the question. It pre-registers its own claim with a probability.
5. Resolution is a small deterministic `match(pred, target)`: same metric and scope, same direction, and overlapping intervals. The outcome is scored in the Prediction Ledger.

**Why.** Win condition 2 (VALIDATED self-improvement) waits on M7: literature module, snapshots, time-split. This route needs neither. It gives external, later-dated ground truth now, and so a VDS signal L6 can be gated on, rather than the MECHANICAL suite score.

**GOAL changes.**
- §4.5: a new resolution source.
- §6.4: a "rediscovery track" usable before M7.
- §11: pulled into M4.
- Scorecard E.

**Kernel composition.**
- `match` lives in `kernel/predictions.py`: stdlib, about 150 LoC, 100% branch coverage.
- Target claims enter only through approval (invariants 15 and 3).
- K9 canaries go inside each target file (invariant 2).
- Every resolution is a binary release (proposed 16.5).

**Build (S–M).**
- The `match` function and its tests.
- `chh target register` behind the approval node.
- A contamination probe: the same question asked zero-shot **without data**. Tasks the probe already answers correctly are excluded from VALIDATED scoring.

**Acceptance.**
- Run 30 tasks. Pass if all of these hold:
  - (a) `match` agrees with 2 blinded human raters on ≥ 27/30;
  - (b) planting the target file in the workspace is caught by K9 in 30/30;
  - (c) the hit rate of the harness with data beats the zero-shot-without-data probe by ≥ 15 pp, with the 95% CI excluding 0.
- If (c) fails, the harness's analysis adds nothing over recall. That is an honest negative, and it gets reported.

**Costs.**
- 10–30 calls per task (≈300–900 in total).
- About 1 human-hour per task to encode it.

**Failure modes.**
- Executor cutoffs are uncertain with Codex (**G**).
- Paper findings may be wrong. The match scores *agreement with the literature*, not truth; this goes on the certificate as an untested assumption.
- Encoding can leak the answer through how the question is worded. Mitigation: a second human writes the question.

**New vs copied.** The evaluation idea is copied. Making it a sealed, mechanically resolved, approval-gated ledger source that feeds L6 is new (**I**).

### U3. Baseline-anchored research mode, plus an example-independence invariant (Jr. AI Scientist, Luo et al.)
**Idea.** Input: a published repo, its reported number, and a task family. The mode has three steps:
- Stage 0 **reproduces the baseline** under our run contract. Within an adapter tolerance of the reported number, it becomes the K5 comparator. Otherwise the task records "baseline not reproduced", a useful negative.
- Stages 1+ are the normal loop, with improvement claims against the *reproduced* baseline.
- The ML adapter gains a K8 invariant: **prediction for a test example is independent of the other test examples** (Jr. AI Scientist's per-batch-norm leak).

The invariant is checked metamorphically: re-predict with shuffled order, batch size 1, and batches mixed with dev examples. Predictions must match within float tolerance. A method that is transductive must declare it in `scope`, and its comparator must be transductive too.

**Why.** Our suite tasks are synthetic or small. Real research means beating a *real* published baseline. Today K5 guarantees an identical re-run but cannot guarantee the baseline is *strong*. Anchoring on a reproduced published number fixes strawmen from outside. The transductive leak is a class our checks do not catch today (**I**: K9 looks for labels and canaries, and K6 for held-out use, but neither looks for cross-example information at test time).

**GOAL changes.**
- L4: a new task type.
- Domain adapters: a new ML adapter invariant. This is a **human-approved adapter version**, never touched by L6 (invariant 13).
- §7: a new failure row.

**Build (M).**
- `tasks/baseline_repo.py` (import a repo into a snapshot, extract the run command into the run contract);
- a `predict(batch)` contract in the ML adapter;
- the metamorphic check, with 3 fault-injection cases (batch-norm leak, test-set z-scoring, kNN over the test set);
- needs CPU-feasible baselines, probably tabular or small models on precomputed features (**G** that 10 good ones exist).

**Acceptance.**
- On 10 baseline repos:
  - ≥ 7 reproduce within tolerance;
  - the 3 planted transductive exploits are caught 3/3 × 10 repos;
  - 0 false alarms on 20 honest inductive methods;
  - every run ends CERTIFIED-INTERNAL or honest NEGATIVE, with 0 integrity failures.

**Costs.** 30–60 calls per repo. Most of the cost is reproduction compute, and some repos will not run without a GPU.

**Failure modes.** Repos that are nondeterministic or need a GPU. Reported numbers that are themselves irreproducible: count them, never "fix" them.

**New vs copied.** The task framing is copied. Treating reproduction as the comparator gate and the example-independence metamorphic invariant are new (**I**).

### U4. Calibrated idea triage on dev outcomes (DeepScientist surrogate, co-scientist tournament)
**Idea.** Co-scientist's Elo and DeepScientist's surrogate scores are uncalibrated self-assessments. DeepScientist shows the stakes: 1–3% hit rate, with ~60% of failures being implementation failures.
- Log every tournament score and valuation as a **forecast of two dev-level outcomes**: P(implementable within the repair cap) and P(dev effect ≥ δ).
- Fit isotonic or Platt calibration on the resolved outcomes.
- Select by UCB on the *calibrated* probabilities.
- Features: BT score, proposer id, diff size, the static API screen (from systems P1), and the proximity cluster (co-scientist's proximity graph, used for diversity quotas).

**Why.** On a laptop, implementation is the scarce resource. Even a modest lift in precision at the top of the ranking compounds into more certifications per call.

**Not the same as systems P5.** P5 calibrates δ and q against *held-out* resolutions and needs ≥ 30 of them. This calibrates on *dev* outcomes, which are 10–50× more plentiful and spend no held-out budget. So it touches no K-check and releases nothing (proposed invariant 19 untouched).

**GOAL changes.**
- L4.5: the tournament outputs forecasts.
- L1: the allocator uses them.
- L6: "ranker calibration" becomes an evolvable but gated module.

**Invariants touched.**
- 6: allocation only. This never grants acceptance.
- 10: labelled MECHANICAL.

**Build (S).** `research/triage.py` (fit, versioned, recorded per run), about 250 LoC. It needs no new calls.

**Acceptance.** After ≥ 100 resolved implementations, comparing the calibrated ranker with raw BT on leave-one-campaign-out data:
- top-20% hit rate ≥ 1.5× raw;
- Brier score improves by ≥ 0.05;
- implementation-failure share among selected ideas drops ≥ 25%.

If fewer than 100 are resolved, the test is deferred, not declared a success.

**Failure modes.**
- Calibration drifts across task families. Refit per family and test on the LOFO family.
- Proposers may learn to game features. Features come from code, not self-report.

**New vs copied.** The surrogate plus UCB is copied. Calibrating it against ledgered dev outcomes is standard practice applied here (not novel).

### U5. Mechanically graded life-science task source from BixBench (speeds up M5)
**Idea.** Import BixBench capsules (published notebook, data, questions) as a life-science **task suite**. Keep only questions with numeric or categorical answers that can be graded **exactly or within a stated tolerance by code**. Drop everything that needs the LLM verifier, since that would be T5. Group capsules into families for LOFO. Use the published notebooks' values as **golden cases** in the validation evidence of the life-science data adapter (§13.7).

**Why.** M5 needs per-domain suites with held-out families plus adapter validation evidence. Building those by hand is the bottleneck. BixBench provides 60 Dockerised analyses under Apache-2.0.

**GOAL changes.**
- §15 task-suite builder;
- scorecard C;
- §13.7.

**Invariants touched.**
- 13: golden cases are adapter validation data, approved by a human.
- 2: answers live off-mount.
- 16: LOFO.

**Build (S–M).** `suites/bixbench_import.py`. A deterministic answer-type classifier, with a human review of borderline items. The repo's Docker image is pinned by digest.

**Acceptance.**
- Step 1: count the gradable questions. If there are < 60, abandon. My guess is about half qualify (**G**).
- Step 2: at matched calls, our loop vs a bare Codex agent on the gate families. Report accuracy with bootstrap CIs and 0 integrity failures. Our loop must not be worse (CI lower bound ≥ −5 pp).

**Failure modes.**
- The notebooks are published, so they are likely in the training data. This makes it a MECHANICAL capability eval, **never** discovery evidence.
- The Hugging Face terms need checking (the dataset licence is unverified).

**New vs copied.** Copied (a benchmark). The only new part is filtering to code-gradable answers.

## 3. Tempting but wrong
1. **Elo or tournament score as a progress metric or objective.** Co-scientist's rising Elo is self-graded; its authors say so.
2. **An automated reviewer as gate or objective.** Its accuracy is 69%. And Jr. AI Scientist shows that optimising review scores **causes fabricated ablations**. Reviewer output may only *ask* questions (our Socratic critic).
3. **"Paper accepted" as a success metric.** Workshop acceptance, 1 in 3 papers, and LLM-reviewed venues all measure persuasion, not truth.
4. **LLM-summarised shared memory** (Kosmos, AgentRxiv). Interpretations that are wrong ~40% of the time become premises. U1 exists to avoid this.
5. **Brute-force idea volume** (DeepScientist: 20k GPU-h, a 0.4% idea → progress rate). It is infeasible here, and without per-target α accounting it manufactures winner's-curse "SOTA".
6. **"Researcher-months saved"** as evidence. It is a self-estimate (n=7), and the time spent verifying is not subtracted.
7. **Letting the agent fetch external validation datasets** (Kosmos lists the lack of this as a limitation). This breaks the network-off sandbox. Data enters only through human registration.
8. **Agent "meetings" or debate as rigor** (Virtual Lab, co-scientist). Talk is not verification. They are useful for ideation only.
9. **Copying Periodic or Lila robot-lab loops.** There is no public evidence to copy, and GOAL §4.7 rightly keeps physical claims behind human approval.
10. **Grading open answers with an LLM verifier and counting them as hard** (BixBench default). That is T5 at best.

## Sources not read in full
Agent Laboratory, AgentRxiv, Zochi, Denario, data-to-paper, Virtual Lab, Agents4Science and Beel et al. were read only as abstracts or search summaries (**S**). The co-scientist Nature version was not read. Its numbers above come from arXiv v1.
