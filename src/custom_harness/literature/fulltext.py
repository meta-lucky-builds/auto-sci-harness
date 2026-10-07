"""Full-text keyword retrieval with match snippets (GOAL §5 L4.3: "full-text keyword search where the terms of
service allow it"; OR uses alphaXiv).

Source: the Europe PMC REST API, open-access subset (search, then `/{PMCID}/fullTextXML`, JATS XML).
Terms (reviewed 2026-10-07, see the report): EMBL-EBI allows automated access at a load that does not stop it
serving others, expects attribution, and puts no extra restriction on the content beyond each article's own
licence. We therefore record every article's licence; a Reproducibility Bundle may include a full text only if its
licence allows redistribution (not enforced here: there is no bundle code in this module). alphaXiv's terms forbid
automated access ("data mining, robots, scraping") and copying, so it is NOT wired, although its API returns
snippets. Endpoint shapes and query fields below are INFERRED from Europe PMC's public documentation as quoted in
EBI material; the Europe PMC pages themselves refused our reader (HTTP 403) and no live request was made.

Snippets are computed locally from the stored full text (Europe PMC returns none), so every snippet can be
re-derived from a stored envelope. The text is title + abstract + body; the reference list and back matter are
never "what the paper says". The text's date is the LATEST date the article gives (e.g. its PMC release), so a
text that became available after T is never used as of T (fail closed; a silent later correction that keeps the
dates is a residual risk)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .http import HttpResponse, LiteratureError
from .records import Identifier, PaperRecord, latest_date
from .sources import Getter, Source, _doi, _json, _parse_xml, _shape, _ws, _ymd

EPMC_REST = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EPMC_SEARCH = f"{EPMC_REST}/search"
_PMCID = re.compile(r"^PMC[1-9][0-9]{0,9}$")
_XLINK = "{http://www.w3.org/1999/xlink}"
_OPERATORS = {"and", "or", "not"}

SNIPPET_WIDTH = 12   # words of context on each side of a hit
MAX_SNIPPETS = 3


@dataclass(frozen=True)
class FullText:
    text: str
    ids: frozenset[str]          # doi:/pmid: identifiers the article itself states
    pmcid: str | None
    title: str
    license: str | None
    version_date: str | None     # the latest date the article gives (ISO, partial allowed)


def _itertext(el: Any) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def parse_jats(resp: HttpResponse) -> FullText:
    _shape(resp.status == 200, f"Europe PMC full text HTTP {resp.status}")
    root = _parse_xml(resp.text, "article")
    meta = root.find("front/article-meta")
    _shape(meta is not None, "JATS article-meta")
    assert meta is not None
    ids: set[str] = set()
    pmcid = None
    for a in meta.findall("article-id"):
        kind, val = a.get("pub-id-type"), (a.text or "").strip()
        if kind == "doi" and val:
            d = _doi(val)
            if d:
                ids.add(f"doi:{d}")
        elif kind == "pmid" and re.fullmatch(r"[1-9][0-9]{0,8}", val):
            ids.add(f"pmid:{val}")
        elif kind in ("pmc", "pmcid") and val:
            pmcid = val if val.startswith("PMC") else f"PMC{val}"
    title = _itertext(meta.find("title-group/article-title"))
    dates = [_ymd(d.findtext("year"), d.findtext("month"), d.findtext("day"))
             for d in [*meta.findall("pub-date"), *meta.findall("history/date")]]
    known = [d for d in dates if d and latest_date(d) is not None]
    version = max(known, key=lambda d: latest_date(d) or date.min) if known else None
    lic = meta.find("permissions/license")
    license_ = None
    if lic is not None:
        license_ = lic.get(f"{_XLINK}href") or lic.get("license-type") or (_itertext(lic)[:200] or None)
    parts = [title, _itertext(meta.find("abstract")), _itertext(root.find("body"))]
    text = " ".join(p for p in parts if p)
    return FullText(text=text, ids=frozenset(ids), pmcid=pmcid, title=title, license=license_, version_date=version)


def match_snippets(text: str, query: str, *, width: int = SNIPPET_WIDTH, max_snippets: int = MAX_SNIPPETS
                   ) -> tuple[str, ...]:
    """Windows of `width` words around every whole-word hit of a query word (case-insensitive); overlapping or
    adjacent windows merge; at most `max_snippets`, in text order."""
    terms = {w for w in (re.sub(r"\W+", "", t).lower() for t in query.split()) if w and w not in _OPERATORS}
    words = text.split()
    norm = [re.sub(r"\W+", "", w).lower() for w in words]
    spans: list[list[int]] = []
    for i, w in enumerate(norm):
        if w in terms:
            s, e = max(0, i - width), min(len(words) - 1, i + width)
            if spans and s <= spans[-1][1] + 1:
                spans[-1][1] = max(spans[-1][1], e)
            else:
                spans.append([s, e])
    out = []
    for s, e in spans[:max_snippets]:
        out.append(("… " if s > 0 else "") + " ".join(words[s:e + 1]) + (" …" if e < len(words) - 1 else ""))
    return tuple(out)


def _author(a: Any) -> str | None:
    if not isinstance(a, dict):
        return None
    first, last = a.get("firstName"), a.get("lastName")
    if isinstance(first, str) and isinstance(last, str) and first and last:
        return f"{first} {last}"
    full = a.get("fullName")
    return full if isinstance(full, str) and full else None


class EuropePMCSource(Source):
    """Search over Europe PMC (restricted to open-access articles with full text). Not a lookup source: identity
    is established by the existing lookup chain; this source only adds text."""

    name = "europepmc"
    lookup_kinds = frozenset()

    def search_request(self, query: str, limit: int) -> tuple[str, dict[str, str]]:
        return EPMC_SEARCH, {"query": f"({query}) AND OPEN_ACCESS:y AND HAS_FT:y", "resultType": "core",
                             "format": "json", "pageSize": str(limit)}

    def _item(self, m: Any) -> PaperRecord:
        _shape(isinstance(m, dict) and isinstance(m.get("title"), str) and isinstance(m.get("id"), str),
               "Europe PMC result")
        pmcid = m.get("pmcid")
        _shape(pmcid is None or (isinstance(pmcid, str) and bool(_PMCID.match(pmcid))), "Europe PMC pmcid")
        pmid = m.get("pmid")
        _shape(pmid is None or (isinstance(pmid, str) and bool(re.fullmatch(r"[1-9][0-9]{0,8}", pmid))),
               "Europe PMC pmid")
        year = m.get("pubYear")
        _shape(year is None or (isinstance(year, str) and bool(re.fullmatch(r"[0-9]{4}", year))), "Europe PMC pubYear")
        date_ = m.get("firstPublicationDate")
        _shape(date_ is None or isinstance(date_, str), "Europe PMC firstPublicationDate")
        lst = (m.get("authorList") or {}).get("author") if isinstance(m.get("authorList"), dict) else None
        if isinstance(lst, list) and lst:
            authors = tuple(a for a in (_author(x) for x in lst) if a)
        else:
            s = m.get("authorString")
            authors = tuple(x.strip().rstrip(".") for x in s.split(",") if x.strip()) if isinstance(s, str) else ()
        return PaperRecord(source=self.name, source_id=pmcid or m["id"], doi=_doi(m.get("doi")), pmid=pmid,
                           title=_ws(m["title"]) or "", authors=authors, published=date_ or year,
                           years=(int(year),) if year else (), abstract=_ws(m.get("abstractText")),
                           url=f"https://europepmc.org/article/{m.get('source', 'MED')}/{m['id']}")

    def parse_search(self, resp: HttpResponse) -> list[PaperRecord]:
        data = _json(resp)
        _shape(isinstance(data, dict) and isinstance(data.get("resultList"), dict)
               and isinstance(data["resultList"].get("result"), list), "Europe PMC resultList")
        return [self._item(m) for m in data["resultList"]["result"]]

    # -- full text
    @staticmethod
    def full_text_request(pmcid: str) -> tuple[str, dict[str, str]]:
        if not _PMCID.match(pmcid):
            raise LiteratureError(f"not a PMCID: {pmcid!r}", infra=False)
        return f"{EPMC_REST}/{pmcid}/fullTextXML", {}

    def find_pmcid(self, get: Getter, ident: Identifier) -> tuple[str | None, HttpResponse]:
        """The PMCID of the open-access article with exactly this DOI / PMID (None: no OA full text known)."""
        if ident.kind == "doi":
            q = f'DOI:"{ident.value}"'
        elif ident.kind == "pmid":
            q = f"EXT_ID:{ident.value} AND SRC:MED"
        else:
            raise LiteratureError(f"Europe PMC full text: no lookup by {ident.kind}", infra=False)
        resp = get(EPMC_SEARCH, {"query": q, "resultType": "core", "format": "json", "pageSize": "1"})
        for rec in self.parse_search(resp):
            if str(ident) in rec.ids() and rec.source_id.startswith("PMC"):
                return rec.source_id, resp
        return None, resp

    def fetch_full_text(self, get: Getter, pmcid: str) -> tuple[FullText, HttpResponse]:
        resp = get(*self.full_text_request(pmcid))
        if resp.status in (404, 410):
            raise LiteratureError(f"Europe PMC has no full text for {pmcid}", infra=False)
        return parse_jats(resp), resp


FULL_TEXT_SOURCES = ("europepmc",)


def same_paper(rec: PaperRecord, ft: FullText) -> bool:
    """The full text states at least one of the record's ids and contradicts none of them."""
    rec_ids = rec.ids()
    if not rec_ids & ft.ids:
        return False
    for kind in ("doi", "pmid"):
        a = {i for i in rec_ids if i.startswith(kind + ":")}
        b = {i for i in ft.ids if i.startswith(kind + ":")}
        if a and b and a != b:
            return False
    return True

