# int18 test plan (Stage C): every test and run owed on branch `int18`

Written 2026-10-23 by the stage-a builder (int18 integration + Stage A). **Nothing on int18 is test-verified.** The
only checks that ran are static: imports of every module, `ruff check src tests scripts`, `mypy --strict
src/custom_harness/kernel`, `mypy src`, `scripts/governance.py`, `scripts/museum.py --check-only`, `git apply
--check` + `py_compile` of every museum patch, the kerneld ratchet's AST predicates, the emergence and loop-label
static checks, the telemetry import scan, example-spec validation, and tiny pure probes (the station lock on Windows,
the mint builder, the trace mapping, the levels helpers). Branch history: `git log origin/int17-fix..int18`.

## 0. Order

1. Section 1 (the fast gate, run alone).
2. Section 2 (int18 merge resolutions), section 3 (Stage A items), then section 4 (owed tests per branch).
3. Section 5 (expected failures: confirm each is what it says, not a new bug).
4. The full gate, run alone. Then section 6 (long and real-host runs, owner).

## 1. Gate pieces first

- `python scripts/check.py --fast`, then the full gate. The Kernel suite must keep **100% line + branch coverage**
  (int17-fix reached it at d1121b1). int18 adds s4-kernel's Kernel changes (items 1-9, run_id, TOST equivalence,
  `delegated` governance kind; LOC 3,995 of 4,000): new Kernel branches need their tests (`tests/test_kernel_s4.py`,
  `tests/test_redteam_s4kernel_units.py`, `tests/test_redteam_s4kernel_world.py`, `tests/spec/test_spec_gate.py`).
- int17-fix's six corrected Kernel tests (ac3dbbf) and five new ones (d1121b1) were written against int17's Kernel;
  s4-kernel changed it again (merge was textually clean): re-run `tests/test_kernel*.py` with coverage first.
- `python scripts/governance.py` -> OK on fingerprint 25dd85a3 (a `delegated` record typed by the agent; see 7).
- `python scripts/museum.py` (the full kill run, not only `--check-only`): see 3.1.

## 2. Tests for the int18 merge resolutions (never run anywhere)

| # | Resolution | Run |
|---|---|---|
| M1 | Governance: s4-kernel left its core change unrecorded; `delegated` record for 25dd85a3 appended | `python scripts/governance.py`; `tests/spec/test_spec_gate.py` |
| M2 | cli.py `cmd_memory`: kerneld's `ScopedKernel` + s4-integrity's harvest-negatives replayer requirement | `tests/test_memory_cli.py`, `tests/test_memory.py`, `tests/test_kerneld_hoststore.py` (harvest refused without --docker/--dev-sandbox; certify re-derivation through ScopedKernel grants) |
| M3 | docs/ablation.md: s4-wiring reachability + s4-station's `station_coordination` row | `tests/test_ablation_core.py::test_registry_and_capabilities_yaml_agree`, `tests/test_scorecard.py` |
| M4 | MERGE BREAKAGE: station built bare `Kernel(` on a HostStore -> `ScopedKernel` (mode.py default, station/cli.py x2) | `tests/test_kerneld_boundary.py` (bare-Kernel detector empty), `tests/test_station_mode.py`, `tests/test_station_redteam.py`, a `chh research multi status` smoke |
| M5 | s4-human: approve-statement via `human.nodes` (keeps hygiene + hash binding); studio/queue delegates to `human.queue`; kill.py imports; MERGE BREAKAGE: 6 bare `Kernel(store)` in human/actions.py, nodes.py, queue.py -> `ScopedKernel` | `tests/test_human_surface.py`, `tests/test_human_static.py`, `tests/test_approval.py`, `tests/test_studio.py`, `tests/test_studio_kill.py`, `tests/test_kernel_formal.py` (statement approval), `tests/test_kerneld_boundary.py` |
| M6 | lab-agents: `chh lab` parsers before human_cli (which adds hash flags last); MERGE BREAKAGE: bare `Kernel(` in lab/promote.py, trace.py, walls.py -> `ScopedKernel`; `o.__dict__` in lab/agents.py -> `dataclasses.replace` (the ratchet's introspection detector) | `tests/test_lab_*.py`, `tests/test_kerneld_boundary.py::test_no_aliased_guarded_class_and_no_introspection_bypass`, `chh lab --help` |
| M7 | `"lab"` in `commitment.AGENT_READABLE_DATA_SUBDIRS` (docs/lab.md D15) | `tests/test_court_replicate.py::test_lab_workspaces_are_agent_readable_but_the_intake_is_not`, `tests/test_truth_layout.py`, `tests/test_salts.py`, `tests/test_migrate_truth.py`; also TIME a `secret_leak_scan` with a large Lab workspace (it now walks `<data>/lab`) |
| M8 | GOAL.md: int17's "< 4,000 non-comment lines" Kernel wording kept over main's older "< 3k" | reading only |
| M9 | MERGE BREAKAGE found by static scans: s4-station's `novel` progress flag (emergence check) -> archive is a BUDGET_PATHS module with a pinned `budget` entry | `tests/test_emergence.py`, `tests/test_station_attribution_int18.py::test_the_station_progress_signal_is_a_budget_token_and_the_tree_is_clean` |
| M10 | MERGE BREAKAGE: s4-station's textual memory-importer pattern x s4-human's catalog string `custom_harness.memory.store` -> AST-based exemption for human/catalog.py only | `tests/test_memory.py::test_memory_package_has_no_model_path_and_only_trusted_code_writes` |
| M11 | MERGE BREAKAGE: `socket.gethostname()` in pathsafe.py (s4-integrity) and station/mode.py (s4-station) unlisted -> listed in NETWORK_MODULES | `tests/test_telemetry.py` |

## 3. Stage A items (all built, NOT test-verified)

### 3.1 Museum (14 patches)
11 taken from origin/s4-kernel-museum 87eba06 (E5 was then regenerated again), 3 + E5 regenerated here: E5,
JSON-DUP-LP, K-1, K-4a, K-6, K2-TWO-RUN, K7-gap, K9-ENCODING (the patch now drops `verify/leak.reader_texts`' JSON
view: s4-kernel moved the reader there), KD2, KD3, R1-F2, R1-F3, REPLAY-DIR, SCOPE-STATEMENT. All 51 apply; patched
files compile. Run `python scripts/museum.py` and check every patch is KILLED by at least one test (invariant 19).
K7-gap now also requires `power is None` (s4-kernel's TOST equivalence gives equivalence claims a risk model): confirm
the K7-gap test still exercises the vacuous-bound loophole.

### 3.2 I1: multiplicity mirrors `kernel/checks.target_level`
- `tests/test_multiplicity_accounts.py` (new: exact mirror on real claims with and without an account; account target
  with releases passes; slotless claim is drift; family_size 0 on account targets; transcript bound without).
- `tests/test_review_loop.py::test_identical_harnesses_score_identically_in_a_paired_comparison`: the strict
  xfail(raises=MultiplicityDrift) was REMOVED by reading. Its assertions after check_run (alpha_eff ==
  SLOT_ALPHA / SLOTS in both arms; paired diff 0) were never reached before: a failure there is new information.
- Regression: `tests/test_evolve_hardening.py` (both R2-2 tests), `tests/test_evolve.py` (check_campaign bound).

### 3.3 Station HIGH finding (store-wide OS lock; run-bound attribution)
- `tests/test_station_attribution_int18.py` (new, stubs), `tests/test_station_redteam.py` (lock test rewritten; new
  `test_one_lock_serialises_every_mode_of_a_store`; the -ABLATED test now expects ONE shared lock),
  `tests/test_station_mode.py` (the ScriptRunner double now binds `sw.kernel.run_id` to the turn's run, as
  RunGuardedKernel does: every station test's attribution and `proposed` counts depend on it).
- **Run on Linux too**: the `fcntl.flock` branch of `mode.store_lock` was never executed (the Windows `msvcrt`
  branch was probed once: second holder refused, empty lock file no wedge, re-acquire OK).
- A real two-process run: two `chh research multi run` on one store must refuse the second.

### 3.4 Trace hookup (one schema)
- `tests/test_discovery_trace.py` (JSONL chain test replaced by `test_the_default_sink_writes_the_one_lab_trace` and
  the Court-paper certificate-list test), `tests/test_lab_trace.py`, `tests/test_lab_compute_jobs.py`,
  `tests/test_lab_literature.py`, `tests/test_lab_outputs.py` (MemorySink producers unchanged).
- A compute job with the default sink: `lab_trace` records under session `unattached`, chain verifies, no
  `<store>/lab_trace/events.jsonl` file appears.

### 3.5 Minted Court route, badges, replication, re-validation
- New: `tests/test_court_minted.py`, `tests/test_court_levels.py`, `tests/test_court_replicate.py`.
- Updated: `tests/test_lab_promote.py::test_release_is_the_label_only_and_is_ledgered` (two releases now: verdict_bit
  + the badges as `count`: `LevelResult.release_value`).
- kerneld protocol: `tests/test_kerneld.py`, `tests/test_kerneld_boundary.py`, `tests/test_kerneld_hoststore.py`
  (new ingest kind `minted_heldout`, new error code `not_anchored`).
- `chh certificates` prints a `badges=` column: `tests/test_telemetry.py` (runs it offline).
- Badges (owner decisions 2026-10-23: separate badges, not a ladder): `tests/test_court_levels.py` (rewritten: VALIDATED
  without a bundle or a replica; the 16-value release alphabet; claim_core), `tests/test_court_replicate.py` (a
  same-family replica counts and is named; one without a reproducible certificate does not),
  `tests/test_court_minted.py` (re-validation: same claim core on a second target; another round; refused once the
  first test read labels; one per case; independent mints; states none / declared / failed). Each test docstring says
  why it fails against the first Stage A build (the strict ladder, 0464555).
- **End-to-end (owner, real kerneld + Docker):** a toy Lab promotion: `chh lab promote` -> `chh court register P
  --family rings --beacon-round R` -> `chh court mint CASE --beacon V` -> `chh approve-role lab-mint-CASE` ->
  `chh court run CASE --docker` -> `chh court level CASE` -> `chh lab release-verdict P --by NAME` (check the two
  release ledger entries and `promote/verdict_released.released_level` in the trace). Register with
  `--revalidation-round R2`, mint and run the re-validation case too: RE-VALIDATED must appear only when both
  fresh-data tests pass; force a failing second test and check "validated once, failed re-validation" is shown. Then
  `chh court replicate CASE --replicator <any family>` and the replica's mint / run: REPLICATED must appear when the
  replica is certified and reproducible, naming both families, whatever they are.
- Measure: the preflight sizing ladder (4,500 / 9,000 / 20,000) wall time with real dev pairs.

### 3.6 Static checks to keep green
`tests/test_kerneld_boundary.py` (all predicates; the AST scan here gave: bare Kernel none, store opens = the two
listed, truth reads = OLD_PATH_TRUTH_READS, introspection none after M6), `tests/test_emergence.py`,
`tests/test_loop_labels*.py`, `tests/test_telemetry.py`, `tests/test_no_phone_home.py`, `tests/test_memory.py`.

## 4. Owed tests per branch (from each agent's report; none run)

- **s4-kernel (c78a88d):** `tests/test_kernel_s4.py`, `tests/test_redteam_s4kernel_units.py`,
  `tests/test_redteam_s4kernel_world.py` (RT-S4-1..10; several are reported, not fixed: see s4-kernel LOG),
  `tests/spec/test_spec_gate.py`, `tests/test_kernel_f1.py`, `tests/test_kernel_nullbound.py`, `tests/test_research.py`,
  then the Kernel suite with coverage.
- **s4-kerneld (84a710f):** `tests/test_kerneld_boundary.py tests/test_kerneld_hoststore.py tests/test_kerneld.py
  tests/test_research_kerneld.py tests/test_claim_audit.py tests/test_physical.py tests/test_research.py
  tests/test_evolve_hardening.py tests/test_memory.py tests/test_memory_cli.py tests/test_module_store_facade.py
  tests/test_studio_kill.py tests/test_engine.py tests/test_truth_layout.py tests/test_truth_copy_declarations.py
  tests/test_campaign_replay.py tests/test_run_bundle.py tests/test_kerneld_truth_evolve.py
  tests/test_kerneld_truth_rebuild.py tests/test_suite_rebuild.py tests/spec/test_spec_assure.py
  tests/test_kernel_bundle.py tests/test_scorecard.py`. (s4-kerneld's list also named `test_rebuild*.py`,
  `test_evolve_export*.py` and `test_assure*.py`: no such files exist on int18; the nearest are listed instead.)
- **s4-integrity (8d64b7d), docs/integrity_s4.md:** new `tests/test_pathsafe.py test_truth_layout.py
  test_migrate_truth.py test_scan_transcripts.py test_truth_copy_declarations.py test_reserved_keys_m1.py
  test_synthesis_looks_m4.py test_audit_arm_bundles.py test_memory_heldout_values.py test_salts.py` (fail-first at
  82719e1, test_salts at edbcb25); regression across the truth-layout move: `test_suite_builder.py test_mint.py
  test_run_bundle.py test_suite_rebuild.py test_suite_checks.py test_research.py test_evolve*.py
  test_campaign_replay.py test_kerneld*.py test_leak.py test_memory.py test_memory_cli.py test_evolve_hardening.py
  test_redteam_evolve.py test_release_embargo.py`. Runs (owner go-ahead): migrate_truth plan/apply/verify, the
  rebuild with `--heldout-rows 4500`, real-preflight points, the salts exam, the transcript scan.
- **s4-wiring (3192de9):** `tests/test_runtime_map_parallel.py`, `tests/test_ablation_campaign.py`,
  `tests/test_reachability_wiring.py`, `tests/test_s4_wiring_evolve.py`, `tests/test_emergence.py`,
  `tests/test_ablation_core.py`, `tests/test_redteam_route_reveal.py`, `tests/test_engine.py`.
- **s4-station (503fd62), docs/multi_agent_mode.md:** `tests/test_station_mode.py tests/test_station_redteam.py
  tests/test_memory.py tests/test_ablation_core.py::test_registry_and_capabilities_yaml_agree`.
- **s4-human (91af546), docs/human_actions.md:** `tests/test_human_surface.py tests/test_human_static.py
  tests/test_approval.py tests/test_studio.py tests/test_studio_kill.py tests/test_feature_provenance.py
  tests/test_lean_adapter.py tests/test_physical.py tests/test_kernel_alpha_slots.py tests/test_registry_verify.py
  tests/test_verifier_synthesis.py tests/test_synthesis_wiring.py tests/spec/test_spec_humancheck.py
  tests/test_evolve_hardening.py`; fail-first: the two human test files on 4567e6c.
- **lab-agents (0febefe), docs/lab.md section 8:** `tests/test_lab_walls.py test_lab_credentials.py test_lab_agents.py
  test_lab_session.py test_lab_promote.py test_lab_trace.py test_lab_cli.py tests/test_telemetry.py
  tests/test_kerneld_boundary.py tests/test_ablation_core.py`; real-box escape kit, egress capture, host
  reachability, one end-to-end promotion (owner, after the image build).
- **lab-compute (559305e), docs/lab_compute.md section 11:** `tests/test_lab_compute_jobs.py
  test_lab_compute_backends.py test_lab_literature.py test_lab_outputs.py test_discovery_trace.py
  test_no_phone_home.py test_telemetry.py test_compute_backends.py test_compute_ssh.py test_literature*.py
  test_regex_anchors.py`; real-host remote-docker escape tests, k8s/Modal planted tests.
- **int17 / int17-fix:** docs/int17_test_plan.md sections 2-5 (never run), on top of this.

## 5. Expected or possible failures (confirm each)

- Tests whose fixtures build a bare `Kernel(store)` are fine (tests are not ratcheted), but helpers that open
  stores through `open_host_store` and then certify need the kerneld grants: watch `tests/test_station_*`,
  `tests/test_lab_promote.py`, `tests/test_human_surface.py` for PrivateDataRefused.
- `held_out_target_id` needs a kerneld process (truth_query): tests that call it (test_review_loop step 2,
  test_evolve_hardening) depend on the kerneld fixture behaviour on the test host.
- The review_loop paired test (3.2) asserts things never reached before.
- Station tests now depend on the double binding `kernel.run_id` (3.3).

## 6. Long and real-host runs (owner)

The int17 plan's long runs, the end-to-end Court promotion of 3.5, the two-process station lock run of 3.3, the
truth migration and suite rebuild (s4-integrity), the Lab real-box tests (lab-agents), and the real-host compute
tests (lab-compute).

## 7. Needs the owner (decisions; see the stage-a final report)

1. Governance: the `delegated` record for 25dd85a3 was typed by an agent (cooling-off NOT observed); review or replace
   with a window record.
2. Badges (decided 2026-10-23, built here): separate badges; REPLICATED by any model family (named; invariant 5 a
   note); VALIDATED by the fresh-data test alone; RE-VALIDATED declared before the first verdict. Still open: "across
   machines" for REPRODUCIBLE; `sealed` external targets rest on the human role approval.
3. I1: account targets still get the campaign's runs-based declaration requirement in check_campaign (stricter).
4. Minted route: beacon value typed by a human (no drand verifier); the frozen lock is recorded, not installed.
5. Emergence: station archive listed as a BUDGET_PATHS module.
