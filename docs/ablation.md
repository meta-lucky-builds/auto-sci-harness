# Ablation switches (GOAL §2.1, invariant 12)

Invariant 12: every claimed capability has code, a test and an ablation. This page covers the ablation part: one
switch mechanism, one closed registry, a per-row table of the 37 GOAL §9 capabilities, and exactly what is and is
not enforced.

Code: `src/custom_harness/ablation.py` (mechanism and registry), `scorecard/runner.py` (`ablation_set`, the entry
point; `run_ablation`), `scorecard/registry.py` (`AblationRecord`, `switch_fact`), `scorecard/axes.py` (`coverage`).
Rows: `src/custom_harness/scorecard/capabilities.yaml`.
Tests: `tests/test_ablation_core.py`, `tests/test_kernel_ablation.py`, `tests/test_ablation_scorecard.py`,
`tests/test_ablation_switches_evolve.py`, `tests/test_ablation_switches_research.py`.

## The mechanism
- **Closed registry.** `ablation.SWITCHES` maps each switch name to `Switch(name, capability, turns_off, test)`. There
  is at most one switch per capability row. A name that is not in the registry is refused everywhere:
  `AblationSet(...)`, `ablation_set(...)` and `AblationSet.off(name)` all raise. So a typo in a hook can never read
  as "on".
- **`AblationSet`.** An immutable set of switched-off names. `NO_ABLATION` (empty) is the default on every path. It is
  threaded through:
  - `Engine(ablation=...)` to `ModuleContext.ablation` (every module step);
  - `Evaluator(ablation=...)` to `execute_run(ablation=...)` to `Engine`;
  - `evolve_generation(ablation=...)` (default: the evaluator's set; a different set is refused).

  A hook reads `ctx.ablation.off("name")` or `abl.off("name")`.
- **The only entry point.** `scorecard.runner.ablation_set(names)` is the only function that can make a non-empty set.
  The constructor checks its caller's module and refuses any other. `run_system` / `run_ablation` pass the set to
  `System.run(items, disabled)`; production and baseline runs get `NO_ABLATION`.
- **Coverage.** A row counts as *switched* only when all of these hold:
  - its record names a switch;
  - that switch is in the closed registry for that same row;
  - the switch names a test;
  - the record lists no `not_covered` part.

  A row counts as *ablated* only when, on top of that, an ablation run of that switch is recorded and paired with a
  run of the same full system on the same benchmark bytes. `coverage()` reports both numbers:
  `capabilities_with_switch` and `capabilities_with_ablation`. For every gap it gives the reason:
  - "no ablation run recorded";
  - "not ablatable: ...";
  - "partial switch: ...";
  - "not in the closed registry";
  - "malformed ablation record".

## Safety design: an ablation is never a production backdoor
Each item below is enforced in code and tested.

| Rule | Where | Test |
|---|---|---|
| Only the scorecard runner's entry point makes a non-empty set; there is no env var, config default or CLI flag for it | `AblationSet.__init__` caller check; static scan | `test_a_nonempty_set_is_made_only_by_the_scorecard_runner_entry_point`, `test_no_env_var_config_default_or_cli_flag_switches_ablation_on` |
| Unknown switch names are refused | `AblationSet`, `ablation_set`, `off` | `test_unknown_switch_is_refused_everywhere`, `test_run_ablation_refuses_a_switch_outside_the_closed_registry` |
| An ablated run is marked ABLATED in its run record, every trace step and its run bundle | `Run.ablated`, `TraceStep.ablated` (stamped by `EvidenceStore.add_step` from the run record, never by the writer), run bundle manifest `ABLATED` | `test_ablated_run_is_marked_in_run_record_trace_and_bundle`, `test_the_store_stamps_the_marker_from_the_run_record_not_from_the_writer` |
| An ablated run cannot be laundered: resume and fork need the same ablation | `Engine._check_ablation` | `test_resume_and_fork_refuse_a_different_ablation` |
| The Kernel refuses to certify (recorded or not) on evidence produced by an ablated run | `Kernel.certify` / `Kernel._ablated_runs` (tighten-only governance record) | `tests/test_kernel_ablation.py` |
| The run-bound Kernel refuses pre-registration, evidence and certification for an ablated run | `studio/kill.py RunGuardedKernel._refuse_if_killed` | `test_run_bound_kernel_refuses_every_claim_making_call_for_an_ablated_run` |
| The memory store refuses memory and skill writes (propose, admit, record_outcome) from an ablated run; reads and retraction cascades stay allowed | `MemoryStore._refuse_if_ablated` | `test_memory_store_refuses_memory_and_skill_writes_from_an_ablated_run` |
| Evolution never accepts an edit while ablated, or when any acceptance run is ablated. The released verdict bit is kept only as `ablated_would_accept`. No branch is proposed. Records go to `lineage:ABLATED`, `harness_edit:ABLATED`, `generation_stats:ABLATED` and `proposer_session:ABLATED`, which no production reader reads | `evolve/loop.py evolve_generation` | `test_an_ablated_generation_never_accepts_and_keeps_its_records_apart`, `test_an_acceptance_resting_on_an_ablated_run_is_refused_even_in_production`, `test_generation_and_evaluator_must_share_one_ablation` |
| Durable evaluator outcomes of an ablated evaluator never mix with production ones | `Evaluator` durable key suffix | `test_an_ablated_evaluator_keeps_durable_outcomes_apart` |
| Ablated runs never count on the production scorecard axes | `scorecard/facts.py` (`ablated_runs` listed apart) | `test_ablated_runs_are_listed_apart_and_never_counted_on_production_axes` |
| Coverage counts only registered, tested, full switches, and says "no ablation run recorded" until a run exists | `registry.switch_fact`, `axes.coverage` | `test_shipped_registry_reports_every_switch_as_no_ablation_run_recorded`, `test_switch_fact_counts_only_a_registered_tested_full_switch_for_its_own_row` |
| The registry and capabilities.yaml agree: every switch is named by its row, every switch's test exists, every row has a record | — | `test_registry_and_capabilities_yaml_agree` |

Integrity mechanisms have no switch:
- the Kernel checks K0-K9;
- sandbox isolation;
- the leak/canary audit;
- held-out, LOFO and sealed gates;
- human approval nodes.

Their rows are `not_ablatable`, with that reason. Their contribution is measured only inside the evaluation harness.
The planted-exploit museum (`scripts/museum.py`) is that check-off arm. For each finding it re-introduces the
loophole as a patch in a scratch copy, never in the checkout. A test must then catch it (KILLED). Scorecard axis H
reports the catch rate. No production path skips a check, even under ablation.

## What is NOT enforced (stated, not solved)
- **The private constructor is a convention plus a runtime check.** Python cannot make a constructor truly private.
  Trusted in-process code could forge an AblationSet, just as it could forge any store row. `runtime/modules.py`
  states the same limit for modules. The caller check stops accidental or casual construction, and the static test
  keeps every entry point greppable.
- **A `Run` record can be built with `ablated=(...)` directly.** That only adds the marker, which makes everything
  stricter. It cannot remove the marker.
- **No ablation run has been made.** A run needs model calls and pinned benchmarks, which are owner decisions. So
  `capabilities_with_ablation` is 0 of 37, and invariant 12 stays FAILED on the scorecard.
- **Ablated research runs cannot reach the Kernel at all.** Pre-registration and certification are refused, so a
  research-loop ablation run ends at its first Kernel write. Benchmark scores for such runs must come from the task
  metric, not from certificates. This is by design: certification fails closed.
- **Multi-generation campaigns (`evolve/generations.py CampaignLedger`) under ablation are refused.** This happens
  indirectly: the ledger finds no production `generation_stats` record and raises `LedgerError`.
- **L8 weights export is not wired to runs.** Trajectories are inputs, so there is no run marker to check there.
- **The Kernel refusal looks only at the run named by `Evidence.produced_by_run`.** Evidence written with another
  run's id would escape this one check. The run-bound Kernel that modules get refuses such writes for an ablated run
  anyway.

## Per-row table
ROW_TABLE_PLACEHOLDER
