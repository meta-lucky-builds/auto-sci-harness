# kerneld boundary: what is behind it, and what kind of boundary it is (GOAL §16.5.2)

Status as of 2026-10-14, branch s4-kerneld. Ratchet: `tests/test_kerneld_boundary.py` (AST). Dynamic tests:
`tests/test_kerneld_hoststore.py`, `tests/test_kerneld.py`, `tests/test_research_kerneld.py`.
Label: built, NOT test-verified (tests deferred by the owner); ruff and mypy clean.

## Process model (decision, 2026-10-14)
Two mechanisms, stated separately because they are different kinds of boundary.

1. **The kerneld process (an OS process boundary for what it serves).** `kerneld/server.py`, spawned by
   `kerneld/client.KernelD` per use, alone makes the host-initiated private operations:
   - reads: held-out scoring (`score`, single-use `evaluate` token per claim/arm/seed), Kernel re-score (`rescore`,
     `kernel` token bound to one claim), leak scan (`leak_scan`, `leakscan` token, per-run-id budget of 8 scans);
   - private ingest (new): `issue_ingest` + `ingest`, single-use `ingest` token bound to (run, kind, subject[, claim]).
   The host names paths; kerneld reads the bytes, checks them, registers them and appends a `kerneld_ingest` record.
   Nothing private crosses the pipe: a float, location strings, ids/counts/a sha256, or closed codes.
2. **Everything else runs in the host process on a `HostStore` (a CODE boundary, not an OS boundary).** The Engine,
   every module (`ModuleContext`, `ModuleStore`), the CLI commands, evolve, studio, memory and the audit hold a
   `kerneld.hoststore.HostStore`. It shares the database and blob directory but refuses, before anything is read or
   written: private and canary blobs (`blobs.get`), a `register_dataset` entry with a private file or a canary, and a
   `data_access` entry with `private: true`, unless the current context holds a grant naming them. The in-process
   Kernel (K-checks, `verify`, bundles) reads private data only inside grants opened by `kerneld/scope.py`:
   - `ScopedKernel.certify` (so `verify`/`verify_status`): private files of THIS claim's targets, logged reads of
     purpose `kernel` on those targets, and every canary blob (K9);
   - `ScopedKernel.bundle`: private files of the datasets the bundle packs (kernel/bundle.py's rule) + canaries;
   - `screening_canaries`: canary blobs only, for the memory and citation/recall content screens.
   A bare `Kernel` on a HostStore fails closed (its private reads raise; a check that raises FAILS).

Why not move the Kernel and the store into the kerneld process now: every host module (Engine, research loop,
evolve, studio, memory, audit) constructs and calls the Kernel and the store directly (dozens of methods and call
sites, some in kernel/, owned by s4-kernel). An RPC Kernel is a much larger change than this phase. The capability
boundary above gives the same *guarantee shape* against accidental and casual bypass, is ratcheted, and moves the
remaining private reads behind one narrow, greppable API.

What the in-process part does NOT give (stated): Python in the same process can walk private attributes, call
`BlobStore.get` unbound, or `open()` the blob files (the blob directory is a normal directory on a single-user
machine); a grant is a contextvar, so a callback the Kernel makes during `certify` (the replayer) runs inside the
grant. The ratchet makes each named bypass a test failure; reading blob files directly is not statically detectable.
A real OS boundary needs the private blobs in a location only a different OS user (the kerneld process) can read, and
the Kernel behind RPC. Not done.

## Fail closed
- kerneld unavailable: `KernelDUnavailable` (an InfraError, not transient). No caller falls back to an in-process
  private read: the HostStore refuses them, and `register_task`, the audit's `prepare` and `chh return-data` call
  only kerneld for private data.
- `PrivateDataRefused` is deliberately not an OSError/ValueError, so code that treats those as "missing" (e.g.
  `except OSError: continue` in bundle/K9) cannot turn a refusal into a silent skip.
- Leak-scan budget, single-use evaluate tokens, kernel/leakscan tokens until `close_run`: unchanged
  (kerneld/tokens.py).

## Sites moved behind kerneld (2026-10-14)
- `cli.py`: the store is opened by `kerneld.hoststore.open_host_store` (the CLI holds a HostStore); `chh return-data`
  ingests through kerneld (`returned_measurements`: kerneld reads the file, runs `check_measurements`, refusals are
  closed problem classes); `chh bundle` goes through `ScopedKernel.bundle`.
- `research/claims.py`: `register_task` ingests through kerneld (`research_task`; the canary consistency check runs in
  kerneld, code `canary_mismatch`); `certify` bundles through `ScopedKernel.bundle`.
- `audit/data.py` no longer registers anything; `audit/claim_audit.prepare` ingests through kerneld (`openml_split`:
  kerneld loads the pinned cache file, splits and registers). The host only ensures the file is cached (an
  owner-approved download still passes through the host).
- `memory/screen.py`, `research/evidence_refs.py`: canaries via `screening_canaries`.
- Every host `Kernel(...)` is a `ScopedKernel(...)`; `RunGuardedKernel` derives from it.

## Ratchet (tests/test_kerneld_boundary.py)
Host code = everything except kernel/ and the privileged kerneld modules (server.py, ingest.py, hoststore.py,
scope.py). Empty old-path lists for: private reads (`.read(purpose=...)`, `.read(**kw)`), store opens, private ingest
(`private=` non-empty, positional, `**kw`, or a canary), canary reads (`.canary()`, `blobs.get(x.canary_ref)`),
`getattr` access to read/canary/register_dataset or facade internals, bare `Kernel(` or `class X(Kernel)`, direct
`build_bundle(`, any use of `_grant`/`_GRANT`/`_HostStore__*`/`_HostBlobs__*`, and `BlobStore(`/unbound
`BlobStore.x(`/`EvidenceStore.x(` outside store/. Exempt with a reason: `research/preflight.py` (scratch store of
simulated labels); `literature/snapshot.py` (its own paper blob store). A non-vacuity test checks each detector fires
on the privileged code it exempts, and a planted line per bypass is caught.

## Coordination
- s4-integrity (moving held-out truth out of agent-readable roots): the task's private location is resolved in ONE
  function, `kerneld/ingest.task_private_dir`, run only inside kerneld. Their new location plugs in there.
- s4-kernel owns kernel/: nothing there was changed. `_bundle_datasets` in scope.py mirrors bundle.py's dataset rule;
  if that rule widens, bundles fail closed (PrivateDataRefused) rather than pack more.

## Red-team review (s4-kerneld-review, 2026-10-15) and what changed
Fixed (built, not test-verified):
1. A caller-chosen replayer ran inside the certify grant: `ScopedKernel` now wraps it (`_UngrantedReplayer`), every
   replay call runs under an EMPTY grant (replays read public inputs only).
2. Module-held kernels could bundle a claim's labels to any path, unlogged: `RunGuardedKernel.bundle` writes only
   under `<data>/bundles` and not on a killed run; every bundling appends a `kerneld_bundle` record (cert, path,
   datasets, private blobs packed). Not a ledger kind (the Kernel's kinds are closed; kernel/ is s4-kernel's).
3. `ScopedKernel` wraps any store it gets in a HostStore; `RunGuardedKernel` builds its ledger/vault on that view.
4. run bundles: a workspace file equal to a private blob is `refused` again (PrivateDataRefused caught) instead of
   crashing the run. `chh reproduce` reads local artifacts through the HostStore (a private blob = missing).
5. `HostStore` keeps no reference to the raw store (closes the shared connection itself); `_PrivateIndex` is
   incremental (one primary-key lookup per read). Ratchet: aliased imports of guarded classes, two-argument
   `super(...)`, `vars(...)`, `__self__`/`__mro__`/`__bases__`/`__dict__`/`__globals__`/`__closure__`,
   `.blobs._path(...)`, and `screening_canaries` only from memory/screen.py and research/evidence_refs.py.
6. Ingest: truth through `truth_layout` (refusal = `truth_layout` code); canary read via the no-symlink reader;
   `openml_split` only for one of the claim's targets; `returned_measurements` binds the claim at token issue;
   research tasks declare their truth copies (`research/truth_copies.declare_task_truth_copies`, K3).
Stated limits (not fixed):
- The ledger carries sha256 of every private file (`register_dataset` bodies are readable through the HostStore): for
  low-entropy labels the host can confirm a guessed label file offline, without the leak-scan budget. Fix needs
  private blobs keyed by an HMAC only kerneld holds (a vault change: kernel/).
- Host code still reads task truth from disk outside kerneld where s4-integrity's layout routes it (evolve/export.py
  leak fingerprints, evolve/multiplicity.held_out_target_id, research/rebuild.py): trusted host code, not behind
  kerneld; a kerneld op returning ids/findings only is the next step.
- The audit host holds the pinned raw ARFF, so `openml_split` hides nothing from host code in substance.
- K9: a non-target private file used as code/input now fails as CHECK_RAISED instead of its coded K9 reason (the
  refusal is fail-closed; better coding needs a kernel/ change).
- Ingest is not atomic (a failure after the train registration leaves it registered; the token is spent) and each
  idempotent re-ingest appends another `kerneld_ingest` record.
