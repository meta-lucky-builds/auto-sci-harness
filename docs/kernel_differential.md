# Kernel differential test (OLD core vs NEW core)

Purpose: prove that a refactor of the Evidence Kernel, such as moving the ML-specific logic of K3-K7 behind the
domain-adapter interface, gives **the same verdicts and the same reasons** as the code before it. The check compares
every Kernel decision made by (a) the Kernel tests and (b) seeded random histories, run once on OLD and once on NEW.

Files (all in the *harness* tree, never in the trees under test, so OLD and NEW are recorded by identical code):

| file | role |
|---|---|
| `tests/differential/kernel_recorder.py` | pytest plugin / library: wraps `Kernel.certify`, `Kernel.preregister`, `Kernel.verify_status` and writes every call RAW to JSONL |
| `tests/differential/histories.py` | seeded-history generator (public Kernel API, `tests/kernel_world.py` worlds of the harness tree) |
| `tests/differential/normalise.py` | normalisation rules, diff, `explain` (proves each rule is needed) |
| `tests/differential/test_normalise.py` | fast tests of the diff tool itself (about 3 s, no Kernel run) |
| `scripts/kernel_differential.py` | CLI: `suite`, `histories`, `diff`, `explain` |

## How to run (OLD = pre-refactor commit, NEW = post-refactor commit)

Run everything through the machine-wide limiter. `locked_run.py` sets cwd to the tree and `PYTHONPATH=<tree>/src`.
The script refuses to run if `custom_harness` is imported from anywhere else (the PYTHONPATH trap).

```
set PY=C:\Users\mykids\custom-harness-data\venv\Scripts\python.exe
set LR=C:\Users\mykids\custom-harness-data\tools\locked_run.py
set H=<harness worktree, e.g. branch s3-kernel-diff>
set R=<a run directory outside both trees>
git -C <main checkout> worktree add --detach <OLD_TREE> <OLD>
git -C <main checkout> worktree add --detach <NEW_TREE> <NEW>

rem 1. Kernel tests, recorded (about 15-30 min each). OLD twice: the second run measures nondeterminism.
%PY% %LR% <OLD_TREE> %R%\old_suite_1.log script %H%\scripts\kernel_differential.py suite --out %R%\old_suite_1.jsonl
%PY% %LR% <OLD_TREE> %R%\old_suite_2.log script %H%\scripts\kernel_differential.py suite --out %R%\old_suite_2.jsonl
%PY% %LR% <NEW_TREE> %R%\new_suite.log   script %H%\scripts\kernel_differential.py suite --out %R%\new_suite.jsonl

rem 2. Seeded histories (same --n and --seed on both sides). OLD twice for the same reason.
%PY% %LR% <OLD_TREE> %R%\old_hist_1.log script %H%\scripts\kernel_differential.py histories --n 42 --out %R%\old_hist_1.jsonl
%PY% %LR% <OLD_TREE> %R%\old_hist_2.log script %H%\scripts\kernel_differential.py histories --n 42 --out %R%\old_hist_2.jsonl
%PY% %LR% <NEW_TREE> %R%\new_hist.log   script %H%\scripts\kernel_differential.py histories --n 42 --out %R%\new_hist.jsonl

rem 3. Prove the normalisation on OLD vs OLD, and list the cells that are nondeterministic even after it.
%PY% %H%\scripts\kernel_differential.py explain %R%\old_suite_1.jsonl %R%\old_suite_2.jsonl --flaky-out %R%\suite_flaky.json
%PY% %H%\scripts\kernel_differential.py explain %R%\old_hist_1.jsonl  %R%\old_hist_2.jsonl  --flaky-out %R%\hist_flaky.json

rem 4. The verdict: exit 0 = no difference, exit 1 = every difference printed.
%PY% %H%\scripts\kernel_differential.py diff %R%\old_suite_1.jsonl %R%\new_suite.jsonl --flaky %R%\suite_flaky.json
%PY% %H%\scripts\kernel_differential.py diff %R%\old_hist_1.jsonl  %R%\new_hist.jsonl  --flaky %R%\hist_flaky.json
```

The `diff` steps are plain JSON processing and take seconds. They do not need the limiter.

Reading the output: each `DIFF <scope> #<i> <kind> <json path>` line names the test node id or history id, the
Kernel call index within it, and the field, with the OLD and NEW values. `<scope>` or `<record>` in the path means a
whole test or call is missing on one side. `test:call /outcome` means a test passed on one side and failed on the
other.

If the refactor *intends* a change (for example, a reason now names the adapter), the diff reports it. Do not
silence it with a new rule. List each intended difference in the refactor's report.

## What is recorded

For each call: `scope` (pytest node id, or `h<i>:<kind>` for a history), `i` (call index within the scope), `api`,
`kwargs` (evidence ids, `full_chain`, `record`, `as_of`; the claim for `preregister`; the certificate hash for
`verify_status`), and then either `result` or `error` (exception type and message).

- `certify`: the whole certificate, `Certificate.model_dump(mode="json")`. This covers the claim and its hash, label,
  tier, and for K0-K9 the status, codes, reasons **and observations**. It also covers resolution (outcome,
  p_null, per-target estimate/lo/hi/met/alpha_eff), verifier_ids, assumptions, adapters, `config` (the
  KernelConfig in force), kernel_version, self_reported, prereg_seq, vg_ladder and evidence_ids. Nested calls are
  recorded too: the two re-derivations inside `verify_status`, and `reproduce_bundle`.
- `preregister`: the ledger body (claim hash and the FROZEN terms: p0, the reference forecast, the K3 reliability
  requirement), or the refusal message (q < p0, retired target).
- `verify_status`: status, forged, stale.
- Each test's outcome: `call`, plus `setup`/`teardown` whenever they did not pass.

## What is normalised, and why (applied identically to both sides)

Each rule is proved necessary by `explain` on two runs of the same commit. `explain` prints, for each rule, how many
raw differences it removes and one example. A rule that removes nothing is printed as UNPROVEN.

| rule | field | why it differs run to run |
|---|---|---|
| `evidence_ids` | `ev_/run_/... + 16 hex` anywhere | `records.new_id` = uuid4. Renamed `<ev#k>` by first appearance within the scope, so "same id twice" vs "two ids" is still compared |
| `issued_at` | `result.issued_at` | `Kernel.clock()` = `time.time()` |
| `resolve_by` | `claim.resolve_by` (certificate claim, prereg kwargs/body) | `kernel_world.World.claim` and the test helpers use `time.time() + 86400` |
| `derived_hashes` | any hex string (8+ chars) that is a prefix of a claim or certificate hash recorded in the scope | claim hashes cover `resolve_by`; certificate hashes cover `issued_at`. Renamed `<h#k:len>`. Data, artifact and adapter hashes are NOT touched |
| `tmp_paths` | `pytest-of-<user>\pytest-<N>`, `kdiff-<random>` | per-session temp dir numbering, mkdtemp |
| `adapter_hash` | `result.adapters[].hash` | **NOT nondeterminism.** It is EXPECTED to change because the refactor moves code into the adapters. Turn it off with `--keep-adapter-hash` to see it |
| `--flaky FILE` | individual (scope, call, path) cells | cells that still differed between two OLD runs after the rules. Each is printed by `diff`. See the measured list below |

The generator removes nondeterminism at the source where it can: a fixed `resolve_by` and a counting `Kernel.clock`
in every world. Its claim hashes and `issued_at` are therefore reproducible, and only uuids are left to normalise.

Nothing that differs deterministically is normalised. In particular `config`, `kernel_version`, the reason texts,
the codes, the observations and the numbers in them are compared verbatim.

## Measured nondeterminism at a7fb608 (OLD vs OLD)

(filled in from the demonstration runs; see the end of this file)

## What is NOT covered

- Unit tests that call check functions directly (`checks.k4_statistics(ctx)` etc. in `test_kernel_units.py`,
  `test_kernel_s16.py`) are compared only through their pass/fail outcome, not their return values. Those functions
  are exactly what the refactor moves, so their names will change. The certificates they build are compared.
- Kernel calls in a subprocess are not recorded, for example `chh reproduce` in
  `test_kernel_bundle.py::test_chh_reproduce_cli`. That test's outcome is compared.
- Kernel API other than certify / preregister / verify_status, such as `record_release`, `record_mint`,
  `open_alpha_account`, `approve_adapter` and Vault methods: only their effect on later certificates is compared,
  and their exceptions only in the generator (as a history `error` outcome) or through test outcomes.
- Formal (Lean) claims are not generated in the histories (they need the pinned-toolchain monkeypatch). They are
  covered by `test_kernel_formal.py` in the suite.
- PHYSICAL-class claims, kerneld (`test_kerneld*.py` is not in the `test_kernel_*.py` glob), and the research loop.
- Cells listed as flaky are not compared, even if the refactor changes them.
- A field that is nondeterministic but happened to be equal in both OLD runs would show up as a false positive in
  OLD vs NEW. Rerun NEW, or run OLD a third time, before accepting such a difference as real.
- Test node ids are the key. If the refactor renames or adds tests, those scopes are reported as present on one
  side only. Compare them by hand, or run the OLD test files against the NEW tree (copy them into a throwaway
  worktree).
- The seeded histories exercise the public API with the HARNESS tree's `kernel_world.py`. If the refactor changes
  the public API, the generator itself fails on NEW, and that shows as a history `error` outcome.
