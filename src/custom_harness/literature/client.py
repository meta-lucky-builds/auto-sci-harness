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
from .records import LookupResult, PaperRecord, earliest_date, in_window, latest_date, parse_identifier
from .snapshot import OfflineTransport, RecordingTransport, SnapshotStore
from .sources import ALL_SOURCES, PACING, DoiResolver, Source

DOI_ORDER = ("crossref", "openalex", "semanticscholar")
BIORXIV_DOI_ORDER = ("biorxiv", "crossref", "openalex", "semanticscholar")
ARXIV_ORDER = ("arxiv", "semanticscholar")
PMID_ORDER = ("pubmed",)
AUTHORITY = {"arxiv": "arxiv", "pmid": "pubmed"}  # DOIs: doi.org (DoiResolver)
SEARCH_ORDER = ("openalex", "crossref", "semanticscholar", "arxiv")  # pubmed / biorxiv on request


def _lookup_order(kind: str, value: str) -> tuple[str, ...]:
    if kind == "arxiv":
        return ARXIV_ORDER
    if kind == "pmid":
        return PMID_ORDER
    return BIORXIV_DOI_ORDER if value.startswith("10.1101/") else DOI_ORDER
MAX_SUBQUERIES = 6


class SearchResult(BaseModel):
    query: str
    subqueries: list[str]
    records: list[PaperRecord]
    errors: list[dict[str, str]]
    excluded_after_as_of: int      # outside the window's end (as of T, or published_to)
    complete: bool  # False if any subquery got no answer from any source: the result list is NOT exhaustive
    as_of: str | None = None
    excluded_before_window: int = 0
    window: tuple[str | None, str | None] = (None, None)  # effective publication window [from, to]
    retrievals: list[dict[str, Any]] = []  # per answered subquery: {subquery, source, envelope} (provenance)


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


class Deduper:
    """A record is a duplicate if ANY of its keys was seen: source id, then DOI / arXiv id / PMID, then the
    normalised title. All keys of every record offered (duplicates included) are remembered, so chains merge."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    @staticmethod
    def keys(rec: PaperRecord) -> set[str]:
        keys = {f"src:{rec.source}:{rec.source_id}"} | rec.ids()
        tk = title_key(rec.title)
        if tk:
            keys.add("title:" + " ".join(tk))
        return keys

    def add(self, rec: PaperRecord) -> bool:
        keys = self.keys(rec)
        new = not (keys & self.seen)
        self.seen |= keys
        return new


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
        authority_says_absent = False
        authority = AUTHORITY.get(ident.kind)
        for name in _lookup_order(ident.kind, ident.value):
            src = self.sources[name]
            try:
                rec, resp = src.lookup(self._get, ident)
            except LiteratureError as e:
                attempts.append({"source": name, "outcome": "error", "detail": str(e)[:300]})
                continue
            if rec is None:
                attempts.append({"source": name, "outcome": "not_found", "envelope": resp.envelope})
                if name == authority:  # arXiv for arXiv ids, PubMed for PMIDs
                    authority_says_absent = True
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
        if authority is not None:
            label = {"arxiv": "arXiv", "pubmed": "PubMed"}[authority]
            if authority_says_absent:
                return result("does_not_resolve", f"{label} has no paper with this id")
            return result("lookup_failed", f"{label} could not be queried and no other source returned this id")
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
    def window(self, published_from: date | None = None, published_to: date | None = None
               ) -> tuple[date | None, date | None]:
        """The effective publication window. It can be narrowed but never extended past `as_of` (T)."""
        end = published_to
        if self.as_of is not None:
            if end is not None and end > self.as_of:
                raise ValueError(f"published_to {end} is later than the as-of date {self.as_of}: windows never widen")
            end = end or self.as_of
        if published_from is not None and end is not None and published_from > end:
            raise ValueError(f"empty publication window: {published_from} > {end}")
        return published_from, end

    def search(self, query: str, limit: int = 10, sources: Sequence[str] | None = None, *,
               published_from: date | None = None, published_to: date | None = None) -> SearchResult:
        start, end = self.window(published_from, published_to)
        subs = split_query(query)
        order = list(sources or SEARCH_ORDER)
        records: list[PaperRecord] = []
        errors: list[dict[str, str]] = []
        retrievals: list[dict[str, Any]] = []
        dedup = Deduper()
        excluded, before, complete = 0, 0, True
        for sub in subs:
            got: list[PaperRecord] | None = None
            for name in order:
                src = self.sources[name]
                try:
                    recs, resp = src.search(self._get, sub, limit)
                except LiteratureError as e:
                    errors.append({"subquery": sub, "source": name, "error": str(e)[:300]})
                    continue
                got = [r.model_copy(update={"envelope": resp.envelope}) for r in recs]
                retrievals.append({"subquery": sub, "source": name, "envelope": resp.envelope})
                break
            if got is None:
                complete = False
                continue
            for rec in got:
                if not dedup.add(rec):
                    continue
                if not in_window(rec, None, end):
                    excluded += 1
                    continue
                if not in_window(rec, start, None):
                    before += 1
                    continue
                records.append(rec)
        iso = (start.isoformat() if start else None, end.isoformat() if end else None)
        return SearchResult(query=query, subqueries=subs, records=records, errors=errors,
                            excluded_after_as_of=excluded, excluded_before_window=before, complete=complete,
                            as_of=self.as_of.isoformat() if self.as_of else None, window=iso, retrievals=retrievals)


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
