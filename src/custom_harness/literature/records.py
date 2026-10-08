"""Paper records, identifiers and publication dates (GOAL §5 L4.3, §6.4). Pure, no I/O."""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

_DOI = re.compile(r"^10\.[0-9]{4,9}/[!-~]+$")  # printable ASCII only: no homoglyphs, no spaces
_ARXIV_NEW = re.compile(r"^([0-9]{4}\.[0-9]{4,5})(v[0-9]+)?$")
_ARXIV_OLD = re.compile(r"^([a-z][a-z\-]*(?:\.[A-Z]{2})?/[0-9]{7})(v[0-9]+)?$")
_DOI_PREFIX = re.compile(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
_PMID = re.compile(r"^(?:pmid:?\s*|https?://pubmed\.ncbi\.nlm\.nih\.gov/)([1-9][0-9]{0,8})/?$", re.IGNORECASE)
_ARXIV_PREFIX = re.compile(r"^(?:arxiv:|https?://(?:export\.)?arxiv\.org/abs/)", re.IGNORECASE)


class Identifier(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["doi", "arxiv", "pmid"]
    value: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def parse_identifier(raw: str) -> Identifier:
    """Normalise a DOI, arXiv id or PMID (ASCII digits only); raise ValueError if it is not one
    (fail closed: a malformed id never resolves)."""
    s = (raw or "").strip()
    if _DOI_PREFIX.match(s) or s.startswith("10."):
        v = _DOI_PREFIX.sub("", s).strip()
        if _DOI.match(v):
            return Identifier(kind="doi", value=v.lower())
        raise ValueError(f"malformed DOI: {raw!r}")
    if re.match(r"^(?:pmid|https?://pubmed)", s, re.IGNORECASE):
        m = _PMID.match(s)
        if m:
            return Identifier(kind="pmid", value=m.group(1))
        raise ValueError(f"malformed PMID: {raw!r}")
    v = _ARXIV_PREFIX.sub("", s).strip()
    for pat in (_ARXIV_NEW, _ARXIV_OLD):
        m = pat.match(v)
        if m:
            return Identifier(kind="arxiv", value=m.group(1))
    raise ValueError(f"not a DOI, arXiv id or PMID: {raw!r}")


class PaperRecord(BaseModel):
    """One paper as one source describes it. `years` = every year the record itself gives (issued, online,
    print); `published` / `updated` are ISO dates of precision year, month or day."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    source_id: str
    title: str
    doi: str | None = None
    arxiv_id: str | None = None
    pmid: str | None = None
    authors: tuple[str, ...] = ()
    published: str | None = None
    updated: str | None = None
    years: tuple[int, ...] = ()
    abstract: str | None = None
    full_text: str | None = None
    url: str | None = None
    envelope: str | None = None  # digest of the stored response this record was parsed from (provenance)
    # full text (fulltext.py): where it came from, its stored response, licence, the latest date the article gives
    full_text_source: str | None = None
    full_text_envelope: str | None = None
    full_text_license: str | None = None
    full_text_date: str | None = None
    snippets: tuple[str, ...] = ()  # match snippets of the search that returned this record (cut from full_text)

    def ids(self) -> set[str]:
        out = set()
        if self.doi:
            out.add(f"doi:{self.doi.lower()}")
        if self.arxiv_id:
            out.add(f"arxiv:{self.arxiv_id}")
        if self.pmid:
            out.add(f"pmid:{self.pmid}")
        return out


class LookupResult(BaseModel):
    """Three-valued ID resolution (see client.py for the exact semantics)."""

    identifier: str
    status: Literal["resolves", "does_not_resolve", "lookup_failed"]
    reason: str
    record: PaperRecord | None = None
    attempts: list[dict[str, Any]] = []


_DATE = re.compile(r"^([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?(?:[T ].*)?$")


def latest_date(s: str | None) -> date | None:
    """The LATEST calendar day the (possibly partial) date allows: '2024' -> 2024-12-31. None if unparseable."""
    m = _DATE.match(s or "")
    if not m:
        return None
    try:
        y = int(m.group(1))
        if m.group(2) is None:
            return date(y, 12, 31)
        mo = int(m.group(2))
        if m.group(3) is None:
            return date(y, mo, calendar.monthrange(y, mo)[1])
        return date(y, mo, int(m.group(3)))
    except ValueError:
        return None


def earliest_date(s: str | None) -> date | None:
    """The EARLIEST calendar day the (possibly partial) date allows: '2024' -> 2024-01-01."""
    m = _DATE.match(s or "")
    if not m or latest_date(s) is None:
        return None
    return date(int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1))


def visible_as_of(rec: PaperRecord, as_of: date) -> bool:
    """Provably published on or before `as_of` (unknown or too-coarse dates are not visible: fail closed)."""
    d = latest_date(rec.published)
    return d is not None and d <= as_of


def text_as_of(rec: PaperRecord, as_of: date) -> bool:
    """The stored text (abstract) can be used as of `as_of`: published by then and not revised after it."""
    if not visible_as_of(rec, as_of):
        return False
    if rec.updated is None:
        return True
    u = latest_date(rec.updated)
    return u is not None and u <= as_of


def full_text_as_of(rec: PaperRecord, as_of: date | None) -> bool:
    """The stored FULL text can be used as of `as_of`: its latest stated date is provably on or before it."""
    if as_of is None:
        return True
    d = latest_date(rec.full_text_date)
    return d is not None and d <= as_of


def in_window(rec: PaperRecord, start: date | None, end: date | None) -> bool:
    """Provably published inside [start, end] (fail closed: an unknown or too-coarse date is outside)."""
    if start is None and end is None:
        return True
    first, last = earliest_date(rec.published), latest_date(rec.published)
    if first is None or last is None:
        return False
    return (start is None or first >= start) and (end is None or last <= end)
