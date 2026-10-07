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
