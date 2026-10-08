# Race allocation: per-completion racing, the single-writer Evaluator, VOI (GOAL §5 L1)

Branch `b-control` (2026-10-08). Code: `control/allocator.py` (from or-compute), `control/evalrace.py`,
`control/voi.py`, `evolve/loop.py` (`Evaluator`, `EvolveConfig.race_*`, `_race`), `evolve/evaluate.py`
(`execute_run` / `finish_run`, durable unit records), `store/blobs.py`. Tests: `tests/test_evalrace.py`,
`tests/test_voi.py`, `tests/test_allocator.py`. Simulation: `scripts/sim_allocation.py`.
The downgrade-only controller gate is documented separately in `docs/control_downgrade.md`.

## What was unsafe (each reproduced on 4bf1a3d by a test that fails there)

| Problem | Evidence on 4bf1a3d |
|---|---|
| `Evaluator.outcome` checked its cache, ran, then filled it: two threads asking for one unit both ran it | `test_one_unit_asked_for_by_two_threads...`: `Evaluator.calls == 2`, the module ran twice |
| Every writer of a blob used `<digest>.tmp`: concurrent runs storing identical bytes (spec versions, workspace snapshots) collided | `test_writers_of_the_same_blob_do_not_collide`: about 200 `PermissionError` out of 360 writes; the same error crashed the second run in the test above |
| A new Evaluator after a crash had an empty cache: every finished unit ran again and was charged again | `test_resume_after_a_crash...`: control half shows `calls == 3` and twice the spend without a durable key |
| Counters `calls` / `infra_failures`, the verdict-bit release (a write to the shared `Releases` object), and the campaign-budget check-then-run were all done on the running thread | by reading the code; covered by the single-writer design rather than by a separate failing test |

## Design

- **Single writer, not locks.** The Evaluator's state is mutated only in `begin()` / `finish()`, and only on one thread,
  the first one that calls them. A second writer thread gets an error. `attempt()` (the harness run) is the only part
  that may run on other threads. `run_harness` = `finish_run(execute_run(...))`: the release emission happens in
  `finish_run`, on the writer.
- **`EvaluatorExecutor.pump()`** runs on the allocator's thread. It commits each completion in this order: score and
  release, the R2-2 multiplicity check, budget charge, then either an infra retry or the final outcome. Then it
  reports the unit to the allocator.
  - The allocator's source of truth is the Evaluator's cache (`EvaluatorUnitStore`).
  - Errors propagate unchanged (`BudgetExceeded`, `MultiplicityDrift`, a crashing run), as in the sequential path.
  - `close()` waits for every run still in flight and commits it, so its spend is charged.
- **Budget.** It is checked before every attempt, on the writer thread. A campaign can therefore overshoot its cap
  by the runs already in flight: at most `race_slots`, compared with 1 sequentially.
- **Durable unit outcomes.** With `Evaluator(durable_key=...)`, each final unit outcome is appended as a store record
  of kind `evolve_unit` AFTER its charge. A new Evaluator with the same key starts from these records, so a recorded
  unit is never re-run and never charged again.
  - A crash between the charge and the record makes the unit run again on resume. That second run really happens and
    is charged once itself, so no run is charged twice (`test_a_crash_between_charge_and_record...`).
  - The record is host-only and is not among the kinds the evidence export writes.
  - Not wired into the driver/CLI. A whole generation is still not resumable, because the proposer is called again.
- **Blob store.** Each writer uses its own temp name. When Windows refuses the rename, the writer retries; an intact
  destination with the same digest counts as the blob.
- **Wiring** (`EvolveConfig`, checked by `check_race_config` before any run, fail closed):
  - `race_allocator="batch"` (default, unchanged), `"per_completion"` or `"voi"`.
  - `race_slots` defaults to 1.
  - `race_slots > 1` is refused unless `race_concurrency_ack=True` (see below).
  - Only the race step changes. The fresh re-measure, gate, LOFO/LODO, cost and exclusion checks are untouched; the
    loop has one `_race(...)` call and one acceptance path.

## Why `race_slots > 1` is refused by default (needs owner decision)

The allocator returns `race()` over the recorded scores. Its decisions therefore equal the batch race's whenever the
unit scores are the same. On the Kernel-backed Evaluator, though, a unit's score is not a pure function of
(spec, unit). It also depends on the ledger history:

- K4's transcript bound counts the verdict-bit releases logged before a claim's registration (inferred from
  `kernel/checks.py:target_level`, `kernel/releases.py:transcript_bound`).
- Concurrent runs over-taint each other's releases through the seq windows (conservative, but timing-dependent).
- `Vault.register_dataset` is check-then-append. This one is not reachable in the race: the parent has already run
  every evolve unit, so the datasets are registered.

With 1 slot the allocator runs the harness in exactly the batch order (verified on the real Evaluator by
`test_per_completion_over_the_evaluator_decides_as_the_batch_race`). With more slots the order differs. Equal
decisions are then shown only for order-independent scores: the module-level worlds in `test_evalrace.py` and the
seeded worlds below.

## M0 ("a Docker outage mid-campaign changes no accept/reject decision")

**It does not hold on 4bf1a3d, independent of this branch** (verified).
`tests/test_infra.py::test_docker_outage_mid_campaign_changes_no_decision` failed on 4bf1a3d: the clean campaign
accepted and the flaky one rejected with "evolve: 2/2 units excluded for infrastructure failures (> 25%)".

Diagnosis (diagnostic tests, not committed):

- Verified: the order of evaluations was identical in both campaigns.
- Verified: the winner (`max_fixes=2`) ended INFRA_FAILED on all 6 of its units, even after the Evaluator's retry.
- Verified: in a single flaky run of the winner, the `certify` node failed with `InfraError` after its 3 node-level
  attempts.
- Verified: K3 now demands 11 consecutive clean replays at the defaults eps_max 0.2 and beta 0.1
  (`kernel/checks.py`).
- Inferred: the test's outage model fails exec calls 5 and 6 of every 11. A node that makes 11 or more sandbox execs
  can therefore never complete, whatever the retries. The parent gives up before `certify` and is unaffected.
- So the outage makes the better candidate unevaluable, and the gate rightly fails closed.

Options (owner):

1. Retry per K3 replay inside the Kernel. This is a Kernel core change, under the governance rule.
2. Make the test's outage time-bounded, or bounded per window, instead of permanently periodic. This changes an
   existing test, so it is the owner's call.
3. Accept that a persistent periodic outage legitimately prevents acceptance, and restate M0 as "a transient outage
   absorbed by retries changes no decision".

What this branch shows instead:

- The per-completion race over a real Evaluator, with sandbox outages (some units recover on retry, one never does),
  decides exactly as the batch race over the same flaky environment, with slots 1 and 3. An excluded unit is dropped
  from the pair, never scored 0 (`test_outages_change_no_race_decision_and_are_excluded_not_scored`).
- Seeded worlds (below): with p_infra 0.05 and one retry, 1 of 300 worlds changed its decision between the clean and
  the outage environment. In that world a unit was excluded after its retry also failed, so the data differ. A pure
  model cannot make "no decision changes" true once a unit stays excluded.

## Simulations (scripts/sim_allocation.py; world model in its docstring; UNCALIBRATED)

See the final report of the b-control builder for the exact runs. Summary at the time of writing:

- `equivalence --worlds 300` (seeds 0..299): 2,400 comparisons (2 environments x 4 slot counts). Per-completion vs
  batch gave **0** differing winners and **0** differing accept/reject decisions after the full acceptance pipeline.
- `voi`: see the table in the report. False-accept rates are given with Wilson 95% two-sided intervals.
  - False accepts: VOI never exceeded the race in any variant.
  - Spend: with the race's spend as its cap, VOI never used more race evaluations than the race in any world.
  - Selection quality: **VOI did not beat racing**. Its default stopping rule stops early and selects the true best
    candidate less often, especially in the larger worlds. At equal spend (`voi_equal`) see the report.

## Parameters that are UNCALIBRATED

- `VoiConfig.n_init`, `prior_sd`, `prior_df`, `stop_frac`.
- `EvolveConfig.race_timeout_s`.
- The simulation's whole world model.
- The allocator's slot count.
