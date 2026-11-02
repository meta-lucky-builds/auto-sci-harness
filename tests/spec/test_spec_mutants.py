"""Tests written to kill mutants that survived the mutation baseline (scripts/mutate.py, GOAL §16.6)."""

from __future__ import annotations

from custom_harness.kernel.ledger import Ledger
from custom_harness.store import EvidenceStore


def test_ledger_verify_detects_a_deleted_middle_entry(tmp_path):
    """Survivor ledger.py `Ledger.verify` full=True -> False: no test deleted an entry from a LIVE ledger. Every
    remaining entry still hashes correctly and seq still increases, so only the unbroken-chain check catches it."""
    store = EvidenceStore(tmp_path / "s")
    try:
        led = Ledger(store)
        for i in range(3):
            led.append("retraction", {"cert_hash": f"c{i}", "reason": "r"})
        assert led.verify() == []
        store._db.execute("DELETE FROM ledger WHERE seq = 2")
        store._db.commit()
        assert [e.seq for e in led.entries()] == [1, 3]  # the deletion really happened
        assert all(e.intact() for e in led.entries())  # and left every remaining entry self-consistent
        assert any("chain broken" in p for p in led.verify()), led.verify()
    finally:
        store.close()


def _three_entries_with_a_hole(tmp_path):
    store = EvidenceStore(tmp_path / "s")
    led = Ledger(store)
    for i in range(3):
        led.append("retraction", {"cert_hash": f"c{i}", "reason": "r"})
    store._db.execute("DELETE FROM ledger WHERE seq = 2")
    store._db.commit()
    return store, led


def test_write_anchor_refuses_a_chain_with_a_deleted_entry(tmp_path):
    """Survivor ledger.py write_anchor full=True -> False: anchoring a chain with a hole would bless the deletion."""
    import pytest

    from custom_harness.kernel.ledger import AnchorError, write_anchor
    store, led = _three_entries_with_a_hole(tmp_path)
    try:
        with pytest.raises(AnchorError, match="broken chain"):
            write_anchor(led, tmp_path / "outside" / "anchor.jsonl", tmp_path / "s")
        assert not (tmp_path / "outside" / "anchor.jsonl").exists()
    finally:
        store.close()


def test_write_anchor_creates_missing_parent_directories(tmp_path):
    """Survivor ledger.py write_anchor mkdir(parents=True -> False): an anchor kept on another disk or a fresh
    folder tree must still be writable."""
    from custom_harness.kernel.ledger import check_anchors, write_anchor
    store = EvidenceStore(tmp_path / "s")
    try:
        led = Ledger(store)
        led.append("retraction", {"cert_hash": "c", "reason": "r"})
        path = tmp_path / "far" / "away" / "anchor.jsonl"
        write_anchor(led, path, tmp_path / "s")
        assert path.exists() and check_anchors(led.entries(), path) == []
    finally:
        store.close()


def test_an_unreadable_anchor_line_is_a_problem_not_a_crash(tmp_path):
    """Survivor ledger.py check_anchors `continue` -> pass: after an unreadable line the check went on with
    undefined (or the previous line's) values."""
    from custom_harness.kernel.ledger import check_anchors
    store = EvidenceStore(tmp_path / "s")
    try:
        led = Ledger(store)
        led.append("retraction", {"cert_hash": "c", "reason": "r"})
        path = tmp_path / "anchor.jsonl"
        path.write_text("not json at all\n", encoding="utf-8")
        problems = check_anchors(led.entries(), path)
        assert len(problems) == 1 and problems[0].startswith("anchor line 1 unreadable"), problems  # locates it
    finally:
        store.close()


def _iv(lo: float, hi: float, degenerate: bool = False):
    from custom_harness.verify.stats import Interval
    return Interval(estimate=(lo + hi) / 2, lo=lo, hi=hi, n=10, alpha=0.05, method="t", degenerate=degenerate)


def _claim(kind):
    from custom_harness.kernel import AnalysisPlan, ArtifactRef, Claim, Effect, MetricRef, Scope
    a, b = ArtifactRef(artifact_id="a", hash="1" * 64), ArtifactRef(artifact_id="b", hash="2" * 64)
    return Claim(kind=kind, statement="s", subject=a, comparator=b, metric=MetricRef(id="accuracy",
                 higher_is_better=True), effect=Effect(min_effect=0.1, predicted_lo=0.0, predicted_hi=0.0,
                 probability=0.5), scope=Scope(datasets=("t",)), development=(), generalization_targets=("t",),
                 analysis_plan=AnalysisPlan(seeds=(0, 1, 2)), resolve_by=1.0)


def test_equivalence_criterion_boundaries_and_seed_use():
    """Survivors in checks._criterion (equivalence branch, line 453): no test exercised its strict bounds, its seed
    interval or its and/or structure."""
    from custom_harness.kernel import ClaimKind
    from custom_harness.kernel.checks import _criterion
    eq = _claim(ClaimKind.EQUIVALENCE)
    inside, flat = _iv(-0.05, 0.05), _iv(0, 0, degenerate=True)
    assert _criterion(eq, flat, inside) is True  # seed interval degenerate: not used
    assert _criterion(eq, inside, inside) is True
    assert _criterion(eq, flat, _iv(-0.1, 0.05)) is False  # lo == -d is not inside (strict)
    assert _criterion(eq, flat, _iv(-0.05, 0.1)) is False  # hi == d is not inside (strict)
    assert _criterion(eq, flat, _iv(-0.2, 0.05)) is False  # one side out is enough to fail
    assert _criterion(eq, flat, _iv(-0.05, 0.2)) is False
    assert _criterion(eq, _iv(-0.1, 0.05), inside) is False  # a non-degenerate seed interval must also fit
    assert _criterion(eq, _iv(-0.05, 0.1), inside) is False
    assert _criterion(eq, _iv(-0.2, 0.05), inside) is False
    assert _criterion(eq, _iv(-0.05, 0.2), inside) is False


def test_unsupported_kinds_never_meet_a_criterion():
    """Survivor checks.py:452 if-True: a kind the Kernel cannot check must never meet the equivalence formula."""
    from custom_harness.kernel import ClaimKind
    from custom_harness.kernel.checks import _criterion
    for kind in (ClaimKind.SCALING_LAW, ClaimKind.REDISCOVERY):
        assert _criterion(_claim(kind), _iv(0, 0, degenerate=True), _iv(-0.05, 0.05)) is False


def test_shared_examples_flags_label_columns_even_when_the_first_row_is_ragged(tmp_path):
    """Survivors checks.py:131 (data[0] -> data[1], t[0] -> t[1]): 'may carry labels' is a property of the
    HEADER, not of the first data row."""
    from custom_harness.kernel import DatasetRole, Kernel
    from custom_harness.kernel.checks import shared_examples
    store = EvidenceStore(tmp_path / "s")
    try:
        v = Kernel(store).vault
        v.register_dataset("t", DatasetRole.TEST, public={"x.csv": b"x1,x2\n1,2,3\n1,2\n"},
                           private={"y.csv": b"y\n0\n1\n"})
        v.register_dataset("o", DatasetRole.DEV, public={"tr.csv": b"x1,x2,label\n9\n1,2,0\n"}, private={})
        assert shared_examples(v, "t", "o") == {"o/tr.csv": (1, True)}
    finally:
        store.close()


def test_k6_without_statistics_fails_k6_itself(tmp_path):
    """Survivor checks.py:522 (reason dropped): with a duplicated record K4 computes no statistics; K6 must fail on
    its own account, not only because K4 also fails."""
    import gen
    from history import Certify, Prereg, Record
    from test_spec_kernel import ARTS, T, claim, play
    c = claim()
    recs = gen.honest_records(c) + (Record("subject", 0, "t"),)
    [v] = play([T, *ARTS, Prereg(c), *gen.eval_reads(c), Certify(c, recs)])
    assert v.checks["K4"] == "fail" and v.checks["K6"] == "fail", v
