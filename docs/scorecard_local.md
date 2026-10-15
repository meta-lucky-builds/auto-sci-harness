# Local scorecard, publication gate, loop labels, third-party reproduction (s3-report, 2026-10-09)

GOAL §2 (axes A–H, rules 4 and 5), §1.3 (VG Table 10 checklist), §6.1 and invariants 8, 9 and 10, §11 M8.
The pinned-benchmark registry is empty (benchmarks need downloads, an owner decision), so this note covers what
can be computed on this machine with no download and no model call.

## `chh scorecard local`

```
chh scorecard local [--repo DIR] [--museum-log FILE] [--no-git] [--json]
```

It reads the store of `CHH_DATA_DIR` (all recorded runs), the repo (`redteam/findings.yaml` and its git
history) and an optional museum run log. It prints a table, the losses, and an **honesty footer** listing every
axis or metric that is NOT MEASURED or NOT COMPARED, each with its reason. Exit code 1 if any source reference
does not resolve. Code: `src/custom_harness/scorecard/local_axes.py` (new file; the shared `axes.py`,
`facts.py` and `registry.py` are reused and not edited).

| Axis | What is computed | Today |
|---|---|---|
| A, C, D, E | only from `scorecard_run` records on a pinned benchmark | NOT MEASURED, with the reason per axis (downloads; M2 exit budget; no known-value problems; time-split data) |
| B (partial) | accepted gate gain per 1k harness runs; OOD drop (LOFO) of accepted edits. Only generations whose `generation_stats` record `harness_runs` (written since this change) count | NOT MEASURED in the real store: no evolve generation there. Never compared: no re-run baseline campaign |
| F | claims without evidence and unverifiable citations (from `axes.axis_f`); reports whose checklist passes the publication gate and whose claim bundle is ledgered; runs with a Reproducibility Bundle still on disk at its manifest hash, and a trace (cost record) | see below |
| G | cost per Kernel-certified **live** claim. Unknown units stay unknown: USD is unknown for any run with a subscription (byos), open-weights or unpriced API call (the gateway stores an unknown price as `usd=0.0`); GPU hours are "not metered". Runs whose calls are all mock or replayed are "scripted" (no real spend) | "undefined: 0 certified claims" |
| H | planted-exploit catch rate = KILLED / registry patches in the log, Wilson 95% two-sided interval, patches missing from the log listed (and the metric fails); red-team findings (open + accepted = unfixed); trend by date from the git history of the registry (last commit per day); leak-audit pass rate | museum not run by this change (see below) |
| loop labels | every loop instance of the store with its label (`loop_labels.audit_store`) | |

Ref kinds added for invariant 8: `repo:<path>:<sha256>` (working-tree file), `git:<commit>:<path>:<sha256>`
(committed file), `file:<sha256>:<absolute path>` (the museum log). `check_local` resolves every source.

Output on a COPY of the real store (`custom-harness-data/store`, one live research run, 2026-10-08) with the
integrator's museum log `museum1.log` (45/45 KILLED, no commit recorded in the log):
F claims without evidence 0, unverifiable citations 0, reports with checklist and bundle 0/1 (its checklist
predates the loop-label item), runs with bundle and cost record 1/1; G total: 234,528 tokens (estimated),
13 quota units, 8,072 s wall, USD unknown, GPU hours not metered, cost per certified live claim undefined
(0 certified claims); H catch rate 45/45, Wilson 95% two-sided [0.921, 1.000], status fail because the registry
has 46 patches and R2-MAP is not in that log; findings 60 (1 open, 13 accepted); trend 2026-10-07 (5 open +
13 accepted of 55) → 2026-10-08 (1 open + 13 accepted of 60); leak audit 1/1; loop labels: 1 research run,
MECHANICAL.

## Publication gate (VG Table 10)

`src/custom_harness/publication.py`: the Kernel checklist's 16 items plus a **loop-label item**. "Answered" means
a non-blank string (the Kernel's `missing` accepts any truthy value, e.g. `"   "`; the Kernel is not edited).
A loop report must say exactly MECHANICAL or VALIDATED; a report that is not of a loop must say
`not a loop: <why>`. Who is a loop is decided by the caller from the report's source, never from the text.

Publication paths, checked end to end (`tests/test_publication_gate.py`):

* `chh audit publish` (`audit/gate.py`), the only export path. **Hole found:** it shipped `report.md` with no
  reporting checklist at all (no traces, cost, selection policy, human-in-the-loop points), published without the
  bundle hash, and published an outcome without a certificate. Now it fills the checklist (`audit/checklist.py`),
  refuses on any blocker, refuses an outcome without a certificate, and ships `checklist.md` with its sha256 and
  the items in `manifest.json`.
* Research reports (`research.certify` / `research.report`): never exported, but the source of headline results.
  **Holes found:** a blank answer counted as answered; the loop label was not a checklist item; the report never
  said whether it was publishable; `headline()` ignored the blockers. Now the checklist carries `loop_label`, the
  stricter gate adds blockers, the report states "Publication: allowed" or "BLOCKED (NOT PUBLISHABLE): reasons",
  and a headline needs both claims publishable.
* The scorecard bundle (`chh scorecard build`) is not a claim report; it is checked by `chh scorecard check`.

## Loop labels (invariant 10)

`src/custom_harness/loop_labels.py`: `audit_store` (runtime: every `generation_stats`, `harness_edit`, decision
`lineage` row, `generation_ledger` through its `generation_stats` reference, and every research run's report line
against the label recomputed from its certificate) and `static_problems` (AST: every writer of those records and
the evolve driver summary names `loop_label`). **Gaps fixed:** `generation_stats` (the one record per generation)
and the driver's `gen<N>.json` summary had no label. `generation_stats` now also records `harness_runs` (axis B).
Neither field is in `export._STATS_VIEW`, so neither reaches the proposer.

**Conflict left to the owner:** the proposer-visible generation report (`evolve/generations.py`) omits the label
on purpose (verdict embargo, invariant 18 treats it as a gate outcome). Invariant 10 asks every report to state it.
One way out: decide a generation's label before it runs, from the declared data roles of its gate tasks, so it
carries no outcome.

## Third-party reproduction (GOAL §2.4, §7 row 1)

`tests/test_third_party_reproduce.py` runs a **scripted (mock-gateway) research run** (no live certified claim
exists), copies its claim bundle to a fresh directory, deletes the original store, data directory, workspace,
task files and bundle, then runs `python -m custom_harness.cli reproduce <bundle> --dev-sandbox` as a separate
process in another working directory with another `CHH_DATA_DIR`. `chh reproduce` now also prints `headline`
(label, tier, per-target estimate and interval of the RE-DERIVED certificate); the test requires exact equality
with the original numbers.

## Not done here

* The museum was not run (it re-runs every finding's tests in a copy of the tree; the machine was saturated).
  A catch rate on a log made at a known commit needs `python scripts/museum.py` through the limiter.
* `axes.axis_g` (shared scorecard bundle) still sums `usd` including unknown 0.0; the Kernel checklist renders
  "$0.0000" for subscription runs (`kernel/checklist.py fill`). Both are the same "unknown cost shown as 0" class;
  neither is edited here (shared file owned by another workstream; Kernel).
* Audit arm runs are created without a per-run bundle (invariant 9); axis F reports such runs as offenders.

## Review fixes (2026-10-14, after a read-only review of this branch)

* Axis B: every (accepted edit, held-out target) OOD value is listed (two edits on one family no longer
  overwrite each other); a negative one is a loss. Accepted edits are still matched to generations by parent
  hash: two campaigns from one parent in one store are not told apart (stated in the metric note).
* Axis F: the run-bundle records, the bundle zips (`runbundle:<manifest sha256>:<path>`), the certificates and
  their ledgered bundle entries are now sources, so a deleted zip or ledger entry fails `check_local`.
* Axis H: a museum log that names no commit, or another commit than this checkout, makes the catch rate fail as
  stale. (The integrator logs used above name no commit, so on the real store the catch rate is now "fail: stale".)
* Axis G: a call's USD counts as known only when it is the single real API call of a step reporting USD > 0.
  Definition, stated: cost per certified live claim = the spend of ALL runs in the store / the live certified claims.
* `chh reproduce` compares the re-derived headline numbers with the bundled certificate's and fails on any
  difference (`original_headline` in the output).
* Audit checklist: a missing human step leaves `human_in_the_loop` unanswered (blocks); `not a loop:` needs a reason.
* Loop labels: the replica checklist's label is cross-checked too.
