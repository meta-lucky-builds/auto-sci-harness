"""Campaign generation ledger (GOAL §5 L6.10): stall detection, annealed operator budget (RRSI), generation report.

All three read the loop's records ONLY through the proposer-visible whitelists of export.py (`stats_view`,
`lineage_view`, `proposer_view`): the verdict embargo (GOAL §16.5.3, invariant 18) leaves the accept/reject bit and
the rejecting stage `rejected_on` as the only gate outcomes, so nothing here carries a gate mean, CI, per-family/OOD
value, per-task gate attribution, loop label or free-text gate decision. The ledger is the store's generic
append-only `records` table (no Kernel ledger kind).

Records (kinds of `EvidenceStore.append_record`):
  - `evolve_campaign` (one per campaign, written when it is first opened): {format, campaign, schedule (the
    AnnealSchedule's fields), schedule_hash, n_edits_cap (cfg.n_edits at declaration), stall_window}.
  - `generation_ledger` (one per completed generation, written by the driver hook): {format, campaign, generation
    (campaign-global, 0-based, continued across resume), parent, new_parent, schedule_hash, budget (the n_edits the
    proposer was given), refs {generation_stats: n, lineage: [start, end), harness_edit: [start, end)} (positions in
    `store.records(kind)`, which is append-only), signal {accepted, proposed, gated, rejected_on {stage: count}},
    stall {window, observed, in_window, window_generations, accepted, gated, proposed, rejected_on, stalled}}.

Stall (the L7 orchestrator's rule, control/islands.py): stalled = at least `STALL_WINDOW`
generations observed in this campaign and no accepted edit in the last `STALL_WINDOW`. "No improvement in the gate
bits": under the embargo the only gate bits are accept/reject and the rejecting stage, so an improvement IS an accepted
edit. The window's evidence counts are recorded so the decision can be recomputed (`verify_generation`).

Annealed budget (RRSI): the per-generation edit budget follows a declared, deterministic `AnnealSchedule`
(constant / geometric / linear / step; monotone non-increasing; never below `floor` >= 1), declared once per campaign
and persisted: re-opening a campaign continues its generation numbering and its schedule; a different schedule, or a
start above cfg.n_edits (the multiplicity bound in driver.py is computed from cfg.n_edits, so annealing may only
shrink the budget), is refused before anything runs.

Generation report: built from the ledger only; every value is a cell {"value", "ref": {"kind", "n", "field"}} that
`audit_report` resolves through the same whitelisted views and compares exactly; it also recomputes each referenced
generation record (signal, stall, budget). Proposer-written text appears only as quoted data, labelled unverified and
escaped in the markdown rendering.

Limits: positions are positions in THIS store (a store rebuilt from a subset renumbers them); a second writer
appending loop records to the same store during a generation makes `record` fail closed (it cannot attribute them).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .. import config
from ..store import EvidenceStore
from .export import _STATS_VIEW, lineage_view, proposer_view, stats_view
from .loop import EvolveConfig, GenerationResult

# the L7 islands' stall window (UNCALIBRATED owner decision, GOAL §13.8): one value, read from config
STALL_WINDOW = config.ISLAND_STALL_WINDOW
FORMAT = "generation-ledger/1"
REPORT_FORMAT = "generation-report/1"
CAMPAIGN_KIND = "evolve_campaign"
GENERATION_KIND = "generation_ledger"
SCHEDULE_KINDS = ("constant", "geometric", "linear", "step")
REJECT_STAGES = ("evolve", "held_out_gate", "cost", "exclusion_asymmetry")
# the counters of the stats whitelist (loop._counters), derived from it rather than re-spelled (one source of truth;
# also keeps this module out of emergence.py's novelty-token scan: the counters only COUNT novelty-gate rejections)
_COUNT_FIELDS = tuple(k for k in _STATS_VIEW if k not in ("parent", "yield"))
# lineage statuses = the per-status counters (every counter except the derived proposed/pruned/gated)
LINEAGE_STATUSES = tuple(k for k in _COUNT_FIELDS if k not in ("proposed", "pruned", "gated"))
_GEN_FIELDS = frozenset({"format", "campaign", "generation", "parent", "new_parent", "schedule_hash", "budget",
                         "refs", "signal", "stall"})
_CAMPAIGN_FIELDS = frozenset({"format", "campaign", "schedule", "schedule_hash", "n_edits_cap", "stall_window"})
_MAX_QUOTE = 2000


class LedgerError(ValueError):
    """A ledger record is malformed or inconsistent, or a campaign cannot be (re)opened: fail closed."""


class ReportAuditError(LedgerError):
    def __init__(self, problems: list[str]):
        super().__init__("generation report audit failed: " + "; ".join(problems[:10]))
        self.problems = problems


def _is_int(x: Any) -> bool:
    return type(x) is int


# ============================================================================== annealed operator budget (RRSI)
@dataclass(frozen=True)
class AnnealSchedule:
    """Edit budget per generation g (0-based, campaign-global), clipped below at `floor`:
      constant:  start                                   (floor is set to start)
      geometric: floor(start x rate^g), 0 < rate <= 1    (exact decimal arithmetic on the shortest repr of rate)
      linear:    start - decrement x g, decrement >= 1
      step:      start - decrement x (g // every), decrement >= 1, every >= 1
    Parameters a kind does not use must be None (one canonical form per schedule, so its hash identifies it)."""
    kind: str
    start: int
    floor: int = 1
    rate: float | None = None
    decrement: int | None = None
    every: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in SCHEDULE_KINDS:
            raise ValueError(f"schedule kind {self.kind!r} not in {SCHEDULE_KINDS}")
        if not _is_int(self.start) or not _is_int(self.floor):
            raise ValueError("start and floor must be integers")
        if self.kind == "constant":
            object.__setattr__(self, "floor", self.start)
        if not 1 <= self.floor <= self.start:
            raise ValueError("need 1 <= floor <= start")
        uses = {"constant": set(), "geometric": {"rate"}, "linear": {"decrement"},
                "step": {"decrement", "every"}}[self.kind]
        for name in ("rate", "decrement", "every"):
            v = getattr(self, name)
            if (name in uses) != (v is not None):
                raise ValueError(f"{self.kind} schedule {'needs' if name in uses else 'takes no'} `{name}`")
        if self.rate is not None:
            if isinstance(self.rate, bool) or not isinstance(self.rate, (int, float)) or not math.isfinite(self.rate) \
                    or not 0 < self.rate <= 1:
                raise ValueError("rate must be a finite number in (0, 1]")
            object.__setattr__(self, "rate", float(self.rate))
        for name in ("decrement", "every"):
            v = getattr(self, name)
            if v is not None and (not _is_int(v) or v < 1):
                raise ValueError(f"{name} must be an integer >= 1")

    def budget_at(self, generation: int) -> int:
        if not _is_int(generation) or generation < 0:
            raise ValueError("generation must be an integer >= 0")
        if self.kind == "constant":
            return self.start
        if self.kind == "linear":
            return max(self.floor, self.start - int(self.decrement or 0) * generation)
        if self.kind == "step":
            return max(self.floor, self.start - int(self.decrement or 0) * (generation // int(self.every or 1)))
        rate = Fraction(repr(self.rate))  # exact decimal: 0.7 is 7/10
        v = Fraction(self.start)
        for _ in range(generation):
            if v <= self.floor or rate == 1:
                break  # v only decreases: the clip at floor (or a constant rate) decides from here on
            v *= rate
        return max(self.floor, math.floor(v))

    def as_record(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def schedule_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.as_record(), sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_record(cls, doc: Any) -> AnnealSchedule:
        names = {f.name for f in dataclasses.fields(cls)}
        if not isinstance(doc, dict) or set(doc) != names:
            raise LedgerError("declared schedule is malformed")
        try:
            s = cls(**doc)
        except (TypeError, ValueError) as e:
            raise LedgerError(f"declared schedule is invalid: {e}") from e
        if s.as_record() != doc:
            raise LedgerError("declared schedule is not in canonical form")
        return s


# ============================================================================== signals and stall
def generation_signal(stats: Mapping[str, Any], lineage: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One generation's gate bits, from its `generation_stats` record and its `lineage` records read through the
    proposer-visible views. Fail closed (LedgerError) on a non-count, an unknown status or stage, or stats counts
    that disagree with the lineage statuses."""
    st = stats_view(dict(stats))
    for k in _COUNT_FIELDS:
        if not _is_int(st.get(k)) or st[k] < 0:
            raise LedgerError(f"generation_stats field {k!r} is not a count")
    rows = [lineage_view(dict(r)) for r in lineage]
    statuses = [r.get("status") for r in rows]
    if any(s not in LINEAGE_STATUSES for s in statuses):
        raise LedgerError("lineage record with an unknown status")
    for s in ("accepted", "rejected_gate"):
        if statuses.count(s) != st[s]:
            raise LedgerError(f"generation_stats {s}={st[s]} disagrees with the lineage ({statuses.count(s)})")
    if st["gated"] != st["accepted"] + st["rejected_gate"] or st["accepted"] > 1:
        raise LedgerError("generation_stats gated/accepted counts are inconsistent")
    rejected: dict[str, int] = {}
    for r in rows:
        if r.get("status") == "rejected_gate":
            stage = r.get("rejected_on")
            if stage not in REJECT_STAGES:
                raise LedgerError(f"rejected_on stage {stage!r} not in {REJECT_STAGES}")
            rejected[stage] = rejected.get(stage, 0) + 1
    return {"accepted": st["accepted"] == 1, "proposed": st["proposed"], "gated": st["gated"],
            "rejected_on": dict(sorted(rejected.items()))}


def _check_signal(sig: Any) -> dict[str, Any]:
    if not isinstance(sig, dict) or set(sig) != {"accepted", "proposed", "gated", "rejected_on"}:
        raise LedgerError("generation signal is malformed")
    if type(sig["accepted"]) is not bool or not all(_is_int(sig[k]) and sig[k] >= 0 for k in ("proposed", "gated")):
        raise LedgerError("generation signal: accept bit or counts malformed")
    rej = sig["rejected_on"]
    if not isinstance(rej, dict) or any(k not in REJECT_STAGES or not _is_int(v) or v < 1 for k, v in rej.items()):
        raise LedgerError("generation signal: rejected_on malformed")
    if sum(rej.values()) > sig["gated"] or (sig["accepted"] and sig["gated"] < 1):
        raise LedgerError("generation signal: more rejections or accepts than gated candidates")
    return sig


def stall_evidence(signals: Sequence[Mapping[str, Any]], window: int = STALL_WINDOW) -> dict[str, Any]:
    """Stalled = at least `window` generations observed and no accepted edit in the last `window` (the islands.py
    rule). `signals` are the campaign's generations in order, the current one last."""
    if not _is_int(window) or window < 1:
        raise LedgerError("stall window must be an integer >= 1")
    sigs = [_check_signal(dict(s)) for s in signals]
    w = sigs[-window:]
    rej: dict[str, int] = {}
    for s in w:
        for k, v in s["rejected_on"].items():
            rej[k] = rej.get(k, 0) + v
    acc = sum(s["accepted"] for s in w)
    return {"window": window, "observed": len(sigs), "in_window": len(w),
            "window_generations": list(range(len(sigs) - len(w), len(sigs))), "accepted": acc,
            "gated": sum(s["gated"] for s in w), "proposed": sum(s["proposed"] for s in w),
            "rejected_on": dict(sorted(rej.items())), "stalled": len(sigs) >= window and acc == 0}


# ============================================================================== campaign ledger (driver hook)
def _check_gen_record(rec: Any) -> dict[str, Any]:
    if not isinstance(rec, dict) or set(rec) != _GEN_FIELDS or rec.get("format") != FORMAT:
        raise LedgerError("generation_ledger record is malformed")
    refs = rec["refs"]
    if not isinstance(refs, dict) or set(refs) != {"generation_stats", "lineage", "harness_edit"} \
            or not _is_int(refs["generation_stats"]) or refs["generation_stats"] < 0:
        raise LedgerError("generation_ledger refs are malformed")
    for k in ("lineage", "harness_edit"):
        r = refs[k]
        if not isinstance(r, list) or len(r) != 2 or not all(_is_int(x) for x in r) or not 0 <= r[0] <= r[1]:
            raise LedgerError(f"generation_ledger ref {k} is not a [start, end) range")
    if not _is_int(rec["generation"]) or rec["generation"] < 0 or not _is_int(rec["budget"]) or rec["budget"] < 1:
        raise LedgerError("generation_ledger generation/budget malformed")
    _check_signal(rec["signal"])
    if not isinstance(rec["stall"], dict):
        raise LedgerError("generation_ledger stall malformed")
    return rec


def _declaration(store: EvidenceStore, campaign: str) -> tuple[int, dict[str, Any], AnnealSchedule]:
    rows = [(i, r) for i, r in enumerate(store.records(CAMPAIGN_KIND)) if r.get("campaign") == campaign]
    if len(rows) != 1:
        raise LedgerError(f"campaign {campaign!r} has {len(rows)} declarations (need exactly 1)")
    n, doc = rows[0]
    if set(doc) != _CAMPAIGN_FIELDS or doc.get("format") != FORMAT or doc.get("stall_window") != STALL_WINDOW:
        raise LedgerError(f"campaign {campaign!r} declaration is malformed or has another stall window")
    sched = AnnealSchedule.from_record(doc["schedule"])
    if doc["schedule_hash"] != sched.schedule_hash() or not _is_int(doc["n_edits_cap"]) \
            or sched.start > doc["n_edits_cap"]:
        raise LedgerError(f"campaign {campaign!r} declaration: schedule hash or cap inconsistent")
    return n, doc, sched


def _campaign_generations(store: EvidenceStore, campaign: str) -> list[tuple[int, dict[str, Any]]]:
    """(position, record) of the campaign's generation records, checked to be numbered 0..k-1 in order."""
    rows = [(i, r) for i, r in enumerate(store.records(GENERATION_KIND)) if r.get("campaign") == campaign]
    for j, (_, r) in enumerate(rows):
        _check_gen_record(r)
        if r["generation"] != j:
            raise LedgerError(f"campaign {campaign!r}: generation records are not numbered 0..k-1")
    return rows


class CampaignLedger:
    """Created by `open_campaign`. Per generation the driver calls `begin(cfg)` (returns cfg with the annealed
    n_edits and snapshots the record counts) before `evolve_generation`, then `record(res)` after it."""

    def __init__(self, store: EvidenceStore, campaign: str, schedule: AnnealSchedule, n_edits_cap: int,
                 next_generation: int):
        self.store, self.campaign, self.schedule = store, campaign, schedule
        self.n_edits_cap, self.generation = n_edits_cap, next_generation
        self._snap: dict[str, int] | None = None
        self._budget = 0

    def begin(self, cfg: EvolveConfig) -> EvolveConfig:
        budget = self.schedule.budget_at(self.generation)
        if budget > self.n_edits_cap:  # unreachable after open_campaign's check; kept fail closed
            raise LedgerError("annealed budget above the n_edits the multiplicity bound was computed from")
        self._snap = {k: len(self.store.records(k)) for k in ("generation_stats", "lineage", "harness_edit")}
        self._budget = budget
        return dataclasses.replace(cfg, n_edits=budget)

    def record(self, res: GenerationResult) -> dict[str, Any]:
        """Attribute this generation's loop records (fail closed when they cannot be attributed), compute its signal
        and the stall over the campaign, and append the `generation_ledger` record."""
        if self._snap is None:
            raise LedgerError("record() without begin()")
        snap, self._snap = self._snap, None
        stats = self.store.records("generation_stats")
        lineage = self.store.records("lineage")
        edits = self.store.records("harness_edit")
        if len(stats) != snap["generation_stats"] + 1:
            raise LedgerError("expected exactly one new generation_stats record for this generation")
        new_lineage = lineage[snap["lineage"]:]
        new_edits = edits[snap["harness_edit"]:]
        if len(new_lineage) != len(res.candidates) or len(new_edits) > 1:
            raise LedgerError("this generation's lineage/harness_edit records cannot be attributed")
        if any(r.get("parent") != res.parent_hash for r in [stats[-1], *new_lineage, *new_edits]):
            raise LedgerError("a record in this generation's range has another parent")
        if stats_view(stats[-1]) != stats_view({"parent": res.parent_hash, **res.counters}):
            raise LedgerError("generation_stats record disagrees with the generation's counters")
        signal = generation_signal(stats[-1], new_lineage)
        if signal["accepted"] != res.accepted:
            raise LedgerError("accept bit in the records disagrees with the generation result")
        prior = [r["signal"] for _, r in _campaign_generations(self.store, self.campaign)]
        if len(prior) != self.generation:
            raise LedgerError("campaign generation records changed while this campaign was running")
        rec = {"format": FORMAT, "campaign": self.campaign, "generation": self.generation,
               "parent": res.parent_hash, "new_parent": res.new_parent.spec_hash(),
               "schedule_hash": self.schedule.schedule_hash(), "budget": self._budget,
               "refs": {"generation_stats": len(stats) - 1, "lineage": [snap["lineage"], len(lineage)],
                        "harness_edit": [snap["harness_edit"], len(edits)]},
               "signal": signal, "stall": stall_evidence([*prior, signal])}
        self.store.append_record(GENERATION_KIND, rec)
        self.generation += 1
        return rec


def open_campaign(store: EvidenceStore, campaign: str | None, schedule: AnnealSchedule | None,
                  cfg: EvolveConfig) -> CampaignLedger | None:
    """Declare a new campaign or resume a declared one (detected by its `evolve_campaign` record). Without a campaign
    there is no ledger (None) and a schedule is refused (it could not be persisted). A new campaign without a schedule
    declares constant(cfg.n_edits); a resume without one continues the declared schedule. Refused (LedgerError):
    a resume with a different schedule; a schedule starting above cfg.n_edits; malformed earlier records."""
    if campaign is None:
        if schedule is not None:
            raise LedgerError("an annealed schedule needs a `campaign` (it is declared in and resumed from its ledger)")
        return None
    if not isinstance(campaign, str) or not campaign:
        raise LedgerError("campaign must be a non-empty string")
    declared = [r for r in store.records(CAMPAIGN_KIND) if r.get("campaign") == campaign]
    if declared:
        _, _, sched = _declaration(store, campaign)
        if schedule is not None and schedule.schedule_hash() != sched.schedule_hash():
            raise LedgerError(f"campaign {campaign!r} was declared with another schedule ({sched.as_record()})")
        nxt = len(_campaign_generations(store, campaign))
    else:
        sched = schedule if schedule is not None else AnnealSchedule("constant", start=cfg.n_edits)
        nxt = 0
    if sched.start > cfg.n_edits:
        raise LedgerError(f"schedule start {sched.start} > cfg.n_edits {cfg.n_edits}: the multiplicity bound is "
                          "computed from cfg.n_edits, so annealing may only shrink the budget")
    if not declared:
        if [r for r in store.records(GENERATION_KIND) if r.get("campaign") == campaign]:
            raise LedgerError(f"campaign {campaign!r} has generation records but no declaration")
        store.append_record(CAMPAIGN_KIND, {"format": FORMAT, "campaign": campaign, "schedule": sched.as_record(),
                                            "schedule_hash": sched.schedule_hash(), "n_edits_cap": cfg.n_edits,
                                            "stall_window": STALL_WINDOW})
    return CampaignLedger(store, campaign, sched, cfg.n_edits, nxt)


def verify_generation(store: EvidenceStore, n: int) -> list[str]:
    """Recompute generation record `n` (position in `store.records("generation_ledger")`) from its sources: the
    signal from its referenced stats/lineage records, the budget from the campaign's declared schedule, the stall from
    the campaign's earlier records. Returns problems (empty = consistent)."""
    gens = store.records(GENERATION_KIND)
    if not _is_int(n) or not 0 <= n < len(gens):
        return [f"generation_ledger #{n} does not exist"]
    try:
        rec = _check_gen_record(gens[n])
        _, decl, sched = _declaration(store, rec["campaign"])
        rows = _campaign_generations(store, rec["campaign"])
    except LedgerError as e:
        return [f"generation_ledger #{n}: {e}"]
    problems = []
    if [i for i, _ in rows].count(n) != 1:
        return [f"generation_ledger #{n} is not part of its campaign's numbering"]
    if rec["schedule_hash"] != decl["schedule_hash"]:
        problems.append(f"generation_ledger #{n}: schedule hash differs from the campaign declaration")
    if rec["budget"] != sched.budget_at(rec["generation"]):
        problems.append(f"generation_ledger #{n}: budget {rec['budget']} != declared schedule "
                        f"{sched.budget_at(rec['generation'])}")
    signals = []
    recomputed = True
    for i, r in rows:
        try:
            sig = _recomputed_signal(store, r)
        except LedgerError as e:
            problems.append(f"generation_ledger #{i}: {e}")
            recomputed = False
            break
        if sig != r["signal"]:
            problems.append(f"generation_ledger #{i}: signal differs from its referenced records")
        signals.append(sig)
        if i == n:
            break
    if recomputed and stall_evidence(signals) != rec["stall"]:
        problems.append(f"generation_ledger #{n}: stall differs from the recomputed one")
    if not recomputed:
        problems.append(f"generation_ledger #{n}: stall cannot be recomputed")
    return problems


def _recomputed_signal(store: EvidenceStore, rec: dict[str, Any]) -> dict[str, Any]:
    refs = rec["refs"]
    stats = store.records("generation_stats")
    lineage = store.records("lineage")
    if refs["generation_stats"] >= len(stats) or refs["lineage"][1] > len(lineage):
        raise LedgerError("refs point past the end of the records")
    st = stats[refs["generation_stats"]]
    lin = lineage[refs["lineage"][0]:refs["lineage"][1]]
    if any(r.get("parent") != rec["parent"] for r in [st, *lin]):
        raise LedgerError("referenced records belong to another parent")
    return generation_signal(st, lin)


# ============================================================================== generation report
def _gen_view(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: rec[k] for k in _GEN_FIELDS if k in rec}


def _campaign_view(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: rec[k] for k in _CAMPAIGN_FIELDS if k in rec}


# the only kinds a report may reference, each through its proposer-visible view (a field outside the view does not
# resolve). harness_edit: per-task attribution needs the current evolve ids, so none is resolvable here.
VIEWS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "generation_stats": stats_view, "lineage": lineage_view, "harness_edit": lambda r: proposer_view(r, set()),
    GENERATION_KIND: _gen_view, CAMPAIGN_KIND: _campaign_view}
_MISSING = object()


def resolve(store: EvidenceStore, ref: Any) -> Any:
    """The value a ref names, read through the kind's view; `_MISSING` when it does not resolve."""
    if not isinstance(ref, dict) or set(ref) != {"kind", "n", "field"} or ref["kind"] not in VIEWS \
            or not _is_int(ref["n"]) or not isinstance(ref["field"], str) or not ref["field"]:
        return _MISSING
    rows = store.records(ref["kind"])
    if not 0 <= ref["n"] < len(rows):
        return _MISSING
    cur: Any = VIEWS[ref["kind"]](rows[ref["n"]])
    for part in ref["field"].split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _cell(store: EvidenceStore, kind: str, n: int, field: str) -> dict[str, Any]:
    ref = {"kind": kind, "n": n, "field": field}
    v = resolve(store, ref)
    if v is _MISSING:
        raise LedgerError(f"report reference does not resolve: {ref}")
    return {"value": v, "ref": ref}


def _quote_cell(store: EvidenceStore, kind: str, n: int, field: str, label: str) -> dict[str, Any] | None:
    ref = {"kind": kind, "n": n, "field": field}
    v = resolve(store, ref)
    if not isinstance(v, str):
        return None
    return {"quoted": True, "label": label, "text": v, "ref": ref}


def generation_report(store: EvidenceStore, campaign: str, generation: int) -> dict[str, Any]:
    """The report of one generation, from the ledger only (fail closed: LedgerError when the generation record is
    missing, duplicated or does not recompute)."""
    rows = [(i, r) for i, r in enumerate(store.records(GENERATION_KIND))
            if r.get("campaign") == campaign and r.get("generation") == generation]
    if len(rows) != 1:
        raise LedgerError(f"campaign {campaign!r} generation {generation}: {len(rows)} ledger records (need 1)")
    n, rec = rows[0]
    problems = verify_generation(store, n)
    if problems:
        raise LedgerError("; ".join(problems))
    m, _, _ = _declaration(store, campaign)
    G = GENERATION_KIND
    s = rec["refs"]["generation_stats"]
    report: dict[str, Any] = {
        "format": REPORT_FORMAT, "campaign": _cell(store, G, n, "campaign"),
        "facts": {k: _cell(store, G, n, k) for k in ("generation", "budget", "parent", "new_parent")}
        | {"accepted": _cell(store, G, n, "signal.accepted"), "schedule_kind": _cell(store, CAMPAIGN_KIND, m,
                                                                                         "schedule.kind"),
           "schedule_hash": _cell(store, G, n, "schedule_hash")},
        "counters": {k: _cell(store, "generation_stats", s, k) for k in (*_COUNT_FIELDS, "yield")},
        "stall": {k: _cell(store, G, n, f"stall.{k}") for k in (
            "stalled", "window", "observed", "in_window", "accepted", "gated", "proposed")}
        | {"rejected_on": {st: _cell(store, G, n, f"stall.rejected_on.{st}")
                           for st in sorted(rec["stall"]["rejected_on"])}},
        "candidates": [], "calibration": None}
    lo, hi = rec["refs"]["lineage"]
    lineage = store.records("lineage")
    for i in range(lo, hi):
        v = lineage_view(lineage[i])
        c: dict[str, Any] = {"status": _cell(store, "lineage", i, "status")}
        for f in ("edit.type", "rejected_on", "survived"):
            if resolve(store, {"kind": "lineage", "n": i, "field": f}) is not _MISSING:
                c[f.replace("edit.", "edit_")] = _cell(store, "lineage", i, f)
        texts = []
        if isinstance(v.get("edit"), dict):
            texts.append(_quote_cell(store, "lineage", i, "edit.rationale", "edit rationale (proposer's words)"))
        if v.get("status") == "declined":
            texts.append(_quote_cell(store, "lineage", i, "reason", "decline reason (proposer's words)"))
        else:
            texts.append(_quote_cell(store, "lineage", i, "reason", "recorded reason (may quote the proposer)"))
        texts.append(_quote_cell(store, "lineage", i, "problem", "recorded problem (may quote the proposer)"))
        c["text"] = [t for t in texts if t is not None]
        report["candidates"].append(c)
    hlo, hhi = rec["refs"]["harness_edit"]
    if hhi > hlo:
        report["calibration"] = {f: _cell(store, "harness_edit", hlo, f)
                                 for f in ("predicted_delta", "actual_evolve_delta")
                                 if resolve(store, {"kind": "harness_edit", "n": hlo, "field": f}) is not _MISSING}
    return report


def audit_report(report: Any, store: EvidenceStore) -> list[str]:
    """Recompute every value of a report from its reference (through the same views) and every referenced generation
    record from its sources. Problems: a number/bool outside a cell, a cell whose ref does not resolve or whose value
    differs (type-strict), a malformed cell, an inconsistent generation record. Empty list = consistent."""
    problems: list[str] = []
    gens: set[int] = set()

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, dict) and "ref" in obj:
            ref = obj["ref"]
            keys = set(obj)
            field = "text" if obj.get("quoted") is True else "value"
            want = {"quoted", "label", "text", "ref"} if field == "text" else {"value", "ref"}
            if keys != want or (field == "text" and not isinstance(obj.get("label"), str)):
                problems.append(f"{path}: malformed cell")
                return
            got = resolve(store, ref)
            if got is _MISSING:
                problems.append(f"{path}: reference does not resolve: {ref}")
                return
            if type(got) is not type(obj[field]) or got != obj[field]:
                problems.append(f"{path}: value differs from {ref}")
            if ref["kind"] == GENERATION_KIND:
                gens.add(ref["n"])
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for j, v in enumerate(obj):
                walk(v, f"{path}[{j}]")
        elif isinstance(obj, (bool, int, float)):
            problems.append(f"{path}: a number with no ledger reference")
        elif obj is not None and not isinstance(obj, str):
            problems.append(f"{path}: unexpected value type {type(obj).__name__}")

    if not isinstance(report, dict) or report.get("format") != REPORT_FORMAT:
        return ["not a generation report"]
    walk(report, "report")
    for n in sorted(gens):
        problems += verify_generation(store, n)
    return problems


def check_report(report: Any, store: EvidenceStore) -> None:
    problems = audit_report(report, store)
    if problems:
        raise ReportAuditError(problems)


# ============================================================================== markdown rendering
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~=:&])")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def quote_text(text: str) -> str:
    """Proposer text as a markdown blockquote of escaped lines: it cannot open a heading, list, link, HTML tag or
    code span, and every line stays inside the quote."""
    t = text if len(text) <= _MAX_QUOTE else text[:_MAX_QUOTE] + " [truncated]"
    lines = [_MD_SPECIAL.sub(r"\\\1", _CTRL.sub(" ", ln)) for ln in t.splitlines()] or [""]
    return "\n".join(f"> {ln}" if ln else ">" for ln in lines)


def _v(cell: dict[str, Any]) -> str:
    r = cell["ref"]
    return f"{json.dumps(cell['value'], sort_keys=True)} [{r['kind']}#{r['n']}.{r['field']}]"


def render_markdown(report: dict[str, Any]) -> str:
    """Deterministic markdown of a report. Values are rendered with their ledger reference; proposer text is quoted
    data. States only what the code computes; it does not judge the edits."""
    f, st = report["facts"], report["stall"]
    out = [f"# Generation report: campaign {_safe(report['campaign']['value'])}, generation "
           f"{f['generation']['value']}", "",
           "Generated from the evolve ledger only (proposer-visible views; no gate mean, CI or per-family value). "
           "Each value carries its ledger reference [kind#position.field]; `audit_report` recomputes them.", "",
           "## Facts"]
    out += [f"- {k}: {_v(f[k])}" for k in ("generation", "budget", "schedule_kind", "schedule_hash", "parent",
                                           "new_parent", "accepted")]
    out += ["", "## Counters"] + [f"- {k}: {_v(c)}" for k, c in report["counters"].items()]
    out += ["", "## Stall",
            f"Rule: stalled when at least {st['window']['value']} generations were observed and none of the last "
            f"{st['window']['value']} accepted an edit (an accepted edit is the only improvement visible under the "
            "verdict embargo)."]
    out += [f"- {k}: {_v(c)}" for k, c in st.items() if k != "rejected_on"]
    out += [f"- rejected_on {k}: {_v(c)}" for k, c in st["rejected_on"].items()]
    out += ["", "## Candidates"]
    if not report["candidates"]:
        out.append("(none)")
    for j, c in enumerate(report["candidates"]):
        out += ["", f"### Candidate {j}"]
        out += [f"- {k}: {_v(v)}" for k, v in c.items() if k != "text"]
        for q in c["text"]:  # blank lines around each quote: no lazy continuation into or out of it
            r = q["ref"]
            out += ["", f"Quoted, unverified: {q['label']} [{r['kind']}#{r['n']}.{r['field']}]:", "",
                    quote_text(q["text"]), ""]
    out += ["", "## Calibration (evolve split)"]
    cal = report.get("calibration")
    out += [f"- {k}: {_v(v)}" for k, v in cal.items()] if cal else ["(no gated candidate)"]
    return "\n".join(out) + "\n"


def _safe(s: Any) -> str:
    return _MD_SPECIAL.sub(r"\\\1", _CTRL.sub(" ", str(s))).replace("\n", " ")
