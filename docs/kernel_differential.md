# Kernel differential test (OLD core vs NEW core)

Purpose: prove that a refactor of the Evidence Kernel gives **the same verdicts and the same reasons** as the code
before it. The refactor in question is the core/adapter split, which moves the ML-specific logic of K1 and K3-K8
behind the domain-adapter interface (`kernel/ml.py`). The check compares every Kernel decision made by (a) the
Kernel tests and (b) seeded random histories, recorded once on OLD and once on NEW.

**Status (2026-10-14).** The harness is built and its diff tool is unit-tested (7 tests). Partial OLD-vs-OLD
measurements exist from an earlier version of the recorder. The full demonstration runs listed under "Still to run"
were NOT done: the session stopped, the machine rebooted, and then the owner ruled no long runs this round.
Everything below that is not marked *measured* is built but NOT test-verified.

Files (all in the *harness* tree, never in the trees under test, so OLD and NEW are recorded by identical code):

| file | role |
|---|---|
| `tests/differential/kernel_recorder.py` | pytest plugin / library. Wraps `Kernel.certify`, `Kernel.preregister` and `Kernel.verify_status`, writes every call RAW to JSONL, and makes record ids deterministic (see below) |
| `tests/differential/histories.py` | seeded-history generator. Uses the public Kernel API and the harness tree's `tests/kernel_world.py` worlds |
| `tests/differential/normalise.py` | normalisation rules, `diff`, and `explain` (proves each rule is needed and writes the flaky list) |
| `tests/differential/test_normalise.py` | fast tests of the diff tool itself (no Kernel run) |
| `scripts/kernel_differential.py` | CLI with `suite`, `histories`, `diff` and `explain` |

## How to run: OLD = 311c3e1 (pre-split), NEW = ee0ef1d (the split commit)

Run every recording through the machine-wide limiter. `locked_run.py` sets cwd to the tree and
`PYTHONPATH=<tree>/src`. The script refuses to run if `custom_harness` is imported from anywhere else (the
PYTHONPATH trap).

Start recordings with `Start-Process` (detached), not as a child of an agent session. Twice on 2026-10-09 a session
stop killed a 2-hour suite recording partway through.

Expected cost on a loaded machine (*measured* on 2026-10-09 with 4 runs at once): a full suite recording takes more
than 2 hours, and the histories take about 66 s each, so `--n 14` (one history of each kind) is about 15 min. On an
idle machine expect roughly a third of that.

`ee0ef1d` sits directly on `311c3e1`. It changes `kernel/adapters.py`, `kernel/checks.py`, the new `kernel/ml.py`,
and `tests/test_kernel_s16.py::test_k4_plan_code_divergence` (it now monkeypatches `ml.paired_example_interval`).
It does not change `tests/kernel_world.py`, `records.py` or `kernel/__init__.py` (*verified* by `git diff --stat`),
so the recorder and the generator apply to both commits unchanged (*inferred*, not run).

PowerShell:

```powershell
$PY = "C:\SERS\<USER>\custom-harness-data\venv\Scripts\python.exe"
$LR = "C:\SERS\<USER>\custom-harness-data\tools\locked_run.py"
$H  = "<harness worktree: branch s3-kernel-diff>"      # e.g. ...\scratchpad\s3-kernel-diff
$KD = "$H\scripts\kernel_differential.py"
$R  = "<run directory outside every tree>"              # e.g. ...\scratchpad\kd-runs
$OLD = "<OLD tree>"; $NEW = "<NEW tree>"
git -C C:\SERS\<USER>\OneDrive\Desktop\META\custom-harness worktree add --detach $OLD 311c3e1
git -C C:\SERS\<USER>\OneDrive\Desktop\META\custom-harness worktree add --detach $NEW ee0ef1d

$runs = @(
  @($OLD, "old_suite_1", "suite --out $R\old_suite_1.jsonl"),
  @($OLD, "old_suite_2", "suite --out $R\old_suite_2.jsonl"),        # repeat: measures nondeterminism
  @($NEW, "new_suite",   "suite --out $R\new_suite.jsonl"),
  @($OLD, "old_hist_1",  "histories --n 14 --out $R\old_hist_1.jsonl"),
  @($OLD, "old_hist_2",  "histories --n 14 --out $R\old_hist_2.jsonl"),
  @($NEW, "new_hist",    "histories --n 14 --out $R\new_hist.jsonl")
)
foreach ($j in $runs) {
  Start-Process -FilePath $PY -WorkingDirectory $R -WindowStyle Hidden `
    -ArgumentList "$LR $($j[0]) $R\$($j[1]).log script $KD $($j[2])"
}
```

Each `*.log` names the import path of `custom_harness`, HEAD, start and end times and the exit code. A suite exit code
of 1 only means some test failed. Failures are compared too (test outcome records), so it is not an error of the
harness. `--n` and `--seed` (default 20261008) must be the same on both sides: history `i` is drawn from
`random.Random(f"{seed}:{i}")`, and the kinds are taken round-robin.

Then the analysis (plain JSON; the 10 MB partial files took about 60 s on a loaded machine):

```powershell
& $PY $KD explain $R\old_suite_1.jsonl $R\old_suite_2.jsonl --flaky-out $R\suite_flaky.json
& $PY $KD explain $R\old_hist_1.jsonl  $R\old_hist_2.jsonl  --flaky-out $R\hist_flaky.json
& $PY $KD diff $R\old_suite_1.jsonl $R\new_suite.jsonl --flaky $R\suite_flaky.json   # exit 0 = identical
& $PY $KD diff $R\old_hist_1.jsonl  $R\new_hist.jsonl  --flaky $R\hist_flaky.json
```

Reading `diff`: each `DIFF <scope> #<i> <kind> <json path>` line names the test node id (or history id), the Kernel
call index within it, and the field, with the OLD and NEW values.

- `<scope>` or `<record>` in the path: a whole test or call is missing on one side.
- `test:call /outcome`: a test passed on one side and failed on the other.
- `history /status`: the generator crashed on one side.

If the refactor *intends* a change, the diff reports it, for example the adapter now listing `procedures`, or a
reason that names the adapter. Do not silence it with a new rule. List each intended difference in the refactor's
report. `--keep-adapter-hash` shows the (expected) adapter-hash change. `--limit N` truncates the printout, not the
count.

### Test of the test (planted changes)

Build two throwaway worktrees at OLD, each with a one-line behaviour change in `kernel/checks.py`. Do not commit
them. The two plants prepared at a7fb608, still in the scratchpad, are:

- `kd-plant-reason`: the K5 not-paired reason text, "(not run under identical conditions)" changed to
  "(not run under the same conditions)".
- `kd-plant-thresh`: the K7 noise model, `se = (hi - lo) / (2 * z)` changed to `/ (2.02 * z)`. This constant is
  hard-coded, not in KernelConfig, so the `config` field cannot give it away.

Record `histories --n 14` on each, plus `suite -k test_kernel_suite` on the threshold plant. `diff` against OLD must
exit 1 and name K5 reasons (strawman histories) or the K7 observations and p_null (every certified data claim).
Use `diff --intersect` for the suite subset. *Not run yet.*

## What is recorded

For each call: `scope` (pytest node id, or `h<i>:<kind>` for a history), `i` (call index within the scope), `api`,
`kwargs` (evidence ids, `full_chain`, `record`, `as_of`; the claim for `preregister`; the certificate hash for
`verify_status`), and then either `result` or `error` (exception type and message).

- `certify`: the whole certificate, `Certificate.model_dump(mode="json")`. This covers the claim and its hash, label,
  tier, and for K0-K9 the status, codes, reasons **and observations**. It also covers resolution (outcome, p_null,
  per-target estimate/lo/hi/met/alpha_eff), verifier_ids, assumptions, adapters, `config` (the KernelConfig in force),
  kernel_version, self_reported, prereg_seq, vg_ladder and evidence_ids. Nested calls are recorded too: the two
  re-derivations inside `verify_status`, and `reproduce_bundle`.
- `preregister`: the ledger body (claim hash and the FROZEN terms: p0, the reference forecast, the K3 reliability
  requirement), or the refusal (q < p0, retired target).
- `verify_status`: status, forged, stale.
- Each test's outcome: `call`, plus `setup`/`teardown` whenever they did not pass.
- Histories only: the outcome of each history (`ok`, or the exception type, message and function).

## Nondeterminism: removed at the source where possible, otherwise normalised

**At the source** (identical for OLD and NEW; it does not touch the Kernel):

- *Record ids.* `kernel_recorder.deterministic_ids()` replaces `custom_harness.records.new_id` (`<prefix>_<uuid4
  hex[:16]>`) with `<prefix>_<sha256(scope|n)[:16]>`, where n counts the ids made in the current test or history.
  This was needed (*measured*, partial runs at a7fb608 with uuid ids). The seed K3 replays is sampled from a hash of
  the evidence. With uuid ids it differed run to run, and so did:
  - K3 observations, e.g. "re-executed 2 time(s) over 2 record(s)" vs "over 1 record(s)" in
    `test_kernel_s16.py::test_replayed_evidence_is_refused`;
  - the whole call sequence of `test_kernel_exploits.py::test_e6_only_the_first_seed_is_deterministic`, which loops
    until a nondeterministic seed is sampled.

  Set `CHH_KERNEL_RECORD_RANDOM_IDS=1` to keep uuid ids. The header records `deterministic_ids`, and `diff` applies
  the `evidence_ids` renaming only when a side used random ids. With deterministic ids, renaming would hide a change
  in how many ids are made.
- *Generator only.* Every claim gets a fixed `resolve_by` (2100-01-01) and the Kernel a counting clock, so claim
  hashes and `issued_at` are reproducible.

**Normalisation rules** (applied identically to both sides; `explain` proves each one on two runs of the same commit):

| rule | field | why it differs run to run | removed (partial OLD vs OLD, a7fb608, uuid ids, 103 shared tests) |
|---|---|---|---|
| `evidence_ids` | `ev_/run_/cl_... + 16 hex` | uuid4. Renamed `<ev#k>` by first appearance in the scope, so "same id twice" vs "two ids" is still compared. Only applied when a side used random ids | 1216 |
| `issued_at` | `result.issued_at` | `Kernel.clock()` = `time.time()` | 228 |
| `resolve_by` | `claim.resolve_by` (certificate, prereg kwargs and body) | `kernel_world.World.claim` and the test helpers use `time.time() + 86400` | 9123 |
| `derived_hashes` | a hex string (8+ chars) that is a prefix of a claim or certificate hash recorded in the scope | claim hashes cover `resolve_by`, certificate hashes cover `issued_at`. Renamed `<h#k:len>`. Data, artifact and adapter-content hashes are NOT touched | 4686 |
| `tmp_paths` | `pytest-of-<user>\pytest-<N>`, `kdiff-<random>` | per-session temp dir numbering, mkdtemp | **0: UNPROVEN** (no temp path reached a recorded field). Kept as a guard. If `explain` keeps showing 0 on the full runs, delete it |
| `adapter_hash` | `result.adapters[].hash` | **NOT nondeterminism.** EXPECTED to change: ee0ef1d adds `kernel.ml` to the ML adapter's modules and a `procedures` field to its hash. `--keep-adapter-hash` turns the mask off | n/a |
| `--flaky FILE` | (scope, call, path) cells, and whole scopes | what still differed between the two OLD runs after the rules (written by `explain --flaky-out`). A scope whose call *sequence* differs keeps only its test outcome. `diff` prints every masked cell and scope | see below |

Raw differences in that partial pair: 15,345 in 76 tests. After the rules, 92 were left in 6 tests. All 6 were
*measured* and are explained:

- `test_kernel_bundle.py::test_rejected_certificate_reproduces_as_rejected`,
  `test_kernel_exploits.py::test_e4b_self_consistent_forgery_is_caught_by_verify` and `::test_e5_...`: the program
  `nondet` uses `random.SystemRandom` on purpose, so K4/K7 observations, resolution numbers and the K3 seed named in
  the reason differ.
- `::test_e6_only_the_first_seed_is_deterministic`: SystemRandom, plus the uuid-dependent seed sampling (call
  sequence differs).
- `test_kernel_s16.py::test_replayed_evidence_is_refused`: the uuid-dependent seed sampling.
- `test_kernel_suite.py::...[true_improvement_seed_dependent_method]`: the last test of a killed run (truncated, not
  a real difference).

With deterministic ids, s16 and part of e6 should disappear. The SystemRandom tests stay flaky (*inferred*; the full
runs will show it).

Nothing that differs deterministically is normalised. `config`, `kernel_version`, reason texts, codes, observations
and the numbers in them are compared verbatim.

## Still to run (none of these were done)

1. OLD (311c3e1) suite x2 and histories x2, then NEW (ee0ef1d) suite x1 and histories x1 (commands above). Then
   `explain` and `diff`.
2. The planted-change demonstration (test of the test on real Kernel runs) on `kd-plant-reason` and
   `kd-plant-thresh`.
3. Optional: OLD=a7fb608 vs NEW=311c3e1 histories. That pair contains real, intended behaviour changes (strict JSON,
   alpha slots, K1 scope, bundle artifacts, CPU limits, K3 non-interference), so it is a second, unplanted test that
   the diff finds real changes.

## What is NOT covered

- Unit tests that call check functions directly (`checks.k1_well_formed(ctx)` etc. in `test_kernel_units.py` and
  `test_kernel_s16.py`) are compared only through their pass/fail outcome, not their return values. The
  certificates those tests build through `Kernel.certify` are compared.
- Kernel calls in a subprocess are not recorded, for example `chh reproduce` in
  `test_kernel_bundle.py::test_chh_reproduce_cli`. That test's outcome is compared.
- Kernel API other than certify / preregister / verify_status (`record_release`, `record_mint`,
  `open_alpha_account`, `approve_adapter`, Vault methods): only their effect on later certificates is compared, plus
  their exceptions in the generator (a history `error` outcome) or through test outcomes.
- Formal (Lean) claims are not generated in the histories (they need the pinned-toolchain monkeypatch). They are
  covered by `test_kernel_formal.py` in the suite. PHYSICAL-class claims are not generated.
- kerneld (`test_kerneld*.py` is not matched by `test_kernel_*.py`) and the research loop are not covered.
- Flaky cells and flaky scopes are not compared (except the test outcome of a flaky scope), even if the refactor
  changes them.
- A field that is nondeterministic but happened to be equal in both OLD runs would show up as a false positive in
  OLD vs NEW. Rerun NEW, or OLD a third time, before accepting such a difference as real.
- Test node ids are the key. A renamed or added test is reported as a scope present on one side only.
  `test_k4_plan_code_divergence` keeps its node id at ee0ef1d, but its monkeypatch target moved. A difference there
  is a test-code difference, not a Kernel one.
- With deterministic ids, the ids (and so the sampled K3 seed) depend on how many records a test made before. If
  NEW makes a different number of records, later ids and sampled seeds shift. That shows as a difference, and it is
  a real behaviour change (an extra or missing record).
- The histories use the harness tree's `kernel_world.py` and the public API. If the refactor changed that API, the
  generator would fail on NEW, and that shows as a history `error`.
