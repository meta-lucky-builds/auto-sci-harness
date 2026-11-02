"""s4-station HIGH finding, fixed on int18 by stage-a (research/station/archive.py): the Kernel-ledger WINDOW of a
turn never attributes anything by itself.
  - a certificate whose evidence names no run (exact LP / circuit evidence) belongs to a turn only through a run
    that names it: the `run_id` on its prereg / certificate ledger entry (s4-kernel) or a trusted MODULE step of one
    of the turn's runs reporting `{...certificate: {"hash": h}}`;
  - only pre-registrations whose ledger `run_id` is one of the turn's runs count (stall signal, open claims);
  - only private reads by the turn's runs count as exposure (plus reads naming no run, unless they are the
    Kernel's own purpose-"kernel" re-reads: an unattributable read over-taints, the safe side).
Unit tests on stubs (no Kernel world): each FAILS on s4-station 503fd62, where `attributable` returned
`named <= set(w.runs)` (True for an empty set) and open_preregs / private_reads / the prereg count took every entry
in the window. Built, NOT test-verified (owner rule: no test runs this round)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from custom_harness.kernel.ledger import Entry
from custom_harness.records import StepKind
from custom_harness.research.station.archive import StationArchive, TurnWindow

H = "c" * 64
CLAIM = "d" * 64


def _e(seq: int, kind: str, body: dict[str, Any]) -> Entry:
    return Entry(seq=seq, kind=kind, body=body, prev="", hash="", at=0.0)


class _Ledger:
    def __init__(self, entries: list[Entry]):
        self._e = entries

    def entries(self, kind: str | None = None) -> list[Entry]:
        return [e for e in self._e if kind is None or e.kind == kind]


def _archive(entries: list[Entry], traces: dict[str, list[Any]] | None = None) -> StationArchive:
    a = StationArchive.__new__(StationArchive)  # no MemoryStore needed for attribution
    store = SimpleNamespace(trace=lambda run_id: (traces or {}).get(run_id, []),
                            get_evidence=lambda ev_id: (_ for _ in ()).throw(KeyError(ev_id)))
    a.kernel = SimpleNamespace(ledger=_Ledger(entries), store=store)  # type: ignore[assignment]
    a.mode_id = "m"
    return a


def _cert() -> Any:
    return SimpleNamespace(prereg_seq=2, evidence_ids=[], claim_hash=CLAIM, claim={"generalization_targets": []})


W = TurnWindow("m", "a1", 0, "p", seq_before=1, seq_after=10, runs=frozenset({"run-1"}))


def test_runless_certificate_is_not_attributed_by_the_window_alone():
    a = _archive([_e(2, "prereg", {"claim_hash": CLAIM}), _e(3, "certificate", {"cert_hash": H})])
    assert a.attributable(_cert(), H, W) is False


def test_runless_certificate_is_attributed_through_the_ledger_run_id():
    a = _archive([_e(2, "prereg", {"claim_hash": CLAIM, "run_id": "run-1"}),
                  _e(3, "certificate", {"cert_hash": H, "run_id": "run-1"})])
    assert a.attributable(_cert(), H, W) is True
    other = _archive([_e(2, "prereg", {"claim_hash": CLAIM, "run_id": "run-9"})])  # another agent's run
    assert other.attributable(_cert(), H, W) is False


def test_runless_certificate_is_attributed_through_a_module_step_of_the_turn():
    module = SimpleNamespace(kind=StepKind.MODULE, output={"certificate": {"hash": H, "label": "CERTIFIED-EXACT"}})
    llm = SimpleNamespace(kind=StepKind.LLM, output={"certificate": {"hash": H}})  # agent text proves nothing
    entries = [_e(2, "prereg", {"claim_hash": CLAIM}), _e(3, "certificate", {"cert_hash": H})]
    assert _archive(entries, {"run-1": [module]}).attributable(_cert(), H, W) is True
    assert _archive(entries, {"run-1": [llm]}).attributable(_cert(), H, W) is False
    assert _archive(entries, {"run-9": [module]}).attributable(_cert(), H, W) is False  # not a turn run


def test_only_the_turns_preregistrations_count():
    a = _archive([_e(2, "prereg", {"claim_hash": "1" * 64, "run_id": "run-1"}),
                  _e(3, "prereg", {"claim_hash": "2" * 64, "run_id": "run-9"}),
                  _e(4, "prereg", {"claim_hash": "3" * 64})])
    assert [e.body["claim_hash"] for e in a.turn_preregs(W)] == ["1" * 64]
    assert a.open_preregs(W, []) == ["1" * 64]


def test_only_the_turns_private_reads_count_and_unknown_reads_over_taint():
    a = _archive([_e(2, "data_access", {"dataset_id": "t-own", "private": True, "run_id": "run-1"}),
                  _e(3, "data_access", {"dataset_id": "t-other", "private": True, "run_id": "run-9"}),
                  _e(4, "data_access", {"dataset_id": "t-kernel", "private": True, "purpose": "kernel"}),
                  _e(5, "data_access", {"dataset_id": "t-unknown", "private": True, "purpose": "evaluate"}),
                  _e(6, "data_access", {"dataset_id": "t-public", "private": False, "run_id": "run-1"})])
    assert a.turn_exposure(W) == {"t-own", "t-unknown"}


def test_the_station_progress_signal_is_a_budget_token_and_the_tree_is_clean():
    """int18 merge finding (emergence static check, GOAL §16.10): s4-station's `novel` progress flag was not
    allowlisted, so tests/test_emergence.py's real-tree check FAILED on int18. It steers budget only (stall lanes,
    cap shrink), so the archive is a BUDGET_PATHS module with a pinned "budget" entry, and the whole tree is clean."""
    from pathlib import Path

    from custom_harness.emergence import ALLOWLIST, BUDGET_PATHS, check_novelty_isolation
    assert ALLOWLIST[("research/station/archive.py", "novel")][0] == "budget"
    assert "research/station/archive.py" in BUDGET_PATHS
    assert check_novelty_isolation(Path(__file__).resolve().parents[1] / "src") == []
