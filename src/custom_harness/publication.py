"""Publication gate (GOAL §1.3, VG Table 10: "a missing item blocks publication"; invariant 10: every loop is labelled).

The Kernel's reporting checklist (`kernel/checklist.py`, 16 items) is the base. This module adds, OUTSIDE the Kernel:

  * the loop label as a checklist item (`loop_label`). A report of a loop (a research run, an evolve generation)
    must answer it with exactly MECHANICAL or VALIDATED (GOAL §6.1). A report that is not of a loop (a Claim Audit:
    one pre-registered confirmation) must still answer it, with a sentence saying why it is not a loop; it may not
    claim MECHANICAL or VALIDATED. Whether a report is of a loop is decided by the CALLER from the report's source
    (research run vs audit), never from the checklist text.
  * a stricter notion of "answered": a non-blank string. The Kernel's `missing` counts any truthy value as an
    answer, so "   " or a non-string passes it; here it does not (fail closed).

`blockers` lists everything that blocks publication (empty = publishable as far as the checklist goes). Every path
that publishes or certifies a report as publishable calls it: `research.certify` (the `publishable` bit and the
report's publication line), `audit.gate.publish` (the only export path) and the local scorecard's axis F.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .kernel.checklist import ITEMS

LOOP_LABELS = ("MECHANICAL", "VALIDATED")
LOOP_ITEM = ("loop_label", "Loop label (GOAL §6.1): MECHANICAL or VALIDATED")
ALL_ITEMS: tuple[tuple[str, str], ...] = (*ITEMS, LOOP_ITEM)
NOT_A_LOOP_PREFIX = "not a loop:"


def answered(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def missing(checklist: Mapping[str, Any] | None) -> list[str]:
    """Items of ALL_ITEMS without a non-blank string answer (the loop label's VALUE is checked by `blockers`)."""
    if not isinstance(checklist, Mapping):
        return [k for k, _ in ALL_ITEMS]
    return [k for k, _ in ALL_ITEMS if not answered(checklist.get(k))]


def loop_label_problem(value: Any, *, is_loop: bool) -> str | None:
    if not answered(value):
        return "checklist item missing: loop_label (GOAL §6.1, invariant 10)"
    v = str(value).strip()
    if is_loop and v not in LOOP_LABELS:
        return f"loop label {v[:60]!r} is not MECHANICAL or VALIDATED (GOAL §6.1, invariant 10)"
    if not is_loop and not v.startswith(NOT_A_LOOP_PREFIX):
        return (f"loop label {v[:60]!r} on a report that is not of a loop: it must say "
                f"'{NOT_A_LOOP_PREFIX} <why>' (it may not claim MECHANICAL or VALIDATED)")
    return None


def blockers(checklist: Mapping[str, Any] | None, *, is_loop: bool) -> list[str]:
    """Everything in the checklist that blocks publication (empty list = the checklist allows it)."""
    if not isinstance(checklist, Mapping):
        return ["reporting checklist missing entirely (VG Table 10)"]
    out = [f"checklist item missing: {k}" for k in missing(checklist) if k != LOOP_ITEM[0]]
    why = loop_label_problem(checklist.get(LOOP_ITEM[0]), is_loop=is_loop)
    if why:
        out.append(why)
    return out


def render_loop_item(checklist: Mapping[str, Any] | None) -> str:
    v = checklist.get(LOOP_ITEM[0]) if isinstance(checklist, Mapping) else None
    return f"- **{LOOP_ITEM[1]}**: {v if answered(v) else '**MISSING (blocks publication)**'}"
