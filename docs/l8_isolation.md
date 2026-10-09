# L8 isolation: Goodhart reports owner-only, licence decision at the export boundary

GOAL §5 L8, invariants 11 and 18. Branch `s3-verifiers` (2026-10-09).

## Coupled-Goodhart report (`weights/goodhart.py`)
- The report (A-score vs sealed-score gap, slope, jump) and its points hold sealed scores: OWNER-only.
- `record_report(store, points, report)` persists them under records kind `goodhart_owner_only` with marker
  `CHH-GOODHART-SEALED`. No export reads that kind.
- `sealed_fingerprints(store)`: the markers plus the decimal forms (repr, 6 and 8 significant digits, rounded to 5/6/8
  decimals) of every recorded number with >= 5 significant digits; `scan(text, fp)` matches them as whole numbers.
- Enforced in: `evolve/export.py` `_audit` (the proposer's evidence export, and `screen_digest`, the diagnosis in the
  proposer's prompt) and `weights/export.py` (`GOODHART_LEAK`; numbers when a store is passed, the marker always).
- Static check (tests/test_l8_isolation.py): outside `weights/`, only `evolve/export.py` may import from the module, and
  only `scan` and `sealed_fingerprints`; no module outside `weights/` names `GoodhartReport`, `goodhart_indicator`,
  `GapPoint` or `record_report`.
- Limits: verbatim matching (like verify/leak.py): a number re-derived in another form, or with fewer than 5
  significant digits, evades it. A report that was never recorded with `record_report` is not fingerprinted.

## Licence (`weights/export.py`, `weights/licence.py`)
- Default: no provider permitted (`weights/config.PROVIDER_TRAINING_PERMITTED = {}`): no row is ever written.
- `licence` must be exactly a `LicencePolicy` (duck types and subclasses: `LICENCE_INVALID`); kept rows are re-checked
  just before writing (`LICENCE_RECHECK`); the manifest records the decision (`licence`: policy digest, permitted keys,
  `default_nothing_permitted`, rows checked).
- Other training-data writers in `src/`: none found (grep for sft / jsonl / training; see the s3-verifiers report).
