"""Citation check (GOAL §4.3 K0 / audit I3, §5 L4.3): three-valued (verified / failed / unverified), fail closed.

Red-team cases are written as a dishonest author would try them: a fabricated DOI that resolves to a different
paper, the right title with the wrong year, a quote stitched from two sentences, Unicode homoglyph and whitespace
tricks, a future paper cited inside an 'as of T' snapshot, and a poisoned cache entry."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from custom_harness.literature.cite import Citation, check_citation, check_citations, i3_verdict
from custom_harness.literature.client import LiteratureClient
from custom_harness.literature.snapshot import OfflineTransport, RecordingTransport, SnapshotStore, request_key
from custom_harness.literature.sources import S2_FIELDS
from lit_world import CR, HDL, OA, S2, r, world

ROOT = Path(__file__).resolve().parents[1]

A = {"key": "okafor2021", "id": "10.5555/lit.2021.001", "title": "Sparse gradient routing for tabular ensembles",
     "year": 2021, "authors": ["Okafor, M.", "Lindqvist, T.", "et al."],
     "quotes": ["the method improves accuracy by 1.8 points over gradient boosting"]}
B = {"key": "hartmann2024", "id": "arXiv:2403.01234", "title": "Calibrated Early Stopping under Distribution Shift",
     "year": 2024, "authors": ["Hartmann", "Mensah"],
     "quotes": ["the rule reduces expected calibration error by 23% relative to validation-loss stopping"]}


def nosleep(_s):
    pass


def client(tr=None, **kw):
    return LiteratureClient(tr if tr is not None else world(), sleep=nosleep, max_attempts=2, **kw)


# retrieval provenance is tested in test_literature_retrieval.py; here every cited paper counts as retrieved
RETRIEVED = frozenset({"doi:10.5555/lit.2021.001", "doi:10.5555/lit.2019.042", "arxiv:2403.01234",
                       "arxiv:2501.00077", "doi:10.5555/lit.2025.077"})


def check(cit: dict, retrieved=RETRIEVED, **kw):
    return check_citation(Citation(**cit), client(**kw), retrieved)


# ------------------------------------------------------------------ honest citations
def test_an_honest_citation_is_verified():
    res = check(A)
    assert res.status == "verified", res.reasons
    assert res.id["status"] == "resolves" and res.title["status"] == "match" and res.year["status"] == "match"
    assert res.authors["status"] == "match" and res.quotes[0]["status"] == "supported"


def test_an_honest_arxiv_citation_is_verified():
    assert check(B).status == "verified"


def test_authors_are_optional_but_title_and_year_are_required():
    assert check({**A, "authors": []}).status == "verified"
    res = check({**A, "year": None})
    assert res.status == "unverified" and res.year["status"] == "missing"
    res = check({**A, "title": None})
    assert res.status == "unverified" and res.title["status"] == "missing"


def test_no_identifier_is_unverified():
    res = check({**A, "id": None})
    assert res.status == "unverified" and res.id["status"] == "missing"


def test_result_is_machine_readable_json():
    res = check(A)
    data = json.loads(json.dumps(res.model_dump()))
    assert data["status"] == "verified" and data["provenance"]["envelope"] is None  # router: nothing stored
    assert set(data) >= {"key", "status", "id", "title", "year", "authors", "quotes", "reasons", "provenance"}


# ------------------------------------------------------------------ dishonest author
def test_fabricated_doi_that_resolves_to_a_different_paper_fails():
    res = check({**A, "id": "10.5555/lit.2019.042"})       # a real DOI, but of "Kernel Density Baselines ..."
    assert res.status == "failed" and res.title["status"] == "mismatch"


def test_doi_that_does_not_exist_fails():
    res = check({**A, "id": "10.5555/lit.fake.777"})
    assert res.status == "failed" and res.id["status"] == "does_not_resolve"


def test_right_title_wrong_year_fails():
    res = check({**A, "year": 2020})
    assert res.status == "failed" and res.year["status"] == "mismatch" and res.title["status"] == "match"


def test_invented_coauthor_fails():
    res = check({**A, "authors": ["Okafor", "Smith"]})
    assert res.status == "failed" and res.authors["status"] == "mismatch"


def test_quote_stitched_from_two_sentences_fails():
    res = check({**A, "quotes": ["the method improves accuracy by 1.8 points and training cost rises by 40%"]})
    assert res.status == "failed" and res.quotes[0]["status"] == "unsupported"


def test_one_bad_quote_among_good_ones_fails_the_citation():
    res = check({**A, "quotes": [A["quotes"][0], "It improves calibration, and training cost rises by 40%"]})
    assert res.status == "failed" and [q["status"] for q in res.quotes] == ["supported", "unsupported"]


@pytest.mark.parametrize("field,value", [
    ("title", "Sparse Gradient Routing for Tabulаr Ensembles"),            # Cyrillic a
    ("authors", ["Okаfor"]),                                                # Cyrillic a
    ("quotes", ["the method imprоves accuracy by 1.8 points over gradient boosting"]),  # Cyrillic o
    ("quotes", ["the method improves accuracy by 1.8 points ‮over gradient boosting"]),  # bidi override
])
def test_homoglyph_and_hidden_character_tricks_fail(field, value):
    assert check({**A, field: value}).status == "failed"


@pytest.mark.parametrize("bad_id", ["10.5555/lit.2021.001​", "10.5555/lit.2021.а001", "10.5555/lit 2021.001"])
def test_identifier_whitespace_and_homoglyph_tricks_fail(bad_id):
    res = check({**A, "id": bad_id})
    assert res.status == "failed" and res.id["status"] == "does_not_resolve"


def test_future_paper_cited_as_of_T_fails():
    future = {"key": "raman2025", "id": "2501.00077", "title": "Routing Tokens Through Learned Sparse Experts at Scale",
              "year": 2025, "authors": ["Raman"], "quotes": []}
    assert check(future).status == "verified"                                  # fine without a time split
    res = check(future, as_of=date(2024, 12, 31))
    assert res.status == "failed" and "after the as-of date" in res.id["reason"]


def test_quote_from_text_revised_after_T_is_not_verified():
    # 2403.01234 v1 is from 2024-03-02, but the stored abstract is v2 (2024-06-10): unusable as of 2024-04-01
    res = check(B, as_of=date(2024, 4, 1))
    assert res.status == "unverified" and res.quotes[0]["status"] == "unchecked"
    assert check(B, as_of=date(2024, 7, 1)).status == "verified"


def test_quote_without_stored_text_is_unverified():
    d = {"key": "aaltonen2019", "id": "10.5555/lit.2019.042", "title": "Kernel Density Baselines for Anomaly Scoring",
         "year": 2019, "quotes": ["kernel density baselines are strong anomaly scorers"]}
    res = check(d)
    assert res.status == "unverified" and res.quotes[0]["status"] == "unchecked"
    assert check({**d, "quotes": []}).status == "verified"


def test_lookup_failure_is_unverified_never_verified():
    tr = world()
    for url, params in [(f"{CR}/10.5555/lit.2021.001", None), (f"{OA}/doi:10.5555/lit.2021.001", None),
                        (f"{S2}/DOI:10.5555/lit.2021.001", {"fields": S2_FIELDS}),
                        (f"{HDL}/10.5555/lit.2021.001", None)]:
        tr.set(url, params, r(503, "down"))
    res = check_citation(Citation(**A), client(tr), RETRIEVED)
    assert res.status == "unverified" and res.id["status"] == "lookup_failed"
    assert res.title["status"] == "unchecked" and res.quotes[0]["status"] == "unchecked"


def test_a_bad_quote_fails_even_when_the_lookup_failed():
    tr = world()
    tr.set(f"{CR}/10.5555/lit.2021.001", None, r(503, "down"))
    tr.set(f"{OA}/doi:10.5555/lit.2021.001", None, r(503, "down"))
    tr.set(f"{S2}/DOI:10.5555/lit.2021.001", {"fields": S2_FIELDS}, r(503, "down"))
    tr.set(f"{HDL}/10.5555/lit.2021.001", None, r(503, "down"))
    res = check_citation(Citation(**{**A, "quotes": ["accuracy ... boosting over"]}), client(tr), RETRIEVED)
    assert res.status == "failed"


def test_poisoned_cache_entry_never_verifies(tmp_path):
    store = SnapshotStore(tmp_path / "lit", clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    live = client(RecordingTransport(world(), store))
    live.lookup("10.5555/lit.2019.042")
    # an attacker points the cache entry for paper A's Crossref request at paper D's stored answer
    poison = store.latest(request_key(f"{CR}/10.5555/lit.2019.042", {}))
    with open(store.index_path, "a", encoding="utf-8", newline="") as f:
        f.write(json.dumps({"key": request_key(f"{CR}/10.5555/lit.2021.001", {}), "digest": poison}) + "\n")
    snap = store.freeze(date(2026, 1, 1))
    res = check_citation(Citation(**{**A, "quotes": []}), client(OfflineTransport(store, snap)), RETRIEVED)
    assert res.status == "unverified" and res.id["status"] == "lookup_failed"
    assert any("integrity" in a.get("detail", "") for a in res.id["attempts"])


def test_snapshot_replay_gives_the_same_verdicts(tmp_path):
    store = SnapshotStore(tmp_path / "lit", clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    cits = [Citation(**A), Citation(**B), Citation(**{**A, "key": "bad", "year": 2020})]
    live = [c.model_dump() for c in check_citations(cits, client(RecordingTransport(world(), store)), RETRIEVED)]
    snap = store.freeze(date(2026, 1, 1))
    off = [c.model_dump() for c in check_citations(cits, client(OfflineTransport(store, snap)), RETRIEVED)]
    assert [x["status"] for x in off] == ["verified", "verified", "failed"]
    for a, b in zip(live, off, strict=True):
        assert a["status"] == b["status"] and a["provenance"]["envelope"] == b["provenance"]["envelope"]
    assert all(x["provenance"]["snapshot"] == snap for x in off)


# ------------------------------------------------------------------ I3 verdict for the audit
def test_i3_verdict():
    ok = check(A)
    bad = check({**A, "key": "x", "year": 2020})
    unk = check({**A, "key": "y", "id": None})
    assert i3_verdict([])["I3"] == "n/a"
    v = i3_verdict([ok])
    assert v["I3"] is True and v["problems"] == [] and v["counts"] == {"verified": 1, "failed": 0, "unverified": 0}
    v = i3_verdict([ok, bad, unk])
    assert v["I3"] is False and v["counts"] == {"verified": 1, "failed": 1, "unverified": 1}
    assert len(v["problems"]) == 2 and any("unverified" in p for p in v["problems"])
    json.dumps(v)


def test_citation_check_imports_no_network_or_model_code():
    code = ("import sys, json, custom_harness.literature.cite; print(json.dumps(sorted(sys.modules)))")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                       env={**{k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"},
                            "PYTHONPATH": str(ROOT / "src")})
    loaded = json.loads(p.stdout)
    bad = [m for m in loaded if m.split(".")[0] in ("httpx", "openai", "anthropic")
           or m.startswith(("custom_harness.gateway", "custom_harness.runtime", "custom_harness.evolve"))]
    assert bad == []
