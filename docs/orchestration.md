# Delegation driver (GOAL §15 last paragraph; OR orx-agent-delegation, enforced in code)

`src/custom_harness/orchestration/`: a library (`Driver`) and CLI (`python -m custom_harness.orchestration`)
for the §15 multi-agent expansion. It runs no agents and calls no models: callers ask it BEFORE they spawn or spend.

- **Sessions.** The owner/orchestrator registers a *lead* (`register-lead`, which may carry its own spend grants).
  Only a lead can spawn a *helper*. Each session gets a secret token (only its sha256 is stored). Tokens travel in
  files, never in argv.
- **Brief** (`brief.py`): a closed schema in which every field is required: `brief_id, project, branch, worktree, files,
  allowed_spends, expected_output, definition_of_done, instructions`. Paths must be relative with no `..`, the
  branch name must be safe, and the instructions must have a minimum length and must not contain context-dependent
  phrases such as "as discussed" (a heuristic floor). `allowed_spends` is a whitelist of `(kind compute|model_quota,
  resource, unit, limit)`. An empty list means no spends at all.
- **Refusals** (closed enum, each ledgered with the requester and the attempted action): `nested_spawn` (a helper
  tries to spawn), `unknown_session`, `brief_invalid`, `in_flight_cap`, `total_cap`, `branch_held`, `worktree_held`,
  `spend_unauthorized`, `spend_over_budget`, `spend_invalid`, `session_finished`, `helper_cannot_claim`,
  `not_holder`, `not_parent`, `duplicate_session`, `invalid_request`.
- **Locks**: one O_EXCL file per held branch and per held worktree (normalised path). Locks written by
  anything outside the driver are honoured. `--git-repo` also refuses a branch checked out in another git worktree.
  Locks never expire. `force-release` is a ledgered HUMAN action.
- **Ledger**: hash-chained JSONL. All state is re-derived from it on every call, and an edited, deleted or
  reordered line raises an IntegrityError. A driver-wide O_EXCL mutex serialises the processes that share a root.
- **Caps** (`params.py`, UNCALIBRATED): 2 helpers in flight, 4 per lead, plus the brief heuristics.

Not built: nothing forces the gateway or sandbox to call `spend()` before a model call or a compute launch. That
wiring belongs to the gateway/sandbox owners. A crashed process leaves `driver.mutex` behind, which must be
removed by hand.
