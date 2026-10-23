"""The human-review surface: one Python API for everything only a human may do (GOAL §4.7, §5 L3/L9, §10
invariants 13, 15, 22, §16.7). The `chh` commands and a future UI both call it; docs/human_actions.md lists every
action, what it binds and the ledger kind it writes.

Submodules (import them explicitly; this package root stays import-light because studio/kill.py reads the catalogue):

- `catalog`   the table of human-only actions; MODULE_FORBIDDEN_KINDS (ledger kinds module code can never write)
- `guard`     who may act: a named human, never from inside a run step; `HumanActionRefused`
- `queue`     `pending(store)`: every pending human action with the exact hash it binds (read-only)
- `nodes`     approval-node decisions (incl. physical protocols) and Lean statement approvals
- `actions`   adapter / role approvals, alpha-account opening, run kill
- `review`    synthesized-verifier and fault-suite reviews
- `features`  feature-provenance rulings (anchor required by default)
- `providers` provider-registry sign-off (what `verify-registry --release` counts)
- `checks`    human-held check registration (invariant 22)
- `cli`       the `chh` commands over all of the above
"""

from .catalog import ACTIONS, MODULE_FORBIDDEN_KINDS, HumanAction
from .guard import HumanActionRefused

__all__ = ["ACTIONS", "MODULE_FORBIDDEN_KINDS", "HumanAction", "HumanActionRefused"]
