# Emergence rule (GOAL §16.10)

> Novelty may steer budget, never acceptance. Generated task families enter the evolve split only.

Background: `code_review/upgrade_review_2026-10-07/emergence.md` (sections 1 and 3, "What NOT to do" 1 and 2).

## 1. Novelty never reaches acceptance (static check)

`custom_harness.emergence.check_novelty_isolation(src_root)` parses every `.py` file of the package with `ast` and
collects identifiers, attribute names, argument and keyword names, import names and string constants that contain
`novel` or `interesting` (any case). Comments and docstrings are ignored.

Every occurrence must be in `ALLOWLIST` as `(file, token) -> (category, reason)`:

- `budget`: a signal that may steer allocation. Allowed only in a `BUDGET_PATHS` module, never in an
  `ACCEPTANCE_PATHS` one. No budget module exists yet, so `BUDGET_PATHS` is empty. `budget.py` is the hard-cap cost
  ledger and has nothing to do with novelty.
- `not-a-score`: the word does not name a score. Today that covers the checklist row about literature novelty
  (`kernel/checklist.py`, `research/claims.py`), the label of the exact-duplicate refusal (`rejected_novelty` in
  `evolve/loop.py` and `evolve/export.py`), and the checker's own pattern.

`ACCEPTANCE_PATHS` lists every module that makes an accept, reject, certify, promote or admit decision. The reason
for each one is in `emergence.py`. These are problems:

- an occurrence that is not allowlisted;
- a `budget` token inside an acceptance module;
- an acceptance module that imports a budget module;
- a path pattern that matches no file;
- a file that is in both path sets;
- an unknown category;
- a stale allowlist entry.

`tests/test_emergence.py` runs the check on the real tree, and on planted attacks in a copy of the tree.

**Limit:** this is a token scan, not a data-flow proof. A renamed variable, or an attribute name built at run time,
gets past it. It does not scan YAML or other non-Python files.

## 2. Generated tasks: evolve split only (`research/suite.py`)

- `TaskSpecFiles.origin` is `"human"` (the default) or `"generated"`. Any other value is refused.
- `assign_splits` sends every generated task to `evolve`, and generated tasks do not take part in the human
  assignment, so adding them never moves a human task. It refuses with `ValueError`:
  - a held-out family that contains a generated task;
  - a generated task that shares a base dataset (`data_source`) with a human task.
- `build_suite` writes `origin` into each entry of `suite.yaml`.
- `check_split_isolation` is called by `build_suite` and by `evolve/driver.load_suite`. It reports:
  - a generated entry in `gate` or `sealed`;
  - an unknown `origin`.

  An entry without `origin` counts as human, so existing suites still load.

Not built yet: the task generator itself, the learnability and correlation filters, and the budget allocator that
would use interestingness (review §1 and §3).
