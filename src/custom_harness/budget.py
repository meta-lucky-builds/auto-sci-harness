"""Budget ledger (PLAN L1, §14.4). Deterministic: LLMs never get budget authority."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .records import Cost

if TYPE_CHECKING:
    from .store import EvidenceStore

UNITS = ("usd", "gpu_hours", "quota_units", "tokens", "wall_s")


class BudgetExceeded(RuntimeError):
    pass


def _value(cost: Cost, unit: str) -> float:
    if unit == "tokens":
        return float(cost.prompt_tokens + cost.completion_tokens)
    return float(getattr(cost, unit))


@dataclass
class Budget:
    """Hard caps per unit; None = unlimited. `warn_at` fraction triggers on_warn once per unit."""

    limits: dict[str, float | None] = field(default_factory=dict)
    warn_at: float = 0.8
    on_warn: Callable[[str, float, float], None] | None = None
    spent: Cost = field(default_factory=Cost)
    # called with each charge BEFORE the cap check, so a persisted ledger never misses spend
    persist: Callable[[Cost], None] | None = None
    _warned: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        unknown = set(self.limits) - set(UNITS)
        if unknown:
            raise ValueError(f"unknown budget units: {unknown}")

    def remaining(self, unit: str) -> float | None:
        limit = self.limits.get(unit)
        return None if limit is None else limit - _value(self.spent, unit)

    def check(self) -> None:
        """Raise if any cap is already exceeded (call before starting expensive work)."""
        for unit, limit in self.limits.items():
            if limit is not None and _value(self.spent, unit) >= limit:
                raise BudgetExceeded(f"{unit}: spent {_value(self.spent, unit):.4g} >= cap {limit:.4g}")

    def charge(self, cost: Cost) -> None:
        with self._lock:
            self.spent = self.spent + cost
            if self.persist is not None:
                self.persist(cost)
            for unit, limit in self.limits.items():
                if limit is None:
                    continue
                used = _value(self.spent, unit)
                if used >= self.warn_at * limit and unit not in self._warned:
                    self._warned.add(unit)
                    if self.on_warn:
                        self.on_warn(unit, used, limit)
        self.check()


def persistent_budget(store: EvidenceStore, budget_id: str, limits: dict[str, float | None],
                      warn_at: float = 0.8) -> Budget:
    """A budget whose spend survives process restarts (GOAL L1): every charge is appended to the evidence
    store as a "budget_charge" record, and a new process re-loads the total. Use one id per campaign."""
    spent = Cost()
    for rec in store.records_where("budget_charge", "budget_id", budget_id):
        spent = spent + Cost.model_validate(rec["cost"])

    def _persist(cost: Cost) -> None:
        store.append_record("budget_charge", {"budget_id": budget_id, "cost": cost.model_dump()})

    return Budget(limits=limits, warn_at=warn_at, spent=spent, persist=_persist)


# ---------------------------------------------------------------- campaign-level budgets (GOAL §5 L1)
CAMPAIGN_OPEN = "campaign_open"
CAMPAIGN_MEMBER = "campaign_member"
CAMPAIGN_CHARGE = "campaign_charge"


class CampaignError(ValueError):
    pass


class Campaign:
    """A budget for a whole campaign: many runs, their forks and resumes, plus spend outside any run.

    The spend is not kept as a running counter that a crash could lose or a retry could add twice. It is DERIVED
    from the store every time it is read:
    - for each member run, each trace step's recorded cost, once per (run, step index);
    - for a step with no trace row (the process died inside it), the model calls in the run's write-ahead cost
      journal for that step. Once `Engine.resume` records the interrupted step, its trace row replaces the journal, so
      that spend is counted once;
    - charges outside any run (`charge_external`), each under an idempotency key, counted once per key.

    A fork is a new member run. Its trace holds only its own steps; the parent's prefix is counted in the parent run.
    The caps are fixed when the campaign is opened. Re-opening with other caps is refused (a new cap needs a new
    campaign id). Honest limits: a step's cost is known only when it ends, so a campaign can overshoot its cap by the
    cost of the steps running when it is reached (as a per-run budget can); wall_s in the journal is what the backend
    reported; reading the spend scans every member run's trace (linear in the campaign's steps)."""

    def __init__(self, store: EvidenceStore, campaign_id: str, limits: dict[str, float | None] | None = None):
        if not campaign_id.strip():
            raise CampaignError("a campaign needs an id")
        self.store, self.id = store, campaign_id
        opened = store.records_where(CAMPAIGN_OPEN, "campaign_id", campaign_id)
        if opened:
            stored = opened[0]["limits"]
            if limits is not None and dict(limits) != stored:
                raise CampaignError(f"campaign {campaign_id!r} was opened with caps {stored}; refusing {limits} "
                                    f"(caps are fixed for a campaign: open a new campaign id)")
            self.limits: dict[str, float | None] = dict(stored)
        else:
            if limits is None:
                raise CampaignError(f"campaign {campaign_id!r} does not exist: give its caps to open it")
            unknown = set(limits) - set(UNITS)
            if unknown:
                raise ValueError(f"unknown budget units: {unknown}")
            self.limits = dict(limits)
            store.append_record(CAMPAIGN_OPEN, {"campaign_id": campaign_id, "limits": self.limits})

    def join(self, run_id: str) -> None:
        """Make a run a member (idempotent). The engine calls this before the run's first step."""
        if run_id not in self.members():
            self.store.append_record(CAMPAIGN_MEMBER, {"campaign_id": self.id, "run_id": run_id})

    def members(self) -> list[str]:
        return list(dict.fromkeys(r["run_id"] for r in self.store.records_where(CAMPAIGN_MEMBER, "campaign_id",
                                                                                self.id)))

    def charge_external(self, key: str, cost: Cost) -> bool:
        """Charge spend made outside any run (e.g. a proposer call). Returns False if `key` was already charged."""
        if any(r["key"] == key for r in self.store.records_where(CAMPAIGN_CHARGE, "campaign_id", self.id)):
            return False
        self.store.append_record(CAMPAIGN_CHARGE, {"campaign_id": self.id, "key": key, "cost": cost.model_dump()})
        return True

    def run_spent(self, run_id: str) -> Cost:
        steps = {s.index: s.cost for s in self.store.trace(run_id)}
        total = Cost()
        for cost in steps.values():
            total = total + cost
        for e in self.store.records_where("cost_journal", "run_id", run_id):
            if e.get("step") not in steps:
                total = total + Cost.model_validate(e["cost"])
        return total

    @property
    def spent(self) -> Cost:
        total = Cost()
        for run_id in self.members():
            total = total + self.run_spent(run_id)
        seen: set[str] = set()
        for r in self.store.records_where(CAMPAIGN_CHARGE, "campaign_id", self.id):
            if r["key"] not in seen:
                seen.add(r["key"])
                total = total + Cost.model_validate(r["cost"])
        return total

    def remaining(self, unit: str) -> float | None:
        limit = self.limits.get(unit)
        return None if limit is None else limit - _value(self.spent, unit)

    def check(self) -> None:
        """Raise BudgetExceeded if any campaign cap is reached (the engine calls this before every step)."""
        spent = self.spent
        for unit, limit in self.limits.items():
            if limit is not None and _value(spent, unit) >= limit:
                raise BudgetExceeded(f"campaign {self.id} {unit}: spent {_value(spent, unit):.4g} >= cap {limit:.4g}")
