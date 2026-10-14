# Key graph (GOAL L4.2) and the report figure (GOAL L2, invariant 8); branch s3-tree

## Multi-table key graph — `research/keys.py`
- Tables: the task's public training file plus `TaskSpec.aux_files` (new, default empty; plain file names under
  `public/`, copied into the workspace `data/` by `research.setup`). Never the held-out features (`test_X`) or
  anything under `private/`: `research.profile_data` passes only those paths (tested by tracing every file open).
- Output (`state.key_graph`): candidate keys (no null, all distinct, type int or str; floats never), near-unique
  non-keys with exact null and duplicate counts, exact inclusion dependencies (all non-null values contained; counts
  of non-null, contained, orphans, null, distinct), partial inclusions (not inclusions), and type-mismatched pairs
  (never compared; their textual overlap is counted). Zero-padded codes ("007") are text, never the integer 7.
- Ideation: `state.key_graph_quoted` (a nonce-wrapped, `| `-prefixed, control/bidi-scrubbed quote, the same rules
  as `research.recall.quote_page`) is rendered in the research_v1/v2 `hypothesize` prompt.
- Caps (UNCALIBRATED): 8 tables, 60 columns, 200,000 rows per table, near-unique share 0.95.
- Limits: single-column keys only; aux tables are ideation context only: the solution CLI receives only TRAIN and
  TEST, so a program cannot read them at run time (that needs a kernel/execution change, not done); aux files are not
  part of the vault's dataset digests.
- Tests: `tests/test_key_graph.py`.

## Report figure — `figures/report_figure.py`
- `research.report` with `figure: true` (on in v1/v2) renders `heldout_effect`: per certificate (claim, and the
  replication if certified), the held-out effect estimate with its finite interval bounds, and the required minimum
  effect. Every plotted value is a `{certificate, pointer}` ref into the run's own Kernel ledger certificate (new ref
  kind in `figures/audit.resolve_refs`, allowed only for certificates the auditor names; evidence refs unchanged).
- The script is harness-written from a fixed template (labels and ref ids, no numbers), run in a fresh sandbox,
  checked (F3) and stored next to the figure with `figure.json` (`make_figure`). `research.audit` I6 re-resolves the
  refs against the ledger and re-renders in a fresh sandbox (F1-F4).
- New chart kind `interval` (`svg.interval_chart`): a point per estimate and one segment per bound, each bound to
  exactly one ref; the audit checks bindings, values, point geometry and rebuilds the exact SVG.
- Labels never carry the target id (a content-addressed id like `data@3e5a.../test` reads as a number to the F3
  free-text check); several targets are lettered in sorted order, and figure.json names each ref's pointer.
- A figure that cannot be made fails the report node (fail closed; no report without its audited figure).
- Not plotted: dev-racing intervals (computed on the fly from private dev labels; no ledgered artifact to resolve).
- Tests: `tests/test_report_figure.py` (a hex target id in the certificate; an injected fake value with rewritten hashes is caught by F3 and F4; a changed
  ledger value is caught; a raw number in the script or a foreign certificate is refused; the v1 run's figure passes
  I6 and fails it after one plotted value is inflated).
