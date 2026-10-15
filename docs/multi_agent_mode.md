# L4 multi-agent research mode ("station")

GOAL §9, row The Station: "decentralised agents, archive coordination, exact evaluators, stagnation lanes", homed
at L4 multi-agent mode, L3, L7. Built 2026-10-14/15 on branch `s4-station` (base origin/int17) by s4-station.

**Status: built, NOT test-verified (tests deferred by the owner).** Checked: ruff, mypy on
`src/custom_harness/research/station/`, `python -c` import checks (the package imports, `station_spec` patches
`examples/research_v2.yaml`, the example config loads, the capabilities registry loads with the new switch). No
test, simulation or mode run has been executed. Nothing here has met a real model or a real campaign.

## Files
- `src/custom_harness/research/station/archive.py`: the archive (publication, taint, rendering, releases, revocation).
- `src/custom_harness/research/station/mode.py`: `ModeConfig`, `StationMode` (schedule, budgets, stagnation lanes,
  labels, the mode log).
- `src/custom_harness/research/station/runner.py`: the turn protocol, `EngineRunner`, `station_spec`.
- `src/custom_harness/research/station/cli.py`: `chh research multi run|status|archive|verify|revoke`.
- Shared files, additive only: `cli.py` (parser hook), `ablation.py` (switch `station_coordination`),
  `scorecard/capabilities.yaml` and `docs/ablation.md` (row `the-station`), `tests/test_memory.py` (importer
  allowlist + a stricter importer pattern).
- `examples/station.yaml`: an example config (task paths are placeholders).
- Tests (not run): `tests/test_station_mode.py`.

## Model
Several research-loop agents (each its own model bindings) work one problem or a family of problems. Cognition is
decentralised; control is not: one process runs the agents' turns one at a time, in a seeded order. This is what
The Station's own code does too (central tick clock, single writer; `code_review/station.md` §3). The agents
coordinate ONLY through the archive. Each agent runs `examples/research_v2.yaml` patched by `station_spec`:
- the archive is shown in the `hypothesize` prompt as `{{input.station_archive}}`;
- every `research.recall_memory` node gets `max_entries: 0`, so the global L5 research memory (a second cross-run
  channel, with held-out estimates in its negative entries, see "Findings") is not read by station agents.

A round: for each agent in the order sorted by sha256(mode, seed, "order", round, agent), one turn:
1. **Budget.** Turn caps = min(the agent's cap left, the mode's cap left) per unit. On the primary unit, also at
   most the agent's share of the mode pool, proportional to every agent's cap left. Nothing left: the turn is
   skipped (`turn_skip`); mode pool spent: `budget_stop`.
2. **Archive view.** Rendered by trusted code (below). None in the isolation rounds, none in the `explore` lane,
   verified failures only in the `revive` lane.
3. **Run.** The runner executes the turn (seed = sha256(mode, seed, "seed", round, agent) mod 2^31). Every run
   joins the agent's `budget.Campaign`, so spend is derived from the store.
4. **Harvest and publish.** The certificates of the turn are published through the archive; the turn's stall
   signal is {accepted: a NEW positive result was admitted, proposed: pre-registrations, gated: certificates}.

After each round, stagnation lanes (below).

## What is enforced in code

### Publication (only Kernel results; L5 rules)
- **No agent text is ever written.** Trusted code harvests the certificates of the turn's Kernel-ledger window
  (`TurnWindow`: the certificate entry AND its pre-registration both after the turn started and before it ended).
  A certificate of another agent's claim (re-certified in this turn), or of a claim registered before the turn, is
  not this turn's. Entries are built from the certificate's PRE-REGISTERED claim fields only (statement, metric,
  direction, minimum effect, evidence class, subject/comparator hashes, exact value). The resolution (held-out
  estimate, interval, scores) is never copied.
- **Kinds.** CERTIFIED-VALIDATED / -INTERNAL / -EXACT -> POSITIVE; REJECTED on K6 alone (a verified failure) ->
  NEGATIVE (honest negative result); a `lineage` request (hashes only: a certified certificate of this turn and a
  registered parent artifact) -> LINEAGE. Everything else is refused with a closed code (`PubCode`): integrity
  failure, unsupported label (SUPPORTED, simulated, formal: the L5 store admits none of them), foreign or
  fabricated certificate, malformed request.
- **L5 store rules.** One memory namespace per mode (`station-<mode>`): content screen (canaries, private-file
  copies, `_private` keys, injection patterns, control/bidi characters, the wrapper marker), quarantine first,
  admission by the deterministic gate (certificate re-derived with `Kernel.verify`, not retracted, label fits kind,
  tier floor), derivation graph, cascading revocation, hash-chained log.
- **Derivation graph across agents.** Each entry lists as `parents` the archive entries rendered to its author in
  that turn. A Kernel retraction (or a human `revoke`) of a source revokes, transitively, every entry built on it,
  including other agents'. A parent revoked between render and publication makes the new entry fail the gate.
- **Exact evaluators.** A problem is EXACT when its class is computational and an approved adapter with an exact
  checker (`exact_module`) serves its metric (today `lp`; `circuit` once its approval exists). On an exact problem
  only a CERTIFIED-EXACT certificate of that metric is published as a result (float screen -> exact certificate,
  the Station rule). A computational problem with no such adapter is refused at open.
- **Held-out value screen (deny-only, heuristic).** An entry whose statement or effect numbers contain a decimal
  equal, at the written precision (>= 3 decimals, or a percentage), to any held-out number the Kernel computed
  (estimates, interval ends, per-seed held-out scores and their means) is refused (`heldout_value`).

### Reading (quoted data; target-derived data only as ledgered releases)
- An agent sees other agents' readable entries only, never its own, as one JSON line per record inside the
  untrusted-data wrapper with a content nonce: data, never instructions. Every rendered line is screened again;
  any finding withholds that entry.
- **Taint.** Each entry records `verdict` = its certificate's generalization targets (the label is a function of
  them) and `continuous` = every target the AUTHOR read privately (vault `data_access`, private) before this claim
  was pre-registered: in earlier turns or earlier in this turn. An entry with continuous taint is rendered
  REDACTED: no agent-written field at all (no statement, number, hash or entry id), only kind, author, problem,
  label, status and a per-render reference. So information the author saw about held-out targets reaches another
  agent only through the label.
- **Releases.** Before an entry first reaches any agent, one `memory`/`verdict_bit` release per verdict target is
  recorded through `kerneld.releases.Releases` -> `Kernel.record_release`, which K4 counts. A failed release
  withholds the entry. An (entry, target) pair is released once: the same bit shown again carries no new
  information, and K4 counts the first release for every claim registered after it, by any agent. Following the
  codebase's convention (`kerneld.releases.VERDICT_LABELS`), a certificate label is released as `verdict_bit`.

### Stagnation lanes and budget reallocation
- **The stall rule is `evolve.generations.stall_evidence`** (the rule `control/islands.py` uses), on the agent's
  turn signals. A stalled agent (outside a cool-down of `stall_window` rounds since its last stall lane) gets the
  next lane, `explore` then `revive`, alternating: `explore` moves it to the next problem of the family and shows
  it no archive; `revive` shows it verified failures only. An agent with progress returns to `exploit`.
- **Reallocation only through the downgrade-only gate.** The stalled agent's primary cap is lowered to
  spent + stall_shrink x (cap - spent) by a code-written proposal submitted to `control.downgrade.DowngradeGate`
  (ledgered there as `controller_proposal` / `controller_action`, controller id `station:<mode>:r<round>`). Caps
  are rebuilt from the gate's APPLIED records as a minimum, so they can only go down; the pool share each agent
  gets is proportional to its cap left, so a shrink is how budget moves to the others. Nothing raises a cap. The
  resubmission of an identical proposal after a crash is skipped (idempotent).

### Budgets, crash and resume, determinism
- Per-agent spend = the agent's `Campaign` (derived from trace steps and the cost journal); mode spend = their sum.
  Caps are per agent (config, then gate shrinks) and per mode; each turn's caps are passed to the Engine as the
  run's `Budget`.
- The mode log (`station:<mode>` store records) is hash-chained and append-only; every decision is folded from
  it. `run()` after a crash: a finished turn is never re-run; an open turn resumes its run (found among the
  agent's campaign runs not owned by a finished turn) with the caps and archive context recorded at its start; a
  terminal run is harvested as is. An infrastructure failure leaves the turn open (`StationInfraError`, exit 3).
  A broken chain refuses the mode; reopening with another config, spec hash, model bindings, exact-adapter set or
  ablation is refused.
- Order, seeds, problems, lanes and caps are functions of the config, the seeds and the store: no wall clock and
  no Python hash randomisation in the schedule.

### Independence and labels
- **Invariant 5.** At open, each agent's bindings go through `policy.model_policy`: a judge/replicator from the
  author's family is refused unless `allow_same_family`, and then recorded as "invariant 5 NOT MET" for that agent
  and for the mode (in the log, `status`, and each entry's data).
- **Replication independence.** `archive.corroborations()` lists pairs of positive entries by different agents on
  the same problem and metric; a pair is INDEPENDENT only if both met invariant 5 and their proposer/executor
  families are disjoint and known. Reporting only: no credit is given for a corroboration anywhere.
- **Labels (invariant 10).** Every turn, every agent and the mode carry MECHANICAL or VALIDATED: VALIDATED only when
  there is at least one progress result and every one is CERTIFIED-VALIDATED. Exact and internal results are
  MECHANICAL (an exact certificate is internal computation, not external data).

### Ablation
Switch `station_coordination` (closed registry, `ablation.py`; row `the-station` in `capabilities.yaml`):
no archive is shown, nothing is published (code `ablated`), no lane or shrink is applied; same schedule, seeds and
budgets. An ablated mode (any non-empty AblationSet) keeps its own log (`station:<mode>:ABLATED`), archive
namespace, campaigns and controller ids; the mode refuses a runner with another ablation set. The row stays a
PARTIAL switch: exact evaluators are an integrity mechanism (no switch), and the switch is library-level only (no
scorecard System drives a station mode; the CLI never passes an ablation).

## What is NOT enforced (stated)
- **Turns are sequential.** Attribution by ledger window assumes no other process writes Kernel entries during a
  turn. Another writer's certificate registered and certified inside the window would be attributed to the agent;
  its private reads would over-taint (safe side). Nothing detects a concurrent writer.
- **Channels outside the archive** that a custom spec opens (a module reading the store or the Kernel ledger of
  other runs) are not closed; `station_spec` closes the known one (L5 recall). The Kernel's own shared state (K4
  families and alpha accounts on a shared target) couples agents statistically; that is correct accounting, not a
  leak.
- **Redaction is the control, the numeric screen is a heuristic.** An agent without earlier private reads writes
  its pre-registered fields freely; they cannot depend on held-out data unless the release log is incomplete. The
  rule trusts the vault's `data_access` log, as K2/K6 do.
- **Taint is conservative and costly.** After an agent's first held-out read, all its later entries are shown
  redacted. With a single shared target, only first-turn entries are shared in full. Fresh (minted) targets per
  round are the way to share more (owner decision below).
- **Release counting assumes verdict_bit for a label** (codebase convention). Publication vs non-publication is a
  function of the same label; a stricter reading would use `count`.
- **A crash between a release and the `turn_start` record re-emits that release on resume** (over-count, safe).
- **A paused run** (approval node) ends its turn as `paused`; certificates it produces after a later human
  approval fall outside every turn window and are never published.
- **The runner protocol.** A custom runner that does not join the agent's campaign has its reported run joined by
  the mode, but runs it never reports are not charged. `EngineRunner` joins through the Engine.
- **Budget caps are store records**: the gate's `controller_action` records are not hash-chained, so deleting one
  would raise a cap (the gate's own stated limit); forging one can only lower a cap.
- **Overshoot.** As elsewhere, a turn can overshoot its caps by the cost of the step running when they are reached.
- **Performance.** Archive reads fold the memory log per call and the held-out value screen scans every
  certificate: quadratic in archive size; fine for tens of entries, not for thousands.
- **Not built:** cross-agent replication credit (corroborations are reported only), an LLM controller for lanes
  (the gate would accept its deny/shrink proposals, but none is wired), Station's peer voting, multistart
  branching, lineage prompt inheritance.

## Findings while building (generalised, outside this module)
- **Negative memory carries held-out values without a release** (inferred by reading, not tested):
  `memory/store.negative_proposal` writes each target's held-out estimate and interval into the entry text and
  `harvest_negatives` harvests every verified failure in the ledger (all runs); `research.recall_memory` renders
  them into later runs' prompts. No `Releases.emit` is on that path. Station agents do not read it
  (`max_entries: 0`). Owner decision below.
- **The memory importer check missed deeper relative imports** (`from ...memory`): fixed in
  `tests/test_memory.py` with a regex covering any relative depth; the station archive is allowlisted.
- **Kernel without a replayer re-derives no data claim**: the archive gate (`Kernel.verify`) re-executes data
  claims; the station CLI gives the Kernel a sandbox replayer when a sandbox is chosen. `chh memory
  harvest-negatives` builds `Kernel(store)` without one (inferred: its gate would reject data negatives).

## Tests to run later (all in `tests/test_station_mode.py`, plus `tests/test_memory.py`,
`tests/test_ablation_core.py::test_registry_and_capabilities_yaml_agree`)
Attack tests: `test_attack_poisoned_archive_entry_that_tries_to_instruct`,
`test_attack_target_derived_value_laundered_through_the_archive`,
`test_attack_uncertified_result_or_foreign_certificate_is_never_published`,
`test_revoked_source_cascades_to_entries_other_agents_built_on_it`. Each test's docstring states what it fails
without.

## Owner decisions needed
1. Taint strictness: keep "any earlier private read redacts" (fail closed), or trust the engine's persisted taint
   labels (`runtime/taint.py`, `kerneld.releases.run_targets`) to redact only what reached a prompt?
2. Release alphabet for a published label: `verdict_bit` (convention) or `count` (publication is three-valued).
3. Station constants (UNCALIBRATED): isolation rounds 1, max 16 entries / 12k characters per view, stall window and
   shrink reused from the islands' config.
4. Whether to fix the negative-memory release gap globally (emit releases in `recall_memory`, or strip estimates
   from negative entries).
5. Minted fresh targets per round for station problems (needed to share more than first-turn results in full).
