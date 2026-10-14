# Feature-provenance rulings in the Kernel ledger (GOAL §16.5.6)

## What changed
Human rulings on routed features (`legitimate` / `leak`, `chh rule-feature`) used to live in an INTERIM
hash-chained JSONL file, `data_dir()/provenance/rulings.jsonl`. That chain detected an edited, reordered or deleted
line but not the removal of TRAILING lines: nothing outside the file recorded how long it was. A deleted trailing
`leak` ruling therefore vanished without trace.

Rulings are now Kernel ledger entries of kind `feature_ruling` (already in `kernel/ledger.py` `KINDS`; no Kernel
change was needed). The writer is `verify/provenance.py` `record_ruling(..., store=...)`, through
`Ledger(store).append`.

## Entry body
| field | meaning |
|---|---|
| `data_digest`, `tags_hash`, `report_hash` | binding to the screen report the human saw (unchanged) |
| `feature` | the routed item (a column, or `__rows__` for the duplicate audit) |
| `ruling` | `legitimate` or `leak` (closed set) |
| `approver` | the human, stripped, non-empty |
| `note`, `timestamp` | free text for the reader; UTC ISO time of the ruling |
| `recorded_by` | `chh rule-feature` or `chh feature-rulings-migrate` |
| `migrated_from`, `migrated_by` | migration only: `{path, line (1-based), line_hash}` and the operator |

## Human-only authorship
- `record_ruling` refuses an approver that is empty, whitespace-only or not a string, and a ruling outside
  `{legitimate, leak}`; it also refuses an item the current screen does not route (refused features cannot be ruled
  legitimate) and a ledger whose chain, or any `--anchor` given, does not verify.
- `chh rule-feature` (its only caller) still needs an interactive terminal and the re-typed feature name.
- The gate ignores a `legitimate` entry whose `approver` is missing, empty, whitespace or not a string (it then
  reports `missing_ruling`). A `leak` entry counts even without an approver: it can only block, never pass.
- Not enforced (inferred, stated honestly): any code holding the store can still call `Ledger(store).append(
  "feature_ruling", ...)` with an invented approver name. The ledger makes such an entry permanent and visible
  (`recorded_by`, seq, time) but cannot prove a human typed it. The same holds for every other human kind
  (`role_approval`, `statement_approval`, ...). `studio/kill.py` `_ModuleLedger.HUMAN_ONLY` blocks module-context
  writes of `run_kill` and `tier_policy` only; adding `feature_ruling` there is proposed to its owner, not done here.

## Gate (rules unchanged)
`gate(report, load_rulings(store, anchors=[...]))` refuses when: the report hash does not match its body; any
feature was refused; the chain is not intact (`ruling_chain_tampered`); a routed item lacks a current `legitimate`
ruling with a human approver (same data digest, tags hash, report hash); or any ruling on that data says `leak`
(even after re-tagging). `chain_ok` is False when the FULL ledger chain fails `verify_chain`, any body is not strict
JSON, or an anchor file is unreadable or no longer matches. Note: any break anywhere in the Kernel ledger, not only
in a ruling, fails the provenance gate (fail closed).

Trailing removal: without an anchor the ledger has the same blind spot as the JSONL (a deleted last entry leaves a
valid chain; the test shows this). With `chh ledger-anchor FILE` run after the ruling and `chh feature-screen TASK
--anchor FILE` (or `record_ruling(..., anchors=[FILE])`), the removal is `ruling_chain_tampered`. Entries made
after the latest anchor are still unprotected (docs/kernel.md).

## Migration of the interim file
`chh feature-rulings-migrate --by NAME [--file PATH]` (HUMAN, interactive: re-type the line count), backend
`migrate_interim_rulings(store, by, path)`:
- all or nothing, fail closed: refused if the JSONL chain does not verify (edited, deleted-middle, unparsable line)
  or if any line holds a ruling a human could not have recorded (no approver, ruling outside the set, missing or
  non-string field). Nothing is imported in either case.
- each line becomes one `feature_ruling` entry with the ORIGINAL approver, note and timestamp, plus `migrated_from`
  and `migrated_by`.
- idempotent: a line whose hash is already in some entry's `migrated_from.line_hash` is skipped.
- **The JSONL file is left in place, unmodified, and is no longer read** by the gate or by `record_ruling`. It is
  neither deleted nor moved (an operator may archive it by hand). A trailing line removed from it BEFORE the
  migration cannot be detected by the migration either: that is the old weakness, and why the migration is a
  human action run once.

## Not done
- No Kernel code changed. `feature_ruling` bodies are not checked by any Kernel check (K0-K…); the Kernel only
  stores and chains them.
- No automatic anchoring: anchoring stays an operator action (`chh ledger-anchor`).
