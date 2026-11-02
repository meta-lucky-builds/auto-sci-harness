"""Lab literature connectors and the alphaXiv gate (literature/lab.py, literature/alphaxiv.py). WRITTEN, NOT RUN
(lab-compute, build-only phase). No network: a scripted transport only.

Fail-first reasoning: the modules do not exist on origin/int17. The gate tests fail if `_gate()` is removed from
`search_request`, or if `lab_sources` adds alphaXiv without a permission record.
"""

from __future__ import annotations

import json

import pytest

from custom_harness.compute.credentials import OwnerConfig
from custom_harness.discovery_trace import MemorySink
from custom_harness.literature.alphaxiv import AlphaXivPermission, AlphaXivSource, ConnectorDisabled, versionless_id
from custom_harness.literature.http import HttpResponse, ScriptedTransport
from custom_harness.literature.snapshot import SnapshotStore

HITS = [{"paperId": "2401.12345v2", "title": "A  Paper", "abstract": "x", "publicationDate": "2024-01-20T00:00:00Z",
         "votes": 3, "snippets": [{"pageNumber": 2, "snippet": "the match"}]}]


# the owner's permission record names the endpoints (no alphaXiv URL is written in the code); test hosts only
ENDPOINTS = {"api_base": "https://api.example.test", "web_base": "https://web.example.test"}
RECORD = {"enabled": True, "tos_status": "permitted", "permission_note": "email 2026-11-01", "recorded_by": "owner"}


def _cfg(section):
    return OwnerConfig(None, {"literature": {"alphaxiv": section}})


def test_alphaxiv_is_off_by_default_and_no_url_is_formed():
    for src in (AlphaXivSource(None), AlphaXivSource(None, strategy="embedding")):
        with pytest.raises(ConnectorDisabled, match="terms of service unknown"):
            src.search_request("q", 5)
        with pytest.raises(ConnectorDisabled):
            src.full_text_request("2401.12345")


@pytest.mark.parametrize("section", [{}, {"enabled": True}, {"enabled": True, "tos_status": "unknown"},
                                     {"enabled": True, "tos_status": "permitted"},
                                     {"enabled": "yes", "tos_status": "permitted", "permission_note": "n",
                                      "recorded_by": "o"} | ENDPOINTS,
                                     RECORD,  # a full permission record that names no endpoints
                                     RECORD | {"api_base": "https://api.example.test"},
                                     RECORD | ENDPOINTS | {"api_base": "http://api.example.test"},  # not https
                                     RECORD | ENDPOINTS | {"web_base": "https://web.example.test/x?y=1"},  # a path
                                     {"tos_status": "permitted", "permission_note": "n", "recorded_by": "o"}
                                     | ENDPOINTS])  # endpoints without enabled: true
def test_only_an_explicit_owner_permission_record_enables_alphaxiv(section):
    with pytest.raises(ConnectorDisabled):
        AlphaXivPermission.from_owner_config(section)


def test_lab_sources_without_permission_exclude_alphaxiv():
    from custom_harness.literature.lab import lab_sources
    names = set(lab_sources(OwnerConfig.empty()))
    assert {"openalex", "pubmed", "biorxiv", "semanticscholar"} <= names and not names & {"alphaxiv",
                                                                                         "alphaxiv-embedding"}


def test_with_permission_the_hits_parse_into_records():
    perm = AlphaXivPermission.from_owner_config(RECORD | ENDPOINTS)
    src = AlphaXivSource(perm)
    url, params = src.search_request("q", 5)
    assert url == "https://api.example.test/search/v2/paper/discover/keyword"  # the owner's base, not a code default
    assert params == {"q": "q", "prioritize": "relevance"}
    assert src.full_text_request("2401.12345v2") == ("https://web.example.test/abs/2401.12345.md", {})
    rec = src.parse_search(HttpResponse(200, json.dumps(HITS)))[0]
    assert rec.arxiv_id == "2401.12345" and rec.published == "2024-01-20" and rec.snippets == ("the match",)
    assert rec.url == "https://web.example.test/abs/2401.12345"
    with pytest.raises(ConnectorDisabled):  # without the permission record nothing is parsed into a link either
        AlphaXivSource(None).parse_search(HttpResponse(200, json.dumps(HITS)))
    assert versionless_id("2401.12345v2") == "2401.12345"


def test_lab_search_asks_each_source_and_traces_the_retrieval(tmp_path):
    from custom_harness.literature.lab import lab_client, lab_search
    from custom_harness.literature.sources import OpenAlexSource, SemanticScholarSource
    oa_url, _ = OpenAlexSource().search_request("q", 3)
    s2_url, _ = SemanticScholarSource().search_request("q", 3)
    transport = ScriptedTransport({oa_url: [HttpResponse(200, json.dumps({"results": []}))],
                                   s2_url: [HttpResponse(200, json.dumps({"data": []}))]})
    sink = MemorySink()
    client = lab_client(SnapshotStore(tmp_path / "lit"), config=OwnerConfig.empty(), transport=transport)
    results = lab_search(client, "q", limit=3, sources=("openalex", "semanticscholar"), trace=sink)
    assert len(results) == 2 and {u for u, _ in transport.calls} == {oa_url, s2_url}
    assert [e.fields["source"] for e in sink.events] == ["openalex", "semanticscholar"]
    assert all(e.kind == "literature.retrieval" and e.fields["envelopes"] for e in sink.events)


def test_lab_search_names_alphaxiv_as_disabled_not_unknown(tmp_path):
    from custom_harness.literature.lab import lab_client, lab_search
    client = lab_client(SnapshotStore(tmp_path / "lit"), config=OwnerConfig.empty(), transport=ScriptedTransport({}))
    with pytest.raises(ConnectorDisabled):
        lab_search(client, "q", sources=("alphaxiv",))


def test_retrieval_log_rederives_with_the_lab_source_mapping(tmp_path):
    """A gated source's log entries need the same mapping; the default (ALL_SOURCES) refuses them."""
    from custom_harness.literature.retrieval import RetrievalLog
    log = RetrievalLog(SnapshotStore(tmp_path / "lit"), "s1", sources={})
    with pytest.raises(Exception):  # noqa: B017 - any integrity error: the source is unknown to this log
        log._derive("alphaxiv", "0" * 64, (None, None))
