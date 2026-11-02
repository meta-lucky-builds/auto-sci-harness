# Downgrade-only LLM controllers (GOAL §1.1, §5 L1)

`src/custom_harness/control/downgrade.py`, `DowngradeGate`. An LLM controller only produces text; the gate parses
it strictly and applies at most three action types, each re-checked by code:

| Action | Effect | Code check |
|---|---|---|
| `deny` | PENDING admission request -> DENIED | request exists and is PENDING |
| `shrink` | lower one budget cap | named budget, unit in `budget.UNITS`, finite number (not bool), >= 0, strictly below the current cap (unlimited may go to any finite value) |
| `kill` | `kill(run_id, "controller_<kind>")` callback | run exists and is still killable (same rule as studio kill), evidence of a closed kind that code recomputes |

Evidence kinds (the claim must equal the recomputation, then the condition must hold):

- `budget_exceeded {unit, spent, cap}`: `store.run_cost(run)` in `unit` equals `spent` (rel/abs tol 1e-9), `cap`
  equals the cap of the Budget the run was given (`run_budgets[run]`), and spent >= cap.
- `no_progress {steps, error_sha256}`: the last `steps` trace steps exist, all failed, and every error's sha256 equals
  `error_sha256`; `steps` >= `no_progress_min_steps`.
- `stalled {last_step_index}`: the run is RUNNING, its last step index equals the claim (-1 = no step), and the clock
  is >= `stall_s` past that step's end (or the run's start). PAUSED runs wait for a human and are never `stalled`.

Rejected, fail closed, ledgered with a `ReasonCode`: every other action type (grant, admit, approve, revive, unkill,
enlarge, ...), extra/missing fields, wrong types, non-JSON, duplicate JSON keys, NaN/inf, negative or non-decreasing
shrinks, unknown budgets/units/requests/runs/evidence kinds, deny of a non-pending request, kill of a terminal or
already-killed run, kill without evidence, evidence that does not match or does not meet the condition, and any
exception in a checker (`checker_error`) or effect callback (`effect_error`).

Batch semantics: a bad envelope (`{"actions": [...]}` is the only accepted shape; size and count limits) rejects the
whole proposal. Otherwise each action is decided independently, in order: legal downgrades in a proposal that also
holds illegal actions are applied.

Ledger: `controller_proposal` (controller id, sha256 of the raw text, action count, envelope code) and one
`controller_action` per action (controller id, proposal sha256, index, action type or `invalid`, target id only if it
names a known object else `target_sha256`, outcome, reason code, and the applied effect). The LLM's free text is
never stored (§16.5.8).

## Uncalibrated

- `DEFAULT_NO_PROGRESS_MIN_STEPS = 3` and `DEFAULT_STALL_S = 600` are guesses, not measured.
- `MAX_ACTIONS = 16`, `MAX_PROPOSAL_CHARS = 65536` are arbitrary limits.

## Not enforced / not wired (yet)

- Bound to the studio 2026-10-08 (branch w2-wiring, `studio/controller.py`, `chh supervise`,
  `tests/test_studio_controller.py`): an automated controller's kill marks the run FAILED and is ledgered as
  `controller_action` (never `run_kill`, which stays human-only); `studio.kill.kill_entry` returns it, so the engine
  stops the run before its next step and refuses resume and fork. Not bound: deny and shrink have no studio target
  (no persisted admission queue, no live per-run Budget object), so from the CLI they are always rejected; the
  Evaluator's campaign budget is not exposed to a controller.
- `AdmissionRegister` is in memory; only the ledger records persist.
- The controller's own model-call cost is not charged to any budget by `ask`.
- If an effect callback partly acts and then raises, the action is ledgered `rejected / effect_error` although part of
  the effect may stand (it is still only a downgrade).
- A store failure while ledgering raises; an effect applied just before it would then be unledgered.
