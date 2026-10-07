"""`chh lit-search`, `chh cite-check`, `chh lit-freeze`: offline against a frozen snapshot; the network only with an
explicit --live (never exercised in tests)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from custom_harness.cli import main
from custom_harness.config import data_dir
from custom_harness.literature.client import LiteratureClient
from custom_harness.literature.retrieval import RetrievalLog
from custom_harness.literature.snapshot import RecordingTransport, SnapshotStore
from lit_world import world


def _record(as_of=date(2026, 1, 1)) -> str:
    store = SnapshotStore(data_dir() / "literature", clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    c = LiteratureClient(RecordingTransport(world(), store), sleep=lambda s: None, max_attempts=2)
    for i in ("10.5555/lit.2021.001", "10.5555/lit.2019.042", "2403.01234", "2501.00077"):
        c.lookup(i)
    log = RetrievalLog(store, "s1")
    log.record_search(c.search("sparse gradient routing tabular", limit=5))
    log.record_search(c.search("calibrated early stopping", limit=5, sources=["arxiv"]))
    return store.freeze(as_of)


def _cites(tmp_path, items):
    p = tmp_path / "cites.json"
    p.write_text(json.dumps(items), encoding="utf-8")
    return str(p)


GOOD = {"key": "a", "id": "10.5555/lit.2021.001", "title": "Sparse Gradient Routing for Tabular Ensembles",
        "year": 2021, "quotes": ["the method improves accuracy by 1.8 points over gradient boosting"]}


def test_cite_check_offline_passes_and_fails(tmp_path, capsys):
    snap = _record()
    assert main(["cite-check", _cites(tmp_path, [GOOD]), "--snapshot", snap, "--session", "s1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["I3"] is True and out["snapshot"] == snap and out["as_of"] == "2026-01-01"
    assert out["session"] == "s1"
    assert main(["cite-check", _cites(tmp_path, [GOOD, {**GOOD, "key": "b", "year": 2020}]),
                 "--snapshot", snap, "--session", "s1"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["I3"] is False and out["counts"]["failed"] == 1


def test_cite_check_as_of_earlier_date_hides_future_papers(tmp_path, capsys):
    snap = _record()
    future = {"key": "f", "id": "2501.00077", "title": "Routing Tokens Through Learned Sparse Experts at Scale",
              "year": 2025}
    assert main(["cite-check", _cites(tmp_path, [future]), "--snapshot", snap, "--session", "s1"]) == 0
    capsys.readouterr()
    assert main(["cite-check", _cites(tmp_path, [future]), "--snapshot", snap, "--session", "s1",
                 "--as-of", "2024-12-31"]) == 1
    assert json.loads(capsys.readouterr().out)["counts"]["failed"] == 1


def test_cite_check_without_a_session_verifies_nothing(tmp_path, capsys):
    snap = _record()
    assert main(["cite-check", _cites(tmp_path, [GOOD]), "--snapshot", snap]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["unverified"] == 1 and out["session"] is None
    assert main(["cite-check", _cites(tmp_path, [GOOD]), "--snapshot", snap, "--session", "other"]) == 1


def test_lit_search_logs_into_a_session_with_a_fixed_window(capsys):
    snap = _record()
    assert main(["lit-search", "sparse gradient routing tabular", "--limit", "5", "--snapshot", snap,
                 "--session", "s2", "--published-from", "2022-01-01"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [p["doi"] for p in out["records"]] == ["10.5555/lit.2025.009"] and out["window"] == ["2022-01-01",
                                                                                                "2026-01-01"]
    store = SnapshotStore(data_dir() / "literature")
    assert RetrievalLog(store, "s2").retrieved_ids() == {"doi:10.5555/lit.2025.009"}
    with pytest.raises(SystemExit, match="never widen"):
        main(["lit-search", "x", "--snapshot", snap, "--published-to", "2027-01-01"])


def test_cite_check_refuses_a_later_as_of_than_the_snapshot(tmp_path):
    snap = _record(as_of=date(2024, 12, 31))
    with pytest.raises(SystemExit, match="later than the frozen snapshot"):
        main(["cite-check", _cites(tmp_path, [GOOD]), "--snapshot", snap, "--as-of", "2025-06-01"])


def test_no_network_without_live_flag(tmp_path):
    with pytest.raises(SystemExit, match="no network access without --live"):
        main(["cite-check", _cites(tmp_path, [GOOD])])
    with pytest.raises(SystemExit, match="no network access without --live"):
        main(["lit-search", "sparse routing"])


def test_malformed_citation_file_is_refused(tmp_path):
    snap = _record()
    with pytest.raises(SystemExit, match="bad citation file"):
        main(["cite-check", _cites(tmp_path, [{"key": "a", "doi": "x"}]), "--snapshot", snap])
    with pytest.raises(SystemExit, match="bad citation file"):
        main(["cite-check", _cites(tmp_path, {"key": "a"}), "--snapshot", snap])


def test_lit_search_offline(capsys):
    snap = _record(as_of=date(2024, 12, 31))
    assert main(["lit-search", "sparse gradient routing tabular", "--limit", "5", "--snapshot", snap]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [p["doi"] for p in out["records"]] == ["10.5555/lit.2021.001"] and out["excluded_after_as_of"] == 1
    assert main(["lit-search", "never recorded query", "--snapshot", snap]) == 1   # incomplete: nonzero exit
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_lit_freeze_prints_a_reusable_snapshot_id(capsys):
    _record()
    assert main(["lit-freeze", "--as-of", "2024-12-31", "--label", "t0"]) == 0
    snap = capsys.readouterr().out.strip().splitlines()[-1]
    assert SnapshotStore(data_dir() / "literature").manifest(snap)["as_of"] == "2024-12-31"
