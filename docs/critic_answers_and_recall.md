# Critic answers cite evidence (L4.8) and paged recall (L4 context management)

GOAL §5 L4 stage 8 ("answers must cite evidence", AHOIS) and "Context management" ("provide paged recall",
SoL-Pi ObservationPack); invariants 2, 7, 18, 21. Code: `research/critic_answers.py`, `research/recall.py`, shared
rules in `research/evidence_refs.py`, a provenance hook in `runtime/engine.py`. Tests: `tests/test_critic_answers.py`,
`tests/test_recall.py`.

## Shared rules (`evidence_refs.py`)

The only sources of truth are records the engine and the Kernel write, never run state (evolved nodes can write state):

| Source | Used for |
|---|---|
| the run's trace (`store.trace(run_id)`) | which steps ran, their outputs, the prompt each LLM step was shown (`output.messages`), the handles archived in it (`output.archived`) |
| the Kernel vault | private-file blobs and canary blobs (by digest), canary tokens (by content) |
| the ledger's `data_access` rows | times of private reads (a step that overlaps one is hidden) |
| `ModuleContext.archive_taint` (engine, in memory) | per archived handle: the held-out targets of the prompt that archived it |

"Shown prompt" = the system message plus the FIRST user message of the step. Later messages are retry turns. They
quote the model's own earlier output or a validator message, so an id that appears only there is not visible.

Reason codes are a closed set (`CODES`): `malformed`, `uncited`, `not_visible`, `unresolvable`, `not_evidence`,
`hidden`, `private`, `target_derived`, `provenance_unknown` (plus `missing`/`duplicate` for questions and
`stale`/`no_request`/`page_out_of_range`/`recall_cap`/`budget` for recall). A refusal never echoes the requested value.

## 1. Critic answers must cite evidence

Wiring (`examples/research_v1.yaml`, `research_v2.yaml`):
`socratic -> critic_evidence -> defend -> check_answers -> next`. When no question was kept, the flow goes
`critic_evidence -> next` and makes no call. A failed `defend` call still goes to `check_answers`. `max_steps` is
60 -> 63.

- `research.critic_evidence` (code): gives the kept questions ids `q1..qn` (read from the latest
  `research.socratic_filter` step in the trace). It also builds the **catalog** of citable steps of this run.
- `defend` (LLM, role `proposer`: the author answers the critic): `{"answers": [{"question", "answer", "evidence"}]}`.
- `research.check_critic_answers` (code): records the outcome in `critic_answers` (for information only).

**What a citation may be.**
- `step:N`: a successful MODULE step of this run, before the answer. It is not a relay module
  (`RELAY_MODULES`: socratic filter, catalog, check, recall, memory recall, queue/unpack, tournament), made no model
  call, wrote no `_private*` key, had nothing redacted, no private vault read was logged while it ran, and no
  private read of THIS run (naming the run, or naming none) was logged before it ended (`run_private_reads`). It must
  also be listed in the catalog AND appear in the shown prompt.
- `blob:<16-64 hex>`: a handle archived in the answering step's own prompt that resolves to exactly one digest.
  It must not be private or target-derived (engine taint, OR the answer's prompt came after a private read of this
  run), its provenance must be known, and its content must carry no canary or run-private value.
- `ev:<id>`: always refused (`target_derived`). Kernel Evidence rows are written only for held-out executions and
  certificates (`kernel/kernel.py`).

**Per answer.**
- An answer must have exactly the keys `{question, answer, evidence}`. An unknown key (for example a model-written
  `status`) makes it malformed, so a model can never mark itself supported.
- Other malformed cases: text empty or over 2000 characters, more than 8 ids, or a top-level object that is not
  exactly `{answers: [...]}` (at most 20).
- An answer with no id is `uncited`. One bad id rejects the whole answer.
- A question counts as `cited` only if it has exactly one answer and that answer passed. No answer is `missing`; two or
  more are `duplicate`.

**What `cited` means.** The cited ids exist, were shown to the author and are not hidden. It does NOT mean the answer
is true or that the evidence supports it. No code here judges meaning, and no model is asked (invariant 21: a cheap
check may only deny).

**Consequence (decision: deny-only).**
- `research.certify` calls `critic_gate(ctx)`. The gate recomputes the check from the trace, round by round: every
  `research.socratic_filter` step is a round, checked by the latest check step before the next round, so a later
  empty round cannot erase an earlier round's open questions.
- A filter step that failed, or ran with `max_questions` < 1 (the critic switched off by a parameter edit), blocks.
- If any kept question is not `cited`, the claim gets a publish blocker (`publishable = False`).
- If kept questions were never checked, the claim is also blocked: there is no check step after the socratic step, or
  the answering step failed (fail closed).
- The certificate, its label and tier, the route and the hypothesis queue are untouched.
- A run whose critic kept no question is not affected.

Alternatives not adopted:
- Re-order or drop hypotheses by their answers. An LLM could then promote its favourite hypothesis over the code
  ranking (v2) by citing well. Citing well is not evidence of truth.
- Block pre-registration. That is too strong for a check that only verifies ids exist and cannot judge meaning.
- Deny the headline. `headline()` reads state only, and the evolve score is owned elsewhere.

## 2. Paged recall (`research.recall`)

An agent (an LLM node named by the module's `agent` param) puts `{"recall": {"handle": "<hex>", "page": k}}` in its
JSON output. The module writes `recalled` for the next prompt: a quoted page or `{"refused": code}`. It also writes
`recall_log` for the trace.

| Rule | Enforced by |
|---|---|
| request taken from the trace (the latest step of node `agent`), never from state. One request is served once (`stale`) | `recall()` |
| handle: lowercase hex, 16-64 characters; it must resolve to exactly one digest archived in a prompt of THAT agent in THIS run. The agent's string never addresses the blob store (forged, traversal, other run, other agent: refused) | `resolve_handle`, `archived_by` |
| not a vault private file, not a canary blob, not a run `_private*` value; content without a canary or a private value (encodings via `verify.leak.secret_patterns`) | `PrivateScreen`, `vet_blob` |
| target-derived: the archiving prompt rendered a tainted key (engine taint), OR it was rendered after a private read of this run (`run_private_reads`; closes laundering through a copying module and the empty taint after a resume): **refused**, never released. Unknown provenance (no engine record, e.g. archived before a resume): refused | `vet_blob` + engine `archive_taint` |
| `page_chars` capped at 8000, page index at 50, requests per run at 16 (refusals included; counted from the trace) | `_cap`, `MAX_*` (UNCALIBRATED, owner decision) |
| budget checked before serving. A served page is charged `ceil(chars/4)` estimated prompt tokens on the step (charged again when a prompt renders it: over-counted, never under) | `ctx.budget.check()`, `ctx.spent` |
| rendered as data: wrapper `<<UNTRUSTED-RECALLED-DATA BEGIN/END nonce=...>>`. The nonce is sha256(digest:page) (content cannot contain its own hash), lines are split on every boundary `str.splitlines()` honours (CR, VT, FF, FS/GS/RS, NEL, LS, PS) and each is prefixed `\| `; C0 (except tab), DEL, C1 and all Bidi_Control characters are replaced | `quote_page` |

Recall never emits a release. The release ledger (§16.5.3) is honoured by refusal: a target-derived handle is never
paged. The only release of such a value is the engine's own, made when it rendered the value into the prompt.

## Not enforced / limits (stated)

- **Recall is not wired into research_v1/v2.** No node in the seed specs requests a page (it would add model calls).
  It is a module any spec can use: agent -> `research.recall` -> a node rendering `{{recalled.text}}`. Keep
  `page_chars` + about 300 below the spec's `context.max_value_chars`, or the page is truncated again.
- **Engine taint does not follow copies** (existing engine limitation, red-team finding): a module copying a tainted
  key into a new key leaves the copy untainted, and a resumed engine starts with no taint. Recall and citations do not
  rely on it alone: anything produced after a private read of this run is refused. The engine's OWN release emission
  for such copies is not fixed here (it needs an engine-wide taint rule: owner decision).
- **Provenance is in memory.** A resumed run refuses recall and blob citations for handles archived before the resume.
- **The gate trusts the socratic step it finds.** If the critic node is removed or produces nothing, nothing is asked
  and nothing is blocked. Edits cannot change the judge role or write `critique_raw` (`evolve/edits.py`). Whether an
  edit can delete the critic node was not changed here. The new module ids and the `critic_answers*` / `recalled`
  keys are not yet in `evolve/edits.py`'s reserved lists, so an edit could add such nodes. They cannot grant anything:
  the gate recomputes from the trace.
- **Hidden-step detection by time** is conservative: a private read anywhere in the store during a step hides that
  step, and a private read of this run (or a run-less Kernel read) hides every later step and handle. Two runs sharing
  a store concurrently can over-hide each other's evidence (fail closed).
- **Encodings outside `secret_patterns`** (spaced out, base32, rot13, colon-hex, zero-width interleaving) of a private
  value pass the content screen (red-team finding; the same shared screen serves the leak audit). Stated limit; the
  structural defence is that only handles of values rendered to the agent are reachable at all.
- **Relay modules are a deny list.** A new module that only re-emits model text must be added to `RELAY_MODULES`.
- **The report does not render the critic outcome.** It appears in `publish_blockers` and `critic_answers`.
- **The defend call** adds one model call per run that has kept questions (GOAL §16.8 yield).
