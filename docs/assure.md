# Assurance of the verifier: honest corpus, resweep, scaled differential, mutation slices (s3-assure)

GOAL §16.6 (invariants 19 and 20), §16.7 (invariant 22) and §16.10 (Resweep). Branch `s3-assure`. Nothing here edits
`src/custom_harness/kernel/`; resweep uses only the Kernel's public API.

## 1. The honest corpus (§16.6, invariant 20)

| Piece | Where |
|---|---|
| Format, re-check, sealed access | `src/custom_harness/assure/corpus.py` |
| Look ledger core (receipts, fail-closed `require`) | `src/custom_harness/assure/looks.py` (`scripts/sealed.py` delegates to it) |
| Builder and checker | `scripts/honest_corpus.py` (`--build`, `--check --half dev`, `--check --half sealed --by --reason`) |
| Committed manifest (ids, pairs, halves; dev expected records; sealed commitments only) | `redteam/honest_corpus/manifest.json` |
| Item files, both halves | `custom-harness-data/assure/honest-corpus/v1/{dev,sealed}/<item id>/` (outside the repository) |
| Tests | `tests/spec/test_spec_assure.py`, `tests/spec/test_spec_gate.py::test_validation_sets_split_into_stable_disjoint_halves` |

An item is one claim history with its verdict: `store.zip` (the whole evidence store just after the certificate),
`bundle.zip` (the certificate's Reproducibility Bundle), `ledger.jsonl` (the ledger rows up to the certificate) and
`expected.json` (role honest/fault, label, every check's status, the planted fault, the replayer kind).

Every honest item is paired with a planted-fault counterpart:

- **spec** (clean-room spec worlds, stored predictions): 8 fault kinds, used in turn: peek before registration, a
  develop read of the target, a byte-identical twin of the target, a dropped record, a stray seed, an unregistered
  claim, no real effect, and a development dataset sharing the target's held-out examples. For every spec item the
  clean-room reference model must give the same label and checks as the Kernel, or the build stops (a disagreement
  is a finding, never a corpus item).
- **world** (`tests/kernel_world.py`, trusted programs re-executed in LocalSandbox): peek, an inflated reported score,
  a dropped seed, a weak subject.
- **research** (the scripted research run of `tests/test_research.py`): the loop's own certificate, and the same claim
  re-certified on half its evidence.

The build fails loudly if an honest item is not certified or a fault is not rejected.

**Build state (2026-10-14).** The full v1 build has NOT completed.
- A build on 2026-10-09 got through all 120 spec pairs: every honest item was certified, every fault was rejected,
  and the model agreed with the Kernel on all of them.
- It then stopped at the first world pair: the honest item was REJECTED on K5. That was a recipe bug: the baseline
  was registered with kind `method`. It is fixed in 7695539, but the fix has not been run.
- The research pair has never been built.
- So `redteam/honest_corpus/manifest.json` is NOT committed yet, and the sealed half does not exist on disk.

**Halves.** A half is assigned per PAIR: `looks.half_of("honest-corpus", pair id)`, the same salted rule as the claim
suite and the museum. Item and pair ids are opaque hashes, so an id does not tell honest from fault. Dev items'
expected records are in the committed manifest. Sealed items carry only `commitment` = sha256 of their expected
record, so the sealed labels are never committed. A tampered sealed record is refused on load.

**Looks.** Every read of the sealed half goes through `corpus.sealed_items(look)`. It refuses unless `look` is a
recorded look in an intact ledger (`redteam/sealed_looks.jsonl`) covering every sealed item. Only counts are ledgered
and printed (flips, escapes, uncheckable), never which items.

**Limits, stated plainly.**

- Owner decision 2026-10-08: sealed halves are **accounting only**. Agents can technically read the sealed files under
  `custom-harness-data`. They can also regenerate them, because the builder and its seeds are committed. The look
  ledger records looks made through the code; it cannot detect a direct read.
- A re-check is a re-derivation as of the certificate (`certify(as_of=seq)`). Spec items replay stored predictions,
  so their K3 is a formality. World and research items re-execute trusted repository programs in LocalSandbox, which
  needs test mode (`CHH_ALLOW_UNISOLATED=1`). Without it they are UNCHECKABLE, and a screen over them fails closed.
  Only the corpus's pinned program hashes may run.
- The corpus is generated, not collected: it tests the Kernel against the scenarios we thought of. The spec worlds
  share the differential test's vocabulary.

## 2. Resweep (§16.10)

`src/custom_harness/assure/resweep.py`, command `scripts/resweep.py`.

- **Rule.** A rule is a named set of `KernelConfig` overrides. Empty overrides mean "today's Kernel code". The rule
  hash binds it to the exact Kernel code.
- **Statuses** (first match wins; all matches are listed):

  | Status | Meaning |
  |---|---|
  | INTEGRITY-FAILURE | Broken ledger chain; a certificate that does not match its entry; or a record more favourable than its own ledger prefix under its own rules and the same Kernel version (forged) |
  | UNCHECKABLE | Unknown rule fields, replayer unavailable, infrastructure error, or a crash |
  | STALE-under-new-rule | The new rule's verdict as of the prefix differs from the certificate's own rules' verdict. Also: issued under another Kernel version that today's code no longer reproduces |
  | STALE-new-data | Under its own rules, today's ledger differs |
  | VALID | None of the above |

- **Clawback** is graded and reversible. Grades: none, flag, downgrade, suspend, hold, revoke, plus `no-upgrade`. A
  rejected record that a looser rule would certify is never granted (invariant 21).
  - Clawbacks are entries in a hash-chained ledger, `<store>/resweep/clawbacks.jsonl`. They are never a Kernel
    retraction, which is permanent.
  - A later sweep that grades a certificate `none` writes `restore`.
- **VDS split.** The Kernel's VDS is unchanged.
  - Misses always count.
  - A positive gain is **seasoned** once its certificate has passed an applied sweep under a real rule change and is
    graded none.
  - It is **clawed back** while held, suspended or revoked.
  - Otherwise it is **provisional**.
- **Screen.** `apply` refuses unless the look ledger holds a passing screen for exactly this rule hash. The screen is
  one ledgered look at the honest corpus's sealed half.
  - The declared bound is **zero**: 0 honest flips, 0 planted-fault escapes and 0 uncheckable items. There is no
    tolerance.
  - The screen reports the one-sided exact 95% upper bound on the honest flip rate. With 0 flips in n honest items,
    that bound is 1 - 0.05^(1/n).
  - It also reports how many earlier screens looked at the half, because a rule patched until it passes wears the half
    out (§16.6).
- **Releases.** Every verdict of an applied sweep is a typed release on each generalization target of the certificate
  (`Kernel.record_release`, channel `report`, alphabet `count`).
- **Kernel API.** No new Kernel API was needed. One thing would help (reported, not built): today, a certificate
  issued under another `kernel_version` cannot be told apart from a forgery by re-derivation. Resweep follows the
  Kernel's own rule and calls it STALE, so an old-version forgery is only caught by the chain and hash checks.

## 3. Clean-room differential at scale (§16.6)

`scripts/diffscale.py` is chunked, seeded, resumable, time-budgeted and code-pinned. It uses the same strategy as the
gate test (`gen.scripts()`), one Hypothesis seed per chunk, and counts distinct histories as well as histories run.

Fast store mode (`CHH_SPEC_FAST=1` in `tests/spec/real_kernel.py`): each world starts from a byte copy of one store
in which the ML adapter was approved, with sqlite fsync off. This changes durability only.

**Measured (2026-10-09, code 487e061, fast mode, machine shared with ~15 agents):** 15 chunks of 100, so
**1,500 histories run, 1,194 distinct, 1,664 certifications, 0 disagreements** (state
`custom-harness-data/assure/diffscale-487e061`, logs `diff_slice1..3.log`). Throughput was 0.237 histories/s, about
4.2 s each. At that rate the GOAL's 1e5 histories need about 117 hours on one limiter slot. **1e5 was NOT run.**
Hypothesis repeats some histories across chunk seeds: 306 of the 1,500 were duplicates. Per-history profile: the ML
adapter hash, which re-reads about 230 source files on every certify, is about 0.7 s of the remaining cost (inferred
from one cProfile run).

## 4. Mutation gate re-measured (§16.6)

`scripts/mutate.py` gained:

- `--out` and `--budget-s` slices, with a map pinned to the target code hash;
- a test list derived from the gate's `check.kernel_tests()` plus the hand list. This adds `test_kernel_f1.py` and
  `test_kernel_s16.py`, which the hand list missed: the same bug class as the gate's old hand-kept list;
- **grown targets**, reported apart from the original set: the K4/K6 helpers (registration, family, releases_on,
  registration_level, fixed_terms, fixed, target_level, plan_code_divergence), and `alpha.py` and `releases.py` whole.

**Not measured.** The run on 487e061 (443 mutants: 376 original targets plus 67 grown) never completed. Its coverage
map run was killed twice by session restarts (`mut_map.log` has no exit line), and the owner then stopped long runs.
**There is no mutation score for the current Kernel.** The last measured score (93.4% baseline, then 100% with 7
justified equivalents, on 361 mutants of the 2026-10-07 Kernel) is stale.

Six of the seven ids in `redteam/equivalent.yaml` (every id except vault.py:195) no longer name a mutant, because
the code moved (verified with `mutate.py --list`). They must be re-justified against the new run, not carried over.

## 5. Invariant 22 scaffold

See `docs/human_check.md` (built by the sub-agent `s3-assure__humancheck`). **Invariant 22 stays NOT MET until a human
supplies a check.**
