"""Source adapters: request builders and parsers for arXiv, Semantic Scholar, OpenAlex, Crossref, plus the doi.org
handle API (the authority on whether a DOI exists). Parsing follows each API's documented schema; anything else
is an infra error (never a silently empty or partial record).

Rate limits used for pacing (published guidance, conservative where it is vague):
  arXiv API terms: no more than one request every 3 s.                        -> 3.0 s
  Semantic Scholar: unauthenticated callers share one pool and get 429s.       -> 1.0 s (+ retries)
  OpenAlex: at most 10 requests/s.                                             -> 0.2 s
  Crossref public pool (no mailto: we send no contact address).                -> 1.0 s
  doi.org handle API: no published figure.                                     -> 1.0 s
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any

from .http import HttpResponse, LiteratureError
from .normalize import strip_markup
from .records import Identifier, PaperRecord

PACING = {"export.arxiv.org": 3.0, "api.semanticscholar.org": 1.0, "api.openalex.org": 0.2,
          "api.crossref.org": 1.0, "doi.org": 1.0}
S2_FIELDS = "paperId,externalIds,title,abstract,year,publicationDate,authors"
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"
_ABS_ID = re.compile(r"arxiv\.org/abs/(.+?)(v\d+)?$")


def _shape(ok: bool, what: str) -> None:
    if not ok:
        raise LiteratureError(f"unexpected response shape: {what}")


def _json(resp: HttpResponse) -> Any:
    try:
        return json.loads(resp.text)
    except ValueError as e:
        raise LiteratureError(f"unexpected response (not JSON): {resp.text[:120]!r}") from e


def _ws(s: str | None) -> str | None:
    if s is None:
        return None
    out = " ".join(strip_markup(s).split())
    return out or None


def _str_or_none(v: Any, what: str) -> str | None:
    _shape(v is None or isinstance(v, str), what)
    return v if isinstance(v, str) else None


def _doi(v: Any) -> str | None:
    s = _str_or_none(v, "doi")
    if not s:
        return None
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s.strip(), flags=re.IGNORECASE).lower()


class Source:
    name = ""
    lookup_kinds: frozenset[str] = frozenset()

    def lookup_request(self, ident: Identifier) -> tuple[str, dict[str, str]]:
        raise NotImplementedError

    def parse_lookup(self, resp: HttpResponse) -> PaperRecord | None:
        raise NotImplementedError

    def search_request(self, query: str, limit: int) -> tuple[str, dict[str, str]]:
        raise NotImplementedError

    def parse_search(self, resp: HttpResponse) -> list[PaperRecord]:
        raise NotImplementedError


# ------------------------------------------------------------------ Crossref
def _date_parts(m: dict[str, Any], field: str) -> str | None:
    d = m.get(field)
    if not isinstance(d, dict):
        return None
    parts = d.get("date-parts")
    if not (isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]):
        return None
    p = parts[0]
    if not all(isinstance(x, int) for x in p):
        return None
    return "-".join([f"{p[0]:04d}", *(f"{x:02d}" for x in p[1:3])])


class CrossrefSource(Source):
    name = "crossref"
    lookup_kinds = frozenset({"doi"})
    base = "https://api.crossref.org/works"

    def lookup_request(self, ident: Identifier) -> tuple[str, dict[str, str]]:
        return f"{self.base}/{ident.value}", {}

    def search_request(self, query: str, limit: int) -> tuple[str, dict[str, str]]:
        return self.base, {"query.bibliographic": query, "rows": str(limit)}

    def _item(self, m: Any) -> PaperRecord:
        _shape(isinstance(m, dict), "crossref work is not an object")
        titles = m.get("title") or []
        _shape(isinstance(titles, list) and all(isinstance(t, str) for t in titles), "crossref title")
        doi = _doi(m.get("DOI"))
        _shape(doi is not None, "crossref DOI")
        authors = []
        for a in m.get("author") or []:
            _shape(isinstance(a, dict), "crossref author")
            name = " ".join(x for x in (a.get("given"), a.get("family")) if isinstance(x, str) and x) or a.get("name")
            if isinstance(name, str) and name:
                authors.append(name)
        dates = [_date_parts(m, f) for f in ("issued", "published", "published-online", "published-print")]
        years = sorted({int(d[:4]) for d in dates if d})
        title = (_ws(titles[0]) or "") if titles else ""
        return PaperRecord(source=self.name, source_id=str(doi), doi=doi, title=title,
                           authors=tuple(authors), published=next((d for d in dates if d), None), years=tuple(years),
                           abstract=_ws(_str_or_none(m.get("abstract"), "crossref abstract")),
                           url=f"https://doi.org/{doi}")

    def parse_lookup(self, resp: HttpResponse) -> PaperRecord | None:
        if resp.status == 404:
            return None
        data = _json(resp)
        _shape(isinstance(data, dict) and isinstance(data.get("message"), dict), "crossref message")
        return self._item(data["message"])

    def parse_search(self, resp: HttpResponse) -> list[PaperRecord]:
        data = _json(resp)
        _shape(isinstance(data, dict) and isinstance(data.get("message"), dict)
               and isinstance(data["message"].get("items"), list), "crossref items")
        return [self._item(m) for m in data["message"]["items"]]


# ------------------------------------------------------------------ OpenAlex
def _inverted_abstract(inv: Any) -> str | None:
    if inv is None:
        return None
    _shape(isinstance(inv, dict), "openalex abstract_inverted_index")
    pos: dict[int, str] = {}
    for word, places in inv.items():
        _shape(isinstance(places, list) and all(isinstance(p, int) for p in places), "openalex abstract positions")
        for p in places:
            pos[p] = word
    return " ".join(pos[i] for i in sorted(pos)) or None


class OpenAlexSource(Source):
    name = "openalex"
    lookup_kinds = frozenset({"doi"})
    base = "https://api.openalex.org/works"

    def lookup_request(self, ident: Identifier) -> tuple[str, dict[str, str]]:
        return f"{self.base}/doi:{ident.value}", {}

    def search_request(self, query: str, limit: int) -> tuple[str, dict[str, str]]:
        return self.base, {"search": query, "per-page": str(limit)}

    def _item(self, w: Any) -> PaperRecord:
        _shape(isinstance(w, dict) and isinstance(w.get("id"), str), "openalex work")
        title = w.get("title") or w.get("display_name") or ""
        _shape(isinstance(title, str), "openalex title")
        authors = []
        for a in w.get("authorships") or []:
            name = (a.get("author") or {}).get("display_name") if isinstance(a, dict) else None
            if isinstance(name, str) and name:
                authors.append(name)
        year = w.get("publication_year")
        _shape(year is None or isinstance(year, int), "openalex publication_year")
        date_ = _str_or_none(w.get("publication_date"), "openalex publication_date")
        return PaperRecord(source=self.name, source_id=w["id"], doi=_doi(w.get("doi")), title=_ws(title) or "",
                           authors=tuple(authors), published=date_ or (str(year) if year else None),
                           years=(year,) if year else (), abstract=_inverted_abstract(w.get("abstract_inverted_index")),
                           url=w["id"])

    def parse_lookup(self, resp: HttpResponse) -> PaperRecord | None:
        if resp.status == 404:
            return None
        return self._item(_json(resp))

    def parse_search(self, resp: HttpResponse) -> list[PaperRecord]:
        data = _json(resp)
        _shape(isinstance(data, dict) and isinstance(data.get("results"), list), "openalex results")
        return [self._item(w) for w in data["results"]]


# ------------------------------------------------------------------ Semantic Scholar
class SemanticScholarSource(Source):
    name = "semanticscholar"
    lookup_kinds = frozenset({"doi", "arxiv"})
    base = "https://api.semanticscholar.org/graph/v1/paper"

    def lookup_request(self, ident: Identifier) -> tuple[str, dict[str, str]]:
        prefix = "DOI" if ident.kind == "doi" else "ARXIV"
        return f"{self.base}/{prefix}:{ident.value}", {"fields": S2_FIELDS}

    def search_request(self, query: str, limit: int) -> tuple[str, dict[str, str]]:
        return f"{self.base}/search", {"query": query, "limit": str(limit), "fields": S2_FIELDS}

    def _item(self, p: Any) -> PaperRecord:
        _shape(isinstance(p, dict) and isinstance(p.get("title"), str), "semantic scholar paper/title")
        ext = p.get("externalIds") or {}
        _shape(isinstance(ext, dict), "semantic scholar externalIds")
        year = p.get("year")
        _shape(year is None or isinstance(year, int), "semantic scholar year")
        date_ = _str_or_none(p.get("publicationDate"), "semantic scholar publicationDate")
        authors = tuple(a["name"] for a in p.get("authors") or []
                        if isinstance(a, dict) and isinstance(a.get("name"), str))
        arxiv = _str_or_none(ext.get("ArXiv"), "semantic scholar ArXiv id")
        return PaperRecord(source=self.name, source_id=str(p.get("paperId")), doi=_doi(ext.get("DOI")),
                           arxiv_id=arxiv, title=_ws(p["title"]) or "", authors=authors,
                           published=date_ or (str(year) if year else None), years=(year,) if year else (),
                           abstract=_ws(_str_or_none(p.get("abstract"), "semantic scholar abstract")))

    def parse_lookup(self, resp: HttpResponse) -> PaperRecord | None:
        if resp.status == 404:
            return None
        return self._item(_json(resp))

    def parse_search(self, resp: HttpResponse) -> list[PaperRecord]:
        data = _json(resp)
        _shape(isinstance(data, dict) and isinstance(data.get("data", []), list), "semantic scholar data")
        return [self._item(p) for p in data.get("data", [])]


# ------------------------------------------------------------------ arXiv (Atom)
def _parse_xml(text: str) -> ET.Element:
    # entity declarations are never needed for arXiv Atom and are the vehicle for entity-expansion attacks
    if re.search(r"<!(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise LiteratureError("unexpected response: XML with DOCTYPE/ENTITY declarations refused")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise LiteratureError(f"unexpected response (not XML): {e}") from e
    _shape(root.tag == f"{_ATOM}feed", "arXiv: not an Atom feed")
    return root


class ArxivSource(Source):
    name = "arxiv"
    lookup_kinds = frozenset({"arxiv"})
    base = "https://export.arxiv.org/api/query"

    def lookup_request(self, ident: Identifier) -> tuple[str, dict[str, str]]:
        return self.base, {"id_list": ident.value, "max_results": "1"}

    def search_request(self, query: str, limit: int) -> tuple[str, dict[str, str]]:
        words = re.findall(r"[A-Za-z0-9]+", query)
        if not words:
            raise LiteratureError(f"arXiv: no searchable words in {query!r}", infra=False)
        return self.base, {"search_query": " AND ".join(f"all:{w}" for w in words), "start": "0",
                           "max_results": str(limit)}

    @staticmethod
    def _is_error(entry: ET.Element) -> bool:
        return "/api/errors" in (entry.findtext(f"{_ATOM}id") or "")

    def _item(self, e: ET.Element) -> PaperRecord:
        m = _ABS_ID.search((e.findtext(f"{_ATOM}id") or "").strip())
        _shape(m is not None, "arXiv entry id")
        assert m is not None
        published = (e.findtext(f"{_ATOM}published") or "")[:10] or None
        updated = (e.findtext(f"{_ATOM}updated") or "")[:10] or None
        _shape(published is not None and published[:4].isdigit(), "arXiv published date")
        assert published is not None
        authors = tuple(" ".join(n.split()) for n in (a.findtext(f"{_ATOM}name") or "" for a in
                                                     e.findall(f"{_ATOM}author")) if n.strip())
        doi = (e.findtext(f"{_ARXIV_NS}doi") or "").strip().lower() or None
        return PaperRecord(source=self.name, source_id=m.group(1) + (m.group(2) or ""), arxiv_id=m.group(1), doi=doi,
                           title=_ws(e.findtext(f"{_ATOM}title")) or "", authors=authors, published=published,
                           updated=updated, years=(int(published[:4]),), abstract=_ws(e.findtext(f"{_ATOM}summary")),
                           url=f"https://arxiv.org/abs/{m.group(1)}")

    def parse_lookup(self, resp: HttpResponse) -> PaperRecord | None:
        _shape(resp.status == 200, f"arXiv HTTP {resp.status}")
        entries = _parse_xml(resp.text).findall(f"{_ATOM}entry")
        if not entries or self._is_error(entries[0]):
            return None  # arXiv itself says: no such id
        return self._item(entries[0])

    def parse_search(self, resp: HttpResponse) -> list[PaperRecord]:
        _shape(resp.status == 200, f"arXiv HTTP {resp.status}")
        entries = _parse_xml(resp.text).findall(f"{_ATOM}entry")
        if any(self._is_error(e) for e in entries):
            raise LiteratureError("arXiv rejected the query", infra=False)
        return [self._item(e) for e in entries]


# ------------------------------------------------------------------ doi.org (existence authority)
class DoiResolver:
    name = "doi.org"
    base = "https://doi.org/api/handles"

    def request(self, doi: str) -> tuple[str, dict[str, str]]:
        return f"{self.base}/{doi}", {}

    def parse(self, resp: HttpResponse) -> bool:
        """True: the DOI is registered. False: doi.org says it is not (responseCode 100). Else: error."""
        data = _json(resp)
        code = data.get("responseCode") if isinstance(data, dict) else None
        if resp.status == 200 and code in (1, 200):
            return True
        if resp.status == 404 and code == 100:
            return False
        raise LiteratureError(f"unexpected doi.org answer: HTTP {resp.status} responseCode={code!r}")


ALL_SOURCES: dict[str, Source] = {s.name: s for s in (ArxivSource(), SemanticScholarSource(), OpenAlexSource(),
                                                     CrossrefSource())}
