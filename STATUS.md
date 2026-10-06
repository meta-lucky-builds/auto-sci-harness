# Build status

Living checklist against `GOAL.md` (the source of truth; milestones in GOAL §11). Updated 2026-10-06.

## Environment
- Code: this folder (git repo → github.com/meta-lucky-builds/auto-sci-harness, private). Runtime data + venv:
  `C:\Users\mykids\custom-harness-data` (outside OneDrive).
- Python 3.12 venv: `C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe`
- Quality gate: `python scripts/check.py` (ruff, mypy on src/, `mypy --strict` on the Kernel, Kernel tests with a
  100% branch-coverage gate, then all tests). `--fast` skips the full suite.
- Tests: `python -m pytest -q` → 172 passed, 1 skipped, ~10 min (includes real-Docker tests, fuzzing, the 50-claim Kernel suite).
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
| M2 Research loop v1 | Next: Kernel-integrated loop, experiment tree, replication agent, ≥ 20-task ML suite. |
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
- Finding for M1 (K4): the seed CI was [+0.607, +0.607]. The generated solution ignores the seed, so the seed check
  was degenerate and the evidence rested on the test-example bootstrap alone. K4 must detect zero seed variance
  and say so instead of counting it as a passed check.

## Known limitations (honest)
- Evolution only tested with scripted LLMs; no evidence yet that a real proposer finds good edits (M3).
- The Kernel is not yet wired into the research loop or the evolution objective: `research.verify` still uses the
  M0 statistics, and `evolve/evaluate.py` still scores a placeholder. Kernel-integrated loop + VDS objective = M2/M3.
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

## Next (GOAL §11)
1. **M2 Research loop v1**: the loop forms a typed claim from its dev results, pre-registers it, confirms through
   the vault, gets a Kernel certificate, and writes the report + checklist + bundle from it; experiment tree with an
   improve operator; replication agent; Socratic critic; richer profiling; ObservationPack context; ≥ 20-task ML
   suite (rings-style + real datasets + ML-repair family). Exit: ≥ X% CERTIFIED-or-NEGATIVE, 0 integrity failures.
2. Re-run the live rings task after M2 so the first real certificate and bundle come from the live loop.
