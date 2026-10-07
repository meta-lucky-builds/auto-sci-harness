"""Retrieval provenance and the retrieval loop (GOAL §5 L4.3, coordinator scope addition 2026-10-07).

- Every search result set is logged content-addressed per session; a citation is verified only if its id appeared
  in a logged SEARCH result of the same session (lookups never count: looking up a model-supplied id would
  launder it). The log is re-derived from the stored envelopes when read, so a forged or edited entry is caught.
- A RetrievalLoop has ONE publication window, fixed at construction and inherited by every follow-up (never
  widened; under a time split it ends at T), a hard cap on rounds, and dedup by source id -> DOI/arXiv/PMID ->
  normalised title."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from custom_harness.literature.cite import Citation, check_citation
from custom_harness.literature.client import Deduper, LiteratureClient
from custom_harness.literature.http import LiteratureError
from custom_harness.literature.records import PaperRecord
from custom_harness.literature.retrieval import RetrievalLog, RetrievalLoop, RoundCapExceeded
from custom_harness.literature.snapshot import OfflineTransport, RecordingTransport, SnapshotStore
from lit_world import world

Q = "sparse gradient routing tabular"
A = Citation(key="a", id="10.5555/lit.2021.001", title="Sparse Gradient Routing for Tabular Ensembles", year=2021,
             quotes=["the method improves accuracy by 1.8 points over gradient boosting"])
D = Citation(key="d", id="10.5555/lit.2019.042", title="Kernel Density Baselines for Anomaly Scoring", year=2019)


def _store(tmp_path):
    return SnapshotStore(tmp_path / "lit", clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))


def _client(store, **kw):
    return LiteratureClient(RecordingTransport(world(), store), sleep=lambda s: None, max_attempts=2, **kw)


# ------------------------------------------------------------------ provenance
def test_cited_id_must_appear_in_a_logged_search_of_the_session(tmp_path):
    store = _store(tmp_path)
    c = _client(store)
    log = RetrievalLog(store, "run_1")
    RetrievalLoop(c, log, max_rounds=3, limit=5).search(Q)
    ids = log.retrieved_ids()
    assert "doi:10.5555/lit.2021.001" in ids
    assert check_citation(A, c, retrieved_ids=ids).status == "verified"
    # D resolves and matches, but no search of this session ever returned it
    res = check_citation(D, c, retrieved_ids=ids)
    assert res.status == "unverified" and res.retrieval["status"] == "not_retrieved"


def test_without_a_retrieval_record_nothing_is_verified(tmp_path):
    res = check_citation(A, _client(_store(tmp_path)))
    assert res.status == "unverified" and res.retrieval["status"] == "unchecked"


def test_lookups_and_the_check_itself_never_count_as_retrieval(tmp_path):
    store = _store(tmp_path)
    c = _client(store)
    log = RetrievalLog(store, "run_1")
    c.lookup("10.5555/lit.2019.042")
    check_citation(D, c, retrieved_ids=log.retrieved_ids())
    assert log.retrieved_ids() == frozenset()


def test_other_sessions_do_not_count(tmp_path):
    store = _store(tmp_path)
    c = _client(store)
    RetrievalLoop(c, RetrievalLog(store, "run_1"), max_rounds=1, limit=5).search(Q)
    assert RetrievalLog(store, "run_2").retrieved_ids() == frozenset()


def test_log_survives_offline_replay(tmp_path):
    store = _store(tmp_path)
    c = _client(store)
    RetrievalLoop(c, RetrievalLog(store, "run_1"), max_rounds=1, limit=5).search(Q)
    c.lookup(A.id)                                  # the check's own lookup must be in the snapshot too
    snap = store.freeze(date(2026, 1, 1))
    off = LiteratureClient(OfflineTransport(store, snap))
    assert check_citation(A, off, retrieved_ids=RetrievalLog(store, "run_1").retrieved_ids()).status == "verified"


def test_a_forged_log_entry_is_detected(tmp_path):
    store = _store(tmp_path)
    log = RetrievalLog(store, "run_1")
    RetrievalLoop(_client(store), log, max_rounds=1, limit=5).search(Q)
    digest = json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])["digest"]
    entry = store.blobs.get_json(digest)
    forged = store.blobs.put_json({**entry, "ids": [*entry["ids"], "doi:10.5555/lit.2019.042"]})
    with open(log.path, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps({"digest": forged}) + "\n")
    with pytest.raises(LiteratureError, match="integrity"):
        log.retrieved_ids()


def test_a_lookup_response_passed_off_as_a_search_is_detected(tmp_path):
    store = _store(tmp_path)
    c = _client(store)
    res = c.lookup("10.5555/lit.2019.042")
    log = RetrievalLog(store, "run_1")
    fake = store.blobs.put_json({"v": 1, "session": "run_1", "kind": "search", "source": "crossref", "query": "x",
                                 "envelope": res.record.envelope, "window": [None, None],
                                 "ids": ["doi:10.5555/lit.2019.042"]})
    log.path.parent.mkdir(parents=True, exist_ok=True)
    with open(log.path, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps({"digest": fake}) + "\n")
    with pytest.raises(LiteratureError, match="integrity"):
        log.retrieved_ids()


def test_an_entry_from_another_session_is_detected(tmp_path):
    store = _store(tmp_path)
    RetrievalLoop(_client(store), RetrievalLog(store, "run_1"), max_rounds=1, limit=5).search(Q)
    other = RetrievalLog(store, "run_2")
    other.path.write_text(RetrievalLog(store, "run_1").path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(LiteratureError, match="integrity"):
        other.retrieved_ids()


def test_results_without_a_stored_envelope_cannot_be_logged(tmp_path):
    c = LiteratureClient(world(), sleep=lambda s: None)          # no snapshot store behind it
    with pytest.raises(LiteratureError, match="stored"):
        RetrievalLoop(c, RetrievalLog(_store(tmp_path), "run_1"), max_rounds=1, limit=5).search(Q)


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "x" * 65, "rаn"])
def test_session_ids_are_restricted(tmp_path, bad):
    with pytest.raises(ValueError):
        RetrievalLog(_store(tmp_path), bad)


# ------------------------------------------------------------------ the loop: one fixed window
def test_window_ends_at_the_time_split_cutoff_and_is_never_widened(tmp_path):
    store = _store(tmp_path)
    c = _client(store, as_of=date(2024, 12, 31))
    loop = RetrievalLoop(c, None, max_rounds=3, limit=5)
    assert loop.window.end == date(2024, 12, 31)
    with pytest.raises(ValueError):
        RetrievalLoop(c, None, max_rounds=3, limit=5, published_to=date(2025, 6, 1))
    with pytest.raises(ValueError):
        c.search(Q, limit=5, published_to=date(2025, 6, 1))
    with pytest.raises(AttributeError):
        loop.window = None  # type: ignore[misc]
    with pytest.raises(AttributeError):
        loop.window.end = date(2030, 1, 1)  # type: ignore[misc]
    with pytest.raises(TypeError):
        loop.search(Q, published_to=date(2030, 1, 1))  # type: ignore[call-arg]


def test_every_follow_up_inherits_the_window(tmp_path):
    store = _store(tmp_path)
    log = RetrievalLog(store, "run_1")
    loop = RetrievalLoop(_client(store), log, max_rounds=3, limit=5, published_from=date(2022, 1, 1),
                         published_to=date(2025, 12, 31), sources=["openalex", "arxiv"])
    first = loop.search(Q)                         # A (2021) is before the window
    assert [p.doi for p in first] == ["10.5555/lit.2025.009"]
    follow = loop.follow_up("calibrated early stopping")   # arXiv: B (2024) in, C (2025) in
    assert sorted(p.arxiv_id for p in follow) == ["2403.01234", "2501.00077"]
    assert {e["window"] for e in log.entries()} == {("2022-01-01", "2025-12-31")}
    assert "doi:10.5555/lit.2021.001" not in log.retrieved_ids()


def test_window_must_be_well_formed(tmp_path):
    with pytest.raises(ValueError):
        RetrievalLoop(_client(_store(tmp_path)), None, max_rounds=1, published_from=date(2025, 1, 1),
                      published_to=date(2024, 1, 1))
    with pytest.raises(ValueError):
        RetrievalLoop(_client(_store(tmp_path)), None, max_rounds=0)


def test_round_cap_is_hard_and_failed_rounds_count(tmp_path):
    store = _store(tmp_path)
    loop = RetrievalLoop(_client(store), None, max_rounds=2, limit=5)
    assert loop.search("zz unknown zz") == [] and loop.complete is False    # every source failed: still a round
    loop.follow_up(Q)
    assert loop.rounds == 2
    with pytest.raises(RoundCapExceeded):
        loop.follow_up(Q)
    assert loop.rounds == 2


def test_loop_dedups_across_rounds(tmp_path):
    store = _store(tmp_path)
    loop = RetrievalLoop(_client(store), None, max_rounds=3, limit=5)
    assert len(loop.search(Q)) == 2
    assert loop.follow_up(Q) == []
    assert len(loop.records) == 2


def _rec(**kw):
    return PaperRecord(**{"source": "s", "source_id": "1", "title": "T", **kw})


def test_dedup_order_source_id_then_ids_then_title():
    d = Deduper()
    assert d.add(_rec(source_id="W1", doi="10.5555/x", title="Sparse Routing"))
    assert not d.add(_rec(source_id="W1", title="Something else"))                    # same source id
    assert not d.add(_rec(source="arxiv", source_id="2401.1", doi="10.5555/X", title="Other"))  # same DOI
    assert not d.add(_rec(source="o", source_id="9", title="sparse  routing."))        # same normalised title
    assert d.add(_rec(source="o", source_id="10", pmid="123", title="A different paper"))
    assert not d.add(_rec(source="p", source_id="11", pmid="123", title="Renamed"))      # same PMID
