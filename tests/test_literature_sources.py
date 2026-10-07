"""Literature sources (arXiv, Semantic Scholar, OpenAlex, Crossref) and the client: parsing to each API's
documented schema, ID resolution with authority-based 'does not resolve', fallback, query splitting, as-of-T
filtering, and exact offline replay from a frozen snapshot. Offline: fixtures only (tests/lit_world.py)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from custom_harness.literature.client import LiteratureClient, split_query
from custom_harness.literature.http import LiteratureError
from custom_harness.literature.snapshot import OfflineTransport, RecordingTransport, SnapshotStore
from custom_harness.literature.sources import (
    S2_FIELDS,
    ArxivSource,
    CrossrefSource,
    OpenAlexSource,
    SemanticScholarSource,
)
from lit_world import AX, CR, HDL, OA, S2, fx, r, world


def nosleep(_s: float) -> None:
    pass


def client(tr=None, **kw) -> LiteratureClient:
    return LiteratureClient(tr if tr is not None else world(), sleep=nosleep, max_attempts=2, **kw)


# ------------------------------------------------------------------ parsing (documented schemas)
def test_crossref_work_parses():
    rec = CrossrefSource().parse_lookup(r(200, fx("crossref_work_a.json")))
    assert rec.doi == "10.5555/lit.2021.001"
    assert rec.title == "Sparse Gradient Routing for Tabular Ensembles"   # JATS/HTML markup removed
    assert rec.authors == ("Mira Okafor", "Tomas Lindqvist", "Wen Zhao")
    assert rec.published == "2021-03-15" and set(rec.years) == {2021}
    assert rec.abstract.startswith("Abstract We study sparse gradient routing")
    assert "−0.5" in rec.abstract


def test_crossref_month_precision_dates():
    rec = CrossrefSource().parse_lookup(r(200, fx("crossref_work_d.json")))
    assert rec.published == "2019-09" and rec.years == (2019,) and rec.abstract is None


def test_openalex_work_parses_and_rebuilds_the_inverted_abstract():
    rec = OpenAlexSource().parse_lookup(r(200, fx("openalex_work_a.json")))
    assert rec.doi == "10.5555/lit.2021.001" and rec.source_id == "https://openalex.org/W4300000001"
    assert rec.authors[0] == "Mira Okafor" and rec.published == "2021-03-15"
    assert rec.abstract == ("We study sparse gradient routing for tabular ensembles. On twelve benchmark datasets "
                            "the method improves accuracy by 1.8 points over gradient boosting.")


def test_s2_paper_parses():
    rec = SemanticScholarSource().parse_lookup(r(200, fx("s2_paper_b.json")))
    assert rec.arxiv_id == "2403.01234" and rec.doi is None
    assert rec.authors == ("Lena Hartmann", "Kofi Mensah") and rec.years == (2024,)


def test_arxiv_entry_parses_with_version_and_update_date():
    rec = ArxivSource().parse_lookup(r(200, fx("arxiv_entry_b.xml")))
    assert rec.arxiv_id == "2403.01234" and rec.source_id == "2403.01234v2"
    assert rec.title == "Calibrated Early Stopping under Distribution Shift"
    assert rec.published == "2024-03-02" and rec.updated == "2024-06-10"
    assert rec.abstract.startswith("We propose a calibrated early stopping rule")
    c = ArxivSource().parse_lookup(r(200, fx("arxiv_entry_c.xml")))
    assert c.doi == "10.5555/lit.2025.077"


def test_arxiv_empty_feed_and_error_entry_mean_not_found():
    assert ArxivSource().parse_lookup(r(200, fx("arxiv_empty.xml"))) is None
    assert ArxivSource().parse_lookup(r(200, fx("arxiv_error.xml"))) is None


def test_xml_with_entity_declarations_is_refused():
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&lol2;</title></entry></feed>')
    with pytest.raises(LiteratureError) as e:
        ArxivSource().parse_lookup(r(200, bomb))
    assert e.value.infra and not e.value.transient


@pytest.mark.parametrize("src,body", [(CrossrefSource(), "{not json"), (CrossrefSource(), '{"message": []}'),
                                      (OpenAlexSource(), "[]"), (SemanticScholarSource(), '{"title": 3}'),
                                      (ArxivSource(), "<feed")])
def test_unexpected_shapes_are_infra_errors(src, body):
    with pytest.raises(LiteratureError) as e:
        src.parse_lookup(r(200, body))
    assert e.value.infra and not e.value.transient


# ------------------------------------------------------------------ lookup: three-valued, fail closed
def test_doi_resolves_via_crossref():
    res = client().lookup("https://doi.org/10.5555/LIT.2021.001")
    assert res.status == "resolves" and res.record.source == "crossref"
    assert res.identifier == "doi:10.5555/lit.2021.001"


def test_fabricated_doi_does_not_resolve_only_on_the_authority():
    tr = world()
    res = client(tr).lookup("10.5555/lit.fake.777")
    assert res.status == "does_not_resolve" and "doi.org" in res.reason
    assert any(u.startswith(HDL) for u, _ in tr.calls)


def test_index_404s_without_the_authority_are_lookup_failed():
    tr = world()
    tr.set(f"{HDL}/10.5555/lit.fake.777", None, LiteratureError("doi.org down", transient=True))
    res = client(tr).lookup("10.5555/lit.fake.777")
    assert res.status == "lookup_failed"


def test_doi_that_exists_but_no_index_has_metadata_is_lookup_failed():
    res = client().lookup("10.5555/lit.datacite.5")
    assert res.status == "lookup_failed" and "exists" in res.reason


def test_transient_failure_falls_back_to_the_next_source():
    tr = world()
    tr.set(f"{CR}/10.5555/lit.2021.001", None, r(503, "busy"))
    res = client(tr).lookup("10.5555/lit.2021.001")
    assert res.status == "resolves" and res.record.source == "openalex"
    assert [a["outcome"] for a in res.attempts][:2] == ["error", "found"]


def test_every_source_down_is_lookup_failed_never_does_not_resolve():
    tr = world()
    for url, params in [(f"{CR}/10.5555/lit.fake.777", None), (f"{OA}/doi:10.5555/lit.fake.777", None),
                        (f"{S2}/DOI:10.5555/lit.fake.777", {"fields": S2_FIELDS}),
                        (f"{HDL}/10.5555/lit.fake.777", None)]:
        tr.set(url, params, r(500, "down"))
    assert client(tr).lookup("10.5555/lit.fake.777").status == "lookup_failed"


def test_malformed_identifier_does_not_resolve_without_any_request():
    tr = world()
    res = client(tr).lookup("10.5555/litа2021")   # Cyrillic homoglyph inside a DOI
    assert res.status == "does_not_resolve" and "malformed" in res.reason and tr.calls == []


def test_a_source_answering_with_a_different_paper_is_not_trusted():
    tr = world()
    tr.set(f"{CR}/10.5555/lit.2021.001", None, r(200, fx("crossref_work_d.json")))   # wrong DOI in the body
    tr.set(f"{OA}/doi:10.5555/lit.2021.001", None, r(404, ""))
    tr.set(f"{S2}/DOI:10.5555/lit.2021.001", {"fields": S2_FIELDS}, r(404, ""))
    tr.set(f"{HDL}/10.5555/lit.2021.001", None, r(200, '{"responseCode":1}'))
    res = client(tr).lookup("10.5555/lit.2021.001")
    assert res.status == "lookup_failed"
    assert res.attempts[0]["outcome"] == "mismatch"


def test_arxiv_lookup_paths():
    assert client().lookup("arXiv:2403.01234v1").status == "resolves"
    assert client().lookup("2403.99999").status == "does_not_resolve"
    tr = world()
    tr.set(AX, {"id_list": "2403.01234", "max_results": "1"}, r(503, ""))
    res = client(tr).lookup("2403.01234")
    assert res.status == "resolves" and res.record.source == "semanticscholar"
    tr.set(AX, {"id_list": "2403.99999", "max_results": "1"}, r(503, ""))
    assert client(tr).lookup("2403.99999").status == "lookup_failed"   # S2 has no fixture -> error, arXiv down


def test_as_of_T_hides_papers_published_after_T():
    t = date(2024, 12, 31)
    res = client(as_of=t).lookup("2501.00077")
    assert res.status == "does_not_resolve" and "after" in res.reason
    assert client(as_of=t).lookup("2403.01234").status == "resolves"
    assert client().lookup("2501.00077").status == "resolves"


# ------------------------------------------------------------------ search: splitting, fallback, as-of
def test_split_query():
    assert split_query("a b; c d") == ["a b", "c d"]
    assert split_query("x OR y\nz") == ["x", "y", "z"]
    assert split_query("a a; a a") == ["a a"]
    assert split_query(" ".join(f"w{i}" for i in range(10)), max_terms=4) == ["w0 w1 w2 w3", "w4 w5 w6 w7", "w8 w9"]
    with pytest.raises(ValueError):
        split_query("  ;  ")
    with pytest.raises(ValueError):
        split_query("; ".join(f"q{i}" for i in range(20)))


def test_search_uses_the_first_working_source_and_merges():
    res = client().search("sparse gradient routing tabular", limit=5)
    assert res.complete and [p.doi for p in res.records] == ["10.5555/lit.2021.001", "10.5555/lit.2025.009"]
    assert res.records[0].source == "openalex"


def test_search_falls_back_and_records_the_error():
    tr = world()
    tr.set(OA, {"search": "sparse gradient routing tabular", "per-page": "5"}, r(500, "down"))
    res = client(tr).search("sparse gradient routing tabular", limit=5)
    assert res.complete and res.records[0].source == "crossref" and res.errors[0]["source"] == "openalex"


def test_search_with_every_source_down_is_incomplete_not_empty_success():
    tr = world()
    tr.set(OA, {"search": "sparse gradient routing tabular", "per-page": "5"}, r(500, "down"))
    tr.set(CR, {"query.bibliographic": "sparse gradient routing tabular", "rows": "5"}, r(500, "down"))
    res = client(tr).search("sparse gradient routing tabular", limit=5)
    assert not res.complete and res.records == [] and len(res.errors) == 4


def test_split_query_results_are_merged_and_deduplicated():
    res = client().search("sparse gradient routing tabular; calibrated early stopping", limit=5,
                          sources=["openalex", "semanticscholar"])
    ids = [sorted(p.ids()) for p in res.records]
    assert res.subqueries == ["sparse gradient routing tabular", "calibrated early stopping"]
    assert ["arxiv:2403.01234"] in ids and len(ids) == len({tuple(i) for i in ids})


def test_search_as_of_T_excludes_later_papers():
    res = client(as_of=date(2024, 12, 31)).search("sparse gradient routing tabular", limit=5)
    assert [p.doi for p in res.records] == ["10.5555/lit.2021.001"] and res.excluded_after_as_of == 1
    res = client(as_of=date(2024, 12, 31)).search("calibrated early stopping", limit=5, sources=["arxiv"])
    assert [p.arxiv_id for p in res.records] == ["2403.01234"] and res.excluded_after_as_of == 1


# ------------------------------------------------------------------ dated snapshot: record live, replay offline
def test_frozen_snapshot_replays_lookups_and_searches_exactly(tmp_path):
    store = SnapshotStore(tmp_path / "lit", clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    live = client(RecordingTransport(world(), store))
    live_results = [live.lookup(i) for i in ("10.5555/lit.2021.001", "10.5555/lit.fake.777", "2501.00077")]
    live_search = live.search("sparse gradient routing tabular", limit=5)
    assert live_results[0].record.envelope is not None
    snap = store.freeze(date(2024, 12, 31))

    off = client(OfflineTransport(store, snap))       # as_of comes from the snapshot
    assert off.as_of == date(2024, 12, 31)
    a, b, c = (off.lookup(i) for i in ("10.5555/lit.2021.001", "10.5555/lit.fake.777", "2501.00077"))
    assert a.record == live_results[0].record and a.status == "resolves"
    assert b.status == "does_not_resolve"
    assert c.status == "does_not_resolve" and "after" in c.reason       # the future paper is hidden as of T
    s = off.search("sparse gradient routing tabular", limit=5)
    assert [p.doi for p in s.records] == ["10.5555/lit.2021.001"]
    assert s.records[0] == live_search.records[0]
    # never-recorded request: unverifiable offline, never "does not resolve"
    assert off.lookup("10.5555/lit.2019.042").status == "lookup_failed"


def test_a_client_cannot_widen_the_snapshot_date(tmp_path):
    store = SnapshotStore(tmp_path / "lit")
    snap = store.freeze(date(2024, 12, 31))
    with pytest.raises(ValueError):
        client(OfflineTransport(store, snap), as_of=date(2025, 6, 1))
    assert client(OfflineTransport(store, snap), as_of=date(2024, 6, 1)).as_of == date(2024, 6, 1)
