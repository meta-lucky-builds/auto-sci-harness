# Binding constraints: buildable answers (2026-10-07)

Labels: **verified** = read in the files or measured on this machine today; **inferred** = reasoned from the files or code;
**guess** = an estimate I have not checked. This file does not repeat earlier proposals: the preflight, fold-racing,
cassettes, α-ledger, info budget and K3 reliability ideas are already in `systems.md` and `evidence-economics.md`.

**The point the ranking rests on (inferred from `policy.py` and `claims.py`).** The LLM roles that invariant 5 covers
do not carry integrity. The Kernel does (invariant 3). The `judge` role (tournament and critic) only decides where
effort goes, and the critic is advisory. A replica counts only if it beats the baseline on dev
(`replica_dev`) and then earns its own Kernel certificate. So a weak second-family model costs **yield**, not
**false certificates**. What it has to show is: (a) it does better than a null at its role; (b) its errors are not
the same as Codex's errors. It does not have to match Codex. This lowers the bar for a second family a lot.

**Machine (verified today):** i7-8550U (4 cores / 8 threads, 15 W), 15.9 GB RAM, Intel UHD 620, no GPU.

---

## 1. A qualified second family: a local CPU model as the floor, a free official API as the upgrade, and role exams to admit either

**Why it binds.** Invariant 5 is NOT MET on every live run (verified, STATUS). Without it there are no headline
results (`headline()` needs `invariant_5 == "met"`), no replication credit, and M4's family-separated critic is blocked.

**Solution.**

*(a) Local floor: Ollama or llama.cpp through the existing `OpenAICompatBackend`.* `policy.py` already maps the
`qwen`, `llama`, `gemma`, `phi` and `mistral` prefixes to families (verified). So adding a local model needs only a
human install plus a registry entry, with almost no code. What fits on this CPU:

- Token generation is limited by memory bandwidth. Assuming dual-channel DDR4-2400 (≈38 GB/s peak, guess) at about 60% efficiency:
  - 3–4B at Q4 (≈2.5 GB): ≈8 tok/s;
  - 7B at Q4 (≈4.7 GB): ≈4–5 tok/s;
  - 14B at Q4 (≈9 GB): ≈2 tok/s, and that size does not fit beside Docker's VM (WSL2 default cap is 50% of RAM, inferred).
  - All guesses.
- Raw prompts are small. Hypothesize was 15,584 input tokens *including* the Codex CLI overhead of about 15k, so the
  role content is about 1–3k tokens (inferred).
- Estimated time per call (guess):
  - critic, 4B (≈2k in / 300 out): ≈1.5 min;
  - one tournament round, 4B: ≈1.5–2 min;
  - replicator, Qwen2.5-Coder-7B (≈2.5k in / 1.5k out): ≈7–9 min.
  - That adds about 15–20 min per run, against today's 845–8,074 s. Acceptable, and it uses no quota.
- Set `keep_alive=0` so the model unloads before sandbox steps. Model calls and sandbox runs alternate in the loop
  anyway (inferred).

*(b) Free official API upgrade.* GitHub Models is free for any GitHub account. A third-party summary gives the limits
(unverified): low-tier models (for example Llama 3.1 8B, Phi-4, Ministral) at about 15 requests/min and
150 requests/day; high tier at about 50/day; about 8k input and 4k output tokens per request. A run needs about
5 second-family calls, so that is 10–30 runs per day, and our prompts fit under 8k. Rules:
- Exclude the OpenAI models hosted there, because they are the same family.
- This is **API mode, not BYOS**, so invariant 11's BYOS allowlist does not apply. A human still verifies the
  provider's terms ("prototyping" use) and creates the token. Claude does neither.
- Gemini's free tier is an alternative (Google family). Its limits and data terms are guesses; prompts never carry
  hidden labels (invariant 2).

*(c) Role exams decide admission.* A model may take a role only if it passed that role's exam, and the policy fails
closed otherwise. The exams are built from artifacts the project already has. They use dev and public data only, so
they wear no held-out data.

| Role | Exam | Null | Pass bar (proposed) |
|---|---|---|---|
| judge/tournament | 60 hypothesis pairs: a sound plan vs the same plan with one planted defect (peeks at test, unscaled distances, wrong metric direction, leaky feature), shown in both orders | 0.5 | pairwise accuracy ≥ 0.80 (the Wilson 95% lower bound ≈ 0.68 at n = 60) and order consistency ≥ 0.85 |
| judge/critic | 51-claim planted suite (STATUS M1 exit) rendered as plans; the critic emits typed flaw tags + questions | tag recall of a fixed-question critic | typed-flaw recall ≥ 0.6; false-flag rate on the 10 true claims ≤ 0.3 |
| replicator | ≥ 10 finished claims (rings, wine A/B, synthetic tasks), dev only, through `replica_dev` | 0 | success ≥ 0.4, and every replica below the 0.8 similarity limit |
| all | phi correlation of misses vs Codex on a 20-item subset | n/a | report it; at ≥ 0.8 label the role "diversity not shown" |

To make the critic testable by a 4B model, it gets a **typed output**: multi-label tags from a fixed flaw taxonomy,
plus free-text questions. The question-only filter stays.

**Touches.** §5 L0 gateway, L4.4/8/9, §10 invariants 5, 2, 11 and 3. Nothing is weakened: the Kernel is unchanged, and
exam scores are recorded in provenance. Model licences (Qwen Apache-2.0 for most sizes, the Llama community licence)
go into the registry.

**Plan.**
1. The human installs Ollama and pulls `qwen2.5:3b` or `qwen3:4b` and `qwen2.5-coder:7b`.
2. Add `providers.yaml` entries (verified_by a human).
3. Write `exams/` (the 3 sets above; about 300 LoC) and `chh exam <role> <model>`.
4. Add a policy check: the role binding requires a passing exam hash for that exact model digest.

**Acceptance.**
- The local 4B passes the tournament and critic exams at the bars above, and the 7B coder passes the replicator exam.
  If it fails, report it and try the API option.
- One live run records `invariant_5: met`.
- The 51-claim suite still has 0 false certifications.

**Costs.** About 5 GB of disk. About 2 h of CPU per full exam pass (guess). No Codex quota.

**Failure modes.**
- The replicator may be too weak on CPU (guess: the most likely failure). Then the API tier is needed for that role.
- Open models trained on GPT outputs may share Codex's errors. The phi measurement exists to show this, not hide it.
- A failed replication still registers a claim and enlarges the target's Bonferroni family. Run the replica's dev gate
  before registering it; it already routes to `report` on dev failure (verified).

**Prior art.** LLM-as-judge position bias and the both-order test (Zheng et al. 2023, MT-Bench). Correlated errors
across LLMs (Kim et al. 2025, "Correlated errors in LLMs", unverified citation). The SDE r > 0.8 finding is already in GOAL §8.7.

---

## 2. Small-train / large-test task designs: certify on n_test, keep CPU cost on n_train

**Why it binds.** The certifiable effect is observed ≥ (0.67 + z_{α/m})·SE (from K7 and K4; systems.md derives 0.135
for n = 54). Test sizes today, counted from `public/test_X.csv` (verified): iris 44, wine 54, diabetes 133,
breast cancer 171, synthetic 270–600, digits 540.

**First principles.** Certification power depends only on the **test** sample. For a paired accuracy difference with
discordant rates p_b and p_c:

SE = √((p_b + p_c − (p_b − p_c)²)/n_test)

Prediction is cheap and linear in n. Training is what costs CPU. So keep training data small and make the test set large.

| n_test | discordance | SE | min certifiable effect (m = 1) | (m = 10, z = 2.576) |
|---|---|---|---|---|
| 54 | 0.2 | 0.058 | 0.135 | 0.19 |
| 540 | 0.2 | 0.019 | 0.044 | 0.062 |
| 5,000 | 0.1 | 0.0045 | 0.010 | 0.015 |
| 20,000 | 0.1 | 0.0022 | 0.005 | 0.007 |

(Approximation: normal, and the effect ≈ p_b − p_c ignored inside the root.) At n_test ≥ 5,000 the normal
approximation K7 uses is also accurate, which removes one of STATUS's listed deviations in practice.

**Build.**
- *Synthetic families* (rings, xor, spirals, friedman, interactions, imbalanced): the generators already exist.
  Regenerate them with n_train ≈ 600–1,000 and n_test = 20,000. There is no licence question.
- *Real families*: use licence-clean large tables. The UCI repository lists donated datasets under CC BY 4.0
  (unverified). Candidates: Adult ≈ 48.8k rows, Bank Marketing ≈ 45k, MAGIC Gamma ≈ 19k, Letter ≈ 20k,
  Dry Bean ≈ 13.6k (all sizes unverified).
- Subsample training to about 2,000 rows, inside the sandbox, with a seed recorded in provenance.
- A human registers each dataset (the trusted-registrant limitation, STATUS §D), and the content hash plus licence
  enter the bundle.
- The claim scope says "trained on n_train = 2,000 rows of S".

**Touches.** L4 task suite and §4.7 data class. K6/K4 are unchanged. No invariant changes.

**Acceptance.**
- Rebuilt synthetic suite: a perfect-on-dev solution's minimum certifiable effect is ≤ 0.02 on every synthetic task.
- K3 replay of 3 seeds on 20k rows takes ≤ 60 s per seed with a sklearn pipeline (guess bound; measure it).
- The planted 51-claim suite, re-instantiated at the large n, still gives 0 false certifications.

**Costs.** Disk ≈ 100 MB. One suite rebuild. Human registration of about 5 datasets.

**Failure modes.**
- Tiny effects become certifiable and may be scientifically trivial. A human-set per-task δ floor of practical
  relevance is needed, and K7 still blocks safe claims.
- On real data the subsample changes the question (small-data regime), so the scope must say so.
- Dev CV is still small, so selection noise stays (fine: held-out confirmation is what got stronger).

**Prior art.** Standard power analysis. Learning-curve evaluation (Perlich et al. 2003). The design inversion
(cheap test, expensive train) is mine; I know of no harness that uses it, but I have not searched for one.

---

## 3. Pooled method-claims across disjoint targets (power ×√K, model calls ÷K)

**Why it binds.** Each task gets its own run (12–13 calls, verified) and its own tiny test set. The M2 exit test is
21 tasks × about 14 calls, which is about 300 calls.

**Solution.** One program P (task-agnostic: it reads `task.yaml`) is claimed against each task's own baseline B_k over
a pre-registered set of K tasks whose test examples are **pairwise disjoint**. The K6 shared-example rule already
detects overlap (verified), so wine-real and wine_knn_unscaled can never be pooled together.

**Statistics (exact form).**
- D_k is the paired mean difference on task k, averaged over seeds.
- The pooled statistic is D̄ = Σ w_k D_k, with weights fixed at registration (w_k = n_k/N, or equal).
- Var(D̄) = Σ w_k² SE_k². This holds because the test sets are independent samples.
- The CI uses a bootstrap stratified by task (resample within each task).
- H0: D̄ ≤ δ.
- **Guard (intersection–union test):** additionally, for every k, the one-sided level-α lower bound of D_k is > −ε.
  An IUT needs no multiplicity correction (Berger 1982), and it stops "large gains on 4 tasks hide harm on 1".
- **Scope:** "the weighted mean effect over these K tasks". It is **not** a claim about new tasks. A random-effects
  claim with K = 3–5 has t_{K−1} degrees of freedom and is nearly powerless, so it is not offered.

**Numbers (inferred).**
- iris + wine + breast cancer (N ≈ 270, discordance ≈ 0.2): SE ≈ 0.027, so the minimum certifiable effect is about
  0.062 instead of 0.135.
- On the §2 synthetic families with K = 4 at n = 20k each: about 0.0025.
- Calls: one pooled run (about 14 calls, slightly longer prompts) replaces K runs.
- Grouping the suite by family and split (evolve, gate and sealed are never mixed) gives about 6 pooled runs, about
  85 calls instead of about 300 for the exit test.

**Touches.**
- §4.2: `scope.datasets[]` already exists.
- K4 gets a stratified pooled test plus the IUT. This is a Kernel change: 100% coverage gate, version bump, user decision.
- K6 runs per target.
- Each pooled claim takes one Bonferroni slot on **each** of the K targets. That is honest wear accounting, not a
  weakening.
- §11: the M2 exit unit changes from "run" to "claim". That is the user's decision.

**Plan.**
1. A `pooled` analysis plan type in the Claim Language.
2. `kernel/stats.py` stratified bootstrap + IUT, about 150 LoC + tests.
3. A research_v1 variant that runs P on K workspaces (map fan-out exists in the plan, not built).

**Acceptance.**
- Simulation, 10,000 trials per configuration: under H0 (true D̄ = δ) the false-certification rate is ≤ α + 0.01.
- With one task harmed (D_k = −2ε) and the others strongly positive, the rejection rate is ≥ 0.95 at its n.
- Live: one pooled claim on the 3 real classification tasks completes with ≤ 16 Codex calls.

**Failure modes.**
- Task-agnostic programs are harder for the model to write. Expect more fix calls.
- Heterogeneous baselines make the mean hard to interpret. Report every D_k.
- K fan-out multiplies sandbox time per candidate (see §5).

**Prior art.** Fixed-effect meta-analysis (Borenstein et al. 2009). Multi-dataset classifier comparison: Demšar 2006
(*JMLR*); Benavoli et al. 2017 (*JMLR*). Note that Demšar's ranks test generalisation across datasets, which we
deliberately do not claim. IUT: Berger 1982 (*Technometrics*).

---

## 4. Codex quota diet: offload judging, batch authoring, strip the CLI context

**Why it binds.** Run B: 13 calls = hypothesize 1 + tournament 3 + critic 1 + implement 5 + fix 3 (verified).
About 16k overhead tokens per call (verified): 8 calls × 16k ≈ 128k of the 207k input tokens.

**Solution.**
- (a) Tournament and critic move to the second family (§1). They must move anyway for invariant 5. That saves 4 calls.
- (b) **One implement call returns all N candidate programs** as a JSON array. Each program is parsed, run, counted as
  an attempt and fixed separately. No invariant requires candidates to be authored independently: dev racing and the
  Kernel judge each program on its own. That saves 4 calls.
- (c) Run `codex exec` from an empty temp directory, with no AGENTS.md and no git repo, and with the leanest official
  config the CLI documents. Measure the overhead with a "reply OK" probe before and after. That the overhead can be
  reduced at all is a guess.

Result: about 5 Codex calls per run (hypothesize, implement-batch, about 3 fixes) instead of 13. Combined with §3,
the M2 exit test needs about 30–40 Codex calls (inferred).

**Touches.** L4.4/5/7. Invariants 5 (helped) and 11 (official CLI only). Attempt counting (§1.2 CR row) stays
per program.

**Acceptance.**
- On 3 synthetic tasks, mean Codex calls per run ≤ 6.
- The fraction of candidates with valid dev predictions is within 0.15 of the per-call design: measure 15 candidates
  each way. This test costs about 25 calls, so ask the user first.

**Failure modes.**
- One truncated reply loses all N programs. Then fall back to per-call.
- Batched candidates may be less diverse. Measure with the same pairwise-similarity function, and require mean
  similarity ≤ the per-call mean + 0.1.

**Prior art.** Request batching is standard. Nothing new here except doing it under the attempt-counting rule.

---

## 5. CPU-time budgets with a contention witness (load-invariant verdicts)

**Why it binds.** In Run B, held-out seed 2 and the K3 replay timed out on a machine "shared with 5 agents"
(verified). That gave K3/K4/K6/K7 failures, and the ledger scored a confident miss. Wall-clock limits mix up host
contention with the candidate's own cost, so verdicts depend on what else the laptop is doing.

**Solution.**
- Limit **CPU-seconds** per sandbox (cgroup `cpu.stat usage_usec`, read inside the container by a trusted supervisor),
  with `--cpus=2` and thread environment variables (OMP, OpenBLAS) pinned to 2.
- Keep a wall-clock backstop at 3× the limit.
- When the backstop fires with CPU use below the limit, ask a **contention witness**: a fixed 2-second CPU benchmark
  container run immediately before and after the attempt.
  - If its wall/CPU ratio is > 1.5, the host was contended. The event is INFRA, and the **whole** evaluation is
    re-run (all seeds, idempotent). Retrying a single seed would be shopping (evidence-economics, "what not to do" #6).
  - Otherwise the timeout is the candidate's own failure. This closes the sleep attack: a program that idles to
    convert its failure into "infra" is still charged with that failure.

**Touches.** L0 sandbox, M0 infra/policy split, K3. Nothing is weakened. This partly addresses the STATUS §D
"candidate-only infra exclusion" limitation.

**Acceptance (20 trials each).**
- A CPU-bound program needing 0.5 × the limit passes 20/20 under an 8-thread stress container. (Measure the wall-clock
  failure count as the control.)
- A `sleep`-based program is charged as a candidate failure 20/20.
- The CPU-time of a fixed program varies by ≤ 25% between idle and loaded hosts. If not, raise the margin.
- Planted Run B replay: no INFRA misclassification.

**Costs.** About 200 LoC + tests. Witness overhead about 4 s per sandbox run.

**Failure modes.**
- WSL2 cgroup accounting may differ from native Linux (guess).
- Hyper-threading contention inflates CPU-time itself. That is why the witness and the margin are both needed.

**Prior art.** RLIMIT_CPU and cgroups. CPU-time limits in Codeforces/ICPC judges. SPEC's idle-machine rules.
The witness design is mine.

---

## Clever but wrong (rejected)

1. **Running gpt-oss locally as the "second family".** It is OpenAI's model, and `policy.py` correctly maps it to openai.
2. **Different Codex models, temperatures or personas as diversity.** These are the same family, and error
   correlation is above 0.8 (GOAL §8.7).
3. **Claude subscription via Claude Code.** Prohibited (HANDOFF §1).
4. **Fine-tuning a local critic on Codex outputs.** `training_on_outputs: forbidden` (verified, registry), and it
   would import Codex's errors.
5. **A 14B local model.** About 9 GB, plus Docker's VM, does not fit in 16 GB. At about 2 tok/s, a replicator call
   would take about 15 min (guess).
6. **Any LLM, of any family, as a Kernel gate.** That violates invariant 3. Second-family models judge only advisory
   roles.
7. **Bootstrap or repeated resampling of the held-out set to "get more n".** It adds no information; n_test bounds it.
8. **Pooling tasks that share a source** (wine + wine_knn_unscaled), or claiming generalisation to new tasks from
   K ≤ 5. The first double-counts examples; the second is a random-effects claim with no power.
9. **Switching to log-loss or Brier for power after seeing results.** That is metric shopping. It is acceptable only
   as a metric fixed per task family by a human beforehand.
10. **GitHub Actions or free Colab as a sandbox backend.** Network is on, the terms on non-CI or automated use are
    unclear (guess), and the backend would need an isolation class plus a human approval (§5 L0). Parked until a
    human reads the terms.
11. **Folding the tournament into the proposer** to avoid invariant 5. That keeps the letter of the invariant and
    removes the independent check.
