"""Spending limits (docs/user_guide.md). The session's recorded spend is checked before each prompt, in `run_prompt`,
the one route every headless model call of a Lab session takes.

What this is not: a hard cap. It stops the next prompt; a prompt already running can overshoot, and an interactive
`chh lab attach` session is not stopped by it. The hard cap is the limit set at the provider on the scoped key
(lab/credentials.py). Spend is what was recorded, so a call of unknown cost counts as unknown, never as zero, and too
many of them stop the session, because then nobody can say what was spent.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..settings import SPEND_WARN_SHARE, load
from ..store import EvidenceStore
from . import trace as T


class SpendingRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class Spend:
    spent_usd: float
    unknown_calls: int
    limit_usd: float
    state: str  # ok | warn | stop
    sentence: str


def spend(store: EvidenceStore, session_id: str) -> Spend:
    s, costs = load(), T.costs(store, session_id)
    spent, unknown = float(costs["usd_known_part"]), int(costs["unknown_usd_events"])  # `usd_total` is None if any is
    limit, calls = float(s["session_spend_limit_usd"]), int(s["unknown_cost_calls"])
    head = f"Recorded spend ${spent:.2f} of the ${limit:.2f} limit for this session"
    if unknown:
        head += f", plus {unknown} call(s) of unknown cost"
    if spent >= limit or unknown > calls:
        return Spend(spent, unknown, limit, "stop", f"{head}. No more prompts: raise the limit with "
                                                    f"`chh exp setup --spend-limit USD` to go on.")
    if spent >= limit * SPEND_WARN_SHARE or unknown:
        return Spend(spent, unknown, limit, "warn", f"{head}. The next prompt may go over it.")
    return Spend(spent, unknown, limit, "ok", f"{head}.")


def require_room(store: EvidenceStore, session_id: str) -> Spend:
    """Before a prompt: refuses when the limit is reached. A refusal has called nothing."""
    s = spend(store, session_id)
    if s.state == "stop":
        raise SpendingRefused(s.sentence)
    return s
