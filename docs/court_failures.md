# Failures never spend truth (GOAL 18.6, UPGRADES N2)

A crash before the first held-out label read keeps the target and its statistical budget. The researcher sees
COULD NOT RUN with the technical reason and repeats the same command.

## The rule

`labels_read` on a COULD NOT RUN outcome is a fact read from the ledger: kerneld writes a `data_access` entry with
purpose `evaluate` before it uses a label, and the outcome asks the ledger whether one exists for the claim. It is
never assumed at the point of failure. The old code assumed `True` for any kerneld failure, so a kerneld that could
not start burned the target without reading anything.

## What each failure costs

| Failure | Label read? | Cost |
|---|---|---|
| A program crashes, times out or writes the wrong row count | no | nothing; retry |
| A prediction the metric could never score (text for `rmse`) | no | nothing: `verify.metrics.predictions_problem` runs before the first read on all three routes |
| The sandbox cannot start (`OSError`, missing image) | no | nothing; retry |
| kerneld cannot start, or fails or refuses at the first score call | no | nothing; retry |
| A failure after a score call was logged | yes | the look is spent and the outcome says so; the claim cannot run again |
| A crash during certification | yes | same |
| A crash inside the mint, before or after its `mint` ledger entry | no | repeat with the same beacon: same bytes, no second entry. Another beacon is still refused as beacon shopping |

The statistical budget is not touched by a retry: an alpha slot is reserved once at pre-registration, and a retry
pre-registers nothing.

## Routes checked

- Minted Court route (`court/minted.py`, `kerneld/ingest.minted_heldout`).
- Claim Audit route (`audit/claim_audit.py`).
- Research route (`research/claims.py`): it already re-uses finished evaluations on retry and validated shape before
  scoring; it gained the same prediction check.

## Not covered

- A process killed outright (power loss, out of memory) after a label read leaves no outcome record: the claim then
  refuses a new run ("one look per claim") and the case shows no outcome. Truth is correctly counted as spent, but
  the display has no COULD NOT RUN row for it.
- A process killed before any read leaves no record either. Nothing is spent and a repeat works.

Tests: `tests/test_failures_spend_nothing.py`.
