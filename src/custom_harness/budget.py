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
