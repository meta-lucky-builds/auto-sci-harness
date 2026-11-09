# s4-integrity: closing the integrity gaps of the last wave (design note)

Branch `s4-integrity` (base origin/int17; newer int17 merged in, never rebased). Labels:
- **built** means the code exists, ruff and mypy are clean, and `python -c` imports work.
- **NOT test-verified** applies to everything here. The owner deferred tests: no test, scan, rebuild, exam or
  migration was run.

All decisions below are the orchestrator's, owner-delegated (2026-10-14 and 2026-10-15).

## 1. Held-out truth outside every agent-readable root (`truth_layout.py`)

**Decision.** Truth moves out of `<data>/suites/...` (an agent-readable root) to `<data>/private/<suites|tasks>/<same
relative path>`.

- **Resolving the location.** Only `truth_layout.truth_dir(task_dir)` resolves it. It is a pure function, and kerneld's
  ingest (`kerneld/ingest.task_private_dir`, s4-kerneld) is meant to call it.
- **Where readers find the truth.** `truth_file` and `truth_files` read it.
  - They refuse an unmigrated suite task, one whose truth is still in `<task>/private/` under a mapped data directory.
    The error points to the migration script.
  - They refuse any truth location inside an agent-readable root. This is the runtime check, alias-safe through
    `pathsafe`.
- **Task directories outside `<data>/suites|tasks`.** These are test temporary directories and archives. They keep the
  legacy in-task `private/`, but only outside every agent-readable root.
- **Static check.** `truth_layout_problems(scan=)`:
  - flags a truth root that overlaps an agent-readable root;
  - with `scan=True`, flags legacy truth found inside a root.
- **Sandbox protection.** `sandbox/base.protected_paths` now includes `<data>/private/suites` and `<data>/private/tasks`.
  No mount may overlap them, even before a task registers.
- **`default_agent_readable_roots(existing_only=False)`.** A containment check no longer passes just because a root
  does not exist yet.
- **Readers switched to the new layout:**
  - `research/claims.register_task` (s4-kerneld's ingest replaces it), `research/modules.setup`, `research/task.load_task`;
  - `store/run_bundle` (truth is recorded under the old `private/` keys);
  - `evolve/export._fingerprints`, `evolve/multiplicity`;
  - `research/suite` (`write_task`, `mint_heldout`, `verify_mint`), `research/rebuild` (digest keys unchanged; refuses
    stranded truth);
  - `salts/generate`;
  - `scripts/{validate_suite,suite_checks,archive_suite (--with-truth)}`;
  - `examples/tasks/make_rings_task.py`.
- **Migration: `scripts/migrate_truth.py`, NOT executed (a data change).**
  - Modes: `plan` (dry run), `apply --archive DIR`, `verify --archive DIR`.
  - It archives and never deletes. It writes a hash manifest of paths, sizes and SHA-256 hashes only, and it is
    idempotent.
  - Coverage:
    - every legacy `private/` under every agent-readable root, including `tasks/rings-v1/private/test_y.csv`
      and the d7fa1cf8 `salts-tmp` copies;
    - with `--only-mapped`, only the live tasks are moved, and other sessions' scratchpads are left for the owner.

## 2. Truth-copy declarations (`research/truth_copies.py`)

- **The exporter's record.** `suite.write_task` records every public column that carries the held-out labels in the
  truth directory as `truth_copies.json`. It never goes in an agent-readable file. A column is recorded when:
  - the author declared it (`TaskSpecFiles.truth_copies`);
  - or it is detected: one-to-one with the labels (classification) or numerically equal to the label (regression),
    with at least 20 rows (UNCALIBRATED).
- **Declaring to the Kernel.** `declare_task_truth_copies(kernel, task, task_dir, ids)` turns each record into
  `Kernel.declare_truth_copy`. It skips an identical earlier declaration.
- **Where the call belongs.** Following the coordination with s4-kerneld, it goes inside
  `kerneld/ingest.research_task`. **That one call is NOT added here** (`kerneld/` belongs to s4-kerneld).
- **Expected effect today.** None: the detector should find nothing on today's suite (inferred), so production declares
  nothing until a task has a copy.

## 3. Salts redesign (sub-agent s4-integrity__salts; `docs/salts_redesign.md`)

- **Positive salts.** A positive salt is a fresh, unmodified draw of the real family's generator. Its known effect is
  the Monte Carlo expected effect of the reference rule over the salt's own baseline (`SALT_MC_ROWS` = 100,000,
  UNCALIBRATED).
- **Null salts.** They come only from a genuinely null family that is also in the real pool. `noise` is opt-in via
  `build_suite(null_families=)`, which is an owner decision.
- **Dropped.** ρ = 1/2 relabelling salts and plasmodes are dropped.
- **Unchanged.** The detectability exam is unchanged. The exam result is NOT MEASURED.

## 4. Large held-out sets

- **Parameter.** `build_suite(heldout_rows=)` and `chh build-suite --heldout-rows N`. The value is frozen in the rebuild
  manifest only when set, so old manifests hash unchanged.
- **How the rows are added.** The training file is byte-identical. The test set is topped up from independent keyed
  streams.
- **Proposed value.** `PROPOSED_HELDOUT_ROWS = 4500`, UNCALIBRATED. It comes from w2-suite's PARTIAL preflight:
  - effect 0.04 at δ = 0.02 gives P(certify) 0.75 at 4,000 rows and 0.88 at 5,000 rows;
  - 4,500 rows was not measured;
  - accuracy tasks only.
- **Consequence.** Any edit to `research/suite.py` changes `generator_code_sha256`, so `--verify` on the frozen m2 suite
  reports "code differs". The large sets need a new rebuild (a new record directory) anyway.

## 5. Windows path aliases (`pathsafe.py`)

- **The helper.** One helper, built on the approach of `evolve/reveal.py`:
  - canonicalise: strip `\\?\`, `\\.\` and `\\?\UNC\`, map a local admin share to its drive, drop trailing dots and
    spaces, then `resolve()`;
  - then compare `samefile` over every existing ancestor.
- **Users.** `sandbox/base.check_mount` (the case verified vulnerable) and `safe_path`, `salts/plan`, `commitment`
  (secret file, `agent_readable_root_of`), `research/rebuild`, `scripts/archive_suite`, `scripts/suite_checks`, and
  `evolve/reveal` (now delegating).
- **`evolve/export_tools.py`** runs stdlib-only inside the proposer sandbox, so it keeps a copy of the prefix rule. A test
  checks that the copy agrees with `pathsafe`.
- **Not touched (s4-kernel's files).** `kernel/ledger.py:136` and `kernel/adapters.py:76` still use `is_relative_to`.

## 6. s3-verifiers M1 and M4

- **M1.** `hypotheses` and `memory` are now in `RESERVED_OUTPUT_KEYS`.
  - Only the existing `hypothesize` node keeps writing `hypotheses`, under its own id (`ESTABLISHED_WRITER_KEYS`), so
    its prompt stays evolvable.
  - `research.recall_memory` is a reserved-writing module.
- **M4.** Every validation of a new verifier against a synthesis suite's sealed half is recorded before it happens, as a
  `sealed_look` store record.
  - Looks are budgeted at `SYNTH_MAX_SEALED_LOOKS` = 8 distinct verifiers per problem (UNCALIBRATED).
  - `usable_tier` refuses a verifier that was never recorded.
  - `research.synthesize_verifier` is a human-only node (`HUMAN_ONLY_MODULE_PREFIXES`).
  - Limit: these are store records, not Kernel ledger entries. A `sealed_look` ledger kind is requested from s4-kernel.

## 7. s3-report items

- **Axis G, unknown USD.** Superseded: int17 (s3-gateway 64bac01) already shows unpriced USD as unknown or UNCALIBRATED,
  using the cost table. My version was dropped at the merge.
- **Audit arm bundles.** `store/run_bundle.write_run_bundle` is called for every claim-audit arm run (invariant 9).

## 8. Transcript scanner (`scripts/scan_transcripts.py`, NOT run)

- **What it reads.** It lists files under %TEMP%\claude and ~/.claude/projects that hold suite held-out rows WITH their
  label (format-agnostic, through `suite_checks.TokenIndex`) or a canary. `--features` also lists feature-only files.
- **What it writes.** A report of paths and counts only, which it refuses to write inside a root.

## 9. Added by the coordinator (s4-station finding, 2026-10-15): held-out values in memory

- **Negative memory.** It keeps only the verdict, the failing check codes and the pre-registered claim fields. No
  held-out estimate or interval is kept.
- **Screen on every memory write.** A write is refused when:
  - a data key names a held-out value;
  - or the text or data quotes a cited certificate's held-out estimate or bound (a standalone number token, 3 or more
    significant digits).
- **Old entries.** When rendered, they show as withheld.
- **`chh memory harvest-negatives`.** It now needs `--docker` or `--dev-sandbox`, which supply the replayer, and fails
  closed otherwise.

## Runs and tests to do later (in this order; none run)

1. **ruff and mypy** on the whole tree (done here only on the touched files).
2. **pytest, new tests:**
   - tests/test_pathsafe.py, test_truth_layout.py, test_migrate_truth.py, test_scan_transcripts.py;
   - test_truth_copy_declarations.py, test_reserved_keys_m1.py, test_synthesis_looks_m4.py;
   - test_audit_arm_bundles.py, test_memory_heldout_values.py, test_salts.py.
   Then their fail-first runs at 82719e1, or at edbcb25 for test_salts.py.
3. **pytest regression** across the layout change. These files used `private/` directly:
   - tests/test_suite_builder.py, test_mint.py, test_run_bundle.py, test_suite_rebuild.py, test_suite_checks.py;
   - test_research.py, test_evolve*.py, test_campaign_replay.py, test_kerneld*.py, test_leak.py;
   - test_memory.py, test_memory_cli.py, test_evolve_hardening.py, test_redteam_evolve.py, test_release_embargo.py.
   Then the full gate, run alone.
4. **The migration, as a run-phase data change after the owner's go-ahead:**
   - `python scripts/migrate_truth.py plan`
   - `python scripts/migrate_truth.py apply --archive C:\SERS\<USER>\custom-harness-data\archive\truth-<date>
     [--only-mapped] --copy-manifest snapshots\truth-migration-<date>.manifest.json`
   - `python scripts/migrate_truth.py verify --archive ...`
5. **The rebuild with large held-out sets.** Use a new record directory and beacon; it is an owner decision:
   `chh build-suite <data>\suites\m3 --frozen --docker --heldout-rows 4500 [--null-family noise] --record-dir
   <data>\private\suite-m3 --beacon-file ...`, then `--verify`.
6. **Real-preflight points:** effect 0.04 at 4,500 rows, and effect 0.05 at 1,500 rows.
7. **The salts exam:** `scripts/suite_checks.py <suite> --record-dir ... --beacon-file ... --only salts --out
   <private>\reports\salts.json`.
8. **The transcript scan:** `python scripts/scan_transcripts.py <suite> --out <data>\private\reports\transcripts.json
   --features`, run on the live suite AND on the archived pre-rebuild suites. Then archive any listed file, with the
   owner's go-ahead.
