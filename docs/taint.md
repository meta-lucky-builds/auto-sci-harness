# Taint labels: target-derived information follows values (GOAL §16.5.3, invariant 18)

Invariant 18: no target-derived information reaches an agent-visible surface except as a ledgered release that K4
counts; continuous held-out values never reach the evolver. The release ledger (`kerneld/releases.py`,
`kernel/releases.py`) only counts what an emission site SEES. This note describes how the engine decides what is
target-derived, and what it does not see.

Code: `src/custom_harness/runtime/taint.py` (labels, `TrackedState`, `step_labels`), `runtime/engine.py` (recording,
persistence, release sites), `kerneld/releases.py` (`attributed_reads`, `run_targets`), `evolve/export.py` and
`evolve/evaluate.py` (the evolver's views). Tests: `tests/test_taint_flow.py`.

## What was wrong (reproduced on c9823b8; each reproduction failed there)

| | finding | reproduction |
|---|---|---|
| (a) | A key was tainted only if a private read happened DURING the step that wrote it. A module that copied (or formatted, split, mutated in place, read in bulk, read back from the store, mixed into a fan-out branch) a tainted value into another key produced an UNTAINTED key; a later prompt rendered it with no release, and K4 did not count it. Module-built prompts (`ctx.llm`) were never release sites. Routing on a tainted key (an implicit flow) was never counted. | `test_a_*` (9 tests) |
| (b) | Taint lived in engine memory. A resume after a hard kill (`kill -9`), a resume after an infra failure in a fresh engine, and a fork in a fresh engine started with no taint: the same leak. | `test_b_*` (3 tests) |
| (c) | Taint used the ledger sequence window of the step: a private read made by a CONCURRENT run (its own run id, another thread) inside the window tainted this run (timing-dependent over-taint). | `test_c_a_concurrent_runs_private_read_does_not_taint_this_run` |
| | The evolver's export withheld held-out values by KEY NAME only (`certificate`, `report`, ...): a copy under another key was exported. Failure signatures quoted target-derived error text. Run-level releases (export verdict, gate decision, branch bits) used `private_reads(run_id=...)`, which missed reads that name no run (Kernel re-check reads). | `test_export_withholds_a_copied_held_out_value`, `test_failure_signatures_withhold_target_derived_error_text`, `test_run_level_releases_count_reads_that_name_no_run` |

### Red-team round 1 (against 2d661e6; each became a regression test in `tests/test_redteam_taint.py`)

| | laundering path that got through | fix |
|---|---|---|
| F1 | a module RETURNING its state view (`return state`, `{"snap": state}`): the label was computed before the engine copied the returned object | `_execute` deep-copies each returned value while the engine still records, so that copy is a bulk read |
| F2/F3 | two state keys aliasing one object (`return {"a": x, "b": x}`, or `state["b"] = state["a"]`): an in-place change through one key changed the other, unlabelled | each returned value and each direct write is deep-copied on its own; and in-place changes are found by fingerprinting the WHOLE state around a module/map step (`taint.changed_in_place`), not only the keys the step read |
| F4 | an in-place change through a reference a module kept from an earlier step | the whole-state fingerprints above |
| F4b | a store facade cached from an earlier step | `ModuleStore` and `ModuleContext` record every touch into the step executing in this context (`modules.STEP_TOUCHED`), whichever step created them |
| F5 | deleting a key iff a target condition holds (absence channel) | a delete keeps a label (tombstone): routing on, or rendering, the absent key is labelled |
| F6 | edge order `[unconditional, error edge]`: the no-error outcome consulted nothing | `_route` counts `_last_error` as consulted whenever any outgoing edge tests it |
| F7 | fan-out metadata `_map` (`n`, `index`, `branch_id`) unlabelled | `_map` carries the items' label in every branch |
| F8 | a private read made INSIDE this run's step that names another known run | the engine observes `data_access` ledger appends in its own execution context (`store.evidence.add_ledger_observer`, contextvar `_CAPTURE`): a read the step made itself counts whatever run it names (`attributed_reads(own_seqs=...)`); run-level releases also union the run's persisted labels (`persisted_run_label`) |
| F9 | in-place REORDERING of a dict's keys (prompts render insertion order; the fingerprint sorted keys) | fingerprints are order-sensitive |
| F10 | a large int (e.g. 750000) released as `count` | `alphabet_of`: an int is `count` only in `[0, KernelConfig.count_alphabet)`, else `continuous` |
| F11 | a module writing into the workspace by PATH (not through `ctx.sandbox`) | a module/map step whose workspace snapshot differs from the previous one adds its label to the workspace label |

### An existing test that encoded a leak (recall)

`test_recall.py::test_target_derived_values_are_refused_not_released` asserted that the only releases were at the
agent's prompt. With labels, the reader's prompt rendering `recalled.text` is released too: the recall step reads
the agent's REQUEST back from the trace, and that request is model output of a tainted prompt.
`test_a_served_page_chosen_by_a_tainted_request_is_released` shows this is a real flow that recall's own rules cannot
refuse: a clean handle (clean provenance, archived before any private read) is served, but WHICH page the agent asks
for is chosen after it saw a target-derived note, so the page the reader sees is a function of the target. The old
assertion was changed to: recall itself releases nothing; the engine's releases are at the agent's prompt and at the
reader's rendering of `recalled.text`. Nothing was relaxed: the change only admits one more (correct) release.

## The labelling model

Every top-level run-state key carries a **label**: the set of held-out target dataset ids its value may derive from.
Labels are per top-level key (a write to `a.b` labels `a`), which is coarser than per path, never finer.

**Sources.** A label starts at a private vault read (`Vault.read` logs a `data_access` ledger entry before returning
the bytes). Reads are attributed **per run** (`attributed_reads`): a read the step made ITSELF (appended in the
step's own execution context, seen by the engine's ledger observer) counts whatever run it names; a read naming this
run (or a run nested inside one of its steps, `run_family`) counts; so does, **fail closed**, a read naming no run
(Kernel re-check reads carry none), naming a run the store does not know, or with an unreadable entry (then every
registered dataset counts). A read made elsewhere (another thread or process) naming another, known run does not
count: that run's own labels and releases cover it.

**Propagation (recorded by the engine, never reported by the module).** The label of everything a step writes is
the union of:
- the labels of what it READ:
  - LLM node / tool node: the keys its templates reference (exact);
  - module: its state arrives as a `TrackedState`, which records every key read (`[]`, `get`, `in`, `pop`,
    `setdefault`); any bulk access (iteration, `keys/values/items`, `len`, `bool`, `copy`, `deepcopy`, `dict(x)`,
    `{**x}`, `f(**x)`, `json.dumps(x)`, `==`, `repr`, `|`, and returning the view itself) counts as reading every
    key; every mutable top-level value of the state is fingerprinted (order-sensitive) before and after a module or
    fan-out step, so an in-place change by any route is seen (that key is then labelled with the old label plus the
    step's);
  - module context surfaces (`ModuleContext` and the `ModuleStore` facade record access, also when cached from an
    earlier step): the store or the Kernel (trace, evidence, blobs: anything the run produced) add the RUN label
    (every target attributed to the run so far); the sandbox adds the WORKSPACE label (every label written into the
    workspace: by a tool node, a module touching `ctx.sandbox`, or any module/map step after which the workspace
    snapshot changed); `ctx.secrets` adds the labels of the `_private*` keys;
  - a fan-out branch: the item and the `_map` metadata carry the label of the list; the reduced value carries the
    list's label and every branch's label (persisted per branch, so reused branches keep theirs);
- the private reads attributed to the run while the step ran;
- the run's **control label**: targets that an earlier routing decision depended on.

Writes replace a key's label (the new value is a function of what the step read); deleting a key leaves a tombstone
label (the step's label plus the old one); module outputs and direct writes are deep-copied into state value by
value, so two keys never share one object. `_last_error` carries the step's label; an approval's outcome keys carry
the label of the item key.

**Control (implicit flows).** A routing decision whose consulted condition keys carry a label (`_last_error` counts
as consulted whenever any outgoing edge tests it, whatever the edge order), at a node with more than one possible
successor, is a tainted decision: its targets join the control label, and the decision is
released ONCE, at the next agent-visible surface (`control:<decider>:<renderer>`), with alphabet `verdict_bit` for 2
successors, `count` for up to `KernelConfig.count_alphabet`, else `continuous`.

**Persistence.** Every checkpoint carries the label state (`cp["taint"]`: labels, declared alphabets, control
label, pending and past decisions, run and workspace labels, the last ledger seq accounted for, and the step's own
label). Resume and fork restore it exactly; private reads attributed to the run after that checkpoint (a step killed
mid-flight read and may have written to the store) join the run label. A checkpoint without labels (written before
this change, or malformed) restores **fail closed**: every key carries every target the run read, and one unknown
`continuous` control decision is pending. A fork's patched keys are caller-supplied values: unlabelled.

## Release sites (all through `Releases.emit`, so the existing ledger, K4, replay pins and alphabet rules apply)

| surface | release |
|---|---|
| LLM node prompt (system + user) | one `prompt` release per rendered labelled path (`prompt:<node>:<path>`), alphabet: an allowlisted declaration for that key, else `alphabet_of(value)` (fail closed: `continuous`); unchanged rules |
| tool node command | same |
| a module's own model call (`ctx.llm` or any backend call while a module step runs) | one release per labelled source the module read so far (`module_llm:<node>:<key>`; `_store`, `_workspace`, `_secrets` are `continuous`), one `continuous` release for private reads attributed during the step so far |
| pending tainted routing decisions | at the next of the above (`control:<decider>:<renderer>`) |
| evidence export (evolver) | labelled trace steps are WITHHELD (outputs, error; node id and node spec too if the step ran under a tainted decision); the run's end detail is withheld once any step is labelled. Certificate labels stay (released as before, `export.run:verdict`). Steps of runs without stored labels: fail closed by time (labelled if an attributed read happened at or before the step ended) |
| failure signatures (evolver) | a labelled step contributes `<node>: <embargoed>` only |
| run-level verdict releases (export, evolution score, gate decision, branch bits) | on `run_targets(run)` / per-run `attributed_reads`, unioned with the run's persisted labels (`persisted_run_label`): reads naming no run, and reads a step made itself naming another run, now count |

`alphabet_of(value)`: `verdict_bit` for booleans, None and certificate labels; `count` only for an int in
`[0, KernelConfig.count_alphabet)`; everything else `continuous`.

Allowlisted alphabet declarations (`RELEASE_ALPHABET_ALLOWLIST`) are not inherited by copies: a copy under another
key is released with `alphabet_of(value)`, fail closed.

## Paged recall and critic citations (b-research's rule)

Recall refuses a handle if its archive provenance carries a target OR the prompt that archived it ran after a private
read of this run (`run_private_reads`). The archive provenance is now computed from the precise labels. The precise
labels are NOT at least as strict as the time rule (a value written after a read but not derived from it is labelled
clean), and the time rule is not as strict as the labels (a fork inherits tainted values whose read happened in the
parent, before the child started: `test_recall_refuses_a_handle_whose_value_a_fresh_engine_fork_inherited_tainted`,
served on c9823b8). So recall keeps BOTH (their union); nothing was relaxed. Archive provenance stays in engine memory
(a handle archived before a resume is still `provenance_unknown`, as the existing red-team test pins): restoring it
would let recall serve more, which is an owner decision, not part of this change.

## What is NOT tracked (stated limits)

- **Termination**: whether a run ends at a step (an error with no error edge, step/budget limits) is not a counted
  decision (termination-insensitive, the standard limit). The evolver sees run status only through the evolution
  score's verdict release.
- **Timing and resource side channels**: wall time, token counts, cost, step counts, the number of fan-out items.
- **Absence**: a module that conditionally does NOT write a key (a delete is labelled, a skipped write is not);
  reading a never-written key carries no label.
- **Values encoded by a module's own logic outside state**: module-global VALUES kept across steps (a reference INTO
  state is covered by the whole-state fingerprints), files outside the sandbox, other processes. A raw `EvidenceStore`,
  a Kernel object or a Sandbox object cached across steps (outside `ctx.store`/`ctx`) is not recorded when used
  later (a cached `ctx` or `ctx.store` facade is). A module reading workspace files by PATH (not through
  `ctx.sandbox`) does not get the workspace label (writing by path is covered). `TrackedState` records what honest
  code reads; hostile code can bypass it (`dict.__getitem__(state, k)`). Modules are trusted, human-written registry
  code (engine docstring).
- **Reads served by kerneld** (another process) are not seen by the engine's ledger observer: they are attributed by
  the run id they name (a kerneld read naming another known run made inside this run's step is not attributed).
- **Cross-run flows through the store**: a module reading another run's trace/evidence, memory entries written by
  earlier runs. The store-access rule adds THIS run's label only. Memory writes are not a release site here.
- **Nested runs on another thread**: a run started inside a module step on a thread that does not copy the context is
  not linked to its parent (`run_family`); its reads then name a known other run and do not taint the parent.
- **Kernel re-check reads** carry no run id, so they still taint every concurrent run whose step is running
  (fail closed; the remaining timing-dependent over-taint). Giving them a run id is a Kernel change.
- **Labels are per top-level key**: a module that reads `a.x` is charged with all of `a`'s label (coarse).
- **The workspace** is not an agent prompt: writing a labelled value into it is not a release; tools and modules that
  read it inherit the workspace label.
- **Replay pins are not substituted into renders**: `Releases.emit` in replay mode returns the pinned value, but the
  engine renders the state value (pre-existing). Campaign replay therefore flags a released-but-differing value too.

## Yield cost (measured)

See "Measured yield cost" below (filled from `scripts/taint_yield.py` runs).
