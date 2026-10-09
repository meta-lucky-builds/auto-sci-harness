"""R2-2 under Kernel 1.2 (docs/r2-2_transcript_bound.md, docs/alpha_accounts.md): on a target with a per-target
alpha-account, every claim is tested at the FIXED slot level alpha_T / S_T and releases are paid in SLOTS at
registration (the union bound over the transcripts they allow), so the order in which paired arms are registered,
released or certified never changes their levels. Without an account nothing changes (transcript bound in K4)."""

from __future__ import annotations

import json

import pytest

from custom_harness.cli import main
from custom_harness.config import data_dir
from custom_harness.kernel import DatasetRole, KernelError
from custom_harness.kernel import alpha as A
from custom_harness.kernel.reasons import Code
from custom_harness.kernel.vault import Vault
from custom_harness.store import EvidenceStore
from kernel_world import World

FAST = {"eps_max": 0.5, "beta": 0.25, "n_boot": 1000}


def _level(w: World, claim, ids) -> float:
    cert = w.kernel.certify(claim, ids, record=False)
    return float(cert.resolution["targets"]["acct"]["alpha_eff"])


def _paired(tmp_path, order: str, slots: int = 64) -> dict[str, float]:
    """Two arms that behave identically (same programs, only the statement differs), registered in `order` with one
    verdict-bit release (the evolution score of the first run) between them."""
    w = World(tmp_path / order, seed=21, n_train=120, n_test=120)
    try:
        w.register_target("acct", DatasetRole.TEST)
        w.kernel.open_alpha_account("acct", 0.064, slots, approver="test-human")
        good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
        arms = {name: w.claim(good, comp, delta=0.2, targets=("acct",), **FAST).model_copy(
            update={"statement": f"{name}: good vs baseline"}) for name in ("candidate", "parent")}
        ids, levels = {}, {}
        for i, name in enumerate(order.split("-")):
            if i:
                w.kernel.record_release("acct", "evolution_score", "verdict_bit", "evolve.score:verdict")
            w.kernel.preregister(arms[name])
            ids[name] = w.evaluate_all(arms[name], targets=("acct",))
            levels[name] = _level(w, arms[name], ids[name])  # certified as soon as it is evaluated (as evolve does)
        later = {name: _level(w, arms[name], ids[name]) for name in arms}  # and re-derived after everything
        assert later == levels, (later, levels)
        return levels
    finally:
        w.close()


def test_identical_arms_get_identical_fixed_levels_in_either_order(tmp_path):
    first = _paired(tmp_path, "candidate-parent")
    second = _paired(tmp_path, "parent-candidate")
    assert first["candidate"] == first["parent"] == pytest.approx(0.064 / 64), first
    assert first == second


def test_releases_are_paid_in_slots_and_exhaustion_retires_the_target(tmp_path):
    w = World(tmp_path / "w", seed=22, n_train=120, n_test=120)
    try:
        k = w.kernel
        w.register_target("acct", DatasetRole.TEST)
        k.open_alpha_account("acct", 0.06, 3, approver="test-human")
        (acct,) = A.accounts(k.ledger, {"acct"})
        good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
        c1 = w.claim(good, comp, delta=0.2, targets=("acct",), **FAST)
        k.preregister(c1)
        assert A.charged(k.ledger, acct) == 1
        k.record_release("acct", "evolution_score", "verdict_bit", "evolve.score:verdict")
        c2 = w.claim(good, comp, delta=0.21, targets=("acct",), **FAST)
        k.preregister(c2)  # one verdict bit before it: 2 transcripts, 2 slots
        reserve = [e.body for e in k.ledger.entries("alpha_reserve")]
        assert [b["charge"] for b in reserve] == [1, 2] and A.charged(k.ledger, acct) == 3
        assert A.retired(k.ledger, acct) and k.ledger.entries("alpha_retire")
        with pytest.raises(KernelError, match="retired"):  # exhausted: further claims fail closed
            k.preregister(w.claim(good, comp, delta=0.22, targets=("acct",), **FAST))
        # the earlier claim keeps its registered level: the later release and claim did not move it
        ids = w.evaluate_all(c1, targets=("acct",))
        assert _level(w, c1, ids) == pytest.approx(0.02)
    finally:
        w.close()


def test_an_account_that_cannot_pay_the_charge_refuses_the_claim(tmp_path):
    w = World(tmp_path / "w", seed=23, n_train=120, n_test=120)
    try:
        k = w.kernel
        w.register_target("acct", DatasetRole.TEST)
        k.open_alpha_account("acct", 0.06, 3, approver="test-human")
        good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
        k.preregister(w.claim(good, comp, delta=0.2, targets=("acct",), **FAST))
        for _ in range(2):  # 4 transcripts reachable: a 4-slot charge against 2 remaining slots
            k.record_release("acct", "prompt", "verdict_bit", "prompt:replicate:replication_brief.claim")
        with pytest.raises(KernelError, match="cannot pay"):
            k.preregister(w.claim(good, comp, delta=0.21, targets=("acct",), **FAST))
        assert len(k.ledger.entries("prereg")) == 1  # refused before anything was written
    finally:
        w.close()


def test_an_overdrawn_account_fails_k4_closed(tmp_path):
    """Reservations written around preregister (only possible by writing the ledger directly) that exceed the slots."""
    w = World(tmp_path / "w", seed=24, n_train=120, n_test=120)
    try:
        k = w.kernel
        w.register_target("acct", DatasetRole.TEST)
        k.open_alpha_account("acct", 0.06, 2, approver="test-human")
        (acct,) = A.accounts(k.ledger, {"acct"})
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, targets=("acct",), **FAST)
        k.preregister(c)
        k.ledger.append("alpha_reserve", {"account": acct.seq, "dataset_id": "acct", "claim_hash": "x" * 64,
                                          "slot": 2, "level": 0.03, "charge": 8})
        cert = k.certify(c, w.evaluate_all(c, targets=("acct",)), record=False)
        assert Code.K4_ACCOUNT_OVERDRAWN in cert.checks["K4"].codes and cert.checks["K4"].status == "fail"
    finally:
        w.close()


def test_cli_alpha_open_needs_a_human_approver_and_status_reads(capsys):
    s = EvidenceStore(data_dir() / "store")
    try:
        Vault(s).register_dataset("held", DatasetRole.TEST, public={"x.csv": b"x\n1\n"}, private={"y.csv": b"y\n0\n"})
    finally:
        s.close()
    with pytest.raises(SystemExit):  # argparse: --approver is required
        main(["alpha", "open", "held", "--alpha", "0.05", "--slots", "8"])
    with pytest.raises(SystemExit, match=r"NOT done: .*human"):
        main(["alpha", "open", "held", "--alpha", "0.05", "--slots", "8", "--approver", "  "])
    assert main(["alpha", "status", "held"]) == 1  # no account yet
    capsys.readouterr()
    assert main(["alpha", "open", "held", "--alpha", "0.05", "--slots", "8", "--approver", "alice"]) == 0
    assert '"approver": "alice"' in capsys.readouterr().out
    assert main(["alpha", "status", "held"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["slots"] == 8 and row["charged"] == 0 and row["next_claim_charge"] == 1 and not row["retired"]
