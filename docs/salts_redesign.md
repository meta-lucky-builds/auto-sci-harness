# Salts redesign (decision 2026-10-14, GOAL §16.9)

Status: **built, NOT test-verified (tests deferred by owner). Detectability exam result: NOT MEASURED.**
Branch `s4-integrity-salts`. This note supersedes the "Construction" and "Detectability" parts of `docs/salts.md`.

## Why
The detectability exam failed twice on the rebuilt suite (w2-suite, `suite_rebuild.md`): d_hat = 1.0 and 0.975.
The capable spotter used one statistic: how often the public train labels agree with the family's reference rule.
- Real tasks scored 0.87-1.00 on this statistic.
- The rho = 1/2 null salts scored 0.62-0.84, because they relabel the D = 0.38-0.66 share of rows where the rule and
  the baseline disagree.
- The two arms did not overlap.

Plasmode nulls have the same class of tell, since their labels are regenerated from the centroid model.

The general bug class: **any salt built by editing a real-looking task's labels toward a known rule moves a statistic
that a capable agent computes anyway.**

## What changed
| | before | now |
|---|---|---|
| positive salt | labels relabelled at rho to plant 2 delta | a fresh draw of the family's generator (`research.suite.synthetic_task` under the salt's key, the real pool's `n_rows` and `heldout_rows`); labels untouched |
| planted effect | realised effect on the test file at the target | the **expected** effect of the reference rule o over the salt's own baseline b (nearest centroid fitted on its train file), by Monte Carlo on `SALT_MC_ROWS` = 100,000 fresh rows (streams `salt-mc/features`, `salt-mc/labels` under the salt key; UNCALIBRATED; SE about 0.0016-0.002). `mc_se`, `mc_rows` and the realised test-file effect (`realised_effect`) are recorded too |
| null salt | rho = 1/2 relabelling of rings/xor/spirals; plasmodes of iris/wine/digits | only genuinely null families (`research.suite.FAMILIES[f].null`, today `noise`) that are also in the campaign's real pool; a fresh draw; `residual_bound = 0` |
| refused | F1/RMSE/MAE families | also: a null salt of any non-null family, any plasmode, and any salt of a family absent from the real pool |
| assignment | `rng.sample` over eligible slots | the same keyed stream, but a salt is never drawn if it would leave its family with no unsalted task in the pool (capacity checked before the draw) |
| unchanged | | `salts/exam.py` (thresholds, per-arm minimum, alpha, presentation), `SALTABLE_SPLITS`, `isolation_problems`, the audit maths, the budget |

**Why the null bound is 0.**
- A noise label is a fair coin, independent of the features and of every other row.
- Any predictor is a function of the test features, the train file and its own randomness. So each test prediction
  is right with probability exactly 1/2.
- Every predictor, the baseline included, therefore has expected accuracy 1/2, and the largest true effect is 0.
- Caveat: this is computational. The coin comes from an HMAC-SHA256 keyed stream, so it holds against any predictor
  that cannot break the PRF without the key.

**Why d = 0 is expected.**
- A salt's files are a draw of exactly the same distribution as a real task of its family, at the same sizes.
- The id is assigned by the campaign and blanked by the exam.
- So no function of the presentation separates the arms, beyond the keyed PRF.

## API changes (for the merger)
- `generate.positive_salt(family, n_rows, *, key, task_id, out, pool_families, heldout_rows=None, mc_rows=None)`:
  `delta` was removed and `pool_families` is required.
- `generate.null_salt(family, n_rows, *, key, task_id, out, pool_families, heldout_rows=None)`: `delta` was removed
  and `pool_families` is required.
- New functions:
  - `generate.salt_spec(family, n_rows, *, key, task_id, heldout_rows=None)`;
  - `generate.expected_effect(family, cent, *, key, mc_rows=None) -> (estimate, se, rows)`;
  - `generate.NULL_FAMILIES`.
- `generate.plasmode_null(...)` always raises `SaltError`, and `PLASMODE_DATASETS` is empty.
- `GeneratedSalt` gained the fields `mc_se`, `realised_effect`, `mc_rows` and `heldout_rows` (defaults None).
  `target_effect` is now the MC value for a positive salt.
- `plan.Slot` gained `heldout_rows: int | None = None`. **Every `campaign_hash` changes**, because `asdict` includes
  the new field.
- `null_ok()` is now true only for genuinely null families.
- New function `plan.pool_families(slots)`.
- `plan.generate(plan, out, *, mc_rows=None)`: `real_data` was removed.
- The plant list gained `mc_se`, `mc_rows`, `realised_effect` and `heldout_rows`.
- `config`:
  - removed `POSITIVE_EFFECT_MULTIPLIER`, `POSITIVE_EFFECT_TOLERANCE` and `NULL_MAX_RESIDUAL_FRACTION`;
  - added `SALT_MC_ROWS`.
- `scripts/suite_checks.py`:
  - `salts_exam(suite, doc, work, per_arm, mc_rows=2000)` returns `{"design", "per_arm", "positive_arm", "null_arm"}`;
  - each arm has the old keys (blind spotter, capable spotter, best threshold, per-family statistic) plus
    `planted_effect_mc` for the positive arm;
  - the null arm is `{"not_run": ...}` unless the suite has a genuinely null family;
  - `tell_statistic` and `run_exam` are unchanged.

## Limits (stated, not assumed away)
- **Coverage is tested only at the natural effect sizes.** The families' natural effects are about 0.36-0.53 (MC
  estimate at 5,000 rows on one key: rings 0.51, xor 0.42, spirals 0.36). Coverage is never tested near delta: no
  "2 delta" salt exists any more.
- **The reference rules are near-Bayes.** `planted_effect` is the expected effect of o, which is a lower bound on the
  best achievable effect. A better-than-o honest subject could read as slightly "inflated".
- **Spirals' rule is about 99% accurate.** A leak can add at most 1 - acc(o), about 0.005, so leak-based cheats are
  invisible on a positive spirals salt. Rings and xor (about 0.1) can show them.
- **The MC ignores `write_task`'s test de-duplication.** With 5-decimal continuous features this is negligible
  (inferred).
- **Null salts need the owner to add a null family to the real pool** (`--null-family noise`, §13.8). Without one,
  campaigns get positive salts only.
- **Every salted family keeps an unsalted task in the pool.** This needs at least 2 slots per salted family.
- **Open tell outside the exam (needs owner decision).** A salt takes a slot's task id. If the real task with that
  id is visible elsewhere (the public suite), an agent can compare the files and see the difference. The exam blanks
  ids and does not model this. Campaigns must use fresh ids, or slots whose real version is never shown.
- **The exam is unchanged.** Its 95% upper bound passes at d_hat <= about 0 at 40 + 40 (20/40 vs 20/40 gives 0.195;
  24 vs 20 gives > 0.2). A truly undetectable design can therefore still fail by chance.
  - P(pass | d = 0) is NOT MEASURED: a quick computation timed out.
  - About 135 + 135 presentations would give margin (config comment).

## Tests and runs to do later (owner: tests deferred)
1. `pytest tests/test_salts.py -q`. The tests were written to fail at origin/s4-integrity (edbcb25); the reasons are
   in the module docstring. Key tests:
   - `test_positive_salt_is_an_unmodified_generator_draw`;
   - `test_positive_salt_planted_effect_is_the_expected_effect_of_the_reference_rule`;
   - `test_null_salt_is_refused_for_every_non_null_family`;
   - `test_salts_of_a_family_absent_from_the_real_pool_are_refused`;
   - `test_null_salt_of_a_genuinely_null_family_has_bound_zero`;
   - `test_every_salted_family_keeps_a_real_task_in_the_pool`;
   - `test_exam_passes_a_blind_spotter_and_the_old_tell_no_longer_separates`;
   - the two device tests.

   The stochastic assertions run on fixed keys. If a fixed draw lands in a stated tail (for example, honest pooled
   inflation at 5%), record it and do not loosen the exam.
2. The full gate (ruff, mypy, pytest), alone.
3. Detectability exam re-run: `python scripts/suite_checks.py SUITE --record-dir DIR --beacon-file F --out
   DIR/reports/salts.json --only salts`. Run it on a suite with heldout_rows and, if the owner adds it,
   `--null-family noise`, so that both arms run. Expected: the capable spotter d_hat is about 0. Whether the bound
   passes is NOT MEASURED.
4. Optionally, compute P(pass | d = 0) of the exam at 40 + 40 offline (the exact bound over a binomial grid).
