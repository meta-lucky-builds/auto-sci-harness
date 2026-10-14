# Experiment-tree extras, wired (GOAL §5 L4.7; branch s3-tree)

`research/tree.py` rules 6-8 (from branch g-weights) are now used by research_v1/v2 (`research/modules.py`), each
behind a spec flag. Every earlier lifecycle rule stays in force: answered nodes are immutable, every run reports the
tree's one contract, repairs are ledgered and capped while provisional, promotion needs dev racing on the answer AND a
fresh-seed re-measure.

| Flag (node.param) | Default in v1/v2 | What it does |
|---|---|---|
| `run.backtrack` | **true** | Exact backtracking. The first candidate freezes the pristine run workspace as the root's snapshot; every answering run freezes the workspace it ran in (`solution.py` = the node's code) into the node's content hash. A NEW option starts from its round parent's frozen snapshot (`tree.restore`: restore, re-snapshot, refuse unless every file's bytes/mode, symlinks and empty dirs match), and the backtrack is logged (`log_checkout`) with the run's other tree commits (sandbox work before tree commits, as before). A dead or failed branch (route is not `fix` and the node is not the incumbent) is backtracked to its parent's snapshot (`tree.checkout`). An inexact restore is `RESTORE_MISMATCH`: logged, and the run stops (fail closed). |
| `run.curriculum` | false | tune -> creative -> ablation with `stage_rule: evidence` and `tune_match: literals`. Rounds off the root are `draft`. The stage of an improve round is `tree.next_stage(parent)`, computed from the tree log only: a node starts at its own stage (a draft node at `tune`) and moves on when that stage plateaued at the node: `STAGE_PATIENCE` (1) failed rounds opened from it (decided with no confirmed winner although an option was answered = dev racing found no confirmed improvement), or `STAGE_MAX_ROUNDS` (3) rounds at that stage along its lineage. A declared stage that differs is `CURRICULUM_ORDER` (logged); a replay recomputes it. The improve prompt shows the decided stage (`{{improve_stage}}`); nothing the model writes is an input, so the model cannot advance it. A tune option must keep the parent's program skeleton (`code_skeleton`: AST with literal values erased): only hyperparameter values change. |
| `next.revive` | false | An incumbent that is not its round's confirmed winner (confirmation refused, e.g. by a near-twin rival, or the round went to another node) may still open an improve round if `tree.revive` accepts it: P(improvement over the round parent) > theta, the minimum over its answer and every fresh re-measure (a fresh re-measure is run first). A refused revival is logged. A revived node stays immutable. |

## Parameters (all UNCALIBRATED, owner decision GOAL §13.8)
`STAGE_PATIENCE = 1`, `STAGE_MAX_ROUNDS = 3`, `REVIVE_THETA = 0.9`, `REVIVE_N_BOOT = 2000` (the last two pre-existing).

## Stated limits
- In research_v1/v2 the dev runs execute in fresh per-fold sandboxes, never in the run workspace; the run workspace
  holds only harness-written files (data, baseline, `solution.py`). Backtracking is byte-exact, but what it protects
  in this loop is that workspace (what later nodes and the audit see), not the fold sandboxes.
- "Literals only" is not "behaviour-preserving": a literal can still select a code path (`if 0:` -> `if 1:`).
- The ensemble node's round is a `draft` round off the root with no snapshot (it writes no workspace file).
- `declared` stage mode (the caller picks within the order) is kept for logs written before this change; the loop
  uses `evidence`.

## Tests
`tests/test_tree_extras_wiring.py` (unit: evidence-decided stages, crash rounds are not evidence, stage budget,
forged stage advance fails replay, literals-only tune, mode validation, revived node immutability; loop: flags in the
specs, backtracking freezes and restores byte-exactly, a sloppy restore stops the run, the model cannot advance the
curriculum, the curriculum advances only after a dev-racing plateau, revival on/off).
