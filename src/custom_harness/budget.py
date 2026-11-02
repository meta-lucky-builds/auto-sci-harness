"""Budget ledger (PLAN L1, §14.4). Deterministic: LLMs never get budget authority."""

from __future__ import annotations

import math
import threading
import time
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
# A campaign-only unit: real (calendar) seconds since the campaign was opened, from the open time persisted in the
# campaign_open record. Not a Cost field, so it is not in the per-run UNITS (docs/campaign_wallclock.md).
ELAPSED = "elapsed_s"
CAMPAIGN_UNITS = (*UNITS, ELAPSED)


class CampaignError(ValueError):
    pass


class CampaignClockError(BudgetExceeded):
    """The campaign clock cannot be trusted (it went backwards). A BudgetExceeded, so the engine's check before every
    step stops the run there (fail closed) instead of continuing on a reset or negative elapsed time."""


def _timestamp(value: object) -> float | None:
    """A usable recorded wall-clock timestamp, or None (missing or malformed: callers fail closed on None)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


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
    reported; reading the spend scans every member run's trace (linear in the campaign's steps).

    Two time units (docs/campaign_wallclock.md):
    - `wall_s` is SUMMED STEP TIME: the measured duration of each member run's steps, added up. Concurrent runs add
      up; time between steps and outside any run is not counted; the elapsed time of a step killed mid-flight is lost
      (its interrupted trace row carries only the journalled backend-reported wall_s).
    - `elapsed_s` is CALENDAR TIME since the campaign was opened: `clock() - opened_at`, where `opened_at` is written
      once into the campaign_open record. One clock per campaign: a crash, a new process, a resume or a fork re-reads
      the same open time, so it can be neither reset nor charged twice. It fails closed: a campaign_open record
      without a usable `opened_at` (written before this unit existed) cannot carry an elapsed_s cap (refused at
      construction), and a clock earlier than the open time, or earlier than a time this object already read, raises
      CampaignClockError (a BudgetExceeded, so the engine stops before the next step)."""

    def __init__(self, store: EvidenceStore, campaign_id: str, limits: dict[str, float | None] | None = None,
                 clock: Callable[[], float] = time.time):
        if not campaign_id.strip():
            raise CampaignError("a campaign needs an id")
        self.store, self.id, self._clock = store, campaign_id, clock
        self._last_now: float | None = None
        opened = store.records_where(CAMPAIGN_OPEN, "campaign_id", campaign_id)
        if not opened:
            if limits is None:
                raise CampaignError(f"campaign {campaign_id!r} does not exist: give its caps to open it")
            unknown = set(limits) - set(CAMPAIGN_UNITS)
            if unknown:
                raise ValueError(f"unknown budget units: {unknown}")
            opened_at = _timestamp(clock())
            if opened_at is None:
                raise CampaignError(f"campaign {campaign_id!r}: the clock gave no finite time to record as opened_at")
            store.append_record(CAMPAIGN_OPEN, {"campaign_id": campaign_id, "limits": dict(limits),
                                                "opened_at": opened_at})
            # Two processes can both see no campaign_open and both append one. The FIRST record is the campaign for
            # every reader, so re-read it and hold this object to it (its caps and its open time), not to our own.
            opened = store.records_where(CAMPAIGN_OPEN, "campaign_id", campaign_id)
        first = opened[0]
        stored = first["limits"]
        if limits is not None and dict(limits) != stored:
            raise CampaignError(f"campaign {campaign_id!r} was opened with caps {stored}; refusing {limits} "
                                f"(caps are fixed for a campaign: open a new campaign id)")
        self.limits: dict[str, float | None] = dict(stored)
        self.opened_at: float | None = _timestamp(first.get("opened_at"))
        if self.limits.get(ELAPSED) is not None and self.opened_at is None:
            raise CampaignError(f"campaign {campaign_id!r} has an {ELAPSED} cap but no usable recorded open time "
                                f"(opened_at={first.get('opened_at')!r}): refusing to run without its clock")

    def elapsed(self) -> float:
        """Real seconds since the campaign was opened (from the persisted open time). Fails closed: CampaignError
        without a recorded open time; CampaignClockError if the clock is non-finite, earlier than the open time, or
        earlier than a time this object already read (a backwards jump)."""
        if self.opened_at is None:
            raise CampaignError(f"campaign {self.id!r} has no recorded open time (opened before {ELAPSED} existed)")
        now = _timestamp(self._clock())
        if now is None:
            raise CampaignClockError(f"campaign {self.id} {ELAPSED}: the clock gave no finite time; refusing")
        if now < self.opened_at:
            raise CampaignClockError(f"campaign {self.id} {ELAPSED}: the clock ({now:.6f}) is earlier than the "
                                     f"campaign's recorded open time ({self.opened_at:.6f}); refusing to continue "
                                     f"rather than count a negative or reset elapsed time")
        if self._last_now is not None and now < self._last_now:
            raise CampaignClockError(f"campaign {self.id} {ELAPSED}: the clock went backwards ({now:.6f} after "
                                     f"{self._last_now:.6f}); refusing to continue rather than undercount")
        self._last_now = now
        return now - self.opened_at

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
        if limit is None:
            return None
        return limit - (self.elapsed() if unit == ELAPSED else _value(self.spent, unit))

    def check(self) -> None:
        """Raise BudgetExceeded if any campaign cap is reached (the engine calls this before every step)."""
        elapsed_cap = self.limits.get(ELAPSED)
        if elapsed_cap is not None:
            elapsed = self.elapsed()
            if elapsed >= elapsed_cap:
                raise BudgetExceeded(f"campaign {self.id} {ELAPSED}: {elapsed:.4g} s since the campaign opened "
                                     f">= cap {elapsed_cap:.4g}")
        spent = self.spent
        for unit, limit in self.limits.items():
            if unit != ELAPSED and limit is not None and _value(spent, unit) >= limit:
                raise BudgetExceeded(f"campaign {self.id} {unit}: spent {_value(spent, unit):.4g} >= cap {limit:.4g}")
