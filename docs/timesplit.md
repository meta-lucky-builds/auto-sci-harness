# Time-split prediction benchmark (GOAL §6.4, §4.5, M7)

Package: `src/custom_harness/timesplit/`. Tests: `tests/test_timesplit.py` (offline fixtures only).

## Flow

1. **Freeze at T.** `freeze_benchmark(snapshots=, blobs=, as_of=T, windows=, tasks=, data_files=)` does three things:
   - It freezes the literature through `SnapshotStore.freeze(as_of=T)`. This module has no literature freezer of its own.
   - It stores each data file in the `BlobStore` as `{name: {sha256, dated}}`. A file dated after T is refused (G1).
   - It writes a content-addressed manifest. The manifest's digest is the benchmark id, and `load_benchmark`
     round-trips it.

   The windows are half-open `(start, end]`. They must be contiguous and strictly increasing, and the first one must
   start at T. Each task names one of the declared windows by index.
2. **Tasks.** Each task has an id, a question, a pure resolution rule, a window index, key phrases and an answer.
   - The rule language is closed. It has one kind, `threshold`, over a JSON field path or one CSV row selected by
     `where`. Unknown kinds or keys are refused.
   - The `answer` is used only by the contamination probes. It must never be shown to the predictor. The caller has
     to strip it before building the predictor's prompt; this module does not build prompts.
3. **Register.** `register_prediction(benchmark, task_id, harness_id, q, p0, registered_on)` refuses two cases (G2):
   `registered_on` after T, and `q < p0`. Every registration has a content hash. The checks run again in `score()`.
4. **Resolve.** `resolve(benchmark, task_id, Observation(content, dated, source))` refuses some observations (G3):
   any dated on or before T, and any dated outside the task's window. The function is pure (G4): no clock and no
   randomness. `Resolution.record_hash` equals its `BlobStore.put_json` digest.
5. **Audit.** `audit(benchmark, snapshots, blobs, memorisation=None)` runs three contamination probes. Each one
   produces a per-task flag with a reason:
   - **answer-leak**: the normalised answer appears in a snapshot entry body or in a frozen data file.
   - **resolved-before-T**: a frozen data file already determines the task under its own rule.
   - **memorisation**: runs through a `MemorisationProbe` protocol.

   The audit also computes the prior-art weight `w = 1/(1+hits)`. This is §6.4's novelty weight. `hits` counts the
   snapshot entries that contain any of the task's key phrases.
6. **Score.** `score(benchmark, predictions, resolutions, as_of_now, audit)` scores one harness and reports per window:
   - n, resolved, expired and pending counts;
   - VDS through `kernel.predictions.vds`;
   - calibration through `kernel.predictions.calibration`;
   - the same numbers on the low-prior-art subset (§6.4's novelty-weighted subset, `w >= LOW_PRIOR_ART_THRESHOLD`);
   - `weighted_vds = sum w_i * gain_i`.

   Scoring uses the Kernel's e-process: `LR = q/p0` if the outcome is true, else `(1-q)/(1-p0)`. A task still
   unresolved after its window ends counts as an **expired miss**. Flagged tasks are excluded from every headline
   number and listed in `excluded`.
7. **Compare.** `compare(seed_scorecard, evolved_scorecard)` reports, per window, the paired per-task difference in
   gain and in Brier score, each with a seeded paired-bootstrap CI.
   - The verdict on the last window is `"evolved better"` only if every one of these holds:
     - both CI lower bounds are above 0;
     - the memorisation probe ran;
     - the last window has closed;
     - no task in the last window is unpaired or pending;
     - there are at least `MIN_PAIRS_FOR_VERDICT` pairs.
   - Otherwise the verdict is `"not shown"`, and the reasons are listed.

All parameters the owner has not set are in `timesplit/config.py`, and each is marked UNCALIBRATED (§13.8).

## Needs owner

- **Post-cutoff data and a real T.** T must come after every executor model's cutoff, and the run needs real
  observations dated in `(T, T1]` and `(T1, T2]`.
- **Model budget** for evolving the harness on `(T, T1]` and for running it on `(T1, T2]`.
- **A live memorisation probe.** Only `FakeMemorisationProbe` ships. Without a live probe, `compare()` never reports
  success. The fake probe's name (`fake-offline`) is recorded in the scorecard.
- **Calibrated values** for every parameter in `config.py`.

## Known limits

- `registered_on` is a declared information date and is not checked against a clock. The real protection is that
  the harness reads only the frozen snapshot (`OfflineTransport`). In a real run, registration happens after T on
  the wall clock, so the owner should decide whether G2 means "information date ≤ T" (as built) or a wall-clock rule.
- The literature snapshot freezes whatever was recorded. Envelopes fetched after T can mention papers published after
  T. This module does not filter by publication date (see `literature/records.py`, `visible_as_of`). The answer-leak
  probe catches only literal answer strings.
- Matching is exact substring matching after casefolding and whitespace collapse. Paraphrases, numbers written
  differently, and JSON escape sequences are missed. Very short answers or phrases over-flag, which is conservative.
- The benchmark id depends on the snapshot's `frozen_at` date, which comes from the `SnapshotStore` clock.
- Skill is not reported, because time-split tasks have no pinned reference forecaster yet.
- The bootstrap is a percentile bootstrap over tasks. It assumes the tasks are exchangeable within a window.
