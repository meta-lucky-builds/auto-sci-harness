# Build status

Living checklist against `GOAL.md` (the source of truth; milestones in GOAL §11). Updated 2026-10-06.

## ⏸ STOPPING POINT (2026-10-06, halted at the user's request)
- **New chat: read `HANDOFF.md` first** (operating knowledge, traps, exact next steps).
- **Where the build stands:** M0 done (+2nd pass), M1 Evidence Kernel done (v1.1 after red-teaming), M2 research
  loop v1 BUILT (Kernel-integrated loop, improve operator, Socratic critic, replication agent, profiling,
  ObservationPack context, 21-task suite) but its **exit test has not been run** (needs a live-run budget).
- **Verification state (resumed 2026-10-06):** full gate on the M2 code: ruff ✅, mypy ✅, `mypy --strict` Kernel ✅,
  Kernel 95 passed at 100% line+branch coverage ✅, **all tests 199 passed, 1 skipped** ✅ (13.5 min).
  The first gate run on resume failed 1 test: `test_injected_fake_number_is_blocked[...certify-I1]`. Cause: a
  TEST bug, not a product bug. M2 added a conditional `certify → replicate` edge before `certify → report`; the
  test helper rewired only the first edge, the engine follows the first matching edge, so the planted
  state-edit node never ran. The helper now splices the planted node before ALL outgoing edges and asserts it ran
  exactly once (the other two variants had the same blind spot in principle).
- **Previous complete full-suite run:** 172 passed, 1 skipped (end of M1, commit 6438201).
- **State bundle:** see "State bundle" just below (code + 21-task suite + environment, frozen at a commit).
- **Next on resume:** (1) run the full gate; (2) one live Docker + Codex run on a suite task to validate M2 end to
  end; (3) user decision on the M2 exit-test budget (≈300 Codex calls, ≈5M tokens); (4) M3 evolution v1.

### State bundle
- File: `C:\Users\mykids\custom-harness-data\bundles\state-f15c3d4fd53f.zip` (1.1 MB, 132 members; NOT in git:
  it contains the suite's hidden test labels). Manifest committed: `snapshots/state-f15c3d4fd53f.manifest.json`,
  manifest sha256 `f724ae6aa625e464cfb580ac6b680757a5a56f37a04e7b878ef3d65f0560fbc9`.
- Contents: `code.tar` = `git archive` of commit `f15c3d4` (139 files); `suites/m2/` = all 21 tasks incl. private
  labels and canaries + `suite.yaml`; `env/` = pip freeze, Python 3.12.10 / Windows platform, Docker 29.8.1 +
  sandbox image id `sha256:187a5f90…dbaa7`, codex-cli 0.160.0. Integrity re-checked after writing: 0 hash mismatches.
- Rebuild without the zip: `chh build-suite OUT --docker` regenerates the same task data (deterministic seeds);
  only the random canary tokens differ.
- Tool: `python scripts/snapshot_state.py OUT SUITE... --note "..."` (refuses to run on a dirty tree).

## Environment
- Code: this folder (git repo → github.com/meta-lucky-builds/auto-sci-harness, private). Runtime data + venv:
  `C:\Users\mykids\custom-harness-data` (outside OneDrive).
- Python 3.12 venv: `C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe`
- Quality gate: `python scripts/check.py` (ruff, mypy on src/, `mypy --strict` on the Kernel, Kernel tests with a
  100% branch-coverage gate, then all tests). `--fast` skips the full suite.
- Tests: `python -m pytest -q` (~12 min; real-Docker tests, fuzzing, the 50-claim Kernel suite). Last COMPLETE run:
  172 passed, 1 skipped at the end of M1; the run after M2 was stopped (see STOPPING POINT).
  The skip is the native-symlink test: this Windows account cannot create symlinks; the same guard is covered by
  the junction test and the real-Docker planted-link test.
- Model access now: ChatGPT subscription via the official Codex CLI only.
- Docker Desktop 29.8.1 installed; sandbox image `custom-harness-sandbox:py312` built. AutoStart is off: start
  Docker Desktop before live runs.
- No GPU locally; cloud GPU to be rented when experiments need it.

## Milestones (GOAL §11)
| Milestone | State |
|---|---|
| **M0 Hardening** | **Done** (2026-10-06), second hardening pass the same day, see below. |
| **M1 Kernel v1** | **Done** (2026-10-06), see "M1" below. |
| M2 Research loop v1 | **Built** (2026-10-06), see "M2" below. **Exit test not yet run**: it needs live model runs on the suite (≈300 Codex calls / ≈5M tokens); waiting on the user's budget decision. |
| M3–M7 | Not started (pieces of M2/M3 exist from the earlier v0 build, see "Done before M0"). |

### M0 — what was built and how it is tested
| GOAL item | Built | Proof (test) |
|---|---|---|
| Infra vs policy failure (L1, CR P0-1) | `errors.py`; `RunStatus.INFRA_FAILED`; transient-only retry with backoff, every attempt charged; infra failures never routed through harness error edges; excluded (score None), never scored 0; Evaluator re-runs them; diagnosis ignores them | `test_infra.py`: a campaign with Docker outages (sandbox creation + exec failures) reaches the **same decision and harness** as a clean one |
| Agent cannot fake an infra failure | DockerSandbox = create → start → inspect → rm; exit codes alone never mean "infra" | real-Docker test: agent exits 125/126/127 → policy failure |
| Gate unit alignment (CR P0-5) | `paired_sample` (same unit list, no duplicates, exclusion drops the pair); gate fails closed above 25% excluded or < 2 valid units; evolve/gate task overlap is an error | `test_infra.py`, `test_fail_closed.py` |
| Winner's curse (CR P0-2) | race winner and parent re-measured on fresh seeds; race data never acceptance evidence | `test_winners_curse.py` (null candidates, 400 trials, Student-t): reused race data pass the evolve check **9.25%**, fresh re-measure **2.75%** (nominal 2.5%); end-to-end false accepts ≤ 5% |
| Safe snapshot restore (CR P0-3) | preflight → staging → archive → swap → rollback; modes, empty dirs, in-workspace links; fingerprint reuse; v1 manifests still restore | `test_snapshot.py`: corrupted/missing blob and failed swap leave the workspace intact; tampered manifests rejected |
| Host escape (GOAL L0 done-when) | `sandbox.base.safe_path` for every host-side workspace access: refuses symlinks, junctions, reparse points, unreadable entries, `..`, absolute paths | real-Docker test: agent code links its output to the hidden labels → refused, recorded as the agent's failure |
| Resume (CR P0-4) | `Engine.resume`, `chh resume`; refuses live runs (pid check, never signals), finished runs, genuine harness failures, other specs | `test_resume.py`: child process **hard-killed** mid-step, workspace corrupted, resume → identical trace prefix, exact workspace, same token total as a clean run |
| Budget persisted + wall clock (CR P0-6) | cumulative spend in every checkpoint; fork/resume restore it (no reset, no double charge); step cost = measured elapsed time; outage time uncharged | `test_resume.py` |
| Canary + leak audit (M0 list; K9 precursor) | `verify/leak.py`; per-run canary + task canary file; audit I5 over every trace string, the report and every workspace file; rendered prompts now traced; private values scrubbed from traces (any scrub fails the audit) | `test_leak.py`: clean run passes; labels copied into workspace, private value sent to a model, labels hard-coded in agent code → all caught; perfect predictions not flagged |
| Fail closed (§7, invariant 7) | hardened `score_state`, gate (non-finite, overflow), proposer parser | `test_fail_closed.py` (Hypothesis fuzzing): gate, score, proposer parser, LLM contracts, judge ranking; injected fake number → audit I4 blocks it |

Bugs found by M0's own tests and fixed: proposer crashed on a `null` reply; gate crashed on overflowing
differences; a container-made symlink (WSL reparse point on Windows) would have crashed snapshotting;
`_private*` values were written into traces; agent code could have faked infra failures via exit codes.

**M0 second pass (commit fed6081):** the T2 seed check was an UNPAIRED percentile bootstrap over 3 values (both arms
use the same seeds) → now a paired Student-t interval (t distribution implemented stdlib-only, matches tables to 1e-6);
zero seed spread is flagged degenerate and never counted; test-example intervals are BCa averaged over all seeds
(fast path: 2.5 s → 0.13 s); gate/race CIs are Student-t (2 gate units can no longer "show" significance); metrics
fail closed (strict lengths, nan/inf, empty); a write-ahead cost journal recovers the spend of a hard-killed step
(explicit "interrupted" trace row); campaign budget persisted across processes (`chh evolve --campaign-id --max-*`);
ruff + mypy clean on all of `src/`.

## M1 — Evidence Kernel v1 (GOAL §4, `src/custom_harness/kernel/`, 1,271 LoC, no model call reachable)
| GOAL item | Built | Proof (test) |
|---|---|---|
| Claim Language (§4.2) | `claim.py`: typed claim (kind, subject/comparator by hash, metric, min effect, scope, development vs generalization targets, analysis plan, assumptions, risk, resolve_by); canonical JSON; NaN/inf impossible | `test_kernel_units.py` |
| Pre-registration commits | `ledger.py`: append-only hash chain (register_dataset/artifact, prereg, data_access, certificate, bundle, retraction); `vault.py`: datasets with public/private files, every private read logged first | ledger tamper/ordering tests; K2 scenarios |
| K0–K9 (§4.3) | `checks.py`, deterministic, fail closed (a crashing check = failed check). K4 = valid procedure (exact seeds, every held-out evaluation reported, Bonferroni over max(declared, all preregs on the target)); K6 = criterion met on never-used targets; K7 = P(success given no effect) ≤ 2^-2 from the target's resampling noise (and the declared prior) | 50-claim suite + units |
| Certificates + retraction (§4.4) | `certificate.py`, `kernel.py`: immutable, content-addressed; CERTIFIED-VALIDATED (external/future targets) / CERTIFIED-INTERNAL / REJECTED; tier; assumptions marked tested/untested; VG-ladder text; retraction entries | units, VALIDATED vs laundering tests |
| Prediction ledger + VDS (§4.5) | `predictions.py`: every prereg is a prediction; log-score information gain vs the K7 no-effect probability; expired = miss; Brier + reliability | `test_prediction_scoring` |
| Reproducibility Bundle (L2) | `bundle.py`: zip of manifest + blobs (claim, certificate, ledger excerpt up to the certificate, evidence, runs + full traces, code, data, predictions); `chh bundle`, `chh reproduce` | `test_kernel_bundle.py`: reproduces with the original store DELETED; rejected stays rejected; tampered code/ledger detected; CLI |
| Reporting checklist (§1.3) | `checklist.py`: 16 VG items; a missing item blocks publication; "not performed" is an answer | `test_checklist_blocks_missing_items` |
| Exit (a): seeded 50-claim suite | `tests/test_kernel_suite.py`: 10 true (improvement, ablation, negative result, equivalence, 2 targets, 5 seeds), 8 p-hacked, 8 leaked, 8 irreproducible, 8 strawman, 8 trivial, + 1 honest overclaim | **all 51 classified correctly, each rejection by the expected check**; full failure sets audited (extra failures are explained cascades) |
| Exit (b): 100% branch coverage, no LLM | `scripts/check.py --fast` coverage gate; `test_kernel_quality.py` imports the Kernel in a clean interpreter and asserts no gateway/runtime/research/evolve/httpx module is loaded; `mypy --strict` | **100% line and branch coverage** of all 11 Kernel modules |

Found while building M1 and fixed: a claim with NaN could not be hashed (now impossible to construct); `reproduce`
crashed on a bundle missing an evidence payload (now reported); the internal test set re-registered under a new id
as "external" would have earned CERTIFIED-VALIDATED (K6 now rejects identical content under another id);
`config.data_dir()` crashed without a home directory. Suite-design errors caught by auditing full failure sets: a
"seed-dependent" method that was not (data too separable), and one scenario whose "true" claim was not true at its
alpha (now a true claim; the overclaim is kept as an explicit rejected case).

**Kernel v1.1 (commit 9e41e43): six loopholes found by red-teaming the Kernel, each first demonstrated by a
failing test** (`tests/test_kernel_exploits.py`): labels laundered as a public input; peeking via a throwaway
(unregistered) claim; certify used as a free look at the labels; forged certificates (lookup now hash-checked,
plus `Kernel.verify()` re-derivation); unbounded re-execution tolerance; a predictable replay seed (now sampled
from a hash of the evidence).

## M2 — Research loop v1 (GOAL §5 L4, §11) — built; exit test pending
| GOAL item | Built | Proof (test) |
|---|---|---|
| Kernel-integrated loop | `research/claims.py`, `examples/research_v1.yaml`: best dev candidate → typed claim (minimum effect = max(task floor, ½ × dev 90% lower bound); probability from dev) pre-registered before any label read → both arms × 3 seeds confirmed in fresh sandboxes through the vault → Kernel certificate (with re-execution) → bundle + checklist → report rendered from the ledger → audit | `test_research_loop_end_to_end`: CERTIFIED-INTERNAL, audit passed, publishable, bundle reproduces |
| Report numbers resolve to artifacts (invariant 8) | report rendered only from ledger + evidence; audit I4 re-renders it byte-identically and recomputes every held-out score | `test_injected_fake_number_is_blocked` × 3 routes (fabricated evidence → K3; edited state → I1; edited report → I4) |
| Experiment tree: improve operator + dev racing | `improve` node refines the best solution (up to 2×); a refinement replaces the incumbent only if a paired dev interval excludes 0 | `test_improve_operator_runs_and_dev_racing_refuses_a_non_improvement`, `test_dev_racing_rule` |
| Socratic critic (question-only, enforced in code) | `critic` node + `research.socratic_filter` (single question, no code, no directive opening; dropped items counted); questions fed to implement/improve | `test_socratic_filter_keeps_only_questions` |
| Independent replication agent | `replicate` (sees claim + idea + profile, never the code) → dev check → near-copy refusal (line Jaccard ≥ 0.8) → own claim at the SAME minimum effect → confirm → certify. Headline = both certified; evolution score 0.5·certified + 0.5·replicated + 0.5·clip(rel) | `test_independent_replication_certifies_the_headline_without_seeing_the_code`, `test_failed_replication_means_no_headline`, `test_a_copied_replica_is_not_an_independent_replication` |
| Richer data profiling (ideation is conditioned on it) | roles (id-like / constant / binary / coded-categorical / continuous), IQR + z outliers, skew, feature-target association ranking (η² / abs Pearson), redundant pairs, leak / duplicate / imbalance flags | `test_profile.py` |
| ObservationPack context | truncated values archived as blobs with handles recorded in the trace; `{{x\|log}}` verified-quote log reducer with raw fallback | `test_context.py` |
| ML task suite ≥ 20 tasks | `research/suite.py`, `chh build-suite OUT --docker`: 12 synthetic (6 families × 2), 5 real (iris, wine, breast cancer, digits, diabetes; built inside the sandbox, offline), 4 ML-repair tasks with known defective baselines; 13/4/4 evolve/gate/sealed | `scripts/validate_suite.py`: **all 21 usable**, every baseline runs in Docker, a generic random-forest reference beats every baseline |
| Evolution protections for the new loop | protected nodes: setup, claim, confirm, certify, replicate, replica_*, report, audit; edits may not write reserved state keys or reuse protected modules | `test_edits_cannot_fake_kernel_state_or_duplicate_protected_modules` |

Bugs and loopholes found while building M2 (all fixed, each with a test): an infrastructure retry of `confirm` would
repeat held-out evaluations and get an honest claim REJECTED by K4 (confirm is now idempotent; verified the test
fails without the fix); claims asserted the weakest bound and K7 correctly rejected them as trivially safe (claims
now assert what dev supports); suite tasks sharing a name collided in the vault (ids are now namespaced by a content
digest); an evolved prompt could have shown the replicator the code, or an added node could have written the
original code as the "replica" (replication nodes protected, `replica_impl` reserved, near-copy refusal); the new
profiler called continuous features "IDs — do not use" (role logic fixed and tested); `reproduce` crashed when a
bundle lacked the certificate blob (now reported); the E6 exploit test was flaky by design (rewritten to be
deterministic in outcome).

## Done before M0 (v0 build)
| Layer | What | Where |
|---|---|---|
| L0 | Typed harness spec + graph interpreter; full traces; per-step checkpoints; fork; contracts with bounded retries; failed attempts costed | `runtime/` |
| L0 | Sandboxes: Docker (no network, caps dropped, non-root, read-only rootfs, gVisor if present) + dev-only local | `sandbox/` |
| L0 | Gateway: mock, OpenAI-compatible (API key / Ollama / vLLM), Codex CLI (BYOS); strict-schema conversion; secret redaction | `gateway/` |
| §14 | Provider registry with BYOS gate, override file, training-on-outputs flags | `gateway/providers.yaml` |
| L1 | Budget ledger in provider-neutral units, 80% warning, hard caps | `budget.py` |
| L2 | Append-only evidence store (SQLite dev), content-addressed blobs, claims only from evidence, records table | `store/` |
| L3 | Tiers T0–T6 + tier policy; T1 metrics; T2 seed bootstrap + paired test-example bootstrap; Bradley-Terry | `tiers.py`, `verify/` |
| L4 | Research loop v0 (setup → profiling → baseline → hypotheses → K-way tournament → implement/fix → hidden-test verify → report → audit I1, I2, I4, I5) | `research/`, `examples/research_v0.yaml` |
| L6 | Typed edits, static checks (protected nodes, leak patterns, audit-only terminal), diagnosis, LLM proposer with predicted effects, racing, re-measure, gate, lineage incl. rejects, ledger, calibration | `evolve/` |

## Verified live
- Codex BYOS smoke (`examples/smoke.yaml`): ~16k input tokens of Codex overhead per call, ~13–15 s per call.
- First live research run in Docker (rings-v1, Codex BYOS), before M0: verified 0.956 vs 0.351 baseline accuracy
  (test-example 95% CI [+0.57, +0.64]), audit passed; 9 Codex calls, ~158k input tokens, ~6 min. Easy synthetic task:
  proves the pipeline, not research ability.
- Re-run after M0 (run_e305a0283a074fbb, Docker create/start/inspect path, Codex BYOS): completed, 23 steps; 5/5
  hypotheses produced working code (dev 0.927–0.953 vs baseline 0.367); verified 0.958 vs 0.351, test-example 95% CI
  [+0.576, +0.639]; audit I1, I2, I4, I5 passed; all 6 LLM steps have rendered prompts in the trace; canary absent from
  the trace; no infra retries, no redactions. 9 Codex calls, 154k input + 16k output tokens, 498 s wall.
- **First live run of research_v1** (2026-10-06, `run_cd99288e65ba4a4f`, task `wine_knn_unscaled`, Codex BYOS in all
  roles, Docker). **Invariant 5 not met** (one model family judges its own proposals; see Known limitations).
  Completed, 33 steps, 12 Codex calls, 224,159 input + 29,939 output tokens, 848 s wall. Every LLM node returned
  schema-valid JSON (0 contract failures; the replicate node was never reached). Held-out (54 rows, 3 seeds): subject
  mean accuracy 0.957, comparator 0.704, effect +0.253; two-sided 90% intervals (lower bounds = one-sided 95%
  bounds, Bonferroni family of 1): BCa bootstrap over examples [+0.167, +0.358], paired Student-t over seeds
  [+0.235, +0.271] (not degenerate). Certificate **REJECTED**:
  K0–K6, K8 pass; **K7 fail** (claimed minimum effect 0.02 = the task floor; success probability without an effect
  0.365 > 0.25, 1.45 bits < 2.0); **K9 fail** (false positive, below). Audit: I1, I2, I4 pass, I5 fail (same false
  positive). `chh reproduce` of its bundle in Docker on a clean store: reproduced, REJECTED → REJECTED, 0 problems.
  - K9/I5 false positive: all 7 candidates were perfect on the 24-row dev split, so their predictions blob had the
    same content hash as the private dev-labels blob, and the trace scrubber redacted it as a private value
    (redaction counts per step = perfect attempts + 1, verified). Fixed: the private dev labels are stored wrapped
    (`research/modules.py` `_dev_labels`), so no predictions blob can share their ref. Test
    `test_perfect_dev_predictions_are_not_a_leak` fails without the fix (verified by stashing it) and passes with it.
  - K7 rejection is correct: the claim rule (δ = max(floor, ½ × dev 90% lower bound)) had almost no dev information
    (baseline 22/24, every candidate 24/24), so it fell back to the 0.02 floor. Changing the dev estimate (e.g.
    cross-validation) is a claim-policy decision pending with the user.
- Finding for M1 (K4): the seed CI was [+0.607, +0.607]. The generated solution ignores the seed, so the seed check
  was degenerate and the evidence rested on the test-example bootstrap alone. K4 must detect zero seed variance
  and say so instead of counting it as a passed check.

## Known limitations (honest)
- Evolution only tested with scripted LLMs; no evidence yet that a real proposer finds good edits (M3).
- The evolution objective is not yet VDS: `evolve/evaluate.py` scores 0.5·certified + 0.5·replicated +
  0.5·clip(relative held-out improvement) from Kernel certificates (M2). VDS as the objective = M3, once enough
  predictions resolve.
- **Invariant 5 (critic ≠ proposer family) is NOT met.** With Codex as the only backend, three places in the M2 loop
  judge the proposer's work with the same model family: the Socratic `critic` node, the hypothesis `tournament`
  (`research/modules.py`, `ctx.llm("judge", …)`), and the replication agent (same family as the original author).
  No code checks model family anywhere: the gateway's "same-family detection" (GOAL §5 L0) does not exist. Fixing it
  needs a second model family (API key or open-weights model; user decision). Until then live results are labelled
  "invariant 5 not met".
- **Invariant 9 incomplete:** Reproducibility Bundles are built only by `research.certify` (and `chh bundle`), so a
  run that stops before certifying has no bundle. Every run does have a cost record.
- **Held-out examples shared across suite tasks (found 2026-10-06, verified on public files):** each real-data task
  and its repair variant are re-splits of the same base dataset. Every test example of `wine-real` (54/54),
  `breast_cancer-real` (171/171), `digits-real` (540/540) and `diabetes-real` (133/133) appears in the paired repair
  task's public test or TRAIN file (e.g. 38 of wine-real's 54 hidden test labels are public in
  `wine_knn_unscaled/public/train.csv`). Today each pair sits in the same split, but only by luck of the cyclic split
  assignment (`research/suite.py`), which does not group tasks by base dataset. The Kernel identifies held-out sets
  by dataset id and exact bytes, so K6 ("never used") and the Bonferroni family cannot see overlapping examples under
  different ids. Single runs are unaffected (a run sees only its own task); cross-task paths (evolution, future
  memory) are not. Fix options pending with the user.
- **Invariant 10 not implemented:** runs and loops carry no MECHANICAL / VALIDATED label (GOAL §6.1).
- Kernel v1 deviations from GOAL §4: untested assumptions are listed, not yet tier-lowering; label SUPPORTED is
  never issued (no judge/retrospective evidence yet); kinds scaling_law/rediscovery are typed but rejected (K1);
  Bonferroni (conservative) rather than Holm; bundles are per certified claim, not yet per run; a ledger excerpt in a
  bundle is checked per entry and for order, not as an unbroken chain from genesis.
- Dataset roles (external/future) are declared by whoever registers the dataset. Content laundering is caught
  (identical bytes), but a role is only as trustworthy as its registrant: registering external/future data should
  become a human-approved (L9) action.
- K7's noise model is a normal approximation from the resampling interval; K9 detects verbatim leaks only.
- Test-set reuse: a target may serve several registered claims; K4 charges it as Bonferroni over every claim
  registered on that target. This is the standard practical defence, NOT a guarantee against adaptive reuse (later
  claims chosen after seeing earlier results; cf. reusable-holdout literature). SEALED targets used once via
  `finalize` (L6, M3) remain the strong form.
- Bundles contain the hidden labels of their targets (needed to re-check); they must never be agent-visible.
- The ledger is tamper-EVIDENT (hash chain), not tamper-proof: whoever controls the database can rewrite it.
  Publishing the head hash (bundles, commits) pins it.
- Few tasks per split → wide CIs → conservative gate. Needs the ≥ 20-task ML suite (M2).
- Resume: model-call spend of a killed step is recovered from the cost journal; other spend inside it (e.g. sandbox
  wall time) is not. Evidence rows it wrote stay unreferenced. Modules re-run on resume/retry must be idempotent on
  the workspace (snapshot restore makes them so for files).
- Leak audit detects verbatim leaks and copies only; transformed labels or information leaked through repeated
  metric probing need evaluator-side defences (L3).
- Report numbers: I4 recomputes claim evidence, but numbers in report prose are not yet each resolved to an
  artifact (invariant 8 complete only when reports are generated from Kernel certificates in the loop, M2).
- Subscription mode is slow and quota-bound; real evolution campaigns need API or open-weights backends.
- I3 (citation check) is n/a until the literature module exists. SQLite only; Postgres before scorecard campaigns.

- M2 exit test not run: needs ≈300 live Codex calls on the 21-task suite (user budget decision). Until then the
  loop's research quality is unmeasured; only its integrity machinery is tested (with scripted models).
- Test-set wear: every claim (and replication) on a task's test set enlarges its Bonferroni family, so a task's
  test set becomes harder to certify on as campaigns reuse it. Campaign specs should pre-declare n_comparisons
  (the claim node accepts it) to keep strictness constant during a campaign; synthetic tasks can be regenerated.
- Small real test sets (iris 45 rows, wine 54) make K7 reject most claims there: correct, but those tasks are
  close to uncertifiable.
- Replication independence is enforced structurally (no code in the replicator's prompt, protected nodes,
  near-copy refusal on line overlap); deeper copying (renamed variables) would pass the overlap check, and with a
  single model family (Codex only today) the replicator shares the original author's biases.
- A model backend raising an unexpected exception type crashes the run (deliberately loud); in a campaign that
  aborts the campaign.

## Next (GOAL §11)
1. **M2 exit test** (user decision on budget): run the loop live on the suite; measure the CERTIFIED-or-NEGATIVE
   rate, integrity failures (target 0), calibration of the claim forecasts, and cost per certified claim.
2. **M3 Evolution v1**: evidence export, agentic proposer, structured diagnosis, family-separated critic, smoke
   run, ledger upgrades, cost term (GOAL §11).
