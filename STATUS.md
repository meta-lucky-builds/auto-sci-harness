# Build status

Living checklist against `GOAL.md` (the source of truth; milestones in GOAL §11). Updated 2026-10-06.

## Environment
- Code: this folder (git repo → github.com/meta-lucky-builds/auto-sci-harness, private). Runtime data + venv:
  `C:\Users\mykids\custom-harness-data` (outside OneDrive).
- Python 3.12 venv: `C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe`
- Tests: `python -m pytest -q` → 74 passed, 1 skipped (~3–4 min; includes real-Docker tests and fuzzing).
  The skip is the native-symlink test: this Windows account cannot create symlinks; the same guard is covered by
  the junction test and the real-Docker planted-link test.
- Model access now: ChatGPT subscription via the official Codex CLI only.
- Docker Desktop 29.8.1 installed; sandbox image `custom-harness-sandbox:py312` built. AutoStart is off: start
  Docker Desktop before live runs.
- No GPU locally; cloud GPU to be rented when experiments need it.

## Milestones (GOAL §11)
| Milestone | State |
|---|---|
| **M0 Hardening** | **Done** (2026-10-06), see below. |
| M1 Kernel v1 | Next. |
| M2–M7 | Not started (pieces of M2/M3 exist from the earlier v0 build, see "Done before M0"). |

### M0 — what was built and how it is tested
| GOAL item | Built | Proof (test) |
|---|---|---|
| Infra vs policy failure (L1, CR P0-1) | `errors.py`; `RunStatus.INFRA_FAILED`; transient-only retry with backoff, every attempt charged; infra failures never routed through harness error edges; excluded (score None), never scored 0; Evaluator re-runs them; diagnosis ignores them | `test_infra.py`: a campaign with Docker outages (sandbox creation + exec failures) reaches the **same decision and harness** as a clean one |
| Agent cannot fake an infra failure | DockerSandbox = create → start → inspect → rm; exit codes alone never mean "infra" | real-Docker test: agent exits 125/126/127 → policy failure |
| Gate unit alignment (CR P0-5) | `paired_sample` (same unit list, no duplicates, exclusion drops the pair); gate fails closed above 25% excluded or < 2 valid units; evolve/gate task overlap is an error | `test_infra.py`, `test_fail_closed.py` |
| Winner's curse (CR P0-2) | race winner and parent re-measured on fresh seeds; race data never acceptance evidence | `test_winners_curse.py` (null candidates, 150 trials): reused race data pass the evolve check **18%**, fresh re-measure **4%** (nominal 2.5%); end-to-end false accepts ≤ 5% |
| Safe snapshot restore (CR P0-3) | preflight → staging → archive → swap → rollback; modes, empty dirs, in-workspace links; fingerprint reuse; v1 manifests still restore | `test_snapshot.py`: corrupted/missing blob and failed swap leave the workspace intact; tampered manifests rejected |
| Host escape (GOAL L0 done-when) | `sandbox.base.safe_path` for every host-side workspace access: refuses symlinks, junctions, reparse points, unreadable entries, `..`, absolute paths | real-Docker test: agent code links its output to the hidden labels → refused, recorded as the agent's failure |
| Resume (CR P0-4) | `Engine.resume`, `chh resume`; refuses live runs (pid check, never signals), finished runs, genuine harness failures, other specs | `test_resume.py`: child process **hard-killed** mid-step, workspace corrupted, resume → identical trace prefix, exact workspace, same token total as a clean run |
| Budget persisted + wall clock (CR P0-6) | cumulative spend in every checkpoint; fork/resume restore it (no reset, no double charge); step cost = measured elapsed time; outage time uncharged | `test_resume.py` |
| Canary + leak audit (M0 list; K9 precursor) | `verify/leak.py`; per-run canary + task canary file; audit I5 over every trace string, the report and every workspace file; rendered prompts now traced; private values scrubbed from traces (any scrub fails the audit) | `test_leak.py`: clean run passes; labels copied into workspace, private value sent to a model, labels hard-coded in agent code → all caught; perfect predictions not flagged |
| Fail closed (§7, invariant 7) | hardened `score_state`, gate (non-finite, overflow), proposer parser | `test_fail_closed.py` (Hypothesis fuzzing): gate, score, proposer parser, LLM contracts, judge ranking; injected fake number → audit I4 blocks it |

Bugs found by M0's own tests and fixed: proposer crashed on a `null` reply; gate crashed on overflowing
differences; a container-made symlink (WSL reparse point on Windows) would have crashed snapshotting;
`_private*` values were written into traces; agent code could have faked infra failures via exit codes.

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
- The run objective in `evolve/evaluate.py` is a placeholder until the Kernel/VDS exist (M1+).
- Percentile bootstrap at small n is slightly anti-conservative (4% vs nominal 2.5% in the simulation, within noise
  of 150 trials). Consider BCa or t-based intervals for small samples.
- Few tasks per split → wide CIs → conservative gate. Needs the ≥ 20-task ML suite (M2).
- Resume: a step killed mid-flight leaves no trace row, so spend inside it before the kill is not recorded;
  evidence rows it wrote stay unreferenced. Modules re-run on resume/retry must be idempotent on the workspace
  (snapshot restore makes them so for files).
- Budget persistence is per run (checkpoints). A campaign-level persisted budget is still to do (L1).
- Leak audit detects verbatim leaks and copies only; transformed labels or information leaked through repeated
  metric probing need evaluator-side defences (L3).
- Report numbers: I4 recomputes claim evidence, but numbers in report prose are not yet each resolved to an
  artifact (invariant 8 complete only when reports are generated from certificates, M1).
- Subscription mode is slow and quota-bound; real evolution campaigns need API or open-weights backends.
- I3 (citation check) is n/a until the literature module exists. SQLite only; Postgres before scorecard campaigns.

## Next (GOAL §11)
1. **M1 Kernel v1**: Claim Language (ML); pre-registration commits; K0–K9 (K6 on sealed internal data);
   certificates; Reproducibility Bundle + `chh reproduce`; reporting checklist; prediction ledger. Exit: seeded
   50-claim suite classified correctly; `chh reproduce` works.
2. (done) Live rings re-run after M0 passed; see "Verified live".
