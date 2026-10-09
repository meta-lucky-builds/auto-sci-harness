# Branch / route (L6.9) and temporal reveal (L6.11), wired

Branch `s3-evolve` (2026-10-09). GOAL §5 L6 steps 9, 11, 12; invariants 6, 16, 18; §16.5.3 (verdict embargo).
The modules `evolve/branches.py`, `evolve/router.py` and `evolve/reveal.py` were built on `g-router` and merged
into `int16-fix`; before this branch nothing imported the router or the reveal module. This note covers what this
branch adds. L7 (islands, policy gate) has its own note: `docs/search_over_search.md`.

## 1. Branch and route

What exists (unchanged): a candidate rejected by the global gate (`rejected_on` evolve or held_out_gate) is tested
per cluster (one public attribute: `family` or `domain`) on the gate units; the paired Student-t lower bound (Bonferroni
over that candidate's tested clusters) is reduced to ONE bit per cluster, released on every target the cluster's runs
read (`Releases.emit`, channel `evolution_score`, alphabet `verdict_bit`). The `branch` record is a whitelist (no mean,
CI, score or gate task id). The router re-checks every bit against the release ledger and refuses any route not
backed by a released winning bit.

### New: routing never bypasses LOFO/LODO (invariant 16)

Gap found (verified at a7fb608, `tests/test_branch_route_lofo.py`): with cluster key `domain`, a candidate that wins a
domain pooled while regressing on a HELD-OUT family inside that domain became a branch, and the router routed the whole
domain (the held-out family included) to it, both through `propose_branch` and through `evolve_generation`. The global
gate had rejected that candidate exactly because of the held-out family.

Rule now (`branches.propose_branch`): a branch is recorded only if the ROUTED harness it would create (the candidate on
its won clusters, the trunk elsewhere) passes `selection.non_regression` (the global LOFO/LODO check: two-sided
Student-t interval at alpha 0.05, lower bound >= -eps, at least 2 valid units, infra exclusions <= the configured share)
on EACH held-out family and EACH held-out domain of the campaign (`evaluate.check_held_out`), separately. On a unit of
an unrouted cluster the routed harness is the trunk (difference 0). eps = min(`BranchConfig.non_regression_eps`,
the loop's `EvolveConfig.non_regression_eps`), so it is never looser than the global gate. A campaign without a held-out
family cannot branch (fail closed).

The result is ONE more verdict bit, released like the cluster bits (note `branch_lofo:<branch id>`, on every target the
held-out groups' runs read) and stored as `lofo {passed, targets, note}`. The router refuses a record without a
released, passing `lofo` bit (`router.verified_bits`), so an old-format or forged record is not routable.

### New: wiring

- `evolve/driver.py::evolve(..., branching=BranchConfig(...))`: after the generations, `route_after` builds the router
  over the FINAL trunk and records its table (spec hashes and cluster names only) as a `route_table` store record and
  `router.json` in the output directory. A refused router is recorded as refused and nothing is routed (fail closed).
  Branches made against an earlier trunk are stale and not routed.
- CLI: `chh evolve SPEC --suite S --branch-by family|domain` turns branching on. Read-only views:
  `chh evolve branches SPEC [--cluster-key]` (whitelisted fields per branch record, whether its LOFO bit passed and
  whether it is routed) and `chh evolve route SPEC [--suite S] [--cluster-key]` (route table, and per task of the
  suite the spec its public descriptor routes to). `chh evolve` intercepts the words `branches` and `route`; a spec
  file literally named `branches` or `route` must be passed as `./branches`.
- Sealed split: routing reads only the public descriptor. `tests/test_branch_route_lofo.py::
  test_the_router_never_reads_sealed_data` traps every `open`/`os.open` under the sealed task directories, every Kernel
  vault read and every held-out target lookup while a router is built and sealed tasks are routed (none happen);
  `test_sealed_access_in_router_py_is_confined_to_finalize_routed` checks statically that the sealed-run capability,
  `run_harness` and `held_out_target_id` occur in router.py only inside `finalize_routed`, the one-shot human command
  (shared once-per-sealed-split record with `finalize`). Both pass at a7fb608 too (pins of existing behaviour); a mutant
  that reads sealed labels in `Router.select` is killed by both (log `scratchpad/logs/ff3_mutant_sealed.log`).
- "Tested on gate data during development": the router's development tests use gate-split scripted outcomes. No
  command evaluates a router on the gate split: its bits came from the gate split, so such a number would be circular
  and a continuous held-out value (invariant 18). The only held-out evaluation is `finalize_routed` on the sealed split.

### Not controlled (stated)

- Cluster tests are Bonferroni-corrected within one candidate, not across generations or branches. The LOFO bit is an
  additional released bit per branch; K4 counts it, but nothing here caps the number of branches per campaign.
- A held-out group with one task and many seeds makes its LOFO bit close to a per-task gate bit (same as the global
  LOFO gate today).
- Two winners on one cluster: refused (no head-to-head gate is built).

## 2. Temporal reveal for streams

What exists (unchanged): labels sealed in a host-only JSONL outside every declared evolver mount; a label is visible iff
it is dated and its date (date-only = END of that day; naive datetime, unparsable or missing = undated) is at or
before the watermark (date-only watermark = START of its day); the watermark only moves forward and never past the
clock; each first reveal is a Kernel release (channel `export`) plus a `reveal` record.

New:
- `watermark()` re-validates EVERY watermark record of the stream: each must parse, be at or before its own recording
  time and the clock, and none may move backwards; a record without a recording time fails closed. Before, only the
  last record was read, so a record appended to the store around `advance` (backdated or future) revealed future
  labels (`tests/test_reveal_wiring.py::test_attack_forged_watermark_records_reveal_nothing_early` fails at a7fb608).
- `check_mounts(paths)`, `check_destination(dest)`, `n_revealed()`.
- Driver wiring: `evolve(..., reveals=[(TemporalReveal, dest)])`. Before anything runs, each seal is checked against
  every task directory of the suite and the output directory (in addition to its declared mounts) and each destination
  must be inside a declared mount (fail closed). Before each generation the visible labels are written to the
  destination (newly visible items are ledgered then).
- Not built: a CLI to seal or advance a stream (the watermark is advanced by host code or a human through the API);
  no live stream exists.

## 3. Parameters

No new parameter beyond `BranchConfig.non_regression_eps` (default 0.0 = `EvolveConfig.non_regression_eps`'s default,
not calibrated). Existing UNCALIBRATED values: `BRANCH_ALPHA` 0.05, `BRANCH_MIN_CLUSTER_TASKS` 2, `BRANCH_MIN_UNITS` 2,
`BRANCH_MAX_EXCLUDED_FRAC` 0.25 (config.py).
