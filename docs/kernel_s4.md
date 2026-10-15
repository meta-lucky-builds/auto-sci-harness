# Kernel round 4 (s4-kernel, branch `s4-kernel`, 2026-10-15)

**Status: every item below is built, NOT test-verified.** The owner deferred all test runs this round. The tests are
written (`tests/test_kernel_s4.py`, `tests/spec/test_spec_gate.py` delegated cases, the red team's
`tests/test_redteam_s4kernel_*.py`) and each is designed to fail first on the pre-s4 Kernel. Only these checks ran:
ruff, `mypy --strict` on kernel/, `scripts/governance.py`, `scripts/museum.py --check-only` and short `python -c`
probes (each is named where used). Kernel version 1.4 (`KernelConfig.version`): certificates of 1.3 that re-derive
differently are reported STALE, not forged.

Touching the adapter modules changes adapter identities: **`chh approve-adapter ml` and `chh approve-adapter lp` are
needed** before live certification (ml.py changed; the ML and LP validation fixtures moved into hashed modules).

## 1. K9 sees what a reader of a program sees (`checks.reader_texts`)
Before: the data-class K9 scanned code and program inputs as UTF-8 bytes. A canary written as Python escapes
(`"\x43\x48..."`, `C`, `\N{...}`, octal), split over adjacent literals (`"CHH-" "CANARY-..."`), in a bytes
literal, or in a comment of a file with a coding cookie (`# coding: utf-7`, with the text in modified base64) was
invisible, while Python decodes all of them (s3-kernel's generalisation note). Now K9 scans, per artifact:
- the bytes as UTF-8, and with NUL bytes dropped (UTF-16/32 spellings of ASCII, with or without a BOM);
- for JSON (any encoding json reads): every parsed key and string, re-encoded canonically (s3's K9-ENCODING);
- for program code only (`source=True`; inputs are data and a numeric CSV is valid Python): the source decoded the way
  Python decodes it (`importlib.util.decode_source`: BOM, coding cookie), and every string and bytes literal from
  `ast.parse`, in source order, joined by newlines (label runs keep their tokens) and joined directly (a canary split
  over literals).
Probe (python -c, 2026-10-15, verified): hex escapes, unicode escapes, split literals, a bytes literal and a utf-7
cookie comment are each missed by the byte scan and found by `reader_texts`; a UTF-16-LE canary without a BOM is found.
NOT seen (stated): strings built at run time (`chr`, `codecs`, arithmetic, decryption). That is out of reach of any
static scan; the K3 non-interference replay (§16.5.4) is the dynamic defence for declared truth copies.
Museum: `K9-SOURCE-LITERALS` (drops the Python-literal texts); `K9-ENCODING` was re-made for the new code.

## 2. Windows path aliases in Kernel containment checks (`ledger.inside`)
`inside(a, b, on_error=...)`: strip the device prefixes `\\?\X:`, `\\.\X:`, `\\?\UNC\` (Path.resolve keeps them), then
compare by path, then by file identity (`os.path.samefile`) of every existing ancestor of `a` against `b` (8.3 short
names, case, admin shares, `\\?\GLOBALROOT`, junctions). The s3-evolve approach (`evolve/reveal.py`), kept inside
kernel/ (a Kernel helper may not import non-Kernel code that could change under it, so s4-integrity's
`pathsafe.is_within` is not imported). An OSError in the identity test returns `on_error`, the caller's fail-closed side:
- `ledger.write_anchor`: `on_error=True` (refuse an anchor that might be inside the store);
- `adapters._source_file`: `on_error=False` (a module that might be project code is hashed into the adapter). The
  external-root classification is cached per process (`_is_external`); the identity walk is slow on a synced drive.
Direction analysis (adapters): identity can only ADD "external" when an ancestor of the module IS an external root,
i.e. when the module really lives there; spelling differences used to make external files count as project code
(fail closed, but the hash then depended on the spelling).

## 3. Equivalence claims (GOAL §13.5)
TOST with a margin fixed at registration, a two-sided positive plasmode control, one-sided levels stated exactly:
`docs/bounded_null.md`, section "Equivalence claims". Governance: a `delegated` record (owner-delegated statistics
decision, cooling-off not observed).

## 4. The data class's scope vocabulary (`ml.scope_phrase`, K1)
s3-kernel: "the data class has no scope vocabulary for statements". Now, as for the exact classes, a data claim's
`statement` must be EXACTLY `ml.scope_phrase(claim)` (the ML adapter's procedures; a pure function of the claim):

    {subject id} {relation} in {metric} ({higher|lower} is better) on the held-out test examples of {targets};
    population: the examples each target is an i.i.d. sample of

with `relation` = "beats {comparator} by at least {delta}" (improvement), "beats its ablation ..." (ablation),
"does not beat {comparator} by {delta} or more" (negative result), "differs from {comparator} by less than {delta}"
(equivalence), and delta by `repr` (never rounded). Datasets = the targets (claim order); split = their held-out test
examples; population = what each target is an i.i.d. sample of; metric phrase = metric, direction, margin. K1 also:
- refuses scope conditions except the adapter's `PROVENANCE_NOTES` keys (the Claim Audit's paper/protocol notes: never
  scope, never rendered into the statement, read by no check);
- refuses scope datasets that are neither targets nor development datasets (no claim about data never touched);
- refuses identifiers that are not plain ids (`_PLAIN_ID`), since ids are rendered into the statement.
Producers changed to render it: `research/claims.py` (the old "On task X, the solution for hypothesis H ..." prose is
gone; the replication brief now quotes the rendering), `audit/spec.py` (the audited sentence stays a provenance note),
test helpers (`tests/kernel_world.render`, `tests/test_kernel_f1.py`, `tests/spec/real_kernel.py`).

## 5. Unknown cost is never `$0.0000` (`checklist.fill`)
A subscription (BYOS) call records `usd` 0.0, meaning NOT REPORTED. The checklist now renders a total of 0 as
"USD unknown / UNCALIBRATED" (and appends "(estimated)" when a cost is estimated). A real zero is indistinguishable in
the record, so it is also rendered unknown (the conservative side).

## 6. Truth-copy declaration hook (`Kernel.declare_truth_copy`)
The public hook task registration calls (s4-integrity wires the suite and task code): `declare_truth_copy(target,
dataset_id, file, column, declarer)`. Now IDEMPOTENT (an identical declaration returns the earlier entry, so
re-registering a suite adds nothing) and the column is matched against the CSV header parsed with `csv` (quoted names).
Declare before the task's first claim: K3 reads the declarations on the ledger it certifies against, and `verify()`
re-derives as of the certificate's prefix, so a later declaration makes earlier certificates STALE, never valid by
omission. Declaring more only adds checks.

## 7. K1 tolerance evidence (`AnalysisPlan.tolerance_evidence`, `checks.tolerance_reasons`)
A `reproduce_tolerance > 0` must cite a noise-floor record: the sha256 of a stored `chh-noise-floor/1` document
(`Kernel.record_noise_floor(NoiseFloor)`; fields reference, nominal, perturbed, rebuild, seed, all floats). K1 re-reads
it and re-calibrates it with `verify.tolerance.claim_tolerance` (the owner's fixed miss rate and sources,
`l3_config`); the declared tolerance must be at most the result. Malformed records (bools, ints, another format, not an
object) and floors whose nominal re-runs are identical (calibration keeps the exact check: 0) justify nothing. The field
is omitted from dumps when unset (old claims keep their hashes). `research/claims.py` stores the floor it measured and
cites it.

## 8. Governance: the `delegated` record kind (`scripts/governance.py`)
For owner-DELEGATED core changes made without the cooling-off. It satisfies the current fingerprint's requirement but
must name `delegated_by`, a `basis`, a non-empty `changes` list and state `cooling_off_observed: false`. `--record`
now APPENDS to the yaml (earlier records keep their bytes). Tests: `tests/spec/test_spec_gate.py`.

## 9. Adapter hash cached per process (`adapters.adapter_hash`)
s3-assure measured about 0.7 s of each ~2 s certify in the adapter hash. Now cached by (spec, sys.path), recomputed in
full when any covered file's (mtime_ns, size, inode) changed, and stored only if the import closure and the stamps
were unchanged across the computation (no edit raced it). Probe (python -c, verified): a cold first call 1.9 s on this
OneDrive checkout (the identity walk of item 2 included, cached afterwards), a full recompute after clearing the hash
cache 0.54 s, a cached call 0.005 s. Residual: an edit that restores all three stamps is not seen until the process
restarts (the process runs the code it imported then anyway).

## Also (s4-station request): run-bound prereg and certificate entries
`Kernel.run_id` is None, set only by the run-bound subclass (`studio/kill.RunGuardedKernel`, from
`ModuleContext.run_id`); no Kernel method takes a run id. When set, `prereg` and `certificate` ledger bodies carry
`run_id` (absent otherwise: old entries keep their bytes; K2 and verify read other fields). Residual: trusted host code
can still build a Kernel and assign `.run_id` (a trust boundary, not enforced).

## Size budget
The features first brought kernel/ to 4,080 non-comment lines. Back under 4,000 by: one K1 form shared by the exact
classes (`checks.exact_form`, including the adapter resolution), one input helper and one K8 for the exact classes, a
table of the exact classes in `certify`, the ML and LP validation fixtures moved to `verify/ml_validation.py` and
`verify/lp_validation.py` (as `verify/lean_validation.py`; now part of their adapters' hashes, which they were not),
and shorter docstrings for the new code (details here). Count: see the final report.
