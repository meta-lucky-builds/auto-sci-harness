"""PubMed (E-utilities) and bioRxiv connectors behind the same interface (GOAL §5 L4.3). Fixtures only."""

from __future__ import annotations

from datetime import date

import pytest

from custom_harness.literature.cite import Citation, check_citation
from custom_harness.literature.client import LiteratureClient
from custom_harness.literature.http import LiteratureError
from custom_harness.literature.records import parse_identifier
from custom_harness.literature.sources import BiorxivSource, PubMedSource
from lit_world import EU, fx, r, world


def client(tr=None, **kw):
    return LiteratureClient(tr if tr is not None else world(), sleep=lambda s: None, max_attempts=2, **kw)


@pytest.mark.parametrize("raw", ["pmid:31000001", "PMID: 31000001", "https://pubmed.ncbi.nlm.nih.gov/31000001/"])
def test_pmid_identifiers(raw):
    ident = parse_identifier(raw)
    assert (ident.kind, ident.value) == ("pmid", "31000001")


@pytest.mark.parametrize("raw", ["pmid:", "pmid:0", "pmid:12a4", "PMID: 3100۱0001", "pmid:1234567890"])
def test_malformed_pmids_are_refused(raw):
    with pytest.raises(ValueError):
        parse_identifier(raw)


def test_pubmed_efetch_parses():
    rec = PubMedSource().parse_lookup(r(200, fx("pubmed_efetch_e.xml")))
    assert rec.pmid == "31000001" and rec.doi == "10.5555/lit.2019.300"
    assert rec.title == "Serum Marker X Predicts Sepsis Mortality in Adults."
    assert rec.authors == ("Ana García", "Wei Li", "Example Sepsis Consortium")
    assert rec.published == "2019-02-11" and rec.years == (2019,)   # electronic date precedes the April issue
    assert "hazard ratio of 2.1 for 28-day mortality" in rec.abstract
    assert {"pmid:31000001", "doi:10.5555/lit.2019.300"} <= rec.ids()


def test_pubmed_xml_with_internal_entities_is_refused_but_public_doctype_is_fine():
    bomb = ('<?xml version="1.0"?><!DOCTYPE PubmedArticleSet [<!ENTITY a "aaaa">]>'
            '<PubmedArticleSet><PubmedArticle/></PubmedArticleSet>')
    with pytest.raises(LiteratureError):
        PubMedSource().parse_lookup(r(200, bomb))
    subset = ('<?xml version="1.0"?><!DOCTYPE PubmedArticleSet [<!ATTLIST PubmedArticleSet x CDATA "y">]>'
              '<PubmedArticleSet></PubmedArticleSet>')   # internal subset without entities: refused too
    with pytest.raises(LiteratureError):
        PubMedSource().parse_lookup(r(200, subset))
    assert PubMedSource().parse_lookup(r(200, fx("pubmed_efetch_empty.xml"))) is None


def test_pmid_lookup_three_valued():
    assert client().lookup("pmid:31000001").status == "resolves"
    res = client().lookup("pmid:39999999")
    assert res.status == "does_not_resolve" and "PubMed" in res.reason
    tr = world()
    tr.set(f"{EU}/efetch.fcgi", {"db": "pubmed", "id": "31000001", "retmode": "xml"}, r(503, ""))
    assert client(tr).lookup("pmid:31000001").status == "lookup_failed"


def test_pubmed_search_is_esearch_then_esummary_and_respects_as_of():
    tr = world()
    res = client(tr).search("serum marker sepsis", limit=5, sources=["pubmed"])
    assert [p.pmid for p in res.records] == ["31000001", "35000002"] and res.complete
    assert [u.rsplit("/", 1)[1] for u, _ in tr.calls] == ["esearch.fcgi", "esummary.fcgi"]
    assert res.records[0].published == "2019-02-11"
    res = client(as_of=date(2024, 12, 31)).search("serum marker sepsis", limit=5, sources=["pubmed"])
    assert [p.pmid for p in res.records] == ["31000001"] and res.excluded_after_as_of == 1


def test_pubmed_empty_search_makes_no_summary_call():
    tr = world()
    tr.set(f"{EU}/esearch.fcgi", {"db": "pubmed", "term": "nothing here", "retmax": "5", "retmode": "json"},
           r(200, '{"esearchresult": {"count": "0", "idlist": []}}'))
    res = client(tr).search("nothing here", limit=5, sources=["pubmed"])
    assert res.records == [] and res.complete and len(tr.calls) == 1


def test_biorxiv_details_parse_versions():
    rec = BiorxivSource().parse_lookup(r(200, fx("biorxiv_details.json")))
    assert rec.doi == "10.1101/2023.05.01.538000"
    assert rec.published == "2023-05-01" and rec.updated == "2023-09-10"      # v1 date, latest version date
    assert rec.authors == ("Ndlovu, T.", "Berg, A.", "Sato, K.")              # latest version's author list
    assert "transient progenitor state" in rec.abstract                        # latest version's abstract
    assert BiorxivSource().parse_lookup(
        r(200, '{"messages":[{"status":"no posts found"}],"collection":[]}')) is None


def test_biorxiv_doi_lookup_uses_biorxiv_first_and_doi_org_as_authority():
    tr = world()
    res = client(tr).lookup("10.1101/2023.05.01.538000")
    assert res.status == "resolves" and res.record.source == "biorxiv" and len(tr.calls) == 1
    tr = world()
    for host in ("https://api.crossref.org/works/", "https://api.openalex.org/works/doi:"):
        tr.set(host + "10.1101/2099.01.01.000001", None, r(404, ""))
    tr.set("https://api.semanticscholar.org/graph/v1/paper/DOI:10.1101/2099.01.01.000001",
           {"fields": __import__("lit_world").S2_FIELDS}, r(404, ""))
    assert client(tr).lookup("10.1101/2099.01.01.000001").status == "does_not_resolve"


def test_biorxiv_text_revised_after_T_is_not_used_for_quotes():
    cit = Citation(key="z", id="10.1101/2023.05.01.538000", title="Single-Cell Atlas of the Regenerating Zebrafish Fin",
                   year=2023,
                   quotes=["identify a transient progenitor state that is lost when Wnt signalling is blocked"])
    assert check_citation(cit, client(), retrieved_ids={"doi:10.1101/2023.05.01.538000"}).status == "verified"
    res = check_citation(cit, client(as_of=date(2023, 6, 1)), retrieved_ids={"doi:10.1101/2023.05.01.538000"})
    assert res.status == "unverified" and res.quotes[0]["status"] == "unchecked"


def test_biorxiv_search_goes_through_openalex_restricted_to_biorxiv():
    res = client().search("zebrafish fin regeneration", limit=5, sources=["biorxiv"])
    assert [p.doi for p in res.records] == ["10.1101/2023.05.01.538000"] and res.complete
