"""Coupled-Goodhart indicator (GOAL §5 L8, §11 M9 "coupled-Goodhart gap not widening").

gap(t) = A-score(t) - sealed score(t): how much better the system looks on the verifier set it is optimised against
than on the sealed split. The indicator is the Theil-Sen slope (median of pairwise slopes, robust to one outlier) of the
gap over the last `config.GOODHART_WINDOW` points; it alarms when the slope exceeds `config.GOODHART_SLOPE_ALARM`,
OR when the newest gap exceeds the median of the earlier window gaps by more than `config.GOODHART_JUMP_ALARM` (the
median slope alone ignores a jump at the newest point). Window and minimum points below 2 are errors.
Fewer than `config.GOODHART_MIN_POINTS` points give "insufficient_data", never "ok". Non-finite scores and times that
do not strictly increase are errors (fail closed).

The inputs include sealed scores, so the report is an OWNER-facing surface: it must not be fed to the evolver or any
agent-visible surface (invariant 18). Enforcement (s3-verifiers; docs/l8_isolation.md):
- `record_report` is the one place a report (and the points it came from) is persisted: the evidence store's records
  kind `RECORD_KIND`, each record carrying `MARKER`. Nothing exports that kind.
- `sealed_fingerprints(store)` returns the marker strings and the verbatim decimal forms of every recorded number
  (a/sealed scores, gaps, slope, jump; also rounded to 5, 6 and 8 decimals) that have at least `MIN_SIGNIFICANT`
  significant digits (shorter numbers such as 0.0, 1 or 0.5 would match unrelated text), matched as whole numbers
  (not as the prefix of a longer number). The proposer's evidence export audit
  (`evolve/export.py` `_audit`, which also screens the proposer's diagnosis) and the L8 SFT export refuse any file
  containing one. Limit: verbatim matching, like verify/leak.py; a number re-derived in another form evades it.
- A static check (tests/test_l8_isolation.py) pins which modules outside `weights/` may import this module, and which
  names: only `evolve/export.py` -> `sealed_fingerprints`.
"""

from __future__ import annotations

import itertools
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from . import config as C


@dataclass(frozen=True)
class GapPoint:
    t: float
    a_score: float
    sealed_score: float


@dataclass(frozen=True)
class GoodhartReport:
    status: str  # alarm | ok | insufficient_data
    slope: float | None
    gaps: tuple[float, ...]
    window: int
    threshold: float
    jump: float | None = None


def goodhart_indicator(points: Sequence[GapPoint], *, window: int = C.GOODHART_WINDOW,
                       min_points: int = C.GOODHART_MIN_POINTS,
                       slope_alarm: float = C.GOODHART_SLOPE_ALARM,
                       jump_alarm: float = C.GOODHART_JUMP_ALARM) -> GoodhartReport:
    if window < 2 or min_points < 2:
        raise ValueError("a slope needs window >= 2 and min_points >= 2")
    for p in points:
        if not all(math.isfinite(x) for x in (p.t, p.a_score, p.sealed_score)):
            raise ValueError(f"non-finite point {p}")
    if any(b.t <= a.t for a, b in itertools.pairwise(points)):
        raise ValueError("time must strictly increase")
    gaps = tuple(p.a_score - p.sealed_score for p in points)
    recent = list(points)[-window:]
    if len(recent) < min_points:
        return GoodhartReport("insufficient_data", None, gaps, window, slope_alarm)
    slope = statistics.median(((b.a_score - b.sealed_score) - (a.a_score - a.sealed_score)) / (b.t - a.t)
                              for i, a in enumerate(recent) for b in recent[i + 1:])
    rg = [p.a_score - p.sealed_score for p in recent]
    jump = rg[-1] - statistics.median(rg[:-1])
    alarm = slope > slope_alarm or jump > jump_alarm
    return GoodhartReport("alarm" if alarm else "ok", slope, gaps, window, slope_alarm, jump)


# ---------------------------------------------------------------------- owner-only persistence and fingerprints
RECORD_KIND = "goodhart_owner_only"
MARKER = "CHH-GOODHART-SEALED"
MIN_SIGNIFICANT = 5  # significant digits a number needs before it is fingerprinted (fewer: false positives)


def record_report(store: Any, points: Sequence[GapPoint], report: GoodhartReport, *, note: str = "") -> None:
    """Persist a report with the points it was computed from (they hold sealed scores) for the OWNER only."""
    store.append_record(RECORD_KIND, {"marker": MARKER, "points": [asdict(p) for p in points],
                                      "report": asdict(report), "note": note})


def _forms(x: object) -> set[str]:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        return set()
    out = set()
    for v in (float(x), *(round(float(x), k) for k in (5, 6, 8))):
        for text in (repr(v), f"{v:.6g}", f"{v:.8g}"):
            digits = text.lstrip("-").split("e")[0].replace(".", "").lstrip("0")
            if len(digits.rstrip("0")) >= MIN_SIGNIFICANT:
                out.add(text.lstrip("-"))
    return out


def _numbers(obj: object) -> set[str]:
    if isinstance(obj, dict):
        return set().union(*(_numbers(v) for v in obj.values())) if obj else set()
    if isinstance(obj, (list, tuple)):
        return set().union(*(_numbers(v) for v in obj)) if obj else set()
    return _forms(obj)


def sealed_fingerprints(store: Any) -> dict[str, list[str]]:
    """What must never appear on an agent-visible surface: the markers, and every recorded number's forms."""
    numbers: set[str] = set()
    for rec in store.records(RECORD_KIND):
        numbers |= _numbers({"points": rec.get("points"), "report": rec.get("report")})
    return {"markers": [MARKER, RECORD_KIND], "numbers": sorted(numbers, key=len, reverse=True)}


def scan(text: str, fp: dict[str, list[str]]) -> list[str]:
    """Findings (never the value) for one text."""
    found = []
    if any(m.lower() in text.lower() for m in fp.get("markers", [])):
        found.append("a Goodhart-report marker")
    if any(re.search(rf"(?<![0-9.]){re.escape(n)}(?![0-9])", text) for n in fp.get("numbers", [])):
        found.append("an owner-only Goodhart number (sealed score, gap, slope or jump)")
    return found
