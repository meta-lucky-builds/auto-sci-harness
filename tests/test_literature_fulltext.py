"""Full-text keyword retrieval with match snippets (OR uses alphaXiv full text; alphaXiv's terms are unknown,
treated as forbidden until the owner checks, so the real connector is Europe PMC's open-access subset). Offline only:
hand-written
fixtures behind a router that raises on any unrouted request.

Checked: pacing host, the fixed retrieval window (no full text is even fetched for a paper outside it), as-of-T
for the TEXT (a full text released after T is never used), dated snapshots (identical offline replay), retrieval
provenance (full-text searches are logged and re-derived), the three-valued citation check, and quote-span
support against the body text, not only the abstract."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from custom_harness.literature.cite import Citation, check_citation
from custom_harness.literature.client import LiteratureClient
from custom_harness.literature.fulltext import EPMC_REST, EPMC_SEARCH, EuropePMCSource, match_snippets, parse_jats
from custom_harness.literature.http import LiteratureError, PacedTransport, Pacer
from custom_harness.literature.records import LookupResult, PaperRecord
from custom_harness.literature.retrieval import RetrievalLog, RetrievalLoop
from custom_harness.literature.snapshot import OfflineTransport, RecordingTransport, SnapshotStore
from custom_harness.literature.sources import ALL_SOURCES, PACING
from lit_world import CR, Router, fx, key, r

Q = "splice ratio hypoxia"
F_DOI = "10.5555/lit.2022.077"
BODY_QUOTE = "the splice ratio fell by 40 percent within six hours of hypoxia onset"
SEARCH_PARAMS = {"query": f"({Q}) AND OPEN_ACCESS:y AND HAS_FT:y", "resultType": "core", "format": "json",
                 "pageSize": "10"}
FT_F = f"{EPMC_REST}/PMC9000077/fullTextXML"
FT_G = f"{EPMC_REST}/PMC9000110/fullTextXML"
CROSSREF_F = json.dumps({"status": "ok", "message": {
    "DOI": F_DOI, "title": ["Hypoxia-Induced Splicing Shifts in Cultured Hepatocytes"],
    "author": [{"given": "Nkem", "family": "Okafor"}, {"given": "Maja", "family": "Lindqvist"}],
    "issued": {"date-parts": [[2022, 3, 15]]},
    "abstract": "<jats:p>We measured alternative splicing in cultured hepatocytes under low oxygen and report a "
                "reproducible shift in exon usage.</jats:p>"}})


def ft_world() -> Router:
    return Router({
        key(EPMC_SEARCH, SEARCH_PARAMS): r(200, fx("europepmc_search.json")),
        key(FT_F): r(200, fx("europepmc_fulltext_f.xml")),
        key(EPMC_SEARCH, {"query": f'DOI:"{F_DOI}"', "resultType": "core", "format": "json", "pageSize": "1"}):
            r(200, fx("europepmc_lookup_f.json")),
        key(EPMC_SEARCH, {"query": "EXT_ID:31000077 AND SRC:MED", "resultType": "core", "format": "json",
                          "pageSize": "1"}): r(200, fx("europepmc_lookup_f.json")),
        key(f"{CR}/{F_DOI}"): r(200, CROSSREF_F),
    })


def client(transport, **kw) -> LiteratureClient:
    return LiteratureClient(transport, sleep=lambda s: None, max_attempts=2, **kw)


def store(tmp_path) -> SnapshotStore:
    return SnapshotStore(tmp_path / "lit", clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))


# ------------------------------------------------------------------ parsing
def test_europepmc_search_parses_to_records():
    recs = EuropePMCSource().parse_search(r(200, fx("europepmc_search.json")))
    f = recs[0]
    assert (f.source, f.source_id, f.doi, f.pmid, f.published) == ("europepmc", "PMC9000077", F_DOI, "31000077",
                                                                  "2022-03-15")
    assert f.title == "Hypoxia-Induced Splicing Shifts in Cultured Hepatocytes."
    assert f.authors[:2] == ("Nkem Okafor", "Maja Lindqvist") and f.years == (2022,)
    assert recs[2].pmid is None and recs[2].source_id == "PPR000123"  # a preprint, no PMID, no PMC full text
    assert "europepmc" in ALL_SOURCES  # so the retrieval log can re-derive full-text searches


def test_jats_body_text_excludes_reference_list_and_carries_license_and_release_date():
    ft = parse_jats(r(200, fx("europepmc_fulltext_f.xml")))
    assert BODY_QUOTE in " ".join(ft.text.split())
    assert "in passing" not in ft.text  # reference list text is never "the paper says"
    assert ft.ids == {f"doi:{F_DOI}", "pmid:31000077"}
    assert ft.license == "https://creativecommons.org/licenses/by/4.0/"
    assert ft.version_date == "2022-04-01"  # the LATEST date the article gives (pmc-release), fail closed


def test_jats_entity_declarations_refused():
    evil = '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "y">]><article>&x;</article>'
    with pytest.raises(LiteratureError):
        parse_jats(r(200, evil))


def test_match_snippets_whole_words_merged_and_bounded():
    text = "alpha beta splice ratio gamma " + "filler " * 40 + "the hypoxia arm " + "x " * 5
    snips = match_snippets(text, "splice ratio hypoxia", width=3, max_snippets=5)
    assert snips[0] == "alpha beta splice ratio gamma filler filler …"  # adjacent hits merge into one window
    assert "hypoxia" in snips[1] and len(snips) == 2
    assert match_snippets("splicer ratios", "splice ratio") == ()  # whole tokens only
    assert len(match_snippets("hit " * 100, "hit", width=1, max_snippets=3)) <= 3


# ------------------------------------------------------------------ search
def test_full_text_search_returns_body_snippets_inside_the_window():
    w = ft_world()
    res = client(w).full_text_search(Q, published_to=date(2024, 12, 31))
    by = {rec.doi: rec for rec in res.records}
    f = by[F_DOI]
    assert f.full_text and BODY_QUOTE in " ".join(f.full_text.split())
    assert any("splice ratio fell by 40 percent" in s for s in f.snippets)
    assert f.full_text_envelope is None and f.full_text_license.startswith("https://creativecommons.org/")
    # the 2025 paper is outside the window: excluded, and its full text was NEVER requested
    assert "10.5555/lit.2025.010" not in by and res.excluded_after_as_of == 1
    assert FT_G not in [u for u, _ in w.calls]
    # the preprint has no PMC full text: kept, no text, counted
    assert by["10.5555/lit.2021.300"].full_text is None
    assert res.full_text == {"attached": 1, "unavailable": 1, "excluded_after_as_of": 0, "errors": 0}


def test_full_text_released_after_T_is_never_used_for_search(tmp_path):
    res = client(ft_world(), as_of=date(2022, 3, 20)).full_text_search(Q)
    f = next(x for x in res.records if x.doi == F_DOI)  # the paper itself was published by T
    assert f.full_text is None and f.snippets == ()  # but its PMC text was released 2022-04-01, after T
    assert res.full_text["excluded_after_as_of"] == 1


def test_europepmc_host_is_paced():
    assert PACING["www.ebi.ac.uk"] >= 1.0
    slept: list[float] = []
    now = [0.0]
    pacer = Pacer(PACING, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    t = PacedTransport(ft_world(), pacer)
    client(t).full_text_search(Q, published_to=date(2024, 12, 31))
    assert slept and min(slept) >= PACING["www.ebi.ac.uk"] - 1e-9  # search then fetch: the second call waited


def test_snapshot_replay_is_identical_and_offline(tmp_path):
    s = store(tmp_path)
    live = client(RecordingTransport(ft_world(), s)).full_text_search(Q, published_to=date(2024, 12, 31))
    snap = s.freeze(date(2024, 12, 31))
    off = client(OfflineTransport(s, snap)).full_text_search(Q, published_to=date(2024, 12, 31))
    assert [x.model_dump() for x in off.records] == [x.model_dump() for x in live.records]
    f = next(x for x in off.records if x.doi == F_DOI)
    assert f.full_text_envelope and f.full_text_envelope == next(x for x in live.records if x.doi == F_DOI
                                                                 ).full_text_envelope
    # a query that was never recorded cannot reach the network
    res = client(OfflineTransport(s, snap)).full_text_search("never asked", published_to=date(2024, 12, 31))
    assert res.complete is False and res.records == []


def test_retrieval_loop_logs_full_text_searches_and_inherits_the_window(tmp_path):
    s = store(tmp_path)
    c = client(RecordingTransport(ft_world(), s))
    log = RetrievalLog(s, "run-ft")
    loop = RetrievalLoop(c, log, max_rounds=2, sources=["europepmc"], published_to=date(2024, 12, 31),
                         full_text=True)
    new = loop.search(Q)
    assert any(x.snippets for x in new)
    ids = log.retrieved_ids()  # re-derived from the stored search envelope
    assert f"doi:{F_DOI}" in ids and "doi:10.5555/lit.2025.010" not in ids
    assert {e["source"] for e in log.entries()} == {"europepmc"}


# ------------------------------------------------------------------ citations and quote spans
def test_body_quote_is_supported_only_with_full_text(tmp_path):
    s = store(tmp_path)
    log = RetrievalLog(s, "run-q")
    log.record_search(client(RecordingTransport(ft_world(), s)).full_text_search(Q, published_to=date(2024, 12, 31)))
    cit = Citation(key="f", id=F_DOI, title="Hypoxia-Induced Splicing Shifts in Cultured Hepatocytes", year=2022,
                   quotes=[BODY_QUOTE])
    ids = log.retrieved_ids()
    abstract_only = check_citation(cit, client(RecordingTransport(ft_world(), s)), ids)
    assert abstract_only.quotes[0]["status"] == "unsupported"  # the known limitation, unchanged without full text
    with_ft = check_citation(cit, client(RecordingTransport(ft_world(), s), full_text=True), ids)
    assert with_ft.status == "verified", with_ft.reasons
    assert with_ft.quotes[0]["status"] == "supported"
    assert with_ft.provenance["full_text_envelope"]
    assert any(a.get("source") == "europepmc" and a["outcome"] == "full_text" for a in with_ft.id["attempts"])


def test_body_quote_with_full_text_released_after_T_is_unverified_not_verified(tmp_path):
    s = store(tmp_path)
    cit = Citation(key="f", id=F_DOI, title="Hypoxia-Induced Splicing Shifts in Cultured Hepatocytes", year=2022,
                   quotes=[BODY_QUOTE])
    res = check_citation(cit, client(RecordingTransport(ft_world(), s), full_text=True, as_of=date(2022, 3, 20)),
                         {f"doi:{F_DOI}"})
    assert res.status == "unverified"
    assert res.quotes[0]["status"] == "unchecked" and "after the as-of date" in res.quotes[0]["reason"]


def test_fabricated_quote_still_fails_against_full_text(tmp_path):
    s = store(tmp_path)
    cit = Citation(key="f", id=F_DOI, title="Hypoxia-Induced Splicing Shifts in Cultured Hepatocytes", year=2022,
                   quotes=["the splice ratio fell by 90 percent within six hours of hypoxia onset"])
    res = check_citation(cit, client(RecordingTransport(ft_world(), s), full_text=True), {f"doi:{F_DOI}"})
    assert res.status == "failed" and res.quotes[0]["status"] == "unsupported"


def test_quote_from_reference_list_is_not_support(tmp_path):
    s = store(tmp_path)
    cit = Citation(key="f", id=F_DOI, title="Hypoxia-Induced Splicing Shifts in Cultured Hepatocytes", year=2022,
                   quotes=["A reference that mentions splice ratio in passing"])
    res = check_citation(cit, client(RecordingTransport(ft_world(), s), full_text=True), {f"doi:{F_DOI}"})
    assert res.quotes[0]["status"] == "unsupported"


def test_full_text_of_another_paper_is_never_attached(tmp_path):
    w = ft_world()
    wrong = fx("europepmc_fulltext_f.xml").replace(F_DOI, "10.5555/someone.else").replace("31000077", "39999999")
    w.set(FT_F, None, r(200, wrong))
    res = client(w, full_text=True).lookup(F_DOI)
    assert res.status == "resolves" and res.record.full_text is None
    assert any(a.get("source") == "europepmc" and a["outcome"] == "mismatch" for a in res.attempts)


def test_full_text_outage_degrades_to_abstract_and_is_recorded():
    w = ft_world()
    w.set(FT_F, None, r(503, "busy"))
    res = client(w, full_text=True).lookup(F_DOI)
    assert res.status == "resolves" and res.record.full_text is None and res.record.abstract
    assert any(a.get("source") == "europepmc" and a["outcome"] == "error" for a in res.attempts)


def test_lookup_by_pmid_gets_full_text():
    w = ft_world()
    w.set("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
          {"db": "pubmed", "id": "31000077", "retmode": "xml"},
          r(200, '<?xml version="1.0"?><PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>31000077</PMID>'
                 '<Article><ArticleTitle>Hypoxia-Induced Splicing Shifts in Cultured Hepatocytes</ArticleTitle>'
                 '<Journal><JournalIssue><PubDate><Year>2022</Year><Month>Mar</Month><Day>15</Day></PubDate>'
                 '</JournalIssue></Journal></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'))
    res = client(w, full_text=True).lookup("pmid:31000077")
    assert res.status == "resolves" and BODY_QUOTE in " ".join((res.record.full_text or "").split())


def test_cite_rechecks_the_text_date_of_a_record_from_anywhere():
    """A resolver that hands back a record with full text dated after T: the check refuses that text itself."""

    class Fake:
        as_of = date(2022, 3, 20)

        def lookup(self, raw):
            rec = PaperRecord(source="x", source_id="1", title="T T T", doi=F_DOI, published="2022-03-15",
                              years=(2022,), abstract="nothing relevant here at all",
                              full_text=BODY_QUOTE, full_text_date="2022-04-01")
            return LookupResult(identifier=f"doi:{F_DOI}", status="resolves", reason="", record=rec)

    res = check_citation(Citation(key="k", id=F_DOI, title="T T T", year=2022, quotes=[BODY_QUOTE]), Fake(),
                         {f"doi:{F_DOI}"})
    assert res.quotes[0]["status"] == "unchecked" and res.status == "unverified"


def test_no_alphaxiv_endpoint_is_wired():
    """alphaXiv's terms are unknown, treated as forbidden until the owner checks: never call it."""
    from pathlib import Path

    import custom_harness.literature as lit
    for p in Path(lit.__file__).parent.glob("*.py"):
        assert "api.alphaxiv.org" not in p.read_text(encoding="utf-8"), p.name



# ------------------------------------------------------------------ CLI (offline against a frozen snapshot)
def test_cli_full_text_search_and_cite_check_offline(tmp_path, capsys):
    from custom_harness.cli import main
    from custom_harness.config import data_dir
    s = SnapshotStore(data_dir() / "literature", clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    c = client(RecordingTransport(ft_world(), s), full_text=True)
    c.full_text_search(Q, published_to=date(2024, 12, 31))
    c.lookup(F_DOI)
    snap = s.freeze(date(2024, 12, 31))
    assert main(["lit-search", Q, "--source", "europepmc", "--full-text", "--snapshot", snap,
                 "--session", "cli-ft"]) == 0
    out = json.loads(capsys.readouterr().out)
    f = next(x for x in out["records"] if x["doi"] == F_DOI)
    assert any("splice ratio fell by 40 percent" in sn for sn in f["snippets"])
    assert out["full_text"]["attached"] == 1
    cites = tmp_path / "c.json"
    cites.write_text(json.dumps([{"key": "f", "id": F_DOI, "year": 2022, "quotes": [BODY_QUOTE],
                                  "title": "Hypoxia-Induced Splicing Shifts in Cultured Hepatocytes"}]),
                     encoding="utf-8")
    assert main(["cite-check", str(cites), "--snapshot", snap, "--session", "cli-ft", "--full-text"]) == 0
    assert json.loads(capsys.readouterr().out)["I3"] is True
    assert main(["cite-check", str(cites), "--snapshot", snap, "--session", "cli-ft"]) == 1  # abstract only
    assert json.loads(capsys.readouterr().out)["counts"]["failed"] == 1
