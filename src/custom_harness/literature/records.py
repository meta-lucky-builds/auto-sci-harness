"""Paper records, identifiers and publication dates (GOAL §5 L4.3, §6.4). Pure, no I/O."""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

_DOI = re.compile(r"^10\.\d{4,9}/[!-~]+$")  # printable ASCII only: no homoglyphs, no spaces
_ARXIV_NEW = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$")
_ARXIV_OLD = re.compile(r"^([a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7})(v\d+)?$")
_DOI_PREFIX = re.compile(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
_ARXIV_PREFIX = re.compile(r"^(?:arxiv:|https?://(?:export\.)?arxiv\.org/abs/)", re.IGNORECASE)


class Identifier(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["doi", "arxiv"]
    value: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def parse_identifier(raw: str) -> Identifier:
    """Normalise a DOI or arXiv id; raise ValueError if it is not one (fail closed: a malformed id never resolves)."""
    s = (raw or "").strip()
    if _DOI_PREFIX.match(s) or s.startswith("10."):
        v = _DOI_PREFIX.sub("", s).strip()
        if _DOI.match(v):
            return Identifier(kind="doi", value=v.lower())
        raise ValueError(f"malformed DOI: {raw!r}")
    v = _ARXIV_PREFIX.sub("", s).strip()
    for pat in (_ARXIV_NEW, _ARXIV_OLD):
        m = pat.match(v)
        if m:
            return Identifier(kind="arxiv", value=m.group(1))
    raise ValueError(f"not a DOI or arXiv identifier: {raw!r}")


class PaperRecord(BaseModel):
    """One paper as one source describes it. `years` = every year the record itself gives (issued, online,
    print); `published` / `updated` are ISO dates of precision year, month or day."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    source_id: str
    title: str
    doi: str | None = None
    arxiv_id: str | None = None
    authors: tuple[str, ...] = ()
    published: str | None = None
    updated: str | None = None
    years: tuple[int, ...] = ()
    abstract: str | None = None
    full_text: str | None = None
    url: str | None = None
    envelope: str | None = None  # digest of the stored response this record was parsed from (provenance)

    def ids(self) -> set[str]:
        out = set()
        if self.doi:
            out.add(f"doi:{self.doi.lower()}")
        if self.arxiv_id:
            out.add(f"arxiv:{self.arxiv_id}")
        return out


_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?(?:[T ].*)?$")


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
