# Taint labels: target-derived information follows values (GOAL §16.5.3, invariant 18)

Invariant 18: no target-derived information reaches an agent-visible surface except as a ledgered release that K4
counts; continuous held-out values never reach the evolver. The release ledger (`kerneld/releases.py`,
`kernel/releases.py`) only counts what an emission site SEES. This note describes how the engine decides what is
target-derived, and what it does not see.

Code: `src/custom_harness/runtime/taint.py` (labels, `TrackedState`, `step_labels`), `runtime/engine.py` (recording,
persistence, release sites), `kerneld/releases.py` (`attributed_reads`, `run_targets`), `evolve/export.py` and
`evolve/evaluate.py` (the evolver's views), `store/evidence.py` (ledger append observers). Tests: `tests/test_taint_flow.py`,
`tests/test_redteam_taint.py`, `tests/test_redteam_taint2.py`, `tests/test_recall.py`.

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

### Red-team round 2 (against a8dd56f; `tests/test_redteam_taint3.py`, and the builder's `test_redteam_taint2.py`)

| | laundering path that got through | fix |
|---|---|---|
| H1 | `state.pop(k)` then a bulk read (`len(state)`, `.items()`): the label used the keys LEFT after the module ran | the keys READ (a bulk read records every key present at that moment) |
| H2 | an in-place change inside a TUPLE value (`state["pair"][0].append(x)`): tuples were not fingerprinted | tuples are fingerprinted |
| H3 | an error edge whose key merely CONTAINS `_last_error` (`retry_last_error`): `_next` follows it after an error, `_route` did not count the error as consulted | `_route` uses `_next`'s substring rule |
| H4 | an early `MapError` ("3 items exceed max_items=2") returned before the items' label was recorded | the items' label is recorded first |
| H5 | a module reads a continuous value, overwrites the key with a bit, then calls its model: released as `verdict_bit` | the alphabet of the value as first READ (`TrackedState.first`); mutable values `continuous` |
| H6 | the evolution score's dev term read `best.dev_score` / `baseline_dev` by name, labels ignored: a continuous function of a held-out value reached the proposer | evolve split: the dev term is withheld (0.0) when a key it reads is labelled (`evaluate.dev_withheld`; fail closed for runs without labels that read a private file). Owner decision noted below |
| H7 | the export kept a control-tainted step's output KEY names (`path_hi` vs `path_lo`) and provenance | the whole output and the provenance are withheld for such a step (kind, cost and timing stay: stated side channels) |
| H8 | `ctx.archive_taint` (digests of target-derived values truncated in prompts) was not a recorded surface | it is (`_archive` source: the union of the archived prompts' targets) |
| H9 | a nested `Engine.run` inside a module step (own backend) rendered the outer value as an unlabelled `input` | a run started inside a module step labels its `input` with that step's label so far (`_PARENT_LABEL`) |
| (gen.) | generalised from the failure-signature fix: the evolver's DIAGNOSIS (`propose.diagnose` trace excerpts and the first-divergence signatures, `diagnosis.py`) quoted raw step errors, routes and node ids, labels ignored | both read `diagnosis.labelled_trace`: labelled steps keep output key names only, errors/routes withheld, control-tainted steps lose node id and output (`tests/test_taint_views.py`) |

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
| diagnosis (evolver: trace excerpts, first-divergence signatures) | `labelled_trace`: as the export |
| evolution score dev term (evolve split) | withheld (0.0) when a key it reads is labelled |
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
- **Reads the engine's ledger observer cannot see**: served by kerneld (another process), or made on a worker thread
  that does not copy the step's context (e.g. a `ThreadPoolExecutor` inside a module). They are attributed by the run
  id they name: naming this run, no run or an unknown run counts; naming another known run does not
  (`test_redteam_taint2.py::test_G1_worker_thread_read_naming_another_known_run`, a strict xfail).
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

`scripts/taint_yield.py` runs the three scripted research runs (mock LLMs, no network: `research_v1.yaml` one
family, `research_v1.yaml` two-family replication, `research_v2.yaml` packed) on a fresh store each and counts every
release. One run per scenario per commit (deterministic scripted runs; no confidence interval applies).

| scenario | c9823b8 (before) | a8dd56f (after round 1) | difference |
|---|---|---|---|
| v1_scripted | 3 releases, all `prompt/verdict_bit` (`replicate:replication_brief.{claim,idea,profile}`) | 4 `prompt/verdict_bit`: the same 3 + `control:certify:replicate` | +1 verdict_bit |
| v1_two_family_replication | 3 (same) | 4 (same) | +1 verdict_bit |
| v2_packed | 3 (same) | 4 (same) | +1 verdict_bit |

The extra release is the routing decision after `certify` (whether the replication runs), which depends on the
held-out certificate: an implicit flow the old engine did not count. No `continuous` release appears, so no target
becomes adaptively exposed; K4's transcript bound per claim grows by a factor of 2 (one more verdict bit) for the
single target family (`data@.../test`, family size 1: 3 -> 4 releases in the family). Certificate labels are
unchanged (CERTIFIED-INTERNAL for both certificates in all three scenarios).
Logs: before `7f9fcb5e.../scratchpad/logs/02_yield_before.log` + `yield_before_c9823b8.json`; after
`f4d503e4.../scratchpad/w2t/yield_after_a8dd56f.log` + `.json`.

After the round-2 fixes (the commit after a8dd56f): NOT MEASURED (owner instruction: no runs in that round). The
round-2 rules can only add releases or withhold more (H5 can turn a `verdict_bit`/`count` module-call release into
`continuous`; H6 can withhold a dev term), so the numbers above are a lower bound on the cost (inferred, not run).
`scripts/taint_yield.py` now also reports which keys end labelled and whether the dev term would be withheld.

## Open (owner decisions, verification still owed)

- **H6 dev term**: withholding it (0.0) when `best`/`baseline_dev`/`task` is labelled is the fail-closed choice made
  here; releasing it instead would put a `continuous` value in front of the evolver, which invariant 18 forbids. If
  research runs end with `best` labelled, evolve-split scores lose their dev term: whether they do is NOT MEASURED.
- **H7**: a control-tainted exported step still shows its `kind`, cost and timing (stated side channels).
- **Performance**: whole-state fingerprints and per-value deep copies run around every module/map step. Not
  profiled. `tests/test_infra.py::test_docker_outage_mid_campaign_changes_no_decision` ran > 30 min at a8dd56f under
  machine contention (it already exceeded 4 min at bd1acbb); whether that is a regression against c9823b8 is NOT
  MEASURED.
- **Round-2 fixes are not test-verified yet** (see the w2-taint RESUME for the exact runs owed).
