# Suite rebuild: freeze the manifest, then mint (GOAL §16.1–16.3, §16.8, §16.9)

Branch `w2-suite`. Labels used below: **verified** = I saw the output in this task; **reported** = another agent ran
it; **inferred** = reasoned from code or arithmetic, not run.

## What was wrong with the suite on disk
- It was generated before the COUPLED-PRNG fix. The pre-fix seeded generators regenerate every held-out label of
  all 12 synthetic tasks exactly (verified again here as the positive control: 12/12 exact on the archived suite).
- Its canaries were fresh entropy, so no rebuild was reproducible, and nothing recorded what was frozen when.
- `suites/m2/_export/export.json` held every labelled row of the 5 real datasets next to the suite.

## The rebuild (`research/rebuild.py`, `chh build-suite --frozen`)
1. **Manifest** (no data yet): generator version and SHA-256 of `research/suite.py`, every task (id, family,
   source, kind, rows, split), the held-out families, the SHA-256 of each real dataset from the trusted exporter,
   the candidate image, and a SHA-256 commitment to the sealed beacon.
2. **Freeze.** The manifest is registered in the Kernel ledger (`register_artifact`, kind `suite_manifest`). The
   ledger is then anchored to `<record-dir>/anchor.jsonl`, outside the store, and `suite.require_anchored` must
   pass.
3. **Mint.** The master key is `draw("heldout_mint", prereg_hash=manifest hash, beacon)`. Everything below comes
   from independent HMAC streams under that key (`suite.build_suite`):
   - every synthetic task's features, labels and split;
   - every real or repair task's split;
   - every canary.

   The real datasets are fixed public data. They stay `retrospective_target`s: only their split is drawn here.
4. **Provenance in `suite.yaml`.** Each task records:
   - `image: custom-harness-sandbox:py312-candidate`;
   - `provenance.generator` (`synthetic:<family>`, `real:<loader>` or `repair:<id>:<loader>`);
   - `generator_version` and `key_commitment` (SHA-256 of the task key);
   - `rows` (synthetic) or `dataset_sha256` (real).

   The generator block holds the manifest hash, the beacon commitment and the code hash. These are commitments only.
5. **Secrets stay out of agent-readable roots.** The beacon, the master key and the task keys are never written.
   The record (`record.json`: manifest, anchor, the hash of every file including the private ones) and
   `real_export.json` sit in the record directory. The command refuses a record directory or beacon file inside
   `commitment.default_agent_readable_roots()`.
6. **Idempotent.** A re-run re-derives the suite in a temporary directory inside the record directory and compares
   it with OUT.
   - Equal: `"status": "unchanged"`, with no new ledger entry and no new anchor line.
   - Different: refused. It never overwrites.

   `--verify` re-derives from the record, the beacon and the recorded export, and lists every differing file.
7. **The full image is used only by the trusted exporter.** `--docker` runs `export_real` in the full image inside
   `cmd_build_suite`, the only full-image site that `tests/test_bundled_data.py` allows. With `--frozen`, its
   workspace is `<record-dir>/_export`.

**The beacon must stay sealed for the suite's life.** The draw is beacon-only. Anyone holding the repo, the manifest
hash and the beacon re-derives every label, so a public beacon (for example a drand round) must not be used for a
long-lived suite.

The beacon used here is 32 bytes of OS entropy, which I generated. It is not a public beacon, and the builder knew it
at freeze time (needs owner decision).

### Commands (as run, 2026-10-08)
```
# archive the old suites first (move + per-file SHA-256 manifest; refuses an agent-readable archive dir)
python scripts/archive_suite.py C:\Users\mykids\custom-harness-data\archive\suites-2026-10-08 ^
    C:\Users\mykids\custom-harness-data\suites\m2 C:\Users\mykids\custom-harness-data\suites\m2-v1 ^
    --copy-manifest snapshots/suites-archive-2026-10-08.manifest.json --reason "..."
# sealed beacon (value never printed)
python -c "import secrets,sys; open(sys.argv[1],'w').write(secrets.token_hex(32)+'\n')" ^
    C:\Users\mykids\custom-harness-data\private\beacons\suite-m2.txt
# build (trusted exporter in the full image), re-run twice, verify
chh build-suite C:\Users\mykids\custom-harness-data\suites\m2 --frozen --docker ^
    --record-dir C:\Users\mykids\custom-harness-data\private\suite-m2 ^
    --beacon-file C:\Users\mykids\custom-harness-data\private\beacons\suite-m2.txt
chh build-suite ...\suites\m2 --frozen --real-export ...\private\suite-m2\real_export.json --record-dir ... --beacon-file ...
chh build-suite ...\suites\m2 --verify --record-dir ... --beacon-file ...
chh ledger-verify --anchor C:\Users\mykids\custom-harness-data\private\suite-m2\anchor.jsonl
# zero-call checks (reads private labels ONLY to score attacks/scans; report goes to the private dir)
python scripts/suite_checks.py ...\suites\m2 --record-dir ... --beacon-file ... --old-suite <archived m2> ^
    --extra-root C:\Users\mykids\OneDrive\Desktop\META\custom-harness --out <private>\reports\suite_checks.json
```

### Results of the build (verified)
- First run: `built`, 21 tasks.
  - Manifest `0e694f4a9b738c31…`, recorded at live-ledger seq 16 and anchored (length 16).
  - Suite digest `f11b55f44eeb1fd4…`.
- Second run (`--real-export` from the record): `unchanged`, same digest.
- Third run (fresh `--docker` export): `unchanged`, same digest.
- The anchor file still has 1 line, and the ledger still has 16 entries.
- `--verify`: 0 problems over 128 files. `ledger-verify --anchor`: intact, 0 problems.
- The fresh full-image export matches the archived `_export/export.json` dataset by dataset (same SHA-256 for all 5).
- Logs: `scratchpad/w2logs/rebuild_run1.log` and `rebuild_idempotence.log`.

## Archive manifest (nothing deleted; every move hash-checked after the move)
| moved | to | files | tree SHA-256 |
|---|---|---|---|
| `suites\m2` (incl. `_export\export.json`) | `archive\suites-2026-10-08\m2` | 130 | `c7b516e8…` |
| `suites\m2-v1` | `archive\suites-2026-10-08\m2-v1` | 130 | `f7d51ed9…` |
| 9 old wine run workspaces `sandboxes\reproduce-*`, `sandboxes\research_v1-wine_knn_unscaled-*` | `archive\sandboxes-2026-10-08\` | see manifest | see manifest |
| red-team scratch `w2-attack` (its guesses are exact wine labels) | `archive\redteam-2026-10-08\` | | `5e72211c…` |

- **Paths.** `archive\` is `C:\Users\mykids\custom-harness-data\archive\`. It is outside every agent-readable root
  and outside the repo.
- **Manifests.**
  - In the archive: `ARCHIVE-*.manifest.json` in each archive directory.
  - Committed: `snapshots/suites-archive-2026-10-08.manifest.json` and
    `snapshots/sandboxes-archive-2026-10-08.manifest.json`. They hold paths, sizes and SHA-256 only.
- **`m2-v1` was archived too.** Its public train files hold labelled rows of the same real datasets under an old
  split, so they label the new held-out rows.
- **Moved paths no longer resolve.** Old store or bundle records that point at the moved workspaces now point at
  paths that do not exist. The data are in the archive.
- **.gitignore.** It now also ignores `**/private/test_y.csv`, `**/private/canary.txt`, `**/private/mint.json`,
  `real_export.json` and `_export/`. `git ls-files` lists no labelled file (verified).

## Zero-call checks per task (verified unless marked)
Runs: `suite_checks` run1 at 5191cb0 (isolation, PRNG, preflight audit, salts, scan), run2 at eff0801 (scan after
archiving), run3 at 54cc52f (isolation, PRNG, salts; its scan was killed when the session ended), the format-agnostic
scan has NOT been run on the suite (two attempts, at d5e25e2 and 18889f1, were killed by session ends or never got
a limiter slot; the owner then paused long runs). Reports are in `custom-harness-data\private\suite-m2\reports\` (never in an
agent-readable root; `suite_checks.py` now refuses that).

Columns:
- **iso**: problems reported by `check_split_isolation`.
- **scan line / agn.**: distinct held-out rows found WITH their label in any agent-readable file. "line" = exact CSV
  line in the suite's format (run2); "agn." = the format-agnostic search (NOT MEASURED: never completed on the suite).
- **PRNG old / keyed**: is any synthetic task's held-out label vector regenerated exactly? "old" = the pre-fix seeded
  generators (`tests/fixtures/suite_prefix_401c00a.py`); "keyed" = the current generators keyed with every public
  value (manifest hash, key commitments, beacon commitment, task id; 12 keys per task). Agreement counts are kept in
  the private report only (each is a small disclosure about a public guess vector).
- **P(cert) audit**: `chh preflight-suite`'s audit (solution perfect on dev against a dev baseline of 0.8; family 1;
  100 simulations; θ = 0.5), with the min effect it used.
- **n_test for P≥0.8**: minted held-out rows for P(certify) ≥ 0.8 at δ = 0.02, from the REAL preflight (next section).

| task | split | metric | n_dev / n_test | iso | scan line / agn. | PRNG old / keyed | P(cert) audit (min effect) | n_test for P≥0.8 at δ=0.02 (Δ=0.04 / Δ=0.05) |
|---|---|---|---|---|---|---|---|---|
| rings-s1 | evolve | accuracy | 630 / 270 | 0 | 0 / NOT MEASURED | no / no | 1.00 (0.088) | 4,000–5,000 / ≤ 2,000 |
| rings-s2 | gate | accuracy | 1050 / 450 | 0 | 0 / NOT MEASURED | no / no | 1.00 (0.090) | 4,000–5,000 / ≤ 2,000 |
| xor-s1 | evolve | accuracy | 630 / 270 | 0 | 0 / NOT MEASURED | no / no | 1.00 (0.088) | 4,000–5,000 / ≤ 2,000 |
| xor-s2 | sealed | accuracy | 1050 / 450 | 0 | 0 / NOT MEASURED | no / no | 1.00 (0.090) | 4,000–5,000 / ≤ 2,000 |
| spirals-s1 | gate | accuracy | 630 / 270 | 0 | 0 / NOT MEASURED | no / no | 1.00 (0.088) | 4,000–5,000 / ≤ 2,000 |
| spirals-s2 | gate | accuracy | 979 / 421 | 0 | 0 / NOT MEASURED | no / no | 1.00 (0.090) | 4,000–5,000 / ≤ 2,000 |
| imbalanced-s1 | evolve | f1_macro | 840 / 360 | 0 | 0 / NOT MEASURED | no / no | not simulated | not modelled |
| imbalanced-s2 | sealed | f1_macro | 1400 / 600 | 0 | 0 / NOT MEASURED | no / no | not simulated | not modelled |
| friedman-s1 | evolve | rmse | 630 / 270 | 0 | 0 / NOT MEASURED | no / no (0 exact values) | not simulated | not modelled |
| friedman-s2 | gate | rmse | 1050 / 450 | 0 | 0 / NOT MEASURED | no / no (0 exact values) | not simulated | not modelled |
| interactions-s1 | evolve | mae | 630 / 270 | 0 | 0 / NOT MEASURED | no / no (0 exact values) | not simulated | not modelled |
| interactions-s2 | evolve | mae | 1050 / 450 | 0 | 0 / NOT MEASURED | no / no (0 exact values) | not simulated | not modelled |
| iris-real | evolve | accuracy | 105 / 44 | 0 | 0 / NOT MEASURED | n/a | 0.67 (0.071) | cannot mint (retrospective) |
| wine-real | evolve | accuracy | 124 / 54 | 0 | 0 (51/54 before archiving) / NOT MEASURED | n/a | 0.84 (0.073) | cannot mint |
| digits-real | sealed | accuracy | 1257 / 540 | 0 | 0 / NOT MEASURED | n/a | 1.00 (0.090) | cannot mint |
| breast_cancer-real | gate | f1_macro | 398 / 171 | 0 | 0 / NOT MEASURED | n/a | not simulated | cannot mint |
| diabetes-real | evolve | rmse | 309 / 133 | 0 | 0 / NOT MEASURED | n/a | not simulated | cannot mint |
| wine_knn_unscaled | evolve | accuracy | 124 / 54 | 0 | 0 (51/54 before) / NOT MEASURED | n/a | 0.84 (0.073) | cannot mint |
| digits_tree_depth2 | sealed | accuracy | 1257 / 540 | 0 | 0 / NOT MEASURED | n/a | 1.00 (0.090) | cannot mint |
| cancer_logreg_unconverged | gate | f1_macro | 398 / 171 | 0 | 0 / NOT MEASURED | n/a | not simulated | cannot mint |
| diabetes_ridge_overregularised | evolve | rmse | 309 / 133 | 0 | 0 / NOT MEASURED | n/a | not simulated | cannot mint |

### Reading the table
- **Split isolation:** 0 problems (verified in run1, run2, run3).
- **COUPLED-PRNG regression: 0/12 exact** under both attacks (verified, run1 at 5191cb0 and run3 at 54cc52f on the
  same suite).
  - Positive control: the same attacker on the archived pre-fix suite gives 12/12 exact (verified, run1 and run3).
  - **Chance.** The keyed attack is the best of 12 keys per task. Its best agreements sit within about 2.5 binomial
    SDs of chance agreement, which is what selection over 12 keys gives (inferred). Regression targets agree on 0
    exact values.
  - The first red team tried 5,000 old seeds, 324 public-value keys, low-entropy beacons up to 2·10^6 and a draw
    over 2·10^5 candidates: 0 hits (reported).
- **Truth scan, exact CSV lines (run2 at eff0801).** 64,996 files: the worktree, the main checkout,
  `custom-harness-data\{sandboxes, repos, suites, tasks}` and all of `%TEMP%\claude`. It skipped `.git` object stores
  and each task's own `private\`.
  - 0 labelled held-out rows for all 21 tasks; 0 canary hits; the beacon in no encoding (verified).
- **Truth scan, format-agnostic (new in 54cc52f, sped up about 3x in 18889f1; NOT RUN on the suite).** The exact-line scan misses a copy in another
  number format, with another delimiter, behind an index column, or inside JSON text whose newlines are escaped (agent
  transcripts). The new `TokenIndex` search normalises every number to 6 significant digits and cuts text at real or
  escaped newlines, brackets, braces, parentheses and quotes; a segment is a hit when it contains a held-out row's
  features in order with the label right after them ("label first" is counted apart). Tests:
  `tests/test_suite_checks.py` (9 tests: fail first at 1790edd and 6955f8c, 9 passed at d5e25e2; verified).
  Results on the suite: NOT MEASURED. The 18889f1 speed-up (one-pass tokenizer, first-number filter) passed ruff
  and mypy; on one 3.5 MB transcript the old code took 3.2 s (7 needles) and the new 1.2 s (3,500 needles) (verified,
  one-off timing on a loaded machine), but the test file
  has NOT been re-run at 18889f1 (not test-verified).
  - It still misses copies with fewer significant digits than the suite's own values, binary formats (files with a
    NUL byte in their first 8 KiB are not token-scanned), and split or transformed copies.
- **Second, independent red team (opus, 2026-10-09, after archiving; did not read the rebuild or check code).**
  Recovered 0 labels and wrote no guesses (reported). Findings (reported):
  - No public copy of the 5 real datasets in any root it searched; no commitment value in agent-visible text.
  - Its numeric scan (48,006 files of `%TEMP%\claude`, stopped by its time budget) found 13 held-out FEATURE rows
    (breast_cancer/cancer_logreg 3, iris 3, wine 2) inside 3 agent transcripts, without deciding whether a label sits
    next to them. Features of held-out rows are public (`test_X.csv`); the format-agnostic scan above would decide
    whether any of them carries its label; it has NOT been run, so this is OPEN.
  - It was halted by a safety classifier while reading the text around those matches; it then stopped that step.
- **Truth scan before archiving the old wine run workspaces.** The first scan found **wine-real and
  wine_knn_unscaled** held-out rows with labels in 56 old `train.csv` copies under `sandboxes\` (verified).
  - The first red-team agent found the same independently. Its guesses scored **51/51 exact** (54/54 overall, the
    other 3 by 3-NN), scored against `private/test_y.csv` by the first builder (reported; that builder's transcript
    is lost). Its guess files are archived in `archive\redteam-2026-10-08\w2-attack`.
  - Bug found and generalised: the scanner credited a row shared by a repair task and its real twin to only one of
    them. Fixed (commit eff0801).
- **Candidate image scan (Docker).** Docker was running and both images existed, so I ran `tests/test_bundled_data.py`
  in full (verified: `w2logs/image_scan.log`, **12 passed**). It includes the whole-filesystem scan of the candidate
  image, with the full image as positive control, and the loader refusals.
  - That scan looks for the 5 real datasets.
  - It cannot contain the newly minted synthetic rows: the image predates them, and the keys are sealed (inferred).

## Certifiability (§16.8) and minted sizes (§16.2)
### The preflight's own audit (verified, real K4/K6/K7)
- **Coverage.** 11 of 21 tasks are accuracy tasks and are simulated. The other 10 (f1_macro, rmse, mae) return
  "unsupported" and are not assessed.
- **Results under the audit's assumption** (perfect on dev, true dev effect 0.2):
  - 10 of the 11 simulated tasks have P(certify) ≥ 0.8 (8 at 1.00; wine-real and wine_knn_unscaled at 0.84);
  - iris-real is at 0.67, with only 44 held-out rows.
- **The assumption is generous.** The synthetic tasks really do have that much headroom over their centroid
  baselines. On the public training files, the reference rule beats the centroid baseline by 0.38–0.53 accuracy
  (verified). Large claims on them are therefore certifiable at today's sizes.

### Small effects at the δ = 0.02 floor: NOT certifiable at today's sizes
Normal approximation (inferred arithmetic, labelled):
- **The model.** A one-sided 95% lower bound must reach δ. The claim's true paired effect is Δ. The comparator alone
  is right on 5% of rows, so the discordant rate is Δ + 0.10, and the baseline is assumed to have 0.8 accuracy.
- **Rows needed for P(certify) ≥ 0.8:**

  | true effect Δ | 0.03 | 0.04 (= 2δ) | 0.05 | 0.10 |
  |---|---|---|---|---|
  | held-out rows needed | 7,982 | 2,140 | 1,014 | 184 |

- **At today's sizes** (44–600 held-out rows), the P(certify) of a Δ = 0.04 claim at δ = 0.02 is:
  - 0.10 for iris;
  - 0.22–0.37 for the synthetic tasks.

  **No task reaches 0.8 for Δ ≤ 0.05.**
- **At `MINT_TEST_ROWS` = 20,000**, every Δ ≥ 0.03 reaches ≥ 0.99 (normal approximation).
- **The normal approximation is optimistic.** It ignores the Dirichlet uncertainty of the dev counts (prior 1 per
  cell) that the real preflight adds. The REAL preflight (`research.preflight.preflight`, real K4/K6/K7, default
  `PreflightConfig`: n_sims = 200, seed 0, θ = 0.5; dev cells for n_dev = 1050, baseline accuracy 0.8,
  comparator-only-right rate 0.05; min_effect δ = 0.02) gives (verified):

  | true effect Δ | n_test | P(certify) | K6 (the only failing check) | log |
  |---|---|---|---|---|
  | 0.04 | 450 | 0.33 (n_sims 100) | 0.33 | first builder, `w2logs/preflight_spot.log` (reported) |
  | 0.04 | 2,140 | 0.68 (n_sims 100) | 0.68 | same (reported) |
  | 0.04 | 4,000 | 0.79 (n_sims 100) / 0.75 (n_sims 200) | 0.79 / 0.75 | `preflight_spot.log` / `preflight_bracket_d04.log` |
  | 0.04 | 4,500 | NOT MEASURED | | (queued, never ran) |
  | 0.04 | 5,000 | 0.88 | 0.88 | `preflight_bracket_d04.log` |
  | 0.05 | 1,500 | NOT MEASURED | | (queued, never ran) |
  | 0.05 | 2,000 | 0.85 | 0.85 | `preflight_bracket_d05b.log` |

  Each estimate has a Monte Carlo SE of about 0.03 at n_sims = 200. Bracket logs are in this task's scratchpad
  `f4d503e4…\scratchpad\logs\`.
- **Minted held-out rows needed per synthetic accuracy task** (rings, xor, spirals; δ = 0.02, P ≥ 0.8, real
  preflight): **between 4,000 and 5,000 for a Δ = 2δ = 0.04 claim**, and **at most 2,000 for Δ = 0.05**
 . Today they have 270–450, so add roughly 3,600–4,700 rows (Δ = 0.04) or up to about 1,750 (Δ = 0.05).
  The normal approximation said 2,140 and 1,014.
- **Not modelled:** f1_macro, rmse and mae tasks (imbalanced, friedman, interactions, breast_cancer, diabetes and
  their repairs). The preflight simulates accuracy only, so I give no minted size for them.
- **Real tasks cannot be minted.** At 44/54/540 rows, iris, wine and digits can only certify large effects.

## Salts detectability exam (§16.9): FAILS, d̂ = 1.0 (run1) and 0.975 (run3)
Run1 (5191cb0) is tabulated below. Run3 (54cc52f, fresh salts and fresh extra real draws) gave the capable spotter
TP 40/40, FP 1/40, d̂ = 0.975, exact one-sided 95% upper bound 1.0: fail again (verified).

**Setup (verified, 0 model calls):**
- 40 null salts (ρ = 1/2, `salts.generate.null_salt`) against 40 real tasks.
- The real arm is the 6 rings/xor/spirals tasks of the rebuilt suite plus 34 fresh draws of the same generators at
  the same sizes.

| spotter | TP/40 | FP/40 | d̂ | exact one-sided 95% upper bound | pass (≤ 0.2) |
|---|---|---|---|---|---|
| blind ("real" always) | 0 | 0 | 0.00 | 0.195 | yes |
| capable: train-label agreement with the family reference rule < 0.85 | 40 | 0 | 1.00 | 1.00 | **no** |

**Diagnosis: what tells the null salts apart.** The statistic is the agreement of the public training labels with the
family's reference rule:
- rings: real 0.873–0.905, salts 0.616–0.661;
- xor: real 0.915–0.935, salts 0.700–0.743;
- spirals: real 0.996–1.000, salts 0.803–0.837.

The two arms do not overlap at all, so a threshold at 0.873 separates them perfectly. The cause is structural:
- A null salt must make the baseline Bayes-optimal. It therefore relabels at ρ = 1/2 the rows where the reference rule
  and the centroid baseline disagree.
- That share is D = 0.38–0.66 of all rows here (verified on the public training files). The reference rule's
  agreement drops by about D/2, which is 0.18–0.26.
- The real tasks' own spread in this statistic is about 0.01–0.03.

Any agent that fits a model better than the baseline sees the gap. This is exactly what the agents are asked to do.

**Proposed fixes, with numbers (inferred):**
1. **Generator-identical salts for minted synthetic families.**
   - What it is: a salt is a fresh draw of the same generator. The known effect is the exact expected effect of the
     reference rule over the baseline, computed by Monte Carlo on the generator (10^5 rows give an SE of about
     0.0016).
   - Detectability: salts then have the real tasks' distribution by construction, so d = 0. At 40 + 40 a blind-level
     result gives an upper bound of 0.195, below 0.2.
   - Cost: they test coverage only at the families' natural effect sizes (0.38–0.53 here), not near δ.
2. **Null salts only from genuinely-null families that are also in the real pool.** An example is a rings variant
   whose labels come from the centroid rule plus noise. The proportions matter:
   - If null-family tasks are a fraction q of the real pool and salts are drawn only from those families, the best
     spotter's statistic no longer separates the arms within a family.
   - Mixing a few null tasks into a high-headroom pool does not help: with a fraction π of null-like reals,
     d = 1 − π, and the bound needs d̂ ≈ 0.
   - The exam needs at least 40 real presentations from the salted families.
3. **Stronger baselines alone are not enough.** To bring the D/2 gap under about one within-arm SD (0.01), every
   baseline would need to agree with the reference rule on at least 98% of rows (D ≤ 0.02). That leaves no headroom
   for improvement claims.

**Recommendation.** Drop ρ = 1/2 null salts on these families. Use (1) for coverage, and (2) only if the owner adds
null families to the suite. This is an owner decision (§13.8).

## What remains unverified / open
- **The beacon is not a public beacon.** It is local entropy that the builder knew before the freeze. The owner may
  replace it, which means a new record directory and a new freeze.
- **The manifest freezes `research/suite.py`'s code hash.** Any later edit to that file, documentation included,
  makes `--verify` report "code differs", and a `--frozen` re-run is refused by the existing record. That is
  deliberate: a changed generator is a new suite.
- **Codex runs with `-s read-only`.** I infer from the gateway code that a read-only Codex sandbox can still read any
  file the user can read: the private dirs, the archive and the record directory. I did not test this. Every
  "outside agent-readable roots" claim here is relative to `commitment.default_agent_readable_roots()` and the
  sandbox mounts, not to Codex's filesystem view.
- **Label copies outside m2 remain in agent-readable roots** (reported by the red team; I did not read them):
  - `custom-harness-data\tasks\rings-v1\private\test_y.csv` (an old rings-v1 task, not m2 data);
  - another session's `d7fa1cf8…\scratchpad\salts-tmp\e\*\private\test_y.csv`.

  Owner or integrator decision.
- **Remaining `sandboxes\research_v0-rings-v1-*` workspaces.** They hold rings-v1 data, not m2. 0 m2 rows were found
  there (verified by the scan).
- **The truth scan's format limits.** The exact-line scan only matched the suite's own CSV format; the
  format-agnostic scan (54cc52f) closes reformatting, delimiters, index columns and escaped JSON, but not
  lower-precision, binary, split or transformed copies.
- **Each task's `private\` (labels, canary, mint record) lives under `custom-harness-data\suites`, which is an
  agent-readable root** in `commitment.default_agent_readable_roots()` (raised by the second red team). This is the
  existing layout: the scans skip `private\` as the by-design truth location, and `research/modules.py` calls
  `protect_path(task_dir / "private")` at run time. I did not test that protection, and with the Codex note above it
  is the main remaining path to held-out labels. Moving the truth out of the suite tree changes the task layout used
  by scoring code outside this branch's area (owner or integrator decision).
- **Agent transcripts are an attack surface.** They sit in `%TEMP%\claude`, an agent-readable root, and they hold
  whatever an agent printed, including held-out feature rows (second red team). The format-agnostic scan is built to check them
  for labelled rows (not yet run); a builder must never print labels (this task printed none: it read only counts, kinds and hashes).
- **The suite-level mint is weaker than per-claim minting.** Truth is minted after the suite freeze, not after each
  claim's freeze. Test-set wear across claims on one suite remains, so `minted` stays false on the tasks.
- **The rebuild wrote one `register_artifact` entry** (kind `suite_manifest`, seq 16) to the LIVE store's ledger, plus
  one anchor line in the record directory.

## Runs still to do (none run this round: the owner paused tests and long runs)
1. `pytest tests/test_suite_checks.py` at 18889f1 or later (the scan speed-up is not test-verified).
2. The format-agnostic truth scan on the suite: `scripts/suite_checks.py ... --only isolation,scan --out
   <private>\reports\suite_checks.run5-scan.json` (fills the "agn." column and settles the 13 transcript feature
   matches). Expect 30-60+ minutes; it logs progress every 5,000 files.
3. Real-preflight points Δ = 0.04 at n_test 4,500 and Δ = 0.05 at 1,500 (and lower) to narrow the minted sizes.
4. `chh build-suite --verify` at the merge 2c7a63c (int16-fix d2ff98e touches no suite code, so the frozen code hash
   should still match; inferred, not run).
