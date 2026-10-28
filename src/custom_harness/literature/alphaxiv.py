"""alphaXiv full-text (BM25) and semantic (embedding) search, plus the per-paper full text and overview: GATED OFF.

STATUS: terms of service UNKNOWN; needs owner permission (GOAL §17; PORTMAP §5). OpenResearch's MIT licence covers
its CODE, not the right to call alphaXiv's API or to store its full text in our snapshots. Until the owner records
written permission in the human-owned config (`literature.alphaxiv` in <data>/human/lab_compute.yaml:
`enabled: true`, `tos_status: permitted`, `permission_note: <where the permission is written>`, `recorded_by: <the
human>`, `api_base: <https URL>`, `web_base: <https URL>`), every request builder here raises ConnectorDisabled before
a URL is even formed (fail closed, like the gateway registry's "unknown" BYOS providers). No agent and no builder may
enable it; this module never does.

No alphaXiv endpoint is written in this code (owner decision 2026-10-28; tests/test_literature_fulltext.py
test_no_alphaxiv_endpoint_is_wired): the API and web base URLs exist only in the owner's permission record, so the
client cannot reach alphaXiv unless the owner both permits it and names where it lives.

Ported from OpenResearch (MIT, Copyright (c) 2026 alphaXiv; src/client.rs and src/config.rs at fe36f74):
- endpoint paths ({api} and {web} are the owner record's api_base and web_base):
  GET {api}/search/v2/paper/discover/{keyword|embedding}?q&prioritize (OR also sends publishedAfter/publishedBefore;
  we do not: the window is applied by our client to every record, so the request carries only the query),
  GET {web}/abs/<id>.md (extracted full text), GET {web}/overview/<id>.md (machine-readable report);
- hit shape {paperId, title, abstract, publicationDate, votes, snippets[{pageNumber, snippet}]};
- `versionless_id` ('2401.12345v2' -> '2401.12345').
Ours: hits become PaperRecords that pass through the same client, window, as-of, dedup, snapshot and retrieval-log
rules as every other source (literature/client.py, retrieval.py); nothing identifying is sent. Built, NOT
test-verified; no request has ever been made.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .http import HttpResponse, LiteratureError
from .records import Identifier, PaperRecord
from .sources import Getter, Source

TOS_STATUS = "unknown"  # the only value this code ever writes; the owner config may say "permitted"
_BASE = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?")  # an owner-named base URL: https, a host, no path
PRIORITIZE = frozenset({"relevance", "recency", "popular", "historical"})
_ARXIV_NEW = re.compile(r"\d{4}\.\d{4,5}")
_ARXIV_OLD = re.compile(r"[a-z-]+(?:\.[A-Z]{2})?/\d{7}")


class ConnectorDisabled(LiteratureError):
    def __init__(self, msg: str):
        super().__init__(msg, transient=False, infra=False)


@dataclass(frozen=True)
class AlphaXivPermission:
    """Proof that the OWNER enabled alphaXiv. Only `from_owner_config` makes one, and only from an explicit
    permission record; nothing in the code base calls it with a default."""

    tos_status: str
    permission_note: str
    recorded_by: str
    api_base: str  # the alphaXiv API base URL, named by the owner (never a default in code)
    web_base: str  # the alphaXiv web base URL (full text, overview, record links), named by the owner

    @staticmethod
    def from_owner_config(section: Mapping[str, Any]) -> AlphaXivPermission:
        if section.get("enabled") is not True:
            raise ConnectorDisabled("alphaXiv is OFF: terms of service unknown; needs owner permission")
        if section.get("tos_status") != "permitted":
            raise ConnectorDisabled(f"alphaXiv is OFF: tos_status is {section.get('tos_status', TOS_STATUS)!r}, "
                                    "not 'permitted' (owner permission required)")
        note, who = str(section.get("permission_note") or "").strip(), str(section.get("recorded_by") or "").strip()
        if not note or not who:
            raise ConnectorDisabled("alphaXiv is OFF: the owner's permission record (permission_note, recorded_by) "
                                    "is missing")
        api, web = (str(section.get(k) or "").strip().rstrip("/") for k in ("api_base", "web_base"))
        if not _BASE.fullmatch(api) or not _BASE.fullmatch(web):
            raise ConnectorDisabled("alphaXiv is OFF: the owner's record does not name its endpoints (api_base, "
                                    "web_base: https base URLs with no path)")
        return AlphaXivPermission("permitted", note, who, api, web)


def versionless_id(paper_id: str) -> str:
    """'2401.12345v2' -> '2401.12345' (port of OR `versionless_id`)."""
    i = paper_id.rfind("v")
    if i > 0 and paper_id[i + 1:].isdigit():
        return paper_id[:i]
    return paper_id


def _arxiv(paper_id: str) -> str | None:
    v = versionless_id(paper_id.strip())
    return v if _ARXIV_NEW.fullmatch(v) or _ARXIV_OLD.fullmatch(v) else None


class AlphaXivSource(Source):
    """Keyword (BM25 over full text) search. `strategy="embedding"` gives the semantic variant."""

    name = "alphaxiv"
    lookup_kinds: frozenset[str] = frozenset()

    def __init__(self, permission: AlphaXivPermission | None, *, strategy: str = "keyword",
                 prioritize: str = "relevance"):
        if strategy not in ("keyword", "embedding") or prioritize not in PRIORITIZE:
            raise ValueError("bad alphaXiv strategy or prioritize value")
        self.permission, self.strategy, self.prioritize = permission, strategy, prioritize
        if strategy == "embedding":
            self.name = "alphaxiv-embedding"

    def _gate(self) -> AlphaXivPermission:
        if not isinstance(self.permission, AlphaXivPermission) or self.permission.tos_status != "permitted":
            raise ConnectorDisabled("alphaXiv is OFF: terms of service unknown; needs owner permission")
        return self.permission

    def lookup_request(self, ident: Identifier) -> tuple[str, dict[str, str]]:
        raise LiteratureError("alphaXiv is not a lookup authority", infra=False)

    def search_request(self, query: str, limit: int) -> tuple[str, dict[str, str]]:
        api = self._gate().api_base
        return f"{api}/search/v2/paper/discover/{self.strategy}", {"q": query, "prioritize": self.prioritize}

    def _item(self, h: Any, web: str) -> PaperRecord:
        if not isinstance(h, dict) or not isinstance(h.get("paperId"), str) or not isinstance(h.get("title"), str):
            raise LiteratureError("unexpected response shape: alphaXiv hit")
        snippets = tuple(str(s["snippet"]) for s in h.get("snippets") or []
                         if isinstance(s, dict) and isinstance(s.get("snippet"), str))
        date_ = h.get("publicationDate")
        published = date_[:10] if isinstance(date_, str) and date_ else None
        abstract = h.get("abstract") if isinstance(h.get("abstract"), str) else None
        return PaperRecord(source=self.name, source_id=h["paperId"], arxiv_id=_arxiv(h["paperId"]),
                           title=" ".join(h["title"].split()), published=published,
                           abstract=" ".join(abstract.split()) if abstract else None, snippets=snippets,
                           url=f"{web}/abs/{versionless_id(h['paperId'])}")

    def parse_lookup(self, resp: HttpResponse) -> PaperRecord | None:
        raise LiteratureError("alphaXiv is not a lookup authority", infra=False)

    def parse_search(self, resp: HttpResponse) -> list[PaperRecord]:
        web = self._gate().web_base
        try:
            data = json.loads(resp.text)
        except ValueError as e:
            raise LiteratureError("unexpected response (not JSON): alphaXiv search") from e
        if not isinstance(data, list):
            raise LiteratureError("unexpected response shape: alphaXiv search is not a list")
        return [self._item(h, web) for h in data]

    # --- per-paper text (Lab reading only; never a citation authority)
    def full_text_request(self, paper_id: str, kind: str = "abs") -> tuple[str, dict[str, str]]:
        web = self._gate().web_base
        if kind not in ("abs", "overview"):
            raise ValueError("kind is 'abs' (full text) or 'overview' (report)")
        pid = _arxiv(paper_id)
        if pid is None:
            raise ValueError(f"not an arXiv id: {paper_id!r}")
        return f"{web}/{kind}/{pid}.md", {}

    def fetch_text(self, get: Getter, paper_id: str, kind: str = "abs") -> tuple[str | None, HttpResponse]:
        resp = get(*self.full_text_request(paper_id, kind))
        return (None if resp.status in (404, 410) else resp.text), resp
