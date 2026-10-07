"""Citation check for K0 / audit I3 (GOAL §4.3, §5 L4.3). Mechanical, LLM-free, fail closed.

A citation is VERIFIED only if every check passes:
  id       resolves (see client.py: three-valued; a lookup failure is never verified),
  title    matches after normalisation (normalize.titles_match),
  year     is one the record itself gives,
  authors  (optional) the claimed authors are an in-order prefix of the record's authors,
  quotes   every quoted span occurs verbatim (normalised, whole tokens, contiguous) in the stored abstract/full text
           that was available as of T,
  retrieval the cited paper appeared in a logged SEARCH result of the same session (retrieval.RetrievalLog);
           without that record (or with no log given) the citation is at best unverified.
FAILED if anything is positively wrong (id does not resolve, a mismatch, an unsupported quote).
UNVERIFIED otherwise (lookup failed, a required field missing, no stored text for a quote). I3 passes only if
every citation is verified.

This module imports no network code: it takes any resolver with `lookup(raw) -> LookupResult` and `as_of`.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .normalize import authors_match, quote_support, titles_match
from .records import LookupResult, full_text_as_of, text_as_of


class Resolver(Protocol):
    as_of: date | None

    def lookup(self, raw: str) -> LookupResult: ...


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    id: str | None = None
    title: str | None = None
    year: int | None = None
    authors: list[str] = []
    quotes: list[str] = []


class CitationResult(BaseModel):
    key: str
    status: Literal["verified", "failed", "unverified"]
    id: dict[str, Any]
    title: dict[str, Any]
    year: dict[str, Any]
    authors: dict[str, Any]
    quotes: list[dict[str, Any]]
    retrieval: dict[str, Any]
    reasons: list[str]
    provenance: dict[str, Any]


_BAD = {"does_not_resolve", "mismatch", "unsupported"}
_UNKNOWN = {"lookup_failed", "missing", "unchecked", "not_retrieved"}


def check_citation(c: Citation, resolver: Resolver, retrieved_ids: Collection[str] | None = None) -> CitationResult:
    as_of = getattr(resolver, "as_of", None)
    snapshot = getattr(getattr(resolver, "transport", None), "snapshot_id", None)
    unchecked = {"status": "unchecked"}
    title: dict[str, Any] = {**unchecked, "claimed": c.title}
    year: dict[str, Any] = {**unchecked, "claimed": c.year}
    authors: dict[str, Any] = {**unchecked, "claimed": list(c.authors)}
    texts: list[str | None] = []
    text_note = ""
    ft_note = ""
    envelope = None
    ft_envelope = None
    found_ids: set[str] = set()

    if not c.id:
        ident: dict[str, Any] = {"status": "missing", "value": None, "reason": "no DOI or arXiv identifier given"}
    else:
        lr = resolver.lookup(c.id)
        ident = {"status": lr.status, "value": c.id, "normalised": lr.identifier, "reason": lr.reason,
                 "source": lr.record.source if lr.record else None, "attempts": lr.attempts}
        rec = lr.record if lr.status == "resolves" else None
        found_ids = {lr.identifier} | (rec.ids() if rec is not None else set())
        if rec is not None:
            envelope = rec.envelope
            if c.title is None:
                title = {"status": "missing", "claimed": None, "found": rec.title}
            else:
                title = {"status": "match" if titles_match(c.title, rec.title) else "mismatch",
                         "claimed": c.title, "found": rec.title}
            if c.year is None:
                year = {"status": "missing", "claimed": None, "found": list(rec.years)}
            elif not rec.years:
                year = {"status": "unchecked", "claimed": c.year, "found": []}
            else:
                year = {"status": "match" if c.year in rec.years else "mismatch", "claimed": c.year,
                        "found": list(rec.years)}
            if not c.authors:
                authors = {"status": "not_claimed", "claimed": [], "found": list(rec.authors)}
            elif not rec.authors:
                authors = {"status": "unchecked", "claimed": list(c.authors), "found": []}
            else:
                authors = {"status": "match" if authors_match(c.authors, rec.authors) else "mismatch",
                           "claimed": list(c.authors), "found": list(rec.authors)}
            if as_of is not None and not text_as_of(rec, as_of):
                text_note = f"stored text was revised after the as-of date {as_of} (updated {rec.updated})"
            else:
                texts = [rec.abstract]
                if rec.full_text_date is not None and not full_text_as_of(rec, as_of):
                    ft_note = (f"the full text is dated {rec.full_text_date}, after the as-of date {as_of}: "
                               f"not usable, and the quote is not in the abstract")
                elif rec.full_text and full_text_as_of(rec, as_of):
                    texts.append(rec.full_text)
                    ft_envelope = rec.full_text_envelope
                elif rec.full_text:
                    ft_note = (f"the full text has no date to place it on or before the as-of date {as_of}: "
                               f"not usable, and the quote is not in the abstract")

    quotes = []
    for q in c.quotes:
        status, reason = quote_support(q, texts)
        if status == "unchecked" and text_note:
            reason = text_note
        # a full text exists but cannot be used as of T: absence from the abstract proves nothing (three-valued)
        if status == "unsupported" and ft_note and quote_support(q, [])[0] == "unchecked":
            status, reason = "unchecked", ft_note
        quotes.append({"quote": q, "status": status, "reason": reason})

    if retrieved_ids is None:
        retrieval: dict[str, Any] = {"status": "unchecked", "reason": "no retrieval log of this session was given"}
    elif not found_ids:
        retrieval = {"status": "unchecked", "reason": "no identifier to look for in the retrieval log"}
    elif found_ids & set(retrieved_ids):
        retrieval = {"status": "match", "ids": sorted(found_ids & set(retrieved_ids))}
    else:
        retrieval = {"status": "not_retrieved",
                     "reason": "no search of this session returned this paper (it did not come from retrieval)"}

    parts = [("id", ident), ("title", title), ("year", year), ("authors", authors), ("retrieval", retrieval)]
    parts += [(f"quote {i + 1}", q) for i, q in enumerate(quotes)]
    reasons = [f"{name}: {p['status']}" + (f" ({p['reason']})" if p.get("reason") else "")
               for name, p in parts if p["status"] in _BAD | _UNKNOWN]
    statuses = {p["status"] for _, p in parts}
    overall: Literal["verified", "failed", "unverified"]
    overall = "failed" if statuses & _BAD else "unverified" if statuses & _UNKNOWN else "verified"
    return CitationResult(key=c.key, status=overall, id=ident, title=title, year=year, authors=authors,
                          quotes=quotes, retrieval=retrieval, reasons=reasons,
                          provenance={"envelope": envelope, "full_text_envelope": ft_envelope, "snapshot": snapshot,
                                      "as_of": as_of.isoformat() if as_of else None})


def check_citations(citations: Sequence[Citation], resolver: Resolver,
                    retrieved_ids: Collection[str] | None = None) -> list[CitationResult]:
    return [check_citation(c, resolver, retrieved_ids) for c in citations]


def i3_verdict(results: Sequence[CitationResult]) -> dict[str, Any]:
    """Audit I3: True iff there is at least one citation and every one is verified; 'n/a' if there are none;
    False otherwise (failed AND unverified both fail I3: fail closed)."""
    counts = {"verified": 0, "failed": 0, "unverified": 0}
    problems = []
    for r in results:
        counts[r.status] += 1
        if r.status != "verified":
            problems.append(f"citation {r.key}: {r.status}: " + "; ".join(r.reasons))
    verdict: bool | str = "n/a" if not results else counts["verified"] == len(results)
    return {"I3": verdict, "counts": counts, "problems": problems, "results": [r.model_dump() for r in results]}
