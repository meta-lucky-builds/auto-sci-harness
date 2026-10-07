"""Offline fixture world for the literature tests: recorded-style responses (tests/fixtures/literature, written to
each API's documented schema; fictional papers under the reserved test DOI prefix 10.5555) behind a router
transport. An unrouted request raises (fail closed), so a test can never silently reach the network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from custom_harness.literature.http import HttpResponse, LiteratureError
from custom_harness.literature.sources import S2_FIELDS

FIX = Path(__file__).parent / "fixtures" / "literature"
CR = "https://api.crossref.org/works"
OA = "https://api.openalex.org/works"
S2 = "https://api.semanticscholar.org/graph/v1/paper"
AX = "https://export.arxiv.org/api/query"
HDL = "https://doi.org/api/handles"
OA_404 = "<!doctype html>\n<html lang=en>\n<title>404 Not Found</title>\n<h1>Not Found</h1>\n"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def r(status: int, text: str = "", headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status=status, text=text, headers=headers or {})


def key(url: str, params: dict[str, str] | None = None) -> tuple[str, frozenset]:
    return url, frozenset((params or {}).items())


class Router:
    offline = False

    def __init__(self, routes: dict[tuple[str, frozenset], Any]):
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in routes.items()}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def set(self, url: str, params: dict[str, str] | None, *responses: Any) -> None:
        self.routes[key(url, params)] = list(responses)

    def get(self, url: str, params: dict[str, str]) -> HttpResponse:
        self.calls.append((url, dict(params)))
        queue = self.routes.get(key(url, params))
        if not queue:
            raise LiteratureError(f"no fixture for GET {url} {sorted(params.items())}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def world() -> Router:
    s2f = {"fields": S2_FIELDS}
    return Router({
        # Crossref
        key(f"{CR}/10.5555/lit.2021.001"): r(200, fx("crossref_work_a.json")),
        key(f"{CR}/10.5555/lit.2019.042"): r(200, fx("crossref_work_d.json")),
        key(f"{CR}/10.5555/lit.fake.777"): r(404, "Resource not found."),
        key(f"{CR}/10.5555/lit.datacite.5"): r(404, "Resource not found."),
        key(CR, {"query.bibliographic": "sparse gradient routing tabular", "rows": "5"}):
            r(200, fx("crossref_search.json")),
        # OpenAlex
        key(f"{OA}/doi:10.5555/lit.2021.001"): r(200, fx("openalex_work_a.json")),
        key(f"{OA}/doi:10.5555/lit.fake.777"): r(404, OA_404),
        key(f"{OA}/doi:10.5555/lit.datacite.5"): r(404, OA_404),
        key(OA, {"search": "sparse gradient routing tabular", "per-page": "5"}): r(200, fx("openalex_search.json")),
        # Semantic Scholar
        key(f"{S2}/DOI:10.5555/lit.fake.777", s2f): r(404, '{"error":"Paper with id not found"}'),
        key(f"{S2}/DOI:10.5555/lit.datacite.5", s2f): r(404, '{"error":"Paper with id not found"}'),
        key(f"{S2}/ARXIV:2403.01234", s2f): r(200, fx("s2_paper_b.json")),
        key(f"{S2}/search", {"query": "calibrated early stopping", "limit": "5", "fields": S2_FIELDS}):
            r(200, fx("s2_search.json")),
        # doi.org handle API (the authority on whether a DOI exists)
        key(f"{HDL}/10.5555/lit.fake.777"): r(404, '{"responseCode":100,"handle":"10.5555/lit.fake.777"}'),
        key(f"{HDL}/10.5555/lit.datacite.5"): r(200, '{"responseCode":1,"handle":"10.5555/lit.datacite.5"}'),
        # arXiv (the authority on arXiv ids)
        key(AX, {"id_list": "2403.01234", "max_results": "1"}): r(200, fx("arxiv_entry_b.xml")),
        key(AX, {"id_list": "2501.00077", "max_results": "1"}): r(200, fx("arxiv_entry_c.xml")),
        key(AX, {"id_list": "2403.99999", "max_results": "1"}): r(200, fx("arxiv_empty.xml")),
        key(AX, {"search_query": "all:calibrated AND all:early AND all:stopping", "start": "0", "max_results": "5"}):
            r(200, fx("arxiv_search.xml")),
    })
