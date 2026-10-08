# Scorecard (GOAL §2, §11 M8)

Package `src/custom_harness/scorecard/`. Tests: `tests/test_scorecard.py`. Parameters the owner has not set (confidence
level, bootstrap resamples and seed) are in `scorecard/config.py`, marked UNCALIBRATED.

**No model calls, no downloads.** The framework is built and tested on fixtures only. No real benchmark is pinned.

## Commands
    chh scorecard build --out DIR [--museum-log F] [--findings F] [--salts F] [--registry F] [--capabilities F]
    chh scorecard check DIR        # the one command that reproduces the scorecard (exit 1 unless reproduced)

One campaign = one Evidence store (`$CHH_DATA_DIR/store`). `build` writes `facts.json` (everything read from the store
and the input files, each with a ref), `scorecard.json` (computed from facts alone), `scorecard.md` (losses first),
`inputs/<sha256>` (input copies) and `manifest.json`. `check` fails closed unless:
- `scorecard.json` equals its recomputation from `facts.json`;
- `facts.json` equals a fresh extraction from the store;
- every number sits in a metric with sources;
- every source ref resolves.

## Pieces
- **Pinned-benchmark registry** (`registry.py`, `benchmarks.yaml`): name, version, axis, licence, content hash, item
  directory. Nothing runs on bytes that differ from the pin. The shipped registry is empty: every real benchmark needs a
  download (needs owner).
- **Baseline re-run runner** (`runner.run_system`): runs a `System` on the pinned items and appends a `scorecard_run`
  record with per-item scores in a blob. `record_as_reported` stores an authors' number as `scorecard_as_reported`,
  labelled "as reported (not re-run)". The scorecard uses an as-reported number only for a system that has no re-run,
  and labels the verdict's basis.
- **Comparison**: paired over the identical item set, with a seeded paired-bootstrap percentile interval. The verdict
  is win / lose / inconclusive against the best baseline.
- **Ablation runner** (`runner.run_ablation`) and coverage (`capabilities.yaml`): every GOAL §9 row is listed with
  its ablation switch. The rows are transcribed, 37 of them. No switch exists yet, so all 37 rows are reported as
  invariant-12 gaps. This is an honest coverage failure, not an omission.
- **Axis F** (from the store): claims without evidence, unverifiable citations, and reports with a checklist and a
  bundle. The exact definitions are in the `facts.py` docstring.
- **Axis G**: total cost and cost per Kernel-certified claim. If there are 0 certified claims, the value is
  "not measured". No baseline reports this number, so the axis is "not compared".
- **Axis H**: four inputs:
  - the museum run log gives the planted-exploit catch rate;
  - `redteam/findings.yaml` gives the open-finding rate (a trend needs successive scorecards);
  - run leak audits (I5) give the leak-audit pass rate;
  - a salt campaign audit JSON gives the salts numbers.

  When an input is absent, its number is "not measured".

Every axis that fails, loses, is inconclusive, not compared or not measured is listed under `losses` / `not_shown`
(GOAL §2.5). The overall verdict is "best" only if both lists are empty.

## Known limits
- Certificates are counted as filed (content-hash checked), not re-derived; `Kernel.verify_status` re-executes them.
- Reports are research-run reports (final checkpoint state with `report`). Claim Audit outcomes contribute their
  citations, not a report row.
- The latest run of each system decides a comparison; how many runs of our system exist is shown (`runs_of_ours`).
- Axes A–E stay "not measured" until the owner approves benchmark downloads and a live budget.
