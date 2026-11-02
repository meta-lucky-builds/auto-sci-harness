"""I1 (int17 incompatibility, fixed on int18 by stage-a): evolve/multiplicity.check_run / family_size must mirror
kernel/checks.target_level EXACTLY, including alpha-account targets (GOAL §16.4: the level is alpha_T / S_T, fixed at
registration). Before the fix check_run mirrored only the NO-account transcript bound, so a declared campaign on an
account target halted with MultiplicityDrift although K4 had certified every arm at the same fixed level.

Fail-first reasoning (each test, on int17-fix e24421f):
- `test_kernel_level_matches_target_level_*`: `kernel_level` does not exist there (ImportError).
- `test_account_target_with_releases_passes_check_run`: the transcript bound (1 + 2 + ...) exceeds the declared
  n_comparisons once releases are logged, so the old check_run raised MultiplicityDrift on a correctly fixed level.
- `test_account_target_claim_outside_the_account_is_drift`: passes on the old code only by accident (transcript bound);
  kept to pin that the account path still raises when K4's level falls below the slot level.
- `test_family_size_is_zero_on_an_account_target`: the old family_size counted claims (> 0).
- `test_family_size_no_account_is_the_transcript_bound`: the old family_size counted claims (2), not N (1 + 2 = 3).
Built, NOT test-verified (owner rule for this round: no test runs)."""

from __future__ import annotations

import pytest

from custom_harness.evolve.multiplicity import (
    MultiplicityDrift,
    check_run,
    family_size,
    kernel_level,
    last_prereg_seq,
)
from custom_harness.kernel import DatasetRole
from custom_harness.kernel.checks import Context, target_level
from kernel_world import World

FAST = {"eps_max": 0.5, "beta": 0.25, "n_boot": 1000}


def _ctx(w: World, claim) -> Context:
    return Context(claim=claim, claim_hash=claim.claim_hash(), store=w.store, vault=w.kernel.vault,
                   ledger=w.kernel.ledger, config=w.kernel.config, replayer=None, evidence_ids=())


def _both(w: World, claim, target: str) -> tuple[tuple[float, int], tuple[float, int]]:
    level, m, _reasons = target_level(_ctx(w, claim), target)
    mine = kernel_level(w.store, claim.claim_hash(), claim.analysis_plan.model_dump(mode="json"), target)
    return (level, m), (mine[0], mine[1])


@pytest.mark.parametrize("account", [False, True])
def test_kernel_level_matches_target_level_on_real_claims(tmp_path, account):
    """The mirror is exact: same level and multiplicity as the Kernel's own target_level, with and without an
    account, after releases and a second claim."""
    w = World(tmp_path / "w", seed=31, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST)
        if account:
            w.kernel.open_alpha_account("t", 0.064, 64, approver="test-human")
        good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
        c1 = w.claim(good, comp, delta=0.2, targets=("t",), **FAST)
        w.kernel.preregister(c1)
        w.kernel.record_release("t", "evolution_score", "verdict_bit", "evolve.score:verdict")
        c2 = w.claim(good, comp, delta=0.21, targets=("t",), **FAST)
        w.kernel.preregister(c2)
        for c in (c1, c2):
            kernel_side, mirror = _both(w, c, "t")
            assert mirror == kernel_side, (kernel_side, mirror)  # exact: the same arithmetic on the same ledger
    finally:
        w.close()


def test_account_target_with_releases_passes_check_run(tmp_path):
    """A declared campaign on an account target: releases between runs grow the transcript bound but not K4's
    level (paid in slots). check_run must accept (the old no-account mirror raised here)."""
    w = World(tmp_path / "w", seed=32, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST)
        w.kernel.open_alpha_account("t", 0.064, 64, approver="test-human")
        good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
        since = last_prereg_seq(w.store)
        w.kernel.preregister(w.claim(good, comp, delta=0.2, targets=("t",), **FAST))
        for _ in range(3):
            w.kernel.record_release("t", "evolution_score", "verdict_bit", "evolve.score:verdict")
        w.kernel.preregister(w.claim(good, comp, delta=0.21, targets=("t",), **FAST))
        checked = check_run(w.store, since)
        assert len(checked) == 2 and all(c["account"] for c in checked)
        assert all(c["level"] == pytest.approx(0.064 / 64) for c in checked), checked
    finally:
        w.close()


def test_account_target_claim_outside_the_account_is_drift(tmp_path):
    """A claim on the account target without a slot (written around preregister): K4 rejects it on K4_NO_SLOT and,
    for every claim of the family, falls back to Bonferroni over the transcript bound when that is stricter: both
    depend on the order of claims. check_run must raise (here on the slotless claim's account reason)."""
    w = World(tmp_path / "w", seed=33, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST)
        w.kernel.open_alpha_account("t", 0.064, 64, approver="test-human")
        good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
        since = last_prereg_seq(w.store)
        w.kernel.preregister(w.claim(good, comp, delta=0.2, targets=("t",), **FAST))
        w.kernel.ledger.append("prereg", {"claim_hash": "z" * 64, "claim": {"generalization_targets": ["t"]}})
        with pytest.raises(MultiplicityDrift, match="alpha-account"):
            check_run(w.store, since)
    finally:
        w.close()


def test_family_size_is_zero_on_an_account_target(tmp_path):
    w = World(tmp_path / "w", seed=34, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST)
        w.kernel.open_alpha_account("t", 0.064, 64, approver="test-human")
        good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
        w.kernel.preregister(w.claim(good, comp, delta=0.2, targets=("t",), **FAST))
        assert family_size(w.store, "t") == 0
    finally:
        w.close()


def test_family_size_no_account_is_the_transcript_bound(tmp_path):
    """No account: one claim, one verdict-bit release, one claim -> N = 1 + 2 = 3 (the old count gave 2)."""
    w = World(tmp_path / "w", seed=35, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST)
        good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
        w.kernel.preregister(w.claim(good, comp, delta=0.2, targets=("t",), **FAST))
        w.kernel.record_release("t", "evolution_score", "verdict_bit", "evolve.score:verdict")
        w.kernel.preregister(w.claim(good, comp, delta=0.21, targets=("t",), **FAST))
        assert family_size(w.store, "t") == 3
    finally:
        w.close()
