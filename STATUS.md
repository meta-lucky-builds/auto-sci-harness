# Build status

This is the living checklist against `GOAL.md`, which is the source of truth (milestones are in GOAL §11).
It was fully rewritten on 2026-10-07.

Every statement here is labelled. **verified** means the output was seen (a test run, a gate log or a store dump).
**inferred** means it was reasoned from the code, not run. **reported** means a builder agent ran it and I did not
re-run it.

Companion files:
- `HANDOFF.md` and `HANDOFF2.md`: operating knowledge, fix designs and traps.
- `uncommitted.md`: unmerged branches and what each needs.
- `liverun1.md`: every metric of both live research_v1 runs.

---

## ▶ Where the build is on GOAL.md right now

```
M0 Hardening ✅ ── M1 Kernel v1 ✅ ── M2 Research loop v1 🟡 ── M3 Core/adapter split + T0 + approval ◀ WE ARE HERE ── M4 Evolution v1 (pieces) ── M5 … M9 ✗
                                      (built; exit test        (about half built: adapters, evidence classes,
                                       not run: needs budget)   approval node, physical path, LP solver on a branch;
                                                                 Lean T0 and simulator adapters NOT started)
```

**Direction.** The active milestone is **M3** (GOAL §11). Two other kinds of work run in parallel:
- an **integrity-repair track**, closing the R1/R2 review findings before anything builds on them;
- **M4 pieces** built early on branches (the LOFO/LODO gates are merged; the evidence export, diagnosis and cost
  term are on `m3-b3-evolve`).

**M2 is functionally complete but not exited.** Its exit test ("≥ X% CERTIFIED-or-NEGATIVE, 0 integrity failures")
needs about 300 live model calls, a budget the user has to approve. The two live runs so far are validation runs,
not the exit test. Both were REJECTED, and both rejections were correct; see liverun1.md.

M3 exit test (GOAL §11 + §5 "Domain adapters" done-when), item by item:

| M3 requirement | State | Label |
|---|---|---|
| Kernel core + adapter interface, adapter registry (versioned, content-hashed, human-approved, validation required) | built (`kernel/adapters.py`), on main | verified (tests; gate 281 passed) |
| ML adapter = today's code, extracted | registered with 9 fault-injection checks. The Kernel core still holds ML logic in K3–K7 (not yet a domain-agnostic core). The adapter hash ignores imported modules (R1-F5, open) | verified (tests); the "agnostic core" gap is inferred from reading `checks.py` |
| Evidence classes in claims and certificates (§4.7) | built, on main | verified |
| Lean adapter (T0): build, comparator, axiom audit, statement fidelity | **NOT STARTED**. It needs Lean 4 + Mathlib on the user's machine / in the sandbox image | — |
| One exact-solver adapter (T1, re-checkable certificates) | `kernel/lp.py` (exact rational LP optimality), `lp` adapter, label CERTIFIED-EXACT. On branch `m3-b1-compute`, **not merged**, full suite not run, no loop produces such claims yet | reported (Kernel 109 passed, 100% coverage) |
| One simulator adapter (simulation-scoped claims) | **NOT STARTED** | — |
| Approval node (PAUSED → human approve/reject, hash-bound, ledgered) | built, on main | verified (`test_approval.py`, 12 tests) |
| Physical-claim dry run: no certificate without human approval; a model or evolved node cannot approve; attestation listed as untested | built, on main | verified (`test_physical.py`: 5 test functions, one parametrised, including `test_evolution_cannot_touch_the_physical_path` and the untested-attestation assertion) |
| "An edit that touches an adapter is rejected statically" | **no dedicated check or test.** Harness edits are typed spec edits and cannot change Python source, so adapter files are out of reach. Nothing in `evolve/` names adapters | inferred, not tested: GAP vs the done-when |
| Same Kernel core certifies ML + Lean + solver + simulator claims | ML only on main; solver on a branch; Lean and simulator missing | verified (ML) |

**Conclusion.** M3 is not done. Remaining M3 work:
1. Merge the solver adapter.
2. Build the Lean T0 adapter (needs the user's machine).
3. Build a simulator adapter.
4. Fix R1-F5 (adapter hash closure).
5. Add the static adapter-edit test.
6. Move the ML-specific parts of K3–K7 behind the adapter interface.

---

## Verification state (2026-10-07)

- **`main` = 9be3b84**: m3-adapters merged after its full gate. **verified** gate on 20cb09e (the merged tree
  differs from it only in 3 Markdown files):

  | Gate step | Result |
  |---|---|
  | ruff | ✅ |
  | mypy | ✅ |
  | `mypy --strict` on the Kernel | ✅ |
  | Kernel tests | **101 passed at 100% line+branch coverage** ✅ |
  | All tests | **281 passed, 1 skipped** ✅ (20 min) |

- The skip is the native-symlink test. This Windows account cannot create symlinks; the same guard is covered by
  the junction test and the real-Docker planted-link test.
- Unmerged branches (all pushed; see uncommitted.md):

  | Branch | State |
  |---|---|
  | `m3-fix-gateway` | R2-3 fix; full gate not run |
  | `m3-b1-compute` | LP solver adapter; full suite not run |
  | `m3-fix-r1` | R1 fixes; WIP, failing |
  | `m3-b3-evolve` | M4 pieces; not reviewed, full suite not run |
  | `m3-r1-kernel`, `m3-r2-loop` | failing review tests only; do not merge |

- Merge order (each step gated): gateway → B1 → R1 fixes → B3 → R2 fixes.

### State bundle (frozen at f15c3d4, end of the M2 build)
- File: `C:\Users\mykids\custom-harness-data\bundles\state-f15c3d4fd53f.zip` (1.1 MB, 132 members).
  - Not in git: it contains the suite's hidden labels.
  - Manifest committed: `snapshots/state-f15c3d4fd53f.manifest.json`.
- Rebuild without the zip: `chh build-suite OUT --docker`. Seeds are deterministic; only the canaries differ.
- Note: the live suite has since been rebuilt (split isolation, LOFO), so this bundle holds the OLD suite.

## Environment
- Code: this folder (git → github.com/meta-lucky-builds/auto-sci-harness, private).
- Runtime data + venv: `C:\Users\mykids\custom-harness-data` (outside OneDrive).
  - Live store: `store`. The old store is archived as `store-v1`.
  - Live suite: `suites/m2`. The old suite is `suites/m2-v1`.
- Python 3.12 venv: `C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe`.
- Gate: `python scripts/check.py` runs ruff, mypy, `mypy --strict` on the Kernel, the Kernel tests with the 100%
  branch-coverage gate, then all tests (~20–28 min). `--fast` skips the full suite.
- Model access: ChatGPT subscription via the official Codex CLI only, so there is **one model family**.
- Docker Desktop 29.8.1 with sandbox image `custom-harness-sandbox:py312` (scikit-learn 1.9). AutoStart is off.
- No GPU.

## Milestones (GOAL §11)
| Milestone | State |
|---|---|
| **M0 Hardening** | **Done** 2026-10-06 (+ second pass) |
| **M1 Kernel v1** | **Done** 2026-10-06 (v1.1 after red-teaming; adapters + evidence classes added 2026-10-07) |
| **M2 Research loop v1** | **Built.** 2 live validation runs (both correctly REJECTED). **Exit test not run** (user budget decision, ≈300 calls) |
| **M3 Core/adapter split + T0 + approval** | **In progress (current).** See the table at the top |
| **M4 Evolution v1** | Pieces only. LOFO/LODO gates on main; export, diagnosis, cost term, ledger upgrades and novelty gate on `m3-b3-evolve` (unmerged). Not started: agentic proposer, smoke run, family-separated critic (needs a 2nd model family), first real campaign |
| M5–M9 | Not started |

---

## M0 — Hardening (done)
| GOAL item | Built | Proof (test) |
|---|---|---|
| Infra vs policy failure | `errors.py`; `RunStatus.INFRA_FAILED`; transient-only retry with backoff, every attempt charged; infra failures excluded (score None), never scored 0 | `test_infra.py`: a campaign with Docker outages reaches the **same decision and harness** as a clean one |
| Agent cannot fake an infra failure | DockerSandbox = create → start → inspect → rm; exit codes alone never mean "infra" | real-Docker test: exits 125/126/127 → policy failure |
| Gate unit alignment | `paired_sample`; the gate fails closed above 25% excluded or < 2 valid units | `test_infra.py`, `test_fail_closed.py` |
| Winner's curse | race winner and parent re-measured on fresh seeds | `test_winners_curse.py`: reused race data pass the check 9.25% of the time, fresh re-measure 2.75% (nominal 2.5%) |
| Safe snapshot restore | preflight → staging → archive → swap → rollback | `test_snapshot.py` |
| Host escape | `sandbox.base.safe_path` refuses symlinks, junctions, reparse points, `..`, absolute paths | real-Docker planted-link test |
| Resume | `Engine.resume`, `chh resume` | `test_resume.py`: hard-killed child, identical trace prefix, same token total |
| Budget persisted + wall clock | cumulative spend checkpointed; write-ahead cost journal | `test_resume.py` |
| Canary + leak audit | `verify/leak.py`, audit I5, private values scrubbed from traces | `test_leak.py` |
| Fail closed | hardened `score_state`, gate, proposer parser | `test_fail_closed.py` (Hypothesis fuzzing) |

**Second pass:**
- The T2 seed check is now a paired Student-t interval; zero spread is flagged as degenerate.
- BCa test-example intervals.
- Strict metrics.
- Campaign budget persisted across processes.

## M1 — Evidence Kernel (done; `src/custom_harness/kernel/`, no model call reachable)
| GOAL item | Built | Proof |
|---|---|---|
| Claim Language | `claim.py`: typed, canonical JSON, NaN impossible | `test_kernel_units.py` |
| Pre-registration | `ledger.py` (append-only hash chain), `vault.py` (every private read logged first) | ledger tamper/order tests, K2 scenarios |
| K0–K9 | `checks.py`, deterministic, fail closed | 50-claim suite + units |
| Certificates + retraction | CERTIFIED-VALIDATED / CERTIFIED-INTERNAL / REJECTED; tiers; untested assumptions | units |
| Prediction ledger | `predictions.py`: log-score information gain, Brier, reliability | `test_prediction_scoring` |
| Reproducibility Bundle | `bundle.py`, `chh bundle`, `chh reproduce` | `test_kernel_bundle.py` (store deleted; tamper detected) |
| Reporting checklist | 16 VG items; a missing item blocks publication | `test_checklist_blocks_missing_items` |
| Exit (a) 50-claim suite | 10 true, 8 p-hacked, 8 leaked, 8 irreproducible, 8 strawman, 8 trivial, + 1 overclaim | all 51 classified correctly by the expected check |
| Exit (b) coverage, no LLM | coverage gate; clean-interpreter import test; `mypy --strict` | 100% line+branch coverage |
| Adapters + evidence classes (2026-10-07, M3) | see M3 below | — |

## M2 — Research loop v1 (built; exit test pending)
| GOAL item | Built | Proof |
|---|---|---|
| Kernel-integrated loop | `research/claims.py`, `examples/research_v1.yaml`: dev → typed claim pre-registered before any label read → 2 arms × 3 seeds in fresh sandboxes → Kernel certificate (with re-execution) → bundle + checklist → report from the ledger → audit | `test_research_loop_end_to_end` |
| Report numbers resolve to artifacts (inv. 8) | report rendered from ledger + evidence; I4 re-renders byte-identically | `test_injected_fake_number_is_blocked` × 3 routes |
| Improve operator + dev racing | refinement replaces the incumbent only if a paired dev interval excludes 0 | `test_improve_operator_…`, `test_dev_racing_rule` |
| Socratic critic | question-only filter in code | `test_socratic_filter_keeps_only_questions` |
| Independent replication | replicator never sees the code; near-copy refusal; same minimum effect | 3 replication tests |
| Data profiling | roles, outliers, skew, association, leak/duplicate/imbalance flags | `test_profile.py` |
| ObservationPack context | archived truncations, verified-quote reducer | `test_context.py` |
| ML suite ≥ 20 tasks | 21 tasks (12 synthetic, 5 real, 4 ML-repair); rebuilt with split isolation + LOFO (spirals held out) | `scripts/validate_suite.py`: 21/21 usable, 0 isolation problems |
| Dev estimate = 5-fold CV (2026-10-07, user decision) | every public training row is a dev example once; each fold runs in a FRESH sandbox in the held-out layout | `test_dev_estimate_covers_every_public_training_row_once`, `test_dev_scoring_cannot_read_the_validation_labels` |
| Dev-time cap (after live run 2) | total dev wall time ≤ 2 × the per-run limit | `test_dev_evaluation_wall_time_is_capped_across_folds` |

CV caveats (stated in `claims.forecast`):
- The bootstrap ignores between-fold model variance.
- Dev selects the best candidate (winner's curse).

Both make the forecast optimistic. Neither is corrected; the prediction ledger measures calibration.

## M3 — built so far (on main unless stated)
| GOAL item | Built | Proof |
|---|---|---|
| Evidence classes + adapters (§4.7, inv. 13) | `Claim.evidence_class` (default `data` kept out of the hash); `kernel/adapters.py`: content-hash identity, fault-injection validation (9 ML checks), `adapter_approval` ledger entry naming a human, K1 refuses a claim without an approved, unchanged adapter; certificates and bundles carry adapters; `chh approve-adapter` (HUMAN only) | `test_k1_requires_a_human_approved_unchanged_adapter…`, `test_adapter_content_change_after_approval_invalidates_it`, `test_adapter_approval_refuses_…`, `tests/test_adapters_cli.py` |
| Approval node (inv. 15) | `ApprovalNode`, PAUSED, item bound by sha256, decisions only from ledger entries written by `chh approve` after an engine-recorded request; evolution cannot touch approval nodes | `tests/test_approval.py` (12) |
| Physical-claim path (§4.7, inv. 14–15) | `research/physical.py`, `examples/physical_v1.yaml`: typed protocol, generic prohibited-category screen (`policy_prohibited.yaml`; the human reviewer is the real screen), prereg before review, approval node, `chh return-data` (HUMAN), receipt approval, Kernel refuses at K1 (no physical adapter) | `tests/test_physical.py` (5 functions, one parametrised) |
| Exact LP solver adapter (T1) | **branch `m3-b1-compute`, unmerged**: `kernel/lp.py` (Fractions only), `CHECK_APPLICABILITY`, `COMPUTED_VALUE`, CERTIFIED-EXACT | reported: Kernel 109 passed, 100% |
| Lean T0, simulator adapter | not started | — |

**Operational consequences:**
1. Live runs can certify only after a HUMAN runs `chh approve-adapter ml --approver <name>`. Claude must not do
   this.
2. The live `suite.yaml` must have a held-out family. It has been regenerated.
3. The dev-time cap rejects solutions slower than about 2/5 of the per-run limit per fold.

## M4 pieces
| Item | Where | State |
|---|---|---|
| LOFO/LODO gates (inv. 16) | main | verified: `tests/test_lofo.py` (14). Per-family non-regression; a suite without a held-out family is refused |
| Evidence export, first-divergence diagnosis, cost term, ledger upgrades, novelty gate, decline | `m3-b3-evolve` | reported: 62 passed. Not reviewed by me. The cost default (1.0) is uncalibrated |
| Agentic proposer, smoke run, one-mechanism rule, family-separated critic, real campaign | — | not started |

## §16 Kernel items (workstream kstats, branch `s16-kstats`, Kernel 1.2)
| Item | State |
|---|---|
| §16.4 VDS e-process (p0 at registration, q < p0 refused, F2 regression, skill vs pinned reference) | built; verified by `tests/test_kernel_s16.py` (fail-first shown on f0f703f: old miss scored +0.910 bits) |
| §16.4 per-target alpha-accounts (human open, fixed slot level, retirement; n_comparisons path kept) | built; verified (`test_alpha_account_*`). No CLI command yet |
| §16.4 as-of `verify_status` with STALE vs forged (MR7) | built; verified (`test_mr7_*`). `verify()` stays strict |
| §16.4 K3 zero-failure reliability demonstration | built; verified (50%-flaky pass rate 0.270 old -> <= 0.02). Presets UNCALIBRATED |
| §16.5.3 Kernel side of the release ledger (`record_release`, transcript bound, exposure) | built; verified incl. F1 against real K4/K6 (`tests/test_kernel_f1.py`) |
| §16.5.8 closed reason codes | built; verified |
| §16.6 plan-code divergence in K4 | built; verified |
| §16.7 self-reported fields cap the tier | built (only: unapproved external/future role -> T4, UNCALIBRATED); verified |
| invariant 21: replayed evidence refused | built (K0); the `Provenance.replayed` path is code-read only until yield's records.py merges |
| Kernel size budget (< 3k non-comment lines) | FAILS: 3,496 lines (`test_kernel_is_small`); owner decision needed |

---

## 🔴 Red-team and loophole ledger (everything found so far)

How findings were proven. Each finding below was demonstrated by a test that FAILS on the pre-fix code, unless it
is marked otherwise. The red-team subagents were halted by safety classifiers several times. Every finding listed
was then reproduced or proven by me (verified) or by a builder agent (reported).

### A. FIXED and on main
| # | Found by | Loophole | Fix | Test |
|---|---|---|---|---|
| K-1…K-6 | Kernel red-team (v1.1) | labels laundered as a public input; peeking via a throwaway unregistered claim; certify used as a free look at labels; forged certificates; unbounded re-execution tolerance; predictable replay seed | lookups hash-checked + `Kernel.verify()`; replay seed hashed from the evidence; bounded tolerance; registration rules | `tests/test_kernel_exploits.py` |
| M1-a | M1 build | internal test set re-registered under a new id as "external" → CERTIFIED-VALIDATED | K6 rejects identical content under another id | units |
| M1-b | M1 build | claim with NaN unhashable; `reproduce` crashed on a missing evidence payload; `data_dir()` crashed without a home dir | each fixed | units |
| M2-a | M2 build | an infra retry of `confirm` repeated held-out evaluations → honest claim REJECTED by K4 | confirm idempotent | yes |
| M2-b | M2 build | claims asserted the weakest bound (trivially safe) | claim asserts what dev supports | yes |
| M2-c | M2 build | suite tasks sharing a name collided in the vault | ids namespaced by content digest | yes |
| M2-d | M2 build | an evolved prompt could show the replicator the code; an added node could write the original code as the "replica" | replication nodes protected, `replica_impl` reserved, near-copy refusal | 3 replication tests |
| M2-e | M2 build | the profiler labelled continuous features "IDs — do not use" | role logic fixed | `test_profile.py` |
| E1 | red-team pass | edges were unprotected: an evolvable node could be inserted between protected nodes | edges out of / into protected nodes must equal the parent's | `test_redteam_evolve.py` |
| E2 | red-team pass | a new module node could write reserved state | `RESERVED_WRITING_MODULES` | `test_redteam_evolve.py` |
| E3 | red-team pass | **replication independence broken**: a node before `claim` wrote `profile` with the code, which the protected replicate prompt rendered; also hypothesis text = code | `profile` reserved; brief recomputed from vault bytes; a brief with ≥ 2 verbatim code lines is refused | `test_redteam_evolve.py` |
| E4 | red-team pass | malformed edit paths crashed instead of being rejected | every op failure → `EditRejected` | yes |
| E5 | red-team pass | trusted modules crashed on malformed evolvable state | defensive parsing | yes |
| F1 | red-team pass | dev folds used different file names/paths than held-out runs (a program could behave differently on dev) | dev folds use the held-out layout | `test_redteam_loop.py` |
| F2 | red-team pass | a blank or multi-column predictions row crashed the run or was silently truncated | the Kernel's rule (exactly one value per row) | yes |
| F4 | red-team pass | near-copy check defeated by appending a comment to every line | token-sequence comparison | yes |
| F5 | red-team pass | model text with newlines injected a fake **Headline result** line into the report | model text rendered as one bounded line | yes |
| K7-gap | red-team pass | negative/equivalence claims got K7 `n/a` = pass, so vacuous claims ("does not improve by 0.9") were CERTIFIED | **decided (delegated): fail closed.** K7 fails these kinds; only "pass" counts | `test_redteam_kernel.py` |
| DEV-1 | live run 1 follow-up | **dev cheating**: a candidate copying labels from the workspace training file scored 1.0 on noisy rings | each fold in a fresh sandbox with only that fold's rows | `test_dev_scoring_cannot_read_the_validation_labels` |
| DEV-2 | review of the CV change | a wired node writing `dev_folds` was accepted (the old test's sneak node was unwired, so it proved nothing) | `dev_folds`, `dev_scheme` reserved; the test wires the node + positive control | `test_edits_cannot_fake_kernel_state…` |
| K9-FP | live run 1 | **false leak flag**: perfect dev predictions hashed like the private dev labels → K9/I5 failed an honest run | private dev labels stored wrapped | `test_perfect_dev_predictions_are_not_a_leak` |
| CERT-INFRA | fixing test_infra after CV | a Docker outage during certification issued a REJECTED certificate | `certify` re-raises `InfraError`; no certificate recorded | `test_infrastructure_outage_during_certification_issues_no_certificate` |
| SPLIT | my audit | **held-out examples of 4 real tasks were public (with labels) in their repair variants** | sources kept together; content-based dataset ids; K6 refuses targets whose examples appear in another public file; K4 family includes shared targets; suite rebuilt; old store archived | `test_tasks_from_one_dataset_share_a_split[...]`, suite-loading refusals |
| INV-5 | verification of claims | invariant 5 was stated but not enforced | `policy.py`, effective-binding check, recorded override = NOT MET | policy tests |
| INV-9/10 | verification of claims | per-run bundles and loop labels missing | `store/run_bundle.py`, `chh rerun`; MECHANICAL/VALIDATED labels | tests |
| DEV-TIME | live run 2 | one 5-fold dev evaluation took 2,423 s (cost blow-up) | cap at 2× the per-run limit | yes |
| TEST-BLIND ×3 | my reviews | tests that silently stopped exercising their attack: unreachable planted node, bundle tamper by position, unwired sneak node | every attack test now asserts the attack happened | the tests themselves |
| BUNDLED-DATA | my audit (2026-10-07) | **the sandbox image ships the real tasks' datasets**: scikit-learn bundles iris/wine/breast_cancer/digits/diabetes (the files the suite is exported from) and scipy/sklearn test fixtures hold more iris copies, so agent code in the candidate sandbox could load every labelled row, held-out rows included, with no network (5 real tasks + their repair variants; split isolation, K6 and the leak audit cannot see a copy that pre-exists in the environment). Verified: `load_iris()` returned (150, 4) in the default sandbox before the fix | candidate image `custom-harness-sandbox:py312-candidate` (`docker/sandbox-candidate.Dockerfile`, no downloads) with every bundled dataset removed; `DockerSandbox` defaults to it and fails closed (`CandidateImageMissing`) if it is missing or lacks its label; the full image only with `trusted_task_building=True`, used solely by the `build-suite` exporter; the exporter no longer leaves `_export/export.json` (all labelled rows) next to the suite. Branch `m3-fix-bundled-data` until merged | `tests/test_bundled_data.py` (loaders fail in the candidate / work in the exporter; AST check of every `DockerSandbox` construction; refusal of a missing or mis-tagged candidate; whole-filesystem scan by file type and by dataset rows, with a positive control on the full image; export cleanup) |

### B. FIXED on a branch, NOT on main
| # | Loophole | Branch | State |
|---|---|---|---|
| R2-3 | request-caused backend errors (context length, 413) classified as infrastructure → an edit can turn its own failures into excluded units | `m3-fix-gateway` | verified: 11 tests, shown failing on old code; full gate not run |
| B3-LEAK | proposer prompt's PAST EDITS built from raw `harness_edit` records including gate means and held-out-family (LOFO) results | `m3-b3-evolve` | reported by the agent; diff not reviewed by me |
| R1-F6 | `non_regression` raised on NaN | `m3-fix-r1` | WIP, not tested together |
| R1-F2 | the sampled re-execution seed can be steered with records on non-target datasets | `m3-fix-r1` | WIP (`replay_seed` uses plan-matched records only; K4 stray-record reason), not tested together |
| R1-F4 | byte-identical private labels under another id not counted in the Bonferroni family | `m3-fix-r1` | WIP (`related_targets`), not tested together |
| K2-TWO-RUN | **sequential runs on one task poison each other**: run 1's audit leakscan read makes every later claim on that task fail K2 (fails closed; blocks evolution and repeated runs) | `m3-fix-r1` | bug **verified** by `tests/test_probe_two_runs.py` (`[CERTIFIED-INTERNAL, REJECTED(K2)]`); fix written, not yet passing |

### C. OPEN — not fixed (designs in HANDOFF2.md §2)
| # | Severity | Loophole | Planned fix |
|---|---|---|---|
| R1-F1 | high | a label read under a registered claim on ANOTHER target escapes K2 and K4 | `vault.read` binds evaluate/kernel reads to the claim's targets; K2 excuses an early read only for a claim registered before it on a related target |
| R1-F3 | high | repeated certification can shop for a lucky replay of a flaky program | K3 fails if an earlier recorded certificate of the same claim failed K3 (re-certification after e.g. a K1 refusal stays allowed). Residual: a 50%-flaky program still passes its first try half the time |
| R1-F5 | medium | an adapter's hash ignores modules its modules import (ML adapter depends on `kernel.vault`, `sandbox.base`) | hash the transitive closure of project imports |
| R2-1 | high | a role-rebinding edit (author nodes run as `replicator`/`judge`) keeps invariant 5 "met" on paper and earns replication credit + a headline | `apply_edit` rejects role changes on existing LLM nodes and new LLM nodes outside proposer/executor |
| R2-2 | medium-high | evolution scores depend on evaluation ORDER through the growing Kernel multiplicity family (identical harnesses scored 1.0 vs 0.5) | campaigns must pre-declare `n_comparisons` ≥ the bound on claims per target |
| R2-4 | low | the generation label ignores the fresh re-measure units | include fresh units |
| R2-5 | low | an edit can render the unfiltered critic output (`critique_raw`) into prompts | reject such templates |
| R1 integrity purposes | — | leakscan/reproduce reads are trusted by K2 once K2-TWO-RUN is fixed | becomes a documented trust boundary, not a silent drop |
| ADAPTER-EDIT | gap vs GOAL | no explicit static rejection or test of an edit touching an adapter (out of reach today because edits cannot change Python; inferred) | add a check + test |

Test files: R1 = `tests/test_review_kernel.py` on `m3-r1-kernel` (10 fail = findings, 8 hold, 1 xfail). R2 =
`tests/test_review_loop.py` on `m3-r2-loop` (9 fail, 19 hold). verified: I reproduced the same failures.

### D. KNOWN LIMITATIONS — accepted, not fixed (inferred unless stated)
- A program can still tell dev folds from held-out runs by data size or content.
- Paraphrased or renamed copies pass both copy detectors (line overlap and replication brief).
- The engine scrubber matches whole private values only. Substrings and encodings pass; canaries and label windows
  are verbatim-only. K9 and the audit detect verbatim leaks only.
- The Socratic filter is a heuristic: question-shaped directives pass.
- No Docker disk quota on the workspace mount, and full stdout/stderr is held in host memory (resource exhaustion
  possible). A zip-bomb bundle member is read into memory.
- A `tool` node (an allowed edit type) could put training rows into a prompt, which would inflate dev scores.
  Held-out results stay safe (K6 would reject).
- Under `LocalSandbox` (tests only) programs can read the host filesystem.
- Candidate-only infra exclusions (wired 2026-10-07, m3-wire): the loop fails a candidate closed when the units lost
  on the candidate only (fresh evolve + gate) exceed random outages by a one-sided exact sign test, alpha 0.01
  (>= 7 one-sided losses needed). Limits: assumes equal exposure of both arms; race data are not counted (selection
  only; the parent's race runs predate the candidates'). The M0 outage test still reaches the clean decision.
- Dataset roles (external/future) are trusted from the registrant. They should become a human-approved action.
- Adaptive test-set reuse: Bonferroni over the claims registered on a target is a practical defence, not a
  guarantee. Sealed `finalize` is the strong form.
- The ledger is tamper-evident, not tamper-proof.
- Bundles contain hidden labels and must never be agent-visible.
- Trusted modules have store access (a trust boundary not enforced in code).
- Checked and held: bundle `reproduce` has no zip-slip; Docker fold runs cannot persist state; protected-node edit
  paths are rejected (test); templates refuse `_private*` (test).

---

## Live runs (details in liverun1.md)
- **Pre-M0 and post-M0 runs** on rings (easy synthetic): pipeline proven. The latest, `run_e305a0283a074fbb`,
  scored 0.958 vs 0.351 with the audit passing; 9 calls, 154k input tokens.
- **research_v1 run 1** (`run_cd99288e65ba4a4f`, wine_knn_unscaled):
  - REJECTED by K7 (24-row dev split → claim at the 0.02 floor; 1.45 bits < 2) and by a K9 false positive (fixed).
  - Held-out effect +0.253, BCa [+0.167, +0.358].
  - 12 calls, 224,159 / 29,939 tokens, 845 s.
- **research_v1 run 2** (`run_f1bff8ab659f48e6`, code a499a6f with CV):
  - CV worked: baseline dev 0.645 on 124 rows; claim at 0.133.
  - REJECTED by K3/K4/K6/K7: the chosen 3-model ensemble timed out in 1 of 3 held-out seeds and in the Kernel
    replay.
  - Audit passed. Invariant 5 NOT MET (recorded override).
  - 13 calls, 207,195 / 27,333 tokens, 8,074 s (machine overloaded).
  - Bundle reproduces (REJECTED → REJECTED).
- No research_v1 run has been CERTIFIED live yet. The loop's research quality is **unmeasured**; only its
  integrity machinery is tested (scripted models + 2 live runs).

## Other known limitations
- Evolution is tested only with scripted LLMs. The objective is not yet VDS: it is 0.5·certified + 0.5·replicated
  + 0.5·clip(rel).
- **Invariant 5 enforced but NOT MET by any run**: one model family (Codex). A second family is needed.
- All loops so far are MECHANICAL (internal data only; invariant 10, strict reading).
- Kernel deviations from GOAL §4:
  - untested assumptions are listed but do not lower the tier;
  - SUPPORTED is never issued;
  - scaling_law/rediscovery are rejected at K1;
  - negative/equivalence claims are never certified (K7);
  - Bonferroni rather than Holm;
  - bundle ledger excerpts are checked per entry, not as an unbroken chain from genesis.
- K7's noise model is a normal approximation.
- Test-set wear: every claim enlarges its target's Bonferroni family.
- Small real test sets (iris 45, wine 54) are nearly uncertifiable.
- Resume recovers model-call spend of a killed step, not its sandbox time.
- The Codex CLI wrote code for older scikit-learn APIs twice in run 2. The sandbox's library versions are not in
  the implement prompt.
- Subscription mode is slow and quota-bound. SQLite only. I3 (citation check) is wired into the audit
  (2026-10-07) but n/a in every run until a research.literature node writes `citations` / `literature_snapshot`
  (not built).

---

## Next (in order)
1. **Integrity track:**
   1. Gate and merge `m3-fix-gateway`, then `m3-b1-compute`.
   2. Finish `m3-fix-r1` (K2-TWO-RUN, R1-F1, F3, F5, coverage back to 100%), gate, merge.
   3. Review, gate and merge `m3-b3-evolve`.
   4. R2-1, 2, 4, 5 with failing-first tests.
2. **M3 remainder:** static adapter-edit test; ML-specific Kernel logic behind the adapter interface; simulator
   adapter; Lean T0 adapter (after the human installs Lean).
3. **M2 exit test** once the ML adapter is approved and the budget is decided. Run it with the machine otherwise
   idle.
4. **M4:** agentic proposer, smoke run, family-separated critic (needs the 2nd model family), first real campaign.

## Needs a human (only the user)
- `chh approve-adapter ml --approver <name>` now, and `lp` once B1 is merged. Claude never runs these.
- A second model family (API key or open-weights model) for invariant 5.
- Lean 4 + Mathlib + a solver in the sandbox image (M3 T0).
- The M2 exit-test budget (≈300 Codex calls, ≈5M tokens).
- The life-science domain choice and the review standard for the prohibited list (GOAL §13.6).
