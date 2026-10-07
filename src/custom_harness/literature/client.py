"""One interface over the literature sources: ID resolution (three-valued) and search, with fallback across
sources, query splitting, and as-of-T filtering (GOAL §5 L4.3, §6.4).

ID resolution is three-valued and fails closed:
  resolves          a source returned a record carrying exactly the requested identifier (and, as of T, the record
                    was provably published by T);
  does_not_resolve  the AUTHORITY for the identifier says it does not exist (doi.org for DOIs, arXiv for arXiv
                    ids), the identifier is malformed, or the paper was provably published after T;
  lookup_failed     anything else (outages, unexpected answers, a DOI that exists but no index has metadata for
                    it, a record whose date cannot be placed before T, a request missing from a frozen snapshot).
An index's 404 alone is never "does not resolve": Crossref does not hold DataCite DOIs (e.g. arXiv's), and
OpenAlex/Semantic Scholar do not cover everything.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .http import HttpxTransport, LiteratureError, PacedTransport, Pacer, Transport, fetch
from .normalize import title_key
from .records import LookupResult, PaperRecord, earliest_date, latest_date, parse_identifier, visible_as_of
from .snapshot import OfflineTransport, RecordingTransport, SnapshotStore
from .sources import ALL_SOURCES, PACING, DoiResolver, Source

DOI_ORDER = ("crossref", "openalex", "semanticscholar")
ARXIV_ORDER = ("arxiv", "semanticscholar")
SEARCH_ORDER = ("openalex", "crossref", "semanticscholar", "arxiv")
MAX_SUBQUERIES = 6


class SearchResult(BaseModel):
    query: str
    subqueries: list[str]
    records: list[PaperRecord]
    errors: list[dict[str, str]]
    excluded_after_as_of: int
    complete: bool  # False if any subquery got no answer from any source: the result list is NOT exhaustive
    as_of: str | None = None


def split_query(query: str, max_terms: int = 8, max_parts: int = MAX_SUBQUERIES) -> list[str]:
    """Split a compound query on ';', newlines and ' OR '; chunk long parts into groups of `max_terms` words.
    Raises ValueError if nothing is left or the query is too broad (> max_parts subqueries)."""
    out: list[str] = []
    for part in re.split(r"\s*(?:;|\n|\bOR\b)\s*", query or ""):
        words = part.split()
        for i in range(0, len(words), max_terms):
            q = " ".join(words[i:i + max_terms])
            if q and q not in out:
                out.append(q)
    if not out:
        raise ValueError("empty query")
    if len(out) > max_parts:
        raise ValueError(f"query too broad: {len(out)} subqueries > {max_parts}; narrow it")
    return out


def _dedup_key(rec: PaperRecord) -> str:
    ids = sorted(rec.ids())
    return ids[0] if ids else "title:" + " ".join(title_key(rec.title))


class LiteratureClient:
    def __init__(self, transport: Transport, *, as_of: date | None = None, sources: dict[str, Source] | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 4):
        snap_as_of = getattr(transport, "as_of", None)
        if snap_as_of is not None:
            if as_of is not None and as_of > snap_as_of:
                raise ValueError(f"as_of {as_of} is later than the frozen snapshot's date {snap_as_of}")
            as_of = as_of or snap_as_of
        self.transport, self.as_of = transport, as_of
        self.sources = dict(sources or ALL_SOURCES)
        self.resolver = DoiResolver()
        self.sleep, self.max_attempts = sleep, max_attempts

    def _get(self, url: str, params: dict[str, str]) -> Any:
        return fetch(self.transport, url, params, sleep=self.sleep, max_attempts=self.max_attempts)

    # ------------------------------------------------------------------ lookup
    def lookup(self, raw: str) -> LookupResult:
        try:
            ident = parse_identifier(raw)
        except ValueError as e:
            return LookupResult(identifier=str(raw), status="does_not_resolve", reason=f"malformed identifier: {e}")
        key = str(ident)
        attempts: list[dict[str, Any]] = []

        def result(status: Any, reason: str, record: PaperRecord | None = None) -> LookupResult:
            return LookupResult(identifier=key, status=status, reason=reason, record=record, attempts=attempts)

        record: PaperRecord | None = None
        arxiv_says_absent = False
        for name in DOI_ORDER if ident.kind == "doi" else ARXIV_ORDER:
            src = self.sources[name]
            try:
                resp = self._get(*src.lookup_request(ident))
                rec = src.parse_lookup(resp)
            except LiteratureError as e:
                attempts.append({"source": name, "outcome": "error", "detail": str(e)[:300]})
                continue
            if rec is None:
                attempts.append({"source": name, "outcome": "not_found", "envelope": resp.envelope})
                if name == "arxiv":  # the authority for arXiv ids
                    arxiv_says_absent = True
                    break
                continue
            if key not in rec.ids():
                attempts.append({"source": name, "outcome": "mismatch", "envelope": resp.envelope,
                                 "detail": f"asked for {key}, got {sorted(rec.ids())}"})
                continue
            attempts.append({"source": name, "outcome": "found", "envelope": resp.envelope})
            record = rec.model_copy(update={"envelope": resp.envelope})
            break

        if record is not None:
            if self.as_of is not None:
                first, last = earliest_date(record.published), latest_date(record.published)
                if first is not None and first > self.as_of:
                    return result("does_not_resolve", f"published {record.published}, after the as-of date "
                                                      f"{self.as_of}", record)
                if last is None or last > self.as_of:
                    return result("lookup_failed", f"publication date {record.published!r} cannot be placed on or "
                                                   f"before the as-of date {self.as_of}", record)
            return result("resolves", f"{record.source} returned a record with this identifier", record)
        if ident.kind == "arxiv":
            if arxiv_says_absent:
                return result("does_not_resolve", "arXiv has no paper with this id")
            return result("lookup_failed", "arXiv could not be queried and no other source returned this id")
        exists = self._doi_exists(ident.value, attempts)
        if exists is False:
            return result("does_not_resolve", "doi.org: no such DOI is registered")
        if exists is True:
            return result("lookup_failed", "the DOI exists (doi.org) but no index returned metadata for it")
        return result("lookup_failed", "no source returned this DOI and doi.org could not be queried")

    def _doi_exists(self, doi: str, attempts: list[dict[str, Any]]) -> bool | None:
        try:
            resp = self._get(*self.resolver.request(doi))
            exists = self.resolver.parse(resp)
        except LiteratureError as e:
            attempts.append({"source": "doi.org", "outcome": "error", "detail": str(e)[:300]})
            return None
        attempts.append({"source": "doi.org", "outcome": "exists" if exists else "not_found",
                         "envelope": resp.envelope})
        return exists

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 10, sources: Sequence[str] | None = None) -> SearchResult:
        subs = split_query(query)
        order = list(sources or SEARCH_ORDER)
        records: list[PaperRecord] = []
        errors: list[dict[str, str]] = []
        seen: set[str] = set()
        excluded, complete = 0, True
        for sub in subs:
            got: list[PaperRecord] | None = None
            for name in order:
                src = self.sources[name]
                try:
                    resp = self._get(*src.search_request(sub, limit))
                    got = [r.model_copy(update={"envelope": resp.envelope}) for r in src.parse_search(resp)]
                except LiteratureError as e:
                    errors.append({"subquery": sub, "source": name, "error": str(e)[:300]})
                    continue
                break
            if got is None:
                complete = False
                continue
            for rec in got:
                k = _dedup_key(rec)
                if k in seen:
                    continue
                seen.add(k)
                if self.as_of is not None and not visible_as_of(rec, self.as_of):
                    excluded += 1
                    continue
                records.append(rec)
        return SearchResult(query=query, subqueries=subs, records=records, errors=errors,
                            excluded_after_as_of=excluded, complete=complete,
                            as_of=self.as_of.isoformat() if self.as_of else None)


# ------------------------------------------------------------------ factories
def snapshot_store(root: Path) -> SnapshotStore:
    return SnapshotStore(root)


def live_client(store: SnapshotStore, *, use_cache: bool = True, as_of: date | None = None) -> LiteratureClient:
    """Network client; every final answer is recorded into `store` (freeze it later for offline replay)."""
    transport = RecordingTransport(PacedTransport(HttpxTransport(), Pacer(PACING)), store, use_cache=use_cache)
    return LiteratureClient(transport, as_of=as_of)


def offline_client(store: SnapshotStore, snapshot_id: str, *, as_of: date | None = None) -> LiteratureClient:
    """No network: answers only from the frozen snapshot, as of its date T (or an earlier one)."""
    return LiteratureClient(OfflineTransport(store, snapshot_id), as_of=as_of)
