# Per-target alpha-accounts with releases paid in slots (R2-2 fix, GOAL §16.4, invariant 17)

Status: built on branch `s3-kernel` (2026-10-09). Kernel core change, NOT a pure tightening (see "Governance").
Problem statement: `docs/r2-2_transcript_bound.md` (origin/int16-fix d2ff98e).

## The rule

A HUMAN opens an account on a held-out target T (`chh alpha open T --alpha a_T --slots S --approver NAME`, or
`Kernel.open_alpha_account`), before the first claim on T or on any target related to it (shared example rows or
byte-identical private labels: the K4 family). Nothing in production code opens an account.

1. **Level, fixed.** Every claim on the family is tested by K4 at

       l = min(alpha / n_comparisons (the claim's own declaration), a_T / S)

   one-sided (the K4 intervals are two-sided at level 2l, so each one-sided bound has level l). `l` does not
   depend on any other claim or release, so it is fixed at registration and the same for every arm (invariant 17).
2. **Releases are paid in slots, at registration.** Let `T_j` be the set of transcripts reachable from the releases
   on the family logged before claim j is registered (`releases.transcript_bound` for that one claim: 2 per
   `verdict_bit`, `count_alphabet` per `count`; a `continuous` release adaptively exposes the target and K6 fails
   every later claim). Claim j is charged `c_j = max(1, |T_j|)` slots, recorded in its `alpha_reserve` entry
   (`charge`). `Kernel.preregister` refuses the claim if the account cannot pay (`sum of charges + c_j > S`); when
   the charges reach S the target retires (`alpha_retire`, code `slots_exhausted`): further claims fail closed.
3. **No account, no change.** Targets without an account keep K4's `alpha / max(n_comparisons, N)` with the
   transcript bound N (Kernel 1.2).
4. **Fail closed.** A family claim without a slot in an account on the family drops the claim to that Bonferroni
   rule (K4_NO_SLOT for the claim itself); an account whose recorded charges exceed S (only possible by writing the
   ledger around `preregister`) fails K4 (`K4_ACCOUNT_OVERDRAWN`).

All release channels are charged, not only the evolution ones (`evolution_score`, `evolve.score:verdict`,
`prompt:replicate:replication_brief.*`): every release on the family reaches an agent-visible surface, so leaving a
channel uncharged would loosen the bound. The Kernel only READS the release log (`kernel/releases.py`, owned by
w2-taint, is unchanged).

## Why it controls the familywise error at a_T

Setting (assumptions, stated):
- (A1) The release log is complete: no target-derived information reaches an agent except as a logged release
  (the same assumption as the transcript bound; it is listed on every data certificate).
- (A2) Given the released transcript, the choice of the j-th claim is a function of the transcript and of
  randomness independent of the held-out sample (agents never read private files: invariant 2).
- (A3) Each fixed claim, tested on the held-out sample, is falsely certified with probability at most its level l
  when its null holds (the validity of the K4/K6 interval procedure for a FIXED hypothesis; K4's BCa example
  interval is asymptotic, so this is approximate, as it was before).

Argument (union bound over transcripts, Dwork et al. 2015, "description length"). Fix the analyst's independent
randomness. Claim j is one of at most |T_j| different claims, one per transcript t in T_j: c_j(t). Each c_j(t) is a
fixed claim, so P(c_j(t) is falsely certified) <= l by (A3), whatever t later turns out to be. Hence

    P(any false certification on the family) <= sum_j sum_{t in T_j} P(c_j(t) falsely certified)
                                             <= sum_j |T_j| * l <= (sum_j c_j) * a_T / S <= a_T,

because preregister never lets the charges exceed S. This is STRONG familywise control (any configuration of true
and null claims), one-sided per claim, at a_T for the family. The level of each claim is l whatever was registered,
released or certified before or after it, so two arms that behave identically get identical levels in either
order (`tests/test_kernel_alpha_slots.py::test_identical_arms_get_identical_fixed_levels_in_either_order`).

### Why the charge is |T_j|, not "one slot per release"

An additive charge (each verdict bit costs one slot) is NOT valid in general. Counterexample: claim 1 has a real
effect and is certified with probability 1/2 depending on the held-out sample; its verdict bit is released; the
analyst then registers null claim A if the bit is 1 and null claim B if it is 0, each crafted so that its false
certification concentrates on the samples that produced that bit. Then P(A certified | bit = 1) can reach
l / P(bit = 1) = 2l, so the second claim's false-certification probability can reach 2l. After b bits it can reach
2^b l. An additive charge of (1 + b) slots under-pays as soon as b >= 2. The multiplicative charge is the price of
the union bound and is tight in this worst case.

Consequence (stated honestly): charges grow exponentially in the number of releases before a claim. A campaign that
releases 4 verdict bits per run on a target pays 16^r slots for the claims of its (r+1)-th run, so an account of
size S supports about log_16 S runs before the target retires; larger S means a smaller per-claim level a_T / S.
The cheap way out is not statistical: release fewer bits from account targets (verdict embargo, or an evolution
score without held-out verdict bits, option 3 of the R2-2 note). That is an owner decision.

Without releases (verdict embargo) every claim pays 1 slot and the rule is plain Bonferroni over S pre-allotted
slots.

## Tests
- `tests/test_kernel_alpha_slots.py`: identical arms in either order (fixed levels); charges [1, 2] and retirement;
  "cannot pay" refusal before anything is written; overdrawn account fails K4; `chh alpha open` needs a non-empty
  `--approver`, `chh alpha status` reads.
- `tests/test_kernel_s16.py::test_release_with_alpha_account_is_paid_in_slots_not_in_level` replaces the old test
  that asserted the order-dependent `alpha 0.01333` (0.02 x k/N).
- `tests/test_review_loop.py::test_identical_harnesses_score_identically_in_a_paired_comparison` now opens an
  account (test stand-in approver) and asserts both arms' K4 level equals a_T / S.
- Museum `redteam/museum/ALPHA-ORDER.patch` re-introduces the order-dependent `level *= k / N`.

## Governance
Not purely tightening: on an account target a claim's level no longer shrinks when later claims or releases arrive
(the old `k / N` factor); the release cost moved into refusals at registration. Recorded in
`redteam/kernel_core_changes.yaml` as an owner-delegated deviation from the 7-day cooling-off (orchestrator
decision, owner delegation 2026-10-08).
