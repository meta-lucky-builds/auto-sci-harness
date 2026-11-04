"""Cost tables per provider and model, and provider-registry checks (GOAL §5 L0 "Model gateway": cost tables; the
provider registry verified each release).

Cost tables turn a backend's reported usage into the neutral budget units of `records.Cost` (tokens, usd,
quota_units). EVERY rate below is a PLACEHOLDER: no price was looked up, none is a real provider price, and none may be
read as one. Placeholders set no usd at all (usd stays whatever the backend reported, usually 0) and mark the cost
`estimated`; they charge one neutral quota unit per call so that call counts are budgetable before the owner sets
real rates. A lookup with no row fails (UnknownRate): there is no silent default rate.

Registry checks: `registry_problems` (structure: every entry has the fields the gateway relies on, with legal values)
and `release_problems` (what must be true before a release: a human verified each usable entry recently, and every
provider has a cost-table row). The bundled registry is NOT release-ready today (no entry has a human `verified_by`);
the checks say so instead of hiding it.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..records import Cost
from .base import Backend, ChatRequest, ChatResponse


class UnknownRate(KeyError):
    pass


@dataclass(frozen=True)
class Rate:
    """Neutral-unit rates for one (provider, model). usd rates None = unknown (never invented)."""

    quota_per_call: float = 0.0
    usd_per_mtok_in: float | None = None
    usd_per_mtok_out: float | None = None
    placeholder: bool = True
    source: str = ""  # where a non-placeholder rate came from (owner-provided; required when placeholder=False)

    def __post_init__(self) -> None:
        for name in ("quota_per_call", "usd_per_mtok_in", "usd_per_mtok_out"):
            v = getattr(self, name)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                                  or v < 0):
                raise ValueError(f"{name} must be a finite number >= 0 or None, not {v!r}")
        if self.placeholder and (self.usd_per_mtok_in is not None or self.usd_per_mtok_out is not None):
            raise ValueError("a placeholder rate may not carry a usd price (prices are never invented)")
        if not self.placeholder and not self.source:
            raise ValueError("a non-placeholder rate needs its source")


_PLACEHOLDER = Rate(quota_per_call=1.0)  # UNCALIBRATED placeholder (owner decision, GOAL §13.8): 1 unit/call, no usd

# (provider, model) -> Rate; model "*" is the provider's row for any model. Every row: UNCALIBRATED placeholder.
COST_TABLE: dict[tuple[str, str], Rate] = {
    ("openai", "*"): _PLACEHOLDER,      # UNCALIBRATED placeholder (owner decision, GOAL §13.8)
    ("anthropic", "*"): _PLACEHOLDER,   # UNCALIBRATED placeholder (owner decision, GOAL §13.8)
    ("google", "*"): _PLACEHOLDER,      # UNCALIBRATED placeholder (owner decision, GOAL §13.8)
    ("deepseek", "*"): _PLACEHOLDER,    # UNCALIBRATED placeholder (owner decision, GOAL §13.8)
    ("local", "*"): _PLACEHOLDER,       # UNCALIBRATED placeholder (owner decision, GOAL §13.8)
    ("mock", "*"): _PLACEHOLDER,        # tests only
}


def rate_for(provider: str, model: str, table: Mapping[tuple[str, str], Rate] | None = None) -> Rate:
    t = COST_TABLE if table is None else table
    for key in ((provider, model), (provider, "*")):
        if key in t:
            return t[key]
    raise UnknownRate(f"no cost-table row for provider {provider!r}, model {model!r}")


def apply_rate(cost: Cost, rate: Rate) -> Cost:
    """Fill the neutral units a backend did not report. A unit the backend reported (non-zero) is never added to
    again (no double charging); usd is filled only from a known (non-None) rate."""
    update: dict[str, Any] = {}
    if cost.quota_units == 0 and rate.quota_per_call:
        update["quota_units"] = rate.quota_per_call
    if cost.usd == 0 and rate.usd_per_mtok_in is not None and rate.usd_per_mtok_out is not None:
        update["usd"] = (cost.prompt_tokens * rate.usd_per_mtok_in
                         + cost.completion_tokens * rate.usd_per_mtok_out) / 1e6
    if rate.placeholder and update:
        update["estimated"] = True
    return cost.model_copy(update=update) if update else cost


class CostedBackend(Backend):
    """Any backend whose responses get cost-table units (rate looked up per call by provider and model; a missing
    row raises UnknownRate before the call is made)."""

    def __init__(self, inner: Backend, table: Mapping[tuple[str, str], Rate] | None = None):
        self._inner, self._table = inner, table
        self.name, self.provider, self.mode = inner.name, inner.provider, inner.mode
        self.training_on_outputs = inner.training_on_outputs

    def complete(self, req: ChatRequest) -> ChatResponse:
        rate_for(self.provider, req.model or "*", self._table)  # fail before spending anything
        resp = self._inner.complete(req)
        rate = rate_for(resp.provenance.provider, resp.provenance.model, self._table)
        return resp.model_copy(update={"cost": apply_rate(resp.cost, rate)})

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        return getattr(self._inner, item)


# ------------------------------------------------------------------------------------------- run cost records
# A run's cost record (invariant 9) prices its model spend through the cost table. usd is reported only when EVERY
# model step was priced from a calibrated (non-placeholder) row; otherwise it is None, never 0 (AI-Sci's "missing = 0"
# pitfall): "unknown" when a step had no row (or spent tokens with no provenance to look one up), "UNCALIBRATED" when
# only placeholder rows applied. The usd a backend itself reported is kept apart and never presented as the cost.
USD_CALIBRATED, USD_UNCALIBRATED, USD_UNKNOWN, USD_NONE = "calibrated", "UNCALIBRATED", "unknown", "no model spend"


def _has_model_spend(cost: Cost) -> bool:
    return bool(cost.prompt_tokens or cost.completion_tokens or cost.quota_units or cost.usd)


def cost_record(steps: Any, table: Mapping[tuple[str, str], Rate] | None = None) -> dict[str, Any]:
    """The priced cost record of a run's trace steps (objects with `.cost` and `.provenance`, e.g. TraceStep)."""
    total = Cost()
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    missing: list[str] = []
    usd = 0.0
    any_model, any_placeholder = False, False
    for s in steps:
        c: Cost = s.cost
        total = total + c
        prov = s.provenance
        if prov is None:
            if _has_model_spend(c):
                missing.append(f"step {getattr(s, 'index', '?')} ({getattr(s, 'node_id', '?')}): model spend with "
                               "no provenance (no cost-table row can be looked up)")
                any_model = True
            continue
        any_model = True
        key = (prov.provider, prov.model)
        row = rows.setdefault(key, {"provider": prov.provider, "model": prov.model, "steps": 0, "tokens": 0,
                                    "quota_units": 0.0, "backend_reported_usd": 0.0, "rate": None})
        row["steps"] += 1
        row["tokens"] += c.prompt_tokens + c.completion_tokens
        row["quota_units"] += c.quota_units
        row["backend_reported_usd"] += c.usd
        try:
            rate = rate_for(prov.provider, prov.model, table)
        except UnknownRate:
            row["rate"] = "missing"
            missing.append(f"no cost-table row for provider {prov.provider!r}, model {prov.model!r}")
            continue
        if rate.placeholder or rate.usd_per_mtok_in is None or rate.usd_per_mtok_out is None:
            row["rate"] = USD_UNCALIBRATED
            any_placeholder = True
            continue
        row["rate"] = f"{USD_CALIBRATED} ({rate.source})"
        usd += (c.prompt_tokens * rate.usd_per_mtok_in + c.completion_tokens * rate.usd_per_mtok_out) / 1e6
    if missing:
        status, value = USD_UNKNOWN, None
    elif any_placeholder:
        status, value = USD_UNCALIBRATED, None
    elif any_model:
        status, value = USD_CALIBRATED, usd
    else:
        status, value = USD_NONE, 0.0
    return {"usd": value, "usd_status": status, "missing_rates": sorted(set(missing)),
            # which rates priced it: UNCALIBRATED whenever any placeholder row applied (even if another step is unknown)
            "calibration": (USD_UNCALIBRATED if any_placeholder else USD_UNKNOWN if missing
                            else "calibrated" if any_model else "n/a"),
            "tokens": total.prompt_tokens + total.completion_tokens, "prompt_tokens": total.prompt_tokens,
            "completion_tokens": total.completion_tokens, "quota_units": total.quota_units,
            "gpu_hours": total.gpu_hours, "wall_s": total.wall_s, "estimated": total.estimated,
            "backend_reported_usd": total.usd,
            "by_model": [rows[k] for k in sorted(rows)]}


def run_cost_record(store: Any, run_id: str, table: Mapping[tuple[str, str], Rate] | None = None) -> dict[str, Any]:
    """`cost_record` of one run in an EvidenceStore."""
    return cost_record(store.trace(run_id), table)


def usd_text(record: Mapping[str, Any]) -> str:
    """How a report shows a cost record's usd: a number only when calibrated, never 0 for unknown."""
    status = record.get("usd_status")
    if status == USD_CALIBRATED and isinstance(record.get("usd"), (int, float)):
        return f"${float(record['usd']):.4f}"
    if status == USD_NONE:
        return "$0 (no model spend)"
    if status == USD_UNCALIBRATED:
        return "usd UNCALIBRATED (placeholder cost table: no real price)"
    return "usd unknown (" + "; ".join(record.get("missing_rates") or ["no priced record"]) + ")"


def combine_usd(records: list[Mapping[str, Any]]) -> tuple[float | None, str]:
    """(total usd, status) over several cost records: a number only when every record is calibrated or has no model
    spend; any unknown makes the total unknown, else any placeholder makes it UNCALIBRATED."""
    statuses = [r.get("usd_status") for r in records]
    if any(s not in (USD_CALIBRATED, USD_UNCALIBRATED, USD_NONE) for s in statuses):
        return None, USD_UNKNOWN
    if USD_UNCALIBRATED in statuses:
        return None, USD_UNCALIBRATED
    return sum(float(r.get("usd") or 0.0) for r in records), (USD_CALIBRATED if USD_CALIBRATED in statuses
                                                              else USD_NONE)


# ------------------------------------------------------------------------------------------- registry checks
MODES = ("api", "byos", "ow")
BYOS_STATUSES = ("allowed", "needs_partner_approval", "prohibited", "unknown")
TRAINING = ("allowed", "forbidden", "unknown")
# A usable entry must have been verified by a human at most this many days before a release.
RELEASE_MAX_AGE_DAYS = 90  # UNCALIBRATED (owner decision, GOAL §13.8)


def _date(v: Any) -> _dt.date | None:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    if isinstance(v, str):
        try:
            return _dt.date.fromisoformat(v)
        except ValueError:
            return None
    return None


def registry_problems(reg: Mapping[str, Any]) -> list[str]:
    """Structural problems of a provider registry (empty = well-formed)."""
    out: list[str] = []
    if not isinstance(reg, Mapping) or not reg:
        return ["registry is empty or not a mapping"]
    for key, e in reg.items():
        if not isinstance(e, Mapping):
            out.append(f"{key}: entry is not a mapping")
            continue
        if not isinstance(e.get("provider"), str) or not e.get("provider"):
            out.append(f"{key}: missing provider")
        mode = e.get("mode")
        if mode not in MODES:
            out.append(f"{key}: mode {mode!r} not in {list(MODES)}")
        status = e.get("byos_status")
        if mode == "byos" and status not in BYOS_STATUSES:
            out.append(f"{key}: subscription entry needs byos_status in {list(BYOS_STATUSES)}, not {status!r}")
        if mode in ("api", "ow") and status != "n/a":
            out.append(f"{key}: {mode} entry must have byos_status 'n/a', not {status!r}")
        if e.get("training_on_outputs") not in TRAINING:
            out.append(f"{key}: training_on_outputs {e.get('training_on_outputs')!r} not in {list(TRAINING)}")
        if mode == "byos" and status == "allowed" and not (e.get("source_url") and e.get("auth_flow")):
            out.append(f"{key}: an allowed subscription entry needs source_url and auth_flow")
        if e.get("verified_on") is not None and _date(e.get("verified_on")) is None:
            out.append(f"{key}: verified_on {e.get('verified_on')!r} is not a date")
    return out


def _usable(e: Mapping[str, Any]) -> bool:
    return e.get("mode") in ("api", "ow") or e.get("byos_status") == "allowed"


def release_problems(reg: Mapping[str, Any], today: _dt.date, *, max_age_days: int = RELEASE_MAX_AGE_DAYS,
                     table: Mapping[tuple[str, str], Rate] | None = None) -> list[str]:
    """What blocks a release (GOAL: the provider registry verified each release). Structural problems, plus for
    every usable entry: a human `verified_by`, a `verified_on` date within `max_age_days` (not in the future), and a
    cost-table row for its provider."""
    out = registry_problems(reg)
    t = COST_TABLE if table is None else table
    for key, e in reg.items():
        if not isinstance(e, Mapping) or not _usable(e):
            continue
        by = e.get("verified_by")
        if not (isinstance(by, str) and by.strip()):
            out.append(f"{key}: not verified by a human (verified_by is empty)")
        d = _date(e.get("verified_on"))
        if d is None:
            out.append(f"{key}: no verified_on date")
        elif d > today:
            out.append(f"{key}: verified_on {d} is in the future")
        elif (today - d).days > max_age_days:
            out.append(f"{key}: verified {(today - d).days} days ago (> {max_age_days})")
        prov = e.get("provider")
        if isinstance(prov, str) and not any(p == prov for p, _ in t):
            out.append(f"{key}: no cost-table row for provider {prov!r}")
    return out
