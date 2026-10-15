# Human-only actions

GOAL §4.7, §5 L3 and L9, §10 invariants 13, 15 and 22, and §16.7.

Status: **built, NOT test-verified** (the owner deferred tests in this phase; s4-human, 2026-10-14). The tests that
must run are listed at the end.

Some actions may only be taken by a person: approving, reviewing, signing off, ruling, registering, opening an
account, killing a run. A model, a module or an evolved harness node must never take them. This page lists every
such action. For each one it gives:

- the command that takes it;
- the Python function a future UI calls;
- the exact hashed item the action binds (invariant 15: an approval binds only the item it names);
- the ledger kind it writes.

`custom_harness/human/catalog.py` is the same table in code, and `chh human-actions` prints it. Run
`tests/test_human_surface.py::test_catalogue_covers_every_queued_kind_and_the_doc_lists_every_action` to check that
this page lists every action in the catalogue.

## One API, one queue

`custom_harness/human/` is the stable Python API. Both the CLI and a future UI call it.

| Module | What it does |
|---|---|
| `human.queue.pending(store)` | **Read-only.** Every pending human action. Each item has `kind`, `binds` (the hashes the action will bind) and `decide` (the command that takes it). `chh queue [--json] [--kind K]` prints it. A source that cannot be read becomes a `queue_error` item; it is never silently dropped. |
| `human.nodes` | `node_item`, `approve_node`, `statement_view`, `approve_statement`. These are the only writers of approval-node decisions and statement approvals. `chh approve` and `chh approve-statement` are thin wrappers over them. |
| `human.actions` | `approve_adapter`, `approve_role`, `open_alpha_account`, `kill_run`. Each checks the actor and the hash, then calls the one Kernel or studio writer. |
| `human.review` | `verifier_view`, `review_verifier`, `suite_view`, `review_suite`, `pending_verifiers`, `pending_suites`. |
| `human.features` | `rule_feature`, `note_screen`, `pending_rulings`. |
| `human.providers` | `entry_hash`, `provider_view`, `sign_off_provider`, `with_ledgered_signoffs`, `pending_signoffs`. |
| `human.checks` | `register_check`, `registrations`, `pending_checks`. |
| `human.guard` | `require_human`, `check_human_name`, `require_tty`, `interactive_stdin`, `HumanActionRefused`. |

Every write function refuses with `HumanActionRefused`. A UI must always pass the hash the person reviewed. The only
place a hash may be `None` is the older `chh approve` / `chh approve-statement` flow: it prints the item just before
it records, and its tests rely on that.

## The actions

Queued means `chh queue` lists the action while it is pending.

The ledger is the Kernel ledger. An `approval` kind with a node id uses a run-id prefix that no engine run carries,
so the engine never reads it as a run's approval. audit/gate.py and evolve/finalize.py use the same scheme.

| Action | Command | Binds | Ledger kind | Queued |
|---|---|---|---|---|
| `approval_node` | `chh approve RUN NODE --decision approve\|reject --approver NAME --item-hash H` | (run, node, sha256 of the canonical JSON of the paused item, recomputed from the checkpoint) | `approval` | yes |
| `physical_protocol` | `chh approve RUN protocol_review ...` | the protocol + claim item | `approval` / `protocol_review` | yes |
| `physical_data` | `chh return-data RUN --file F --attest T`, then `chh approve RUN data_received ...` | the returned dataset of exactly the approved protocol | `approval` / `data_received` | yes |
| `adapter` | `chh approve-adapter NAME --approver NAME [--adapter-hash H]` | adapter content hash | `adapter_approval` | yes |
| `statement` | `chh approve-statement FILE --artifact-id ID --approver NAME --statement-hash H` | sha256 of the statement file | `statement_approval` | yes |
| `role` | `chh approve-role DATASET --approver NAME [--dataset-hash H]` | dataset content hash | `role_approval` | yes |
| `alpha_account` | `chh alpha open DATASET --alpha A --slots S --approver NAME [--dataset-hash H]` | held-out target's dataset hash | `alpha_open` | yes (optional; only before the first claim) |
| `verifier_review` | `chh review verifier REF --decision approve\|reject --reviewer NAME --verifier-hash H` | verifier spec hash AND the hash of the suite it was validated against | `verifier_review` | yes |
| `suite_review` | `chh review suite PROBLEM --decision approve\|reject --reviewer NAME --suite-hash H` | fault-suite hash (all four halves) | `verifier_review` (subject `suite`) | yes |
| `feature_ruling` | `chh rule-feature TASK_DIR --feature F --ruling legitimate\|leak --approver NAME --anchor FILE [--report-hash H]` | data digest, tags hash, screen report hash | `feature_ruling` | yes |
| `provider_signoff` | `chh sign-off-provider KEY --by NAME --entry-hash H` | sha256 of the registry entry without `verified_by` / `verified_on` | `approval` / `provider_signoff` | yes |
| `human_check` | `chh register-human-check ID --by NAME --sha256 H` | sha256 of the check file's raw bytes | `approval` / `human_check_registration` (plus `registry.json` and `redteam/human_checks.lock`) | yes |
| `human_check_run` | `python scripts/human_check.py --run` | sha256 of each registered check | `redteam/human_check_runs.jsonl` (hash chain) | yes |
| `kill` | `chh kill RUN --by NAME` | the run id | `run_kill` | no |
| `tier_policy` | `chh tier-policy pin --by NAME --sha256 H` | sha256 of tier_policy.json | `tier_policy` | no |
| `alpha_retire` | no CLI yet; `Kernel.retire_alpha_account` | the target's account | `alpha_retire` (code `human`) | no |
| `finalize` | `chh finalize SPEC --suite S --approver NAME` | spec hash, baseline hash, sealed tasks, seeds | `approval` / `finalize` | no |
| `audit_fidelity` | `chh audit approve-fidelity ...` | audit spec hash + claim hash | `approval` / `audit_fidelity` | no |
| `audit_reply` | `chh audit reply ...` | report hash | `approval` / `audit_reply` | no |
| `audit_publication` | `chh audit approve-publication ...` | report hash | `approval` / `audit_publication` | no |
| `non_isolated_compute` | host code `compute.backend.record_non_isolated_approval` | the exact run request on that backend | `approval` / `compute:non-isolated` | no |
| `salt_reveal` | `chh salts-reveal SEALED --by NAME` | campaign hash + commitment | `salt_reveal` | no |
| `memory_revoke` | `chh memory revoke ENTRY --reason R --by NAME` | the memory entry id | memory log (records table) | no |
| `feature_rulings_migrate` | `chh feature-rulings-migrate --by NAME` | every line of the interim rulings.jsonl (chain verified) | `feature_ruling` | no |

Why some actions are not queued:

- **Kill, tier policy, finalize, the three audit steps, salt reveal and memory revocation.** A person starts these on
  their own initiative. Nothing in the store records a pending request for them.
- **Non-isolated compute.** It has no request record yet.

## What each new piece does

### Verifier and suite reviews (GOAL §5 L3)

Every synthesized verifier and every fault suite is AGENT-AUTHORED. An unreviewed synthesized verifier is capped at
T2. It reaches T1 only when two ledgered human approvals exist: one of the verifier (by its hash, against its suite's
hash) and one of that suite (by hash). verify/synthesis.py `tier` enforces this.

`chh review verifier REF` shows:

- the spec (closed vocabulary; never executable);
- the author and model family;
- the AGENT-AUTHORED marker;
- the fault-injection validation evidence. The public half is shown by case id. The **sealed half is shown by
  counts only**, so its ids and contents stay out of any view a model could be shown (invariant 20).

`chh review suite PROBLEM` shows both halves, because the person approving a suite must see every case. For that
reason it runs only in an interactive terminal, even with `--show`.

Verifiers are found from run output: the `verifier_synthesis.ref` of each step. Only that ref is taken from output a
model could have influenced. The blob is re-parsed, re-validated and re-hashed at every use.

### Feature-provenance rulings (orchestrator decision 2026-10-14)

`feature_ruling` is a human-only kind. Module code cannot write it.

**An anchor is required by default.** `--anchor FILE` must point to a file written by `chh ledger-anchor`. An anchor
is what detects removed trailing ledger entries. `--no-anchor` records the ruling without one; the ruling then
carries `anchors_checked: 0`.

The ruling binds the screen report hash the person saw (`--report-hash`, or by default the hash the command prints).
verify/provenance.py `record_ruling(expect_report_hash=...)` checks it between the screen and the append.

`chh feature-screen` notes each screen in the records table so the queue can list routed items. It records only
hashes and routed item names; never data values.

### Provider sign-off (invariant 11; s3-gateway)

The release check stops counting a `verified_by` string written into providers.yaml or the override file. Anyone with
the repository or the data directory can write that text.

`human.providers.with_ledgered_signoffs(reg, store)` replaces each entry's `verified_by` / `verified_on` with the
latest ledgered sign-off of the entry's **current** hash. If there is none, it clears them. So:

- any change to the entry's terms voids the sign-off;
- the age rule (`costs.RELEASE_MAX_AGE_DAYS`) applies to the sign-off date.

`chh verify-registry --release` (cli.py `cmd_verify_registry`) applies `with_ledgered_signoffs` before
`verify_registry`, so the release check fails until a human signs off each usable entry with
`chh sign-off-provider`. The pure function `costs.release_problems` is unchanged.

### Human-held check registration (invariant 22; s3-assure)

`register_check` binds the sha256 of the check file's raw bytes. The content is never decoded, printed or recorded.
It then writes three things:

- the entry in `<data_dir>/human/checks/registry.json`;
- the line in `redteam/human_checks.lock` (the person commits it);
- the ledger entry naming the registrant.

To register a different file under an existing id, pass `--replace`.

Invariant 22 stays NOT MET until a person writes a check. Nothing here can satisfy it.

## How the paths are closed to models, modules and evolved nodes

### Runtime checks

1. **Module Kernel.** studio/kill.py `_ModuleLedger.HUMAN_ONLY` refuses every kind in
   `catalog.MODULE_FORBIDDEN_KINDS`. That includes `approval`, `feature_ruling`, `verifier_review` and
   `alpha_open`. It also refuses `alpha_retire` with code `human`. The Kernel's own retirement of an exhausted
   account is still allowed.

   Change from before: a module's `ctx.kernel().ledger.append("approval", ...)` used to succeed. Only the engine's
   request-order rule then stopped it. Now it is refused at the append.

2. **Not from inside a run.** Every write in `custom_harness.human` calls `guard.require_human`. It refuses when the
   Python stack passes through the engine's step executor or any registered module function. An evolved node can
   only call registered modules, so it cannot reach these functions in-process.

3. **Named human.** Empty names are refused. So are names that mark an agent, a model or a workstream: `agent...`,
   `claude`, `gpt-5`, `openai/gpt-5`, `s4-human`, `w2-taint`, `b-evolve`, `integrator`, `main`, `...-bot`, and so
   on. `chh` applies the same check to the older human commands before they run (`human.cli.check_legacy_actor`).
   An empty name is left to each command's own refusal.

4. **Interactive console.** The new commands refuse a stdin that is not an interactive console. `chh rule-feature`
   and `chh feature-rulings-migrate` now use the same check.

   On Windows, `isatty()` is also True for the NUL device. This was observed on 2026-10-14: an agent's shell
   passed the plain `isatty()` check. `guard.interactive_stdin` therefore also requires a Windows console mode. The
   person also re-types the first 12 characters of the hash being bound.

### Static checks (tests/test_human_static.py)

- **Writers.** Each human-only kind may be appended only by its listed writer files.
- **Imports.** Only the CLI layers import the write side of `custom_harness.human`: cli.py, studio/cli.py,
  studio/queue.py and verify/provenance_cli.py.
- **No modules.** The package registers no module.

### Honest limits

All of these are accountability checks, not authentication.

- A name is not proof of a person.
- Trusted host code that imports `Ledger` can still append entries directly.
- A module is in-process Python. It could reach the real store (`ctx._store`) or call from a new thread and get past
  the stack check. tests/test_approval.py keeps a test for the raw-store path: the engine still ignores an approval
  written before its own pause request.
- An agent harness that gives its shell a real console would pass the console check. The typed hash and the
  agent-name refusal still apply.

## Tests to run (NOT run; owner deferred tests)

Run these once int17 holds s3-assure (s3-verifiers and s3-gateway are merged):

```
pytest -q tests/test_human_surface.py tests/test_human_static.py tests/test_approval.py tests/test_studio.py \
  tests/test_studio_kill.py tests/test_feature_provenance.py tests/test_lean_adapter.py tests/test_physical.py \
  tests/test_kernel_alpha_slots.py tests/test_registry_verify.py tests/test_verifier_synthesis.py \
  tests/test_synthesis_wiring.py tests/spec/test_spec_humancheck.py
```
