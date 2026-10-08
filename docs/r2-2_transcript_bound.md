# R2-2 after Kernel 1.2: paired evolution arms are certified at different strictness (OPEN; fail closed)

## Decision (orchestrator, owner-delegated, 2026-10-09)

- **The fix:** GOAL §16.4's per-target α-accounts charge releases to an explicit test-set-wear budget. Option 2
  below, with m fixed at registration (option 1, invariant 17). This makes R2-2 hold by construction.
  - It is a Kernel change, assigned to **s3-kernel**.
  - The exponential transcript bound is honest worst-case accounting and is **not** loosened.
- **Until that lands: fail closed (option 4).** `evolve/multiplicity.py::check_run` now mirrors K4's real family
  size (`kernel_family_size`, the transcript bound; merged from x-reviewloop 70d2ac6). It raises `MultiplicityDrift`
  whenever that size exceeds the declared `n_comparisons`.
  - In practice **every evolution campaign halts** at the first run after a verdict-bit release on its target.
    No campaign is running today.

The tests on `int16-fix` that record the open state:

| Test | State |
|---|---|
| `tests/test_evolve_hardening.py::test_run_check_uses_the_kernels_transcript_bound_not_the_claim_count` | plain test, passes: the detector |
| `tests/test_review_loop.py::test_identical_harnesses_score_identically_in_a_paired_comparison` | **strict xfail**, `raises=MultiplicityDrift`, assertions unchanged |
| `tests/test_evolve.py::test_generation_finds_structural_fix_and_blocks_unsafe_edits` | **strict xfail**, `raises=MultiplicityDrift`, assertions unchanged; keeps the acceptance coverage for when the fix lands |
| `tests/test_evolve.py::test_generation_halts_fail_closed_while_releases_outgrow_the_declared_family` | new plain test: the same campaign stops with `MultiplicityDrift` naming the transcript bound, with no decision and no `harness_edit` record |

Other evolution tests the detector halts are listed in the integrator's report, each with its treatment.

Strict means a marker turns into a gate failure as soon as the Kernel fix makes its test pass, which forces the
marker's removal. These markers record a known violation; they do not weaken anything.

Known fidelity gap (not soundness): the certifiability preflight (`research/claims.py` and `research/preflight.py`)
estimates the family as `max(n_comparisons, claims + 1)`, not the transcript bound.
- The preflight is deny-only (invariant 21), so under-estimating m makes it deny less often than K4 would reject.
- It can never cause a certification.
- Not changed.

## Mechanism

R2-2 (`src/custom_harness/evolve/multiplicity.py`) keeps the Kernel's Bonferroni divisor constant during a campaign.
Every claim node declares `n_comparisons` at least as large as any family the campaign can reach. Then
`check_run` verifies after each run that the family is at most the declared size. It was designed when K4 used
`m = max(n_comparisons, number of claims on the target family)`.

Kernel 1.2 (2778d48) changed K4 (`kernel/checks.py::target_level`) to `m = max(n_comparisons, N)`, where N is the
**transcript bound** (`kernel/releases.py::transcript_bound`). In N, each claim's term is multiplied by 2 for every
`verdict_bit` release logged before it. Two release sources fire during every evolution run on an evolve-split
task:

1. `research.certify`'s `replication_brief` is declared `verdict_bit` (`runtime/engine.py`
   `RELEASE_ALPHABET_ALLOWLIST`). Rendering it into the replicator prompt logs one release per rendered field:
   `prompt:replicate:replication_brief.claim`, `.idea` and `.profile`, so 3 bits for one brief.
2. `evolve/evaluate.py::finish_run` releases the run's score verdict bits (`evolution_score`,
   `evolve.score:verdict`, fc24adc).

So N grows exponentially with the number of runs on a target, not linearly. `check_run` still counts claims, so it
passes runs whose certificates K4 actually issued at a far stricter, and **order-dependent**, level.

### Evidence: the review_loop test

The ledger was dumped by the previous integrator in `diag1.txt` (old scratchpad 7f9fcb5e).

| Step | Ledger seq | Effect |
|---|---|---|
| Refused run, its claims | 6 and 20 | terms 1 and 2^3 = 8 (3 brief releases at 16-18 come before 20) |
| Release | 35 | `evolution_score` |
| Candidate claim | 36 | term 2^4 = 16, so **N = 25** (alpha 0.05/25 = 0.002) |
| Release | 50 | `evolution_score` |
| Parent claim | 51 | term 2^5 = 32, so **N = 57** (alpha 0.05/57 = 0.000877) |

The certificates show `25 claim(s) tested on this target -> Bonferroni alpha 0.002` (candidate) and
`57 claim(s) ... 0.0008772` (parent). The declared value is 6. The table's arithmetic was recomputed with
`transcript_bound` in this session and gives exactly 25 and 57.

The same arms evaluated in the other order would swap the levels. Whichever arm runs first is judged more
leniently. In this test both arms still ended REJECTED (K6), so the paired difference happened to be 0. The
assertion that fails is the K4 family-size one.

The failure is not caused by the integration. The same test fails on `b-control` 5137bec alone (`bctl/regress/test_review_loop.txt`,
old scratchpad). That branch contains both fc24adc and 2778d48.

## Why no fix outside `kernel/` makes the test pass

- **Make `check_run` mirror K4 (x-reviewloop 70d2ac6).** This is correct as a detector and fails closed. But N
  exceeds any practical declaration after the first release:
  - the review_loop test stops at the candidate run (declared 6, N 25);
  - `tests/test_evolve.py::test_generation_finds_structural_fix_and_blocks_unsafe_edits` stops with declared 40,
    N 594 (`evolve_fix.txt`, old scratchpad).

  Merged alone, it halts every evolution campaign.
- **Declare n_comparisons at the transcript bound in `check_campaign`.** The bound is about
  2^(releases per run x runs per task). With 4 releases per run and 32 runs that is 2^128, so no claim could
  ever certify.
- **Defer the `evolution_score` releases of a unit until both arms are scored, so both see the same release
  prefix.** This does not equalise N:
  - the brief releases happen inside each run;
  - the parent's family also contains the candidate's claim.

  Only `N <= declared` gives both arms the same m.
- **Count one bit per brief instead of 3.** This is an over-count today, so it errs conservative. Removing it would
  *loosen* the release accounting. Even then, N for the candidate is 1 + 2 + 4 = 7 > 6.
- **Fork the store per arm.** Hiding one arm's claims from the other's family under-counts multiplicity. That is not
  acceptable.

## Options considered (decided above: 1+2 in the Kernel, 4 until then)

1. **Kernel:** K4 uses the transcript bound fixed at registration, or a declared release budget, so m cannot move
   after a claim is registered. The R2-2 declaration would then cover both claims and releases.
2. **Kernel/statistics:** releases on the `evolution_score` channel of an alpha-account target are charged to the
   account's slots, not multiplied into N.
3. **Evolve:** the evolution score drops the verdict bits on the evolve split (dev term only). That removes
   release source 2 and the score's dependence on certification order. The brief releases (source 1) remain, so
   it is still not sufficient alone.
4. **Accept fail-closed:** merge 70d2ac6. Campaigns stop until option 1 or 2 lands.
5. **Randomise the arm order per unit.** The bias becomes zero in expectation but individual comparisons still
   differ. The review_loop test would still fail; this only mitigates.

## Same class, checked

These are evolve- or research-layer mirrors of K4's family size that still count claims:

- `evolve/multiplicity.py::family_size`, used by both `check_campaign` and `check_run`. Open as above.
- `research/claims.py` preflight: `family = max(n_comparisons, claims + 1)`, also in `research/preflight.py`. The
  preflight is a deny-only proxy, so under-estimating m makes it deny less often than it should. It never grants.
  This is a fidelity gap and was not changed (statistics).

`grep -rn "n_comparisons\|family_size" src` outside `kernel/` turns up no other computation of a family size. The
remaining hits declare the value (claim params, `declared_probability`) or pass it through (`cli.py` suite audit).
