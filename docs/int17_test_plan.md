# int17 test plan (the later, dedicated test phase)

Branch `int17` = `origin/main` b2e9ffd + int16-fix, w2-taint, w2-wiring, w2-ablation, w2-suite, s3-kernel,
s3-verifiers, s3-evolve, s3-hypo, s3-tree, s3-report, s3-gateway, s3-gateway-wall (+ its 0.6 s test fix), s3-domain,
s3-assure, merged in that order by the s4-int17 integrator on 2026-10-14/15.

**Nothing on int17 has been test-run.** Owner rule for the integration round: no pytest, no `scripts/check.py`, no
museum, mutation or simulation run. What WAS checked on every merge (verified, outputs seen): every
`custom_harness` module imports (228 at the end); `ruff check src tests scripts` clean; `mypy --strict
src/custom_harness/kernel` clean; `mypy src` clean; `scripts/governance.py` OK; `scripts/museum.py --check-only` OK;
a static AST comparison against the kerneld-boundary ratchet's lists; every `examples/*.yaml` validates as a
HarnessSpec with all module nodes registered; Kernel LOC 3,956 (< 4,000).

Run everything below on ONE snapshot worktree of the int17 head, through the limiter (`locked_run.py`), with the
machine otherwise idle, `-o faulthandler_timeout=1800`, `PYTHONPATH=<snapshot>\src` (the venv's editable install
points at the main checkout). Several suites have long tests (`test_infra` docker-outage tests ran > 30 min under
load); do not read a faulthandler dump as a hang.

## 0. Order

1. Section 1 (gate pieces), so a broken base is found first.
2. Section 2 (the merge resolutions): these are the integrator's own unverified code.
3. Section 3 (each branch's owed tests, as its agent listed them).
4. Section 4 (expected failures): confirm each fails for the stated reason only.
5. Section 5 (long runs: museum, mutation, differential, corpus, yield, scans).
6. Then the orchestrator's full gate `python scripts/check.py`, alone on the machine.

## 1. Gate pieces first

- `python scripts/check.py --fast` (lint, types, Kernel strict types, governance, museum registry, Kernel tests at
  100% line+branch coverage). The Kernel now holds w2-ablation's `Kernel._ablated_runs` and all of Kernel 1.3
  (s3-kernel) plus the circuit registration (s3-domain): coverage of the union has never been measured.
- Kernel suite with coverage: `pytest tests/test_kernel_*.py --cov=custom_harness.kernel --cov-branch
  --cov-fail-under=100` (new on int17: test_kernel_ablation, _dupkeys, _alpha_slots, _scope_statement,
  _bundle_artifacts, _cpu_limits, _noninterference, _nullbound, _encoding; changed: _s16, _formal, _units).

## 2. Tests for the integrator's conflict resolutions (int17 only, never run anywhere)

| # | Resolution (merge) | Test to run | Expect |
|---|---|---|---|
| R1 | `evolve/loop.py`: w2-wiring durable ProposalLog + w2-ablation switches; ablation switches now act only on a non-resumed proposal; `rejected_before` honours `novelty-gate` before `ProposalLog.save` (merge 4) | `tests/test_ablation_switches_evolve.py tests/test_evolve_durable_resume.py tests/test_resume.py tests/test_evolve_generations.py` | pass |
| R2 | NEW fail-closed: an ablated generation given a `proposals` log raises ValueError (merge 4). **No test exists**; write one: `evolve_generation(..., ablation=AblationSet(("novelty-gate",)), proposals=ProposalLog(store, "c", 0))` raises before any run | new test in `tests/test_ablation_switches_evolve.py` | raises ValueError, no run, no record |
| R3 | `runtime/engine.py`: tool node = taint `_step_label` (rendered labels + workspace label) + ablation `observation_pack` render flag; ModuleContext gets both TrackedState wrapping and `ablation=` (merge 4) | `tests/test_redteam_taint*.py tests/test_taint_flow.py tests/test_taint_views.py tests/test_runtime_map.py tests/test_ablation_switches_research.py tests/test_ablation_core.py tests/test_engine.py` | pass |
| R4 | `research/claims.py` confirm(): kerneld held-out score (w2-wiring) + `cpu_s` on run_program and in ExecRecord limits (s3-kernel §16.8) (merge 6) | `tests/test_research.py::test_infra_retry_during_confirmation_does_not_reject_the_claim tests/test_research_kerneld.py tests/test_kernel_cpu_limits.py tests/test_research.py::test_research_loop_end_to_end` | pass; K3 replays under CPU time |
| R5 | `tests/test_review_loop.py::test_identical_harnesses_score_identically_in_a_paired_comparison`: s3-kernel's account-based step 2 kept AND int16-fix's strict xfail(raises=MultiplicityDrift) kept (merge 6; see §6 incompatibility I1) | run it alone | XFAIL with MultiplicityDrift raised in step 2 (check_run). An XPASS or another exception = report |
| R6 | `research/memory_wiring.py`: w2-ablation `ablated_kinds` + s3-verifiers SkillWriter path (merge 7) | `tests/test_memory.py tests/test_memory_cli.py tests/test_skill_writer.py tests/test_ablation_switches_research.py tests/test_recall.py` | pass |
| R7 | `cli.py`: s3-evolve's `chh evolve branches|route` views now run inside the single store open (kerneld ratchet allows exactly one EvidenceStore in cli.py); `--branch-by` + `evolve-report` both registered (merge 8) | `tests/test_router.py tests/test_branch_route_lofo.py tests/test_evolve_report_cli.py tests/test_kerneld_boundary.py` and a CLI smoke: `chh evolve branches <spec>` and `chh evolve report <campaign>` on a scratch data dir | pass; views print as on s3-evolve |
| R8 | `control/islands.py`: Orchestrator(ablation, weights_lever, entropy); stall order more_evaluation -> ablation `orchestrator-reallocation` -> weights lever -> diversify (merge 8) | `tests/test_islands.py tests/test_policy_gate.py tests/test_redteam_l7.py tests/test_evolve_search.py tests/test_ablation_switches_evolve.py` | pass |
| R9 | `evolve/loop.py`: + L7 `edit_admission` applied after the (possibly resumed) proposal (merge 8); `evolve/driver.py`: wiring `_resume_parent` + s3-evolve route/reveal helpers (merge 8) | `tests/test_redteam_l7.py tests/test_reveal_wiring.py tests/test_reveal.py tests/test_evolve.py tests/test_evolve_hardening.py tests/test_campaign_replay.py` | pass (except §4 markers) |
| R10 | `examples/research_v2.yaml` max_steps 63 + 11 (wiring paged recall) + 3 (s3-hypo formulate/front/diversify) = 77; run params = union incl. `run_log_chars`, `backtrack`, `curriculum` (merges 9, 10) | `tests/test_hypothesis_front.py tests/test_research_recall_wiring.py tests/test_tree_extras_wiring.py tests/test_research_wiring.py` | pass; no run ends at max_steps |
| R11 | `research/modules.py` profile_data: the `data_profile_kway_rank` switch removes the profile only; the key graph is computed either way (merge 10) | `tests/test_key_graph.py tests/test_ablation_switches_research.py` | pass |
| R12 | `cli.py` cmd_reproduce: s3-report headline comparison + s3-kernel `missing_artifacts`; `evolve/loop.py` generation_stats = ablation suffix + `loop_label`/`harness_runs`; driver summary = campaign-global `gi` + `loop_label` (merge 11) | `tests/test_third_party_reproduce.py tests/test_run_bundle_artifacts.py tests/test_loop_labels.py tests/test_evolve_generations.py tests/test_kernel_bundle.py` | pass |
| R13 | `tests/test_campaign_wallclock.py`: one 0.6 s step under a 0.5 s cap (merge 13) | `tests/test_campaign_wallclock.py tests/test_campaign_budget.py tests/test_resume.py` | pass, repeatably (run the wall_s test 5 times) |
| R14 | `redteam/kernel_core_changes.yaml`: integrator tighten+deviation records for merged fingerprints d9524887 and b540544f (merges 6, 14) | `python scripts/governance.py`; `tests/spec/test_spec_gate.py` | OK; owner reviews the records |
| R15 | `scripts/mutate.py`: targets moved to kernel/ml.py (statistics, criterion, plan_code_divergence) after the split (merge 15) | `python scripts/mutate.py --list` then §5 mutation run | 443 mutants listed |
| R16 | `tests/test_kerneld_boundary.py`: `assure/corpus.py` scratch-copy store open listed (merge 15) | `tests/test_kerneld_boundary.py` | pass; owner confirms the listing |

## 3. Owed tests per branch (from each agent's RESUME.md / final report)

### int16-fix (integrator, 678e6c5)
1. `pytest -v tests/test_infra.py tests/test_evolve.py tests/test_evolve_mechanism_smoke.py tests/test_resume.py
   "tests/test_exclusion_asymmetry.py::test_outage_campaign_reaches_the_same_decision_with_the_rule"`: XFAIL for the
   strict markers (raises=MultiplicityDrift); PASS for test_generation_halts_fail_closed_while_releases_outgrow_the_declared_family
   and test_smoke_runs_one_task_through_the_real_loop.
2. `scripts/check.py --fast`; the full gate.

### w2-taint (d060173)
1. Fail-first of the round-2 tests at a8dd56f (worktree ff-a8d): test_redteam_taint3.py, test_taint_views.py,
   test_redteam_taint.py: the 12 H tests, the 2 taint_views tests and test_nested_engine_using_ctx_backends... FAIL.
2. `tests/test_redteam_taint.py test_redteam_taint2.py test_redteam_taint3.py test_taint_views.py test_taint_flow.py
   test_runtime_map.py test_recall.py test_reveal.py`: all pass (G1 strict xfail).
3. Regression A: test_release_embargo, test_campaign_replay, test_resume, test_engine, test_runtime_fork,
   test_approval, test_module_store_facade, test_redteam_critic_recall, test_critic_answers, test_evolve_map_flow,
   test_kerneld, test_kerneld_boundary, test_fail_closed.
4. Regression B (never completed): test_infra, test_evolve, test_evolve_diagnosis, test_evolve_evidence,
   test_evolve_generations, test_evolve_hardening, test_evolve_mechanism_smoke, test_evolve_tightening,
   test_exclusion_asymmetry, test_lofo, test_proposer_agent, test_redteam_evolve_claims, test_review_loop,
   test_router, test_voi; time test_infra against c9823b8 (performance regression check).
5. Yield: `scripts/taint_yield.py OUT.json`.

### w2-wiring (bf98dce)
- test_evolve_hardening.py (strict xfail from d2ff98e must still xfail with the durable-resume loop).
- test_infra.py.
- test_kernel_bundle, test_kernel_integrity, test_kernel_quality, test_kernel_s16, test_lean_adapter,
  test_literature_cli, test_literature_fulltext, test_memory_cli, test_orchestration_cli, test_physical,
  test_preflight, test_regex_anchors, test_release_embargo, test_run_bundle_anchor, test_run_bundle_artifacts,
  test_scorecard, test_snapshot, test_suite_builder, test_timesplit.
- Known pre-existing failure: test_gateway_cassette::test_recorded_research_run_replays_with_zero_model_calls.

### w2-ablation (d2384fc)
1. Fail-first: test_ablation_switches_evolve.py::test_per_completion_race_switch with loop.py from 8816539~1: FAIL.
2. test_ablation_switches_evolve, test_ablation_switches_research, test_ablation_core, test_kernel_ablation,
   test_ablation_scorecard, test_scorecard.
3. test_engine, test_islands, test_voi, test_evalrace, test_evolve, test_evolve_hardening, test_run_bundle,
   test_context.
4. test_memory, test_recall, test_tournament, test_literature_sources, test_literature_retrieval,
   test_literature_fulltext, test_weights_export, test_research, test_evolve_map_flow, test_redteam_critic_recall.
- Doc follow-up: the ablation reachability notes say milo/sia switches are off every run path ("Orchestrator
  constructed nowhere in src"); s3-evolve's evolve/search.py now constructs it. Re-check those rows.

### w2-suite (7186ef5)
1. test_suite_checks.py.
2. Format-agnostic truth scan (30-60+ min): `scripts/suite_checks.py ...suites\m2 --only isolation,scan` (exact
   command in agent_worklogs\w2-suite\RESUME.md).
3. Preflight bracket points 1050 0.04 4500 and 1050 0.05 1500.
4. `chh build-suite --verify`.

### s3-kernel (1f12636)
1. Kernel suite + 100% branch coverage (section 1).
2. test_review_loop (see R5), test_research::test_infra_retry_during_confirmation_does_not_reject_the_claim,
   test_infra (docker outage), test_run_revisions, test_float_screen, test_memory, test_memory_cli,
   test_feature_provenance, test_adapters_cli, test_preflight, test_noninterference, tests/spec/test_spec_mutants.py.
3. Differential OLD=311c3e1 vs NEW=ee0ef1d (docs/kernel_differential.md): core/adapter split bit-identity.
4. Museum `--only JSON-DUP-LP JSON-DUP-KERNEL ALPHA-ORDER SCOPE-STATEMENT K9-ENCODING` (each KILLED).
5. nullbound: test_kernel_nullbound fail-first on ee0ef1d + pass; MC study `scripts/sim_bounded_null.py 200`.
6. test_kernel_encoding fail-first (1f12636 minus d3feabd) + pass.
7. Red-team of the new checks (not done).

### s3-verifiers (37baffc)
1. test_tolerance_wiring, test_l8_isolation, test_skill_writer, test_synthesis_wiring, test_verifier_synthesis,
   test_tolerance_calibration, test_float_screen, test_weights_export (~1 h).
2. test_memory, test_memory_cli, test_exploit_eval, test_research_wiring, test_research, test_evolve_evidence,
   test_evolve_diagnosis, test_release_embargo, test_evolve_tightening, test_evolve_hardening.
3. Optional: scratchpad\diag_k3.py.

### s3-evolve (3e2db8a)
- test_policy_gate, test_islands, test_redteam_l7, test_redteam_route_reveal, test_branch_route_lofo, test_reveal,
  test_reveal_wiring, test_router, test_emergence, test_release_embargo, test_evolve_search, test_lofo,
  test_evolve_generations, test_evolve_evidence, test_campaign_replay, test_regex_anchors, test_evolve,
  test_orchestration_driver.
- Fail-first: test_redteam_route_reveal.py on efc0930; test_redteam_l7.py + test_policy_gate.py on 8f1bf8a.

### s3-hypo (a09034d)
1. test_hypothesis_front.py (25): investigate the end-of-session hang; rerun
   test_v2_rejected_hypothesis_set_ends_the_run_honestly alone several times (one unreproduced F).
2. test_research_yield, test_research_wiring, test_critic_answers, test_tournament, test_formulator,
   test_hypotheses, test_evolve_hardening, test_redteam_evolve, test_literature_audit,
   test_research::test_research_loop_end_to_end.

### s3-tree (02fad13)
1. test_report_figure.py (and the label regression test at 2a22c9c as fail-first).
2. test_research_wiring, test_research, test_research_yield, test_research_sequential_runs (find the unknown F at
   position ~113).
3. test_exptree, test_exptree_l47, test_figures, test_figures_canonical, test_figures_report, test_profile.
4. Re-confirm test_tree_extras_wiring, test_key_graph.

### s3-report (1d678a4)
1. test_scorecard_local, test_publication_gate, test_third_party_reproduce, test_loop_labels, test_claim_audit,
   test_research, test_critic_answers, test_kernel_bundle, test_scorecard.
2. test_figures_report, test_redteam_loop, test_copy_detection, test_review_loop, test_research_wiring,
   test_sandbox_hardening, test_run_bundle_artifacts, test_evolve_mechanism_smoke, test_evolve_evidence,
   test_evolve_generations, test_evolve, test_evolve_hardening.
3. Fail-first at 015d92b with the 1d678a4 test files: `-k "ood_drop or deleted_run_bundle or two_api_calls or
   headline_number_differs or gate_rules or human_steps or every_number_resolves"`.
4. Museum at a known commit, then `chh scorecard local --museum-log <log>`.

### s3-gateway (04cc6c8) and s3-gateway-wall (80cbf87 + fix)
- test_gateway_wiring, test_registry_verify, test_compute_ssh, test_compute_backends, test_scorecard (agent ran
  57 passed / 2 skipped at 04cc6c8; re-run on int17).
- test_campaign_wallclock, test_campaign_budget, test_resume (R13).

### s3-domain (9ede4e9)
- `pytest -q -rxX tests/test_circuit_redteam.py tests/test_circuit_adapter.py tests/test_engineering_suite.py`:
  all pass; 8 strict xfails (7 K1 statement/scope + 1 lp encoding generalisation), no XPASS. NOTE: s3-kernel's K1
  scope tightening and K9 parsed-string scan are now in the same tree; some of those 8 strict xfails may now XPASS
  (that is the fix landing; remove the marker, do not weaken anything).
- Optional regression: test_kernel_units, test_kernel_sim, test_adapters_cli, test_evolve_hardening, test_lofo.
- The circuit adapter now runs on the split Kernel (procedures_for falls back to ML procedures, as for lp).

### s3-assure (fce3993)
1. Corpus trial `scripts/honest_corpus.py --build --spec-pairs 0 --world-pairs 4 ...`, then full build
   (`--spec-pairs 120 --world-pairs 4`, CHH_SPEC_FAST=1), commit `redteam/honest_corpus/manifest.json`.
2. tests/spec/test_spec_assure.py tests/spec/test_spec_gate.py tests/spec/test_spec_humancheck.py.
3. Mutation (section 5); re-justify redteam/equivalent.yaml (6 of 7 ids stale, more after the ml.py move).
4. Differential slices (`run_diff.sh`, resumes at chunk 15).

## 4. Expected failures on int17 (each pre-existing on its own branch, not caused by the merge)

| Test | Why | Source |
|---|---|---|
| tests/test_emergence.py (real-tree check, `check_novelty_isolation(SRC) == []`) | 6 'novelty' tokens: 5 from w2-ablation (`ablation.py` 119-122 novelty-gate switch, `evolve/loop.py` `abl.off("novelty-gate")`), 1 from s3-report (`audit/checklist.py:70` checklist field `novelty=`) | already failing on origin/w2-ablation and origin/s3-report; owner decision: allowlist or rename |
| museum full run: E5, JSON-DUP-LP, K-4a, K-6, K2-TWO-RUN, K7-gap, KD2, R1-F2, R1-F3, REPLAY-DIR = BROKEN (patch does not apply) | E5: w2-wiring rewrote research/claims.py; the other 9: s3-kernel's split moved checks.py code to ml.py | `git apply --check` fails on origin/w2-wiring and origin/s3-kernel themselves; patches need regenerating |
| test_review_loop paired test | strict XFAIL (R5, I1) | int16-fix + s3-kernel |
| test_evolve structural-fix test, test_infra x2, test_exclusion_asymmetry outage test | strict XFAIL raises=MultiplicityDrift (no account on their targets) | int16-fix |
| s3-evolve driver out_dir gate-mean text test | strict xfail, OPEN owner decision | s3-evolve |
| test_gateway_cassette::test_recorded_research_run_replays_with_zero_model_calls | pre-existing at c9823b8 | w2-wiring report |

## 5. Long runs (after sections 1-4)

- Museum: `python scripts/museum.py` (and `--control`) after regenerating the BROKEN patches.
- Mutation: `scripts/mutate.py --map`, slices, `--report` (s3-assure scratchpad\run_mut.sh); new map needed (the
  targets moved to ml.py).
- Kernel differential 311c3e1 vs ee0ef1d (s3-kernel); diffscale slices (s3-assure).
- Honest corpus build (s3-assure); bounded-null MC study (s3-kernel).
- Taint yield (w2-taint); suite truth scan, preflight brackets, build-suite --verify (w2-suite).

## 6. Incompatibilities recorded at integration (stricter side kept)

- **I1 (merge 6):** s3-kernel's per-target alpha-accounts fix R2-2 in the Kernel (K4 level fixed at alpha_T/S_T on an
  account target), but int16-fix's `evolve/multiplicity.check_run` (merged from x-reviewloop after s3-kernel
  branched) still mirrors only the NO-account transcript bound, so any campaign on an account target still halts with
  MultiplicityDrift. Kept the stricter side: check_run unchanged, the review_loop strict xfail kept (reason extended).
  Next step (owner/orchestrator): make check_run account-aware by mirroring `kernel/checks.target_level` (no drift on
  a target whose accounts every family claim holds a slot in), then remove the marker. Test to write then: the
  review_loop paired test passes, and test_run_check_uses_the_kernels_transcript_bound_not_the_claim_count still
  passes on a no-account target.
- **I2 (merge 4):** w2-ablation measurement arms vs w2-wiring durable proposals: an ablated generation with a proposal
  log is refused (R2).

## 7. Needs the owner

- Governance records written by the integrator for merged Kernel fingerprints d9524887 and b540544f (tighten +
  deviation, cooling-off not observed), on top of s3-kernel's 8dc2497e and s3-domain's 59e42223 records.
- `chh approve-adapter ml` (hash changed by the split) and `chh approve-adapter circuit`: human only.
- I1 decision; emergence allowlist decision; the assure/corpus.py ratchet listing (R16).

## 8. Results (branch `int17-fix`, from int17 916bc5d)

**2026-10-23, section 1 (fast gate), Windows laptop, run alone.**
- First run on 916bc5d: everything OK up to the Kernel tests, then 6 failed / 232 passed and coverage 98.71% (40 min).
- All 6 failures were stale TESTS, not product bugs (details in commit ac3dbbf):
  - encoding utf-8-sig: false precondition;
  - nullbound: the 15-row target is NOT insensitive (11/11 plantings detected), so it was shrunk to 5 rows;
  - two bundle tests: the missing-blob problem was reworded by the artifact listing;
  - lp provenance: K1's scope-statement rule refuses free-text statements;
  - lp adapter hash: the "procedures" key was added by the split.
- After that: 238 passed, coverage 99.77%. Five untested Kernel branches (sim BOM, other-target truth copy, crashing
  counterfactual replay, artifact-list mismatch, unreadable bundled ledger entry) got tests in d1121b1. Each new
  assertion was shown to fail when its guarded line is mutated.
- **Fast gate on d1121b1: all steps OK; Kernel 240 passed, 100.00% line+branch coverage** (48.5 min).

Not run yet: sections 2–5 and the full gate.
