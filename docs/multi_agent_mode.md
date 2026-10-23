# L4 multi-agent research mode ("station")

GOAL §9, row The Station: "decentralised agents, archive coordination, exact evaluators, stagnation lanes", homed
at L4 multi-agent mode, L3, L7. Built 2026-10-14/15 on branch `s4-station` (base origin/int17) by s4-station; revised
2026-10-15 after a red-team review (17 findings, all addressed below).

**Status: built, NOT test-verified (tests deferred by the owner).** Checked: ruff, mypy on
`src/custom_harness/research/station/`, `python -c` import checks. No test, simulation or mode run has been executed.
Nothing here has met a real model or a real campaign.

## Files
- `src/custom_harness/research/station/archive.py`: the archive (attribution, publication, taint, rendering, releases,
  revocation, deferred publication).
- `src/custom_harness/research/station/mode.py`: `ModeConfig`, `StationMode` (lock, schedule, budgets, stagnation
  lanes, labels, the mode log, deferred sweep).
- `src/custom_harness/research/station/runner.py`: the turn protocol, `EngineRunner`, `station_spec`.
- `src/custom_harness/research/station/cli.py`: `chh research multi run|status|archive|verify|revoke`.
- Shared files, additive only: `cli.py` (parser hook), `ablation.py` (switch `station_coordination`),
  `scorecard/capabilities.yaml` and `docs/ablation.md` (row `the-station`), `tests/test_memory.py` (importer
  allowlist + a stricter importer pattern).
- `examples/station.yaml`: an example config (task paths are placeholders).
- Tests (not run): `tests/test_station_mode.py`, `tests/test_station_redteam.py`.

## Model
Several research-loop agents (each with its own model bindings) work one problem or a family of problems.
Cognition is decentralised; control is not: one process runs the agents' turns one at a time, in a seeded order,
under an exclusive store-wide lock (one per store, every mode; int18). The Station's own code does this too (central tick clock, single writer;
`code_review/station.md` §3). The agents coordinate ONLY through the archive. Each agent runs
`examples/research_v2.yaml` patched by `station_spec`:
- the archive is shown in the `hypothesize` prompt as `{{input.station_archive}}`;
- every `research.recall_memory` node gets `max_entries: 0`, so the global L5 research memory (a second cross-run
  channel) is not read by station agents.

A round: for each agent in the order sorted by sha256(mode, seed, "order", round, agent), one turn:
1. **Budget.** Turn caps = min(the agent's cap left, the mode's cap left) per unit. On the primary unit, also at
   most the agent's share of the mode pool, proportional to every agent's cap left. Nothing left: `turn_skip`;
   mode pool spent: `budget_stop`.
2. **Archive view** (rendered by trusted code, below). None in the isolation rounds, none in the `explore` lane,
   verified failures only in the `revive` lane.
3. **Run.** The runner executes the turn (seed = sha256(mode, seed, "seed", round, agent) mod 2^31). Every run joins
   the agent's `budget.Campaign`. The outcome (run id, status, typed requests, window end, the turn's runs) is
   logged as `turn_outcome` before anything is published.
4. **Harvest.** Attributable certificates are published; the result is logged as `turn_published`, then
   `turn_end`. Claims registered but not certified in the turn stay open (`open_preregs`) and are published later
   against their turn when certified (`deferred`).

After each round: the deferred sweep, then stagnation lanes.

## What is enforced in code

### Attribution (whose result is it)
A certificate belongs to a turn when:
- its certificate entry AND its pre-registration are in the turn's Kernel-ledger window;
- every run its evidence names (`Evidence.produced_by_run`, `ExecRecord.run_id`; `research.confirm` records them
  under the turn's own Engine run) is one of the turn's runs (the agent's campaign runs no earlier turn owns); an
  unreadable evidence record names an unknown run (never attributable);
- if the prereg / certificate ledger entries name a run (`run_id`, recorded by the run-bound Kernel since
  s4-kernel), that run too;
- **int18 (s4-station HIGH finding fixed, stage-a):** the set of named runs must be NON-EMPTY. Exact (LP / circuit)
  evidence names no run, so such a certificate is attributed only through the `run_id` on its prereg / certificate
  ledger entry, or (entries written before the Kernel recorded `run_id`) a trusted MODULE step of one of the turn's
  runs whose output reports `{...certificate: {"hash": h}}`. The window alone never attributes.
- Only pre-registrations whose ledger `run_id` is one of the turn's runs count (`turn_preregs`: the stall signal's
  `proposed`, `open_preregs`), and only private reads by the turn's runs count as exposure (reads that name no run
  count too unless they are the Kernel's own purpose-`kernel` re-reads: over-taint, the safe side).
The **exclusive lock** is ONE lock per store for every mode (`<store>/station.lock`, `mode.store_lock`): an OS lock
on a handle held for the whole run (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere), released by the OS when
the holder dies; the file is never deleted and its content (a holder note) decides nothing, so an empty or garbage
file cannot wedge it. `run()` folds the mode log only after taking it. Every log append is also checked after the
fact (a concurrent append breaks the chain and fails closed). Certificates in the window that are not attributable
are listed as `unattributed` in `turn_end`.

### Publication (only Kernel results; L5 rules)
- **No agent text is written.** Entries are built from the certificate's PRE-REGISTERED claim fields only
  (statement, metric, direction, minimum effect, evidence class, subject/comparator hashes, exact value). The
  resolution (held-out estimate, interval, scores) is never copied.
- **Kinds.** CERTIFIED-VALIDATED / -INTERNAL / -EXACT -> POSITIVE; REJECTED on K6 alone -> NEGATIVE (honest negative
  result); a `lineage` request (hashes only) -> LINEAGE. Everything else is refused with a closed code (`PubCode`).
- **L5 store rules.** One memory namespace per mode (`station-<mode>`): content screen (canaries, private-file
  copies, `_private` keys, the run secrets of every run of the mode, injection patterns, control/bidi characters,
  the wrapper marker), quarantine first, the deterministic gate (certificate re-derived with `Kernel.verify`, not
  retracted, label fits kind, tier floor), derivation graph, cascading revocation, hash-chained log.
- **Derivation graph across agents.** Each entry lists as `parents` the archive entries rendered to its author in
  that turn. A Kernel retraction (or a human `revoke`) of a source revokes, transitively, every entry built on it,
  including other agents'.
- **Exact evaluators.** A problem is EXACT when its class is computational and an approved adapter with an exact
  checker serves its metric (today `lp`; `circuit` once approved). On an exact problem only a CERTIFIED-EXACT
  certificate of that metric is a result (float screen -> exact certificate). A computational problem with no such
  adapter is refused at open.
- **Held-out value screen (deny-only, heuristic).** Refuses an entry whose statement, effect numbers or exact value
  write a number equal, at its precision, to any held-out number the Kernel computed. Forms: decimals with >= 3
  decimals, comma decimals (`0,873`), exponents (`8.73e-1`), percentages, fractions p/q with q >= 100.
- **Deferred certificates.** A claim pre-registered in a turn and certified after it (future data, a deferred
  resolution) is published against that turn: same author and parents, exposure frozen at the pre-registration,
  evidence runs among the author's campaign runs. Logged once (`deferred`); it never feeds a stall signal.

### Reading (quoted data; target-derived data only as ledgered releases)
- An agent sees other agents' readable entries only, as one JSON line per record inside the untrusted-data wrapper
  with a content nonce. Parents appear only as per-render references (`entries_shown_here`) and a count, never as
  raw ids. Every rendered line is screened again; any finding withholds the entry.
- **Taint.** `verdict` = the certificate's generalization targets; `continuous` = every target the AUTHOR read
  privately (vault `data_access`, private) before this claim was pre-registered. An entry with continuous taint is
  rendered REDACTED: only kind, author, problem, label, status and a per-render reference.
- **Releases** (`kerneld.releases.Releases` -> `Kernel.record_release`, counted by K4), alphabet **`count`** (a
  publication outcome is three-valued: published positive / published negative / nothing). Before an entry first
  reaches any agent: one per `verdict` target, and for a redacted entry also one per `continuous` target (its
  existence and timing were chosen by an author who had read those targets). Each release is journaled in the mode
  log (`intent` before, `done` after); a `done` pair is never released again; an `intent`-only pair (a crash
  between) is released again (an over-count, the safe side). A failing release withholds the entry.

### Stagnation lanes and budget reallocation
- **The stall rule is `evolve.generations.stall_evidence`** (as `control/islands.py`). A stalled agent (outside a
  cool-down of `stall_window` rounds) gets the next lane, `explore` then `revive`; progress returns it to `exploit`.
- **Reallocation only through the downgrade-only gate.** The stalled agent's primary cap is lowered to
  spent + stall_shrink x (cap - spent): the proposal is computed once, logged (`lane_intent`), then submitted to
  `control.downgrade.DowngradeGate` (controller id `station:<mode>:r<round>`) unless the gate already has a record
  of that controller id and proposal hash. Caps are the minimum over EVERY applied gate shrink of the agent's budget
  name, so they can only go down; pool shares follow the caps left.

### Budgets, crash and resume, determinism
- Per-agent spend = the agent's `Campaign`; mode spend = their sum. Each turn's caps are the run's Engine `Budget`.
- The mode log (`station:<mode>` store records) is hash-chained and append-only; every decision is folded from it.
  After a crash: a turn with `turn_end` is never re-run; one with `turn_published` only writes `turn_end`; one with
  `turn_outcome` is harvested from that record (same window, same runs, same requests); an open turn resumes its run
  (or harvests a terminal one) with the caps and archive context recorded at `turn_start`; a re-harvested entry of
  the turn counts as published. An infrastructure failure leaves the turn open (exit 3).
- Reopening with another config, spec hash (also checked against the runner's), model bindings, exact-adapter set or
  ablation is refused. A broken chain refuses the mode.
- Order, seeds, problems, lanes and caps are functions of the config, the seeds and the store (sha256, no
  `hash()`, no wall clock in the schedule).
- `status` / `archive` / `verify` write nothing: no campaign is opened, no retraction applied (an entry whose
  certificate is retracted but not yet cascaded is flagged `retraction_pending`), no read logged.

### Independence and labels
- **Invariant 5.** At open, each agent's bindings go through the runner's `policy.model_policy`; a violation is
  refused unless the CONFIG sets `allow_same_family` (a runner cannot override it), and is then recorded as
  "invariant 5 NOT MET" for that agent and for the mode.
- **Replication independence.** `archive.corroborations()` lists pairs of positive entries by different agents on the
  same problem and metric; INDEPENDENT only if both met invariant 5 and their proposer/executor families are
  disjoint and known. Reporting only: no credit anywhere.
- **Labels (invariant 10).** Turns, agents and the mode carry MECHANICAL or VALIDATED: VALIDATED only when there is
  at least one progress result and every one is CERTIFIED-VALIDATED. Exact and internal results are MECHANICAL.

### Ablation
Switch `station_coordination` (closed registry, `ablation.py`; row `the-station`): no archive shown, nothing
published (code `ablated`), no lane or shrink, no deferred publication; same schedule, seeds and budgets. An
ablated mode (any non-empty AblationSet) uses `<mode_id>-ABLATED` for its archive namespace, campaigns, budgets,
controller ids (upper case: no production mode id can take that name; the lock is store-wide since int18) and the log kind
`station:<mode_id>:ABLATED`; the mode refuses a runner with another ablation set. The row stays a PARTIAL switch
(exact evaluators are an integrity mechanism; the switch is library-level only).

## What is NOT enforced (stated)
- **`Kernel.run_id`** is set by trusted host code (a residual stated by s4-kernel): host code could assign it by hand.
- **The lock** binds processes that use this code on this filesystem. A process that writes Kernel entries to the
  store without the lock (another tool) is not excluded; since int18 it can no longer get an exact certificate
  attributed by the window (a named run is required); its private reads naming no run over-taint (safe side).
- **Channels outside the archive** that a custom spec opens (a module reading another run's data) are not closed;
  `station_spec` closes the known one (L5 recall). Checked by reading: shipped research modules dereference blobs
  only from their own run's state or through handles vetted against their own run's trace (`evidence_refs.vet_blob`,
  `research.recall`); none takes another agent's artifact or code hash from model output. The archive shows
  subject hashes in full records; no shipped module turns a hash from a prompt into a read (inferred, by reading).
- **Redaction is the control, the numeric screen a heuristic.** The taint rule trusts the vault's `data_access` log.
- **Taint is conservative and costly.** After an agent's first held-out read, its later entries are redacted. Fresh
  (minted) targets per round are the way to share more (owner decision).
- **A paused run** (approval node) ends its turn as `paused`; a certificate issued after a later human approval is
  published by the deferred sweep only if its evidence runs are the author's campaign runs.
- **Runner protocol.** A custom runner that does not join the agent's campaign has its reported run joined by the
  mode; runs it never reports are not charged and their evidence is not attributable.
- **Gate records** (`controller_action`) are not hash-chained: deleting one would raise a cap; forging one can only
  lower a cap.
- **Overshoot.** A turn can overshoot its caps by the cost of the step running when they are reached.
- **Performance.** Archive reads fold the memory log per call and the screens scan every certificate: quadratic in
  archive size; fine for tens of entries, not thousands.
- **Not built:** cross-agent replication credit, an LLM lane controller, Station's peer voting, multistart
  branching, lineage prompt inheritance.

## Findings while building (generalised, outside this module)
- **Negative memory carries held-out values without a release** (confirmed by the red team, by reading):
  `memory/store.negative_proposal` writes held-out estimates into entry text; `research.recall_memory` renders them
  with no release. s4-integrity is fixing it globally (stripping the estimates). Station agents do not read it.
- **The memory importer check missed deeper relative imports** (`from ...memory`): fixed in `tests/test_memory.py`.
- **A Kernel without a replayer re-derives no data claim**: the station CLI gives its Kernel a sandbox replayer
  when a sandbox is chosen. `chh memory harvest-negatives` builds `Kernel(store)` without one (inferred).

## Tests to run later
`tests/test_station_mode.py`, `tests/test_station_redteam.py`, `tests/test_memory.py`,
`tests/test_ablation_core.py::test_registry_and_capabilities_yaml_agree`. Each test docstring states what it fails
without.

## Owner decisions needed
1. Taint strictness: keep "any earlier held-out read redacts", or trust the engine's persisted taint labels.
2. Release alphabet: `count` for a publication outcome (chosen, fail closed) vs `verdict_bit` (cheaper for K4).
3. Station constants (UNCALIBRATED): isolation rounds 1, 16 entries / 12k characters per view, stall window and
   shrink reused from the islands' config.
4. Minted fresh targets per round for station problems.
