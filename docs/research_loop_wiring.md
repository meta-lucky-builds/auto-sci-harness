# Research loop wiring: experiment tree (L4.7) and memory (L5)

Applies to `examples/research_v1.yaml` and `examples/research_v2.yaml`. Tests: `tests/test_research_wiring.py`,
`tests/test_exptree.py` (tree rules), `tests/test_memory.py`.

## Experiment tree (`research/tree.py`, wired in `research/modules.py`)

| Loop event | Tree operation |
|---|---|
| first `run_candidate` of the run | `ExperimentTree.create` (contract = `dev_contract`, cap = the run node's `max_fixes`); the root is answered by the baseline's dev run, under the contract that run reported |
| a hypothesis is run | `add_option` in the hypothesis round (a new round from the root when the current one is full or decided), then `record` |
| code from the `fix` node | `edit(..., repair_of=<failed attempt>)` (a ledgered repair) then `record`. Unchanged code is NOT edited, so the tree refuses the re-run (`REPAIR_UNLEDGERED`) and the run is not a candidate |
| the loop moves on from a provisional node | `abandon` (logged) |
| before an improve round | `promote_incumbent`: fresh-seed re-measure of the incumbent, then `confirm` (dev racing on answer and re-measure against the round's parent and every answered rival). Refused -> the improve call is skipped and recorded in `skipped_calls` |
| an improvement | a one-option round from the confirmed incumbent; if it wins dev racing on its run it is re-measured (seed 1) and replaces the incumbent only if `confirm` passes |
| research_v2 ensemble | its wrapper's dev run and a fresh re-measure are a node in an ensemble round; it is used only if `confirm` passes (and, as before, it beats the incumbent). With `verify_dev: false` it is not used |
| `report` | `tree.report()` (a logged `report` event); `render_report` includes `report_lines()` read from the stored log, so the audit's re-render (I4) checks it |

- **One repair parameter.** `max_fixes` on the `run` node is the tree's cap, fixed in the create event (replay
  uses the same cap). `tree.MAX_REPAIRS` is only the default when the param is absent.
- **Dev labels stay private.** They are read from the `_private_dev_labels_ref` blob and handed to `confirm`
  (which stores its own wrapped copy for replay); they never enter state or the trace.
- **Tree change (made here, in ortree's file):** `confirm` excludes a rival whose answering run made exactly the
  winner's dev predictions (the same behaviour; nothing can beat its own predictions, so such a twin blocked every
  round). Twins are returned as `duplicates`. A rival differing on one row still races.
- **Behaviour changes** (intended): an improvement needs a fresh-seed re-measure; an improve round needs a
  confirmed incumbent (which must beat the baseline under dev racing, not only on the point score), so fewer
  improve calls are made; re-running unchanged code after a crash is refused. The claim route itself is unchanged:
  the held-out Kernel certificate is still the acceptance test.
- **Resume.** Sandbox work in a module happens before its tree commits. A writer refuses a tree whose log is
  ahead of the checkpointed state (`tree_seq`): a crash between the commit and the checkpoint makes the resumed
  run fail closed instead of re-running unledgered. Forking a run shares its tree id (same rule applies).

## Memory (`research/memory_wiring.py`)

- `recall` node (before `hypothesize`, never raises): `MemoryStore.query` + `render` per entry with
  `secrets=ctx.secrets`; an entry that fails the render screen (canary, a private value of this run) is withheld
  by id (`memory.withheld`); the rest is rendered as quoted untrusted data into `{{memory.context}}`. At most
  `MAX_RECALL` entries / `MAX_CONTEXT_CHARS` (UNCALIBRATED). Every read syncs Kernel retractions first, so entries
  depending on a retracted certificate are revoked and never rendered.
- `research.certify` calls `after_certify`: `harvest_negatives(secrets=...)`, then `record_outcome` for every
  playbook/method entry rendered into this run's prompt. "Used" means shown to the proposer; whether the model
  followed it is not known. Memory failures are recorded (`memory_after_certify.error`), never change the
  certificate or route.
- The engine passes `_private_values(state)` (values and their encodings) to every module as
  `ModuleContext.secrets`.
- Namespace `research` (param `namespace`). No skill writer exists in the loop; skills are admitted only through
  `MemoryStore` with an exploit check (`research/perturbed.py`).
