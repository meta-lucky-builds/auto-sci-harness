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
