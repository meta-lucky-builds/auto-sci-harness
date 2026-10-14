"""The multi-agent research mode: a deterministic orchestrator over research-loop agents (docs/multi_agent_mode.md).

Cognition is decentralised (each agent runs its own research loop with its own model bindings); control is not, as
in The Station's own code (code_review/station.md §3): one process schedules turns in a seeded order, one at a time.

Per round r (0-based), agents take turns in the order sorted by sha256(mode|seed|order|r|agent). A turn:
  1. budget: the agent's turn caps = min(its cap left, the mode's cap left) per unit; on the primary unit, also at
     most its share of the mode's pool, proportional to the cap left of every agent (so shrinking a stalled agent's
     cap through the gate moves pool share to the others: the ONLY reallocation path). No cap left = turn skipped;
     no mode pool left = the mode stops (`budget_stop`).
  2. archive: the agent's view is rendered by trusted code (archive.render_for: other agents' entries, quoted data,
     ledgered releases); none in the isolation rounds, none in the `explore` lane, failures only in `revive`.
  3. run: the runner executes the turn (seed = sha256(mode|seed|seed|r|agent)); every run joins the agent's
     `Campaign`, so spend is derived from the store and survives crashes.
  4. harvest: the certificates of the turn's ledger window are published through the archive; the turn's stall
     signal is {accepted: a NEW positive result was admitted, proposed: pre-registrations, gated: certificates}.
After a round, each agent's signals go through THE stall rule (`evolve.generations.stall_evidence`, the rule of
control/islands.py). A stalled agent (outside a cool-down of `stall_window` rounds since its last stall lane) gets
the next stagnation lane (explore, revive, alternating) and its primary-unit cap is shrunk to spent + stall_shrink x
(cap - spent) by a code-written proposal through `control.downgrade.DowngradeGate` (ledgered there; caps are rebuilt
from its applied records, so a cap can only ever go down). An agent back to progress returns to the exploit lane;
its cap is never raised again.

State: a hash-chained, append-only log in the store (records kind `station:<mode_id>`); every decision is folded
from it, so `run()` after a crash continues where it stopped: an open turn resumes its run (Engine.resume) with the
budget caps and archive context recorded when it started; a finished turn is never re-run. A broken chain refuses
the mode (fail closed). Reopening with another config, spec, model bindings or ablation is refused.

Labels (GOAL §6.1, invariant 10): every turn, every agent and the mode carry MECHANICAL or VALIDATED. VALIDATED only
when every result that counted as progress (and so steered budget) is CERTIFIED-VALIDATED (K6 on external or future
data) and there is at least one; exact (CERTIFIED-EXACT) and internal results are MECHANICAL.
Independence (invariant 5): each agent's bindings go through policy.model_policy at open (a violation is refused
unless `allow_same_family`, then recorded as "invariant 5 NOT MET" for that agent and for the mode).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ... import config
from ...ablation import MARKER, NO_ABLATION, AblationSet
from ...budget import UNITS, Budget, Campaign, _value
from ...control.downgrade import ACTION_KIND, AdmissionRegister, DowngradeGate
from ...errors import infra_cause
from ...kernel import Kernel, Label
from ...kernel.adapters import REGISTRY, resolve
from ...kernel.claim import EvidenceClass, canonical_json
from ...kerneld.releases import Releases
from ...records import Cost, RunStatus
from .archive import ARCHIVE_KINDS, Kind, PubCode, Publication, StationArchive, TurnWindow
from .runner import AgentRunner, TurnOutcome, TurnRequest

FORMAT = "chh-station/1"
LOG_PREFIX = "station:"
DEFAULT_LANE = "exploit"
STALL_LANES = ("explore", "revive")  # assigned in turn to a stalled agent (Station's stagnation lanes, reduced)
ABLATION_SWITCH = "station_coordination"  # custom_harness/ablation.py, capabilities.yaml row the-station
ISOLATION_ROUNDS = 1  # UNCALIBRATED (owner decision, GOAL §13.8): rounds an agent works before it sees the archive
MAX_ARCHIVE_ENTRIES = 16  # UNCALIBRATED (owner decision, GOAL §13.8): entries rendered into one turn
MAX_ARCHIVE_CHARS = 12_000  # UNCALIBRATED (owner decision, GOAL §13.8): under the template's 20k truncation
_SUPPORTED_CLASSES = (EvidenceClass.DATA, EvidenceClass.COMPUTATIONAL)
_RESUMABLE = (RunStatus.RUNNING, RunStatus.INFRA_FAILED, RunStatus.PAUSED)


class StationError(ValueError):
    pass


class StationInfraError(RuntimeError):
    """The environment failed during a turn: the turn stays open and `run()` resumes it later (infra != policy)."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProblemSpec(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")  # pydantic's (Rust) `$` is the end of text
    task_dir: str | None = None
    inputs: dict[str, str] = Field(default_factory=dict)
    evidence_class: EvidenceClass = EvidenceClass.DATA
    metric: str | None = None  # required for a computational problem (selects the exact adapter)

    @model_validator(mode="after")
    def _check(self) -> ProblemSpec:
        if {"task_dir", "station_archive"} & set(self.inputs):
            raise ValueError("problem inputs may not set the reserved keys task_dir / station_archive")
        if self.evidence_class not in _SUPPORTED_CLASSES:
            raise ValueError(f"evidence class {self.evidence_class.value!r} is not supported by the station (data "
                             f"and computational only: the archive admits no simulated/formal/physical label)")
        if self.evidence_class == EvidenceClass.COMPUTATIONAL and not self.metric:
            raise ValueError("a computational problem must name its metric")
        return self


class AgentSpec(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    roles: dict[str, str] = Field(default_factory=dict)  # role -> backend kind (used by the CLI)


class BudgetSpec(_Strict):
    unit: str  # the primary allocation unit (finite caps required on it)
    mode: dict[str, float]
    agent: dict[str, float]  # per-agent caps (each agent gets these)

    @model_validator(mode="after")
    def _check(self) -> BudgetSpec:
        if self.unit not in UNITS:
            raise ValueError(f"budget unit {self.unit!r} is not one of {UNITS}")
        for name, caps in (("mode", self.mode), ("agent", self.agent)):
            if set(caps) - set(UNITS):
                raise ValueError(f"{name} budget has unknown units {sorted(set(caps) - set(UNITS))}")
            if self.unit not in caps:
                raise ValueError(f"{name} budget must cap the primary unit {self.unit!r}")
            if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in caps.values()):
                raise ValueError(f"{name} budget caps must be finite and > 0")
        return self


class ModeConfig(_Strict):
    mode_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$")
    problems: tuple[ProblemSpec, ...] = Field(min_length=1, max_length=64)
    agents: tuple[AgentSpec, ...] = Field(min_length=2, max_length=32)
    rounds: int = Field(ge=1, le=1000)
    seed: int = Field(ge=0)
    budget: BudgetSpec
    stall_window: int = Field(default=config.ISLAND_STALL_WINDOW, ge=1)
    stall_shrink: float = Field(default=config.ISLAND_STALL_SHRINK, gt=0, lt=1)
    isolation_rounds: int = Field(default=ISOLATION_ROUNDS, ge=0)
    max_archive_entries: int = Field(default=MAX_ARCHIVE_ENTRIES, ge=1, le=32)
    max_archive_chars: int = Field(default=MAX_ARCHIVE_CHARS, ge=1000, le=19_000)
    allow_same_family: bool = False

    @model_validator(mode="after")
    def _check(self) -> ModeConfig:
        for what, ids in (("agent", [a.id for a in self.agents]), ("problem", [p.id for p in self.problems])):
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate {what} ids")
        return self

    def config_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


# ---------------------------------------------------------------------- the hash-chained mode log
class _Log:
    def __init__(self, store: Any, kind: str):
        self.store, self.kind = store, kind

    @staticmethod
    def _hash(seq: int, typ: str, payload: dict[str, Any], prev: str) -> str:
        return hashlib.sha256(canonical_json([seq, typ, payload, prev])).hexdigest()

    def raw(self) -> list[dict[str, Any]]:
        return list(self.store.records(self.kind))

    def verify(self) -> list[str]:
        problems, prev = [], "genesis"
        for i, r in enumerate(self.raw(), start=1):
            try:
                ok = (r["seq"] == i and r["prev"] == prev
                      and r["hash"] == self._hash(r["seq"], r["type"], r["payload"], r["prev"]))
            except (KeyError, TypeError, ValueError):
                ok = False
            if not ok:
                problems.append(f"station log record {i} does not match the chain")
            prev = r.get("hash", "") if isinstance(r, dict) else ""
        return problems

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        problems = self.verify()
        if problems:
            raise StationError(f"station log {self.kind!r} is not trusted: {problems[0]}")
        return [(r["type"], r["payload"]) for r in self.raw()]

    def append(self, typ: str, payload: dict[str, Any]) -> None:
        raw = self.raw()
        seq, prev = len(raw) + 1, (raw[-1]["hash"] if raw else "genesis")
        payload = json.loads(json.dumps(payload, sort_keys=True, allow_nan=False))  # plain JSON, as stored
        self.store.append_record(self.kind, {"seq": seq, "type": typ, "payload": payload, "prev": prev,
                                             "hash": self._hash(seq, typ, payload, prev)})


@dataclass
class _Fold:
    open: dict[str, Any] | None = None
    started: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    ended: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    skipped: set[tuple[int, str]] = field(default_factory=set)
    lanes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lane_rounds: set[tuple[int, str]] = field(default_factory=set)
    released: set[tuple[str, str]] = field(default_factory=set)
    rounds_ended: set[int] = field(default_factory=set)
    stopped: dict[str, Any] | None = None
    closed: bool = False

    def lane(self, agent: str) -> str:
        ev = self.lanes.get(agent, [])
        return str(ev[-1]["lane"]) if ev else DEFAULT_LANE

    def explores(self, agent: str) -> int:
        return sum(1 for e in self.lanes.get(agent, []) if e["lane"] == "explore")

    def exposure(self, agent: str) -> set[str]:
        return {t for (_, a), e in self.ended.items() if a == agent for t in e.get("exposure_added", [])}

    def runs(self, agent: str) -> set[str]:
        return {r for (_, a), e in self.ended.items() if a == agent for r in e.get("runs", [])}

    def signals(self, agent: str, upto: int) -> list[dict[str, Any]]:
        rounds = sorted(r for (r, a) in self.ended if a == agent and r <= upto)
        return [self.ended[(r, agent)]["signal"] for r in rounds]


def _fold(events: list[tuple[str, dict[str, Any]]]) -> _Fold:
    f = _Fold()
    for typ, p in events:
        if typ == "open":
            f.open = f.open or p
        elif typ == "turn_start":
            f.started[(p["round"], p["agent"])] = p
            f.released |= {(str(e), str(t)) for e, t in p.get("released", [])}
        elif typ == "turn_end":
            f.ended[(p["round"], p["agent"])] = p
        elif typ == "turn_skip":
            f.skipped.add((p["round"], p["agent"]))
        elif typ == "lane":
            f.lanes.setdefault(p["agent"], []).append(p)
            f.lane_rounds.add((p["round"], p["agent"]))
        elif typ == "round_end":
            f.rounds_ended.add(p["round"])
        elif typ == "budget_stop":
            f.stopped = p
        elif typ == "close":
            f.closed = True
    return f


def _sha_int(*parts: object) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:8], 16)


def _no_kill(run_id: str, code: str) -> None:
    raise RuntimeError("the station never kills runs through the gate")


def loop_label(labels: Sequence[str]) -> str:
    """GOAL §6.1: VALIDATED iff there is at least one progress result and every one is CERTIFIED-VALIDATED."""
    return "VALIDATED" if labels and all(lb == Label.CERTIFIED_VALIDATED.value for lb in labels) else "MECHANICAL"


class StationMode:
    """One multi-agent research mode over one evidence store. `runner` executes turns; `ablation` (an AblationSet,
    made only by the scorecard runner's entry point) must be the one the runner's Engine runs with. Any non-empty
    ablation marks the mode: its log goes to `station:<mode_id>:ABLATED` (kept apart from production modes);
    `station_coordination` switches the coordination capability off (no archive shown, nothing published, no
    stagnation lanes): N independent research loops."""

    def __init__(self, store: Any, cfg: ModeConfig, runner: AgentRunner, *, spec_hash: str,
                 kernel: Kernel | None = None, releases: Releases | None = None,
                 ablation: AblationSet = NO_ABLATION, secrets: Sequence[str] = ()):
        if not isinstance(ablation, AblationSet):
            raise StationError("ablation must be an AblationSet (custom_harness.ablation)")
        if getattr(runner, "ablation", NO_ABLATION) != ablation:
            raise StationError("the mode and its runner must run under one ablation set")
        self.store, self.cfg, self.runner = store, cfg, runner
        self.spec_hash = str(spec_hash)
        self.kernel = kernel if kernel is not None else Kernel(store)
        # an ablated mode is kept apart from a production mode of the same id: its own log, archive namespace,
        # campaigns, budget names and controller ids (the schedule, seeded by the configured id, is the same)
        self.mid = f"{cfg.mode_id}-ablated" if ablation else cfg.mode_id
        self.archive = StationArchive(self.kernel, self.mid, releases=releases)
        self.ablation = ablation
        self.ablated = ablation.off(ABLATION_SWITCH)
        self.ablation_names = list(ablation.names)
        self.secrets = tuple(secrets)
        self.log = _Log(store, f"{LOG_PREFIX}{cfg.mode_id}:{MARKER}" if ablation else f"{LOG_PREFIX}{cfg.mode_id}")

    # ------------------------------------------------------------------ names
    def campaign_id(self, agent: str) -> str:
        return f"station:{self.mid}:{agent}"

    def budget_name(self, agent: str) -> str:
        return f"station:{self.mid}:{agent}"

    def _controller_prefix(self) -> str:
        return f"station:{self.mid}:r"

    # ------------------------------------------------------------------ open / reopen
    def exact_problems(self) -> dict[str, dict[str, Any] | None]:
        """problem id -> {"metric", "adapter"} when an approved adapter with an exact checker serves the problem's
        computational class and metric; None for a data problem. A computational problem without one is refused."""
        out: dict[str, dict[str, Any] | None] = {}
        for p in self.cfg.problems:
            if p.evidence_class != EvidenceClass.COMPUTATIONAL:
                out[p.id] = None
                continue
            adapter, why = resolve(self.kernel.ledger, p.evidence_class, str(p.metric))
            spec = REGISTRY.get(str((adapter or {}).get("name")), (None, ()))[0]
            if adapter is None or spec is None or getattr(spec, "exact_module", None) is None:
                raise StationError(f"problem {p.id}: no approved adapter with an exact checker serves "
                                   f"{p.evidence_class.value}/{p.metric} ({'; '.join(why) or 'no exact checker'})")
            out[p.id] = {"metric": p.metric, "adapter": adapter["name"], "adapter_hash": adapter["hash"]}
        return out

    def open(self) -> dict[str, Any]:
        policies = {a.id: dict(self.runner.model_policy(a.id)) for a in self.cfg.agents}  # PolicyError propagates
        not_met = sorted(a for a, p in policies.items() if p.get("invariant_5") != "met")
        payload = {"format": FORMAT, "mode_id": self.cfg.mode_id, "config": self.cfg.model_dump(mode="json"),
                   "config_hash": self.cfg.config_hash(), "spec_hash": self.spec_hash,
                   "exact": self.exact_problems(), "policies": policies,
                   "invariant_5": "met" if not not_met else f"NOT MET (agents {', '.join(not_met)})",
                   "ablated": self.ablation_names}
        f = _fold(self.log.events())
        if f.open is not None:
            for k in ("config_hash", "spec_hash", "policies", "ablated", "exact"):
                if json.loads(json.dumps(payload[k], sort_keys=True)) != f.open.get(k):
                    raise StationError(f"mode {self.cfg.mode_id!r} was opened with another {k}: refusing to "
                                       f"continue it (open a new mode id)")
            return f.open
        for a in self.cfg.agents:  # campaign caps are fixed when opened (budget.Campaign refuses other caps)
            Campaign(self.store, self.campaign_id(a.id), limits=dict(self.cfg.budget.agent))
        self.log.append("open", payload)
        return payload

    # ------------------------------------------------------------------ deterministic schedule
    def order(self, r: int) -> list[str]:
        return sorted((a.id for a in self.cfg.agents),
                      key=lambda a: (_sha_int(self.cfg.mode_id, self.cfg.seed, "order", r, a), a))

    def turn_seed(self, r: int, agent: str) -> int:
        return _sha_int(self.cfg.mode_id, self.cfg.seed, "seed", r, agent) % (2 ** 31)

    def problem_for(self, agent: str, f: _Fold) -> str:
        idx = [a.id for a in self.cfg.agents].index(agent)
        return self.cfg.problems[(idx + f.explores(agent)) % len(self.cfg.problems)].id

    # ------------------------------------------------------------------ budgets (derived from the store)
    def agent_spent(self, agent: str) -> Cost:
        return Campaign(self.store, self.campaign_id(agent)).spent

    def agent_caps(self, agent: str) -> dict[str, float]:
        """The agent's caps: the configured ones, lowered by every APPLIED shrink the downgrade gate recorded for its
        budget under this mode's controller ids. Only ever lower (min)."""
        caps = {k: float(v) for k, v in self.cfg.budget.agent.items()}
        for rec in self.store.records_where(ACTION_KIND, "target_id", self.budget_name(agent)):
            new = rec.get("new_cap")
            if (rec.get("action_type") == "shrink" and rec.get("outcome") == "applied"
                    and str(rec.get("controller_id", "")).startswith(self._controller_prefix())
                    and rec.get("unit") in caps and isinstance(new, (int, float)) and not isinstance(new, bool)
                    and math.isfinite(new)):
                caps[rec["unit"]] = min(caps[rec["unit"]], float(new))
        return caps

    def allocation(self, agent: str) -> dict[str, float] | None:
        """This turn's caps for `agent`, or None when it (or the mode) has nothing left."""
        spent = {a.id: self.agent_spent(a.id) for a in self.cfg.agents}
        caps = {a.id: self.agent_caps(a.id) for a in self.cfg.agents}
        mode_spent = Cost()
        for c in spent.values():
            mode_spent = mode_spent + c
        mode_caps = self.cfg.budget.mode
        alloc: dict[str, float] = {}
        for unit in sorted(set(caps[agent]) | set(mode_caps)):
            left = []
            if unit in caps[agent]:
                left.append(caps[agent][unit] - _value(spent[agent], unit))
            if unit in mode_caps:
                left.append(mode_caps[unit] - _value(mode_spent, unit))
            alloc[unit] = min(left)
        u = self.cfg.budget.unit
        rem = {a: max(0.0, caps[a][u] - _value(spent[a], u)) for a in caps}
        pool = mode_caps[u] - _value(mode_spent, u)
        total = sum(rem.values())
        share = pool * rem[agent] / total if total > 0 and pool > 0 else 0.0
        alloc[u] = min(alloc[u], share)
        if any(not (v > 0) for v in alloc.values()):
            return None
        return alloc

    def mode_exhausted(self) -> str | None:
        mode_spent = Cost()
        for a in self.cfg.agents:
            mode_spent = mode_spent + self.agent_spent(a.id)
        for unit, cap in sorted(self.cfg.budget.mode.items()):
            if _value(mode_spent, unit) >= cap:
                return f"mode budget {unit}: spent {_value(mode_spent, unit):.6g} >= cap {cap:.6g}"
        return None

    # ------------------------------------------------------------------ the main loop
    def run(self) -> dict[str, Any]:
        """Open (or reopen) the mode and run every remaining round. Returns `status()`."""
        self.open()
        for r in range(self.cfg.rounds):
            f = _fold(self.log.events())
            if f.stopped is not None or f.closed:
                break
            if r in f.rounds_ended:
                continue
            stop = False
            for agent in self.order(r):
                f = _fold(self.log.events())
                if (r, agent) in f.ended or (r, agent) in f.skipped:
                    continue
                if (r, agent) not in f.started:
                    why = self.mode_exhausted()
                    if why is not None:
                        self.log.append("budget_stop", {"round": r, "reason": why})
                        stop = True
                        break
                self._turn(r, agent, f)
            if stop:
                break
            self._lanes(r, _fold(self.log.events()))
            self.log.append("round_end", {"round": r})
        f = _fold(self.log.events())
        if not f.closed and (f.stopped is not None or len(f.rounds_ended) == self.cfg.rounds):
            self.log.append("close", {"rounds_done": len(f.rounds_ended)})
        return self.status()

    # ------------------------------------------------------------------ one turn
    def _render(self, r: int, agent: str, lane: str, f: _Fold) -> tuple[str, dict[str, Any], list[list[str]]]:
        empty: dict[str, Any] = {"shown": [], "redacted": [], "withheld": []}
        if self.ablated:
            return (f"(the station archive is switched off for this run: ablation {ABLATION_SWITCH})", empty, [])
        if r < self.cfg.isolation_rounds:
            return (f"(isolation phase: no archive entries are shown before round {self.cfg.isolation_rounds})",
                    empty, [])
        if lane == "explore":
            return ("(exploration lane: this turn works without the archive)", empty, [])
        kinds = (Kind.NEGATIVE,) if lane == "revive" else ARCHIVE_KINDS
        released = set(f.released)
        res = self.archive.render_for(agent, released=released, kinds=kinds, max_entries=self.cfg.max_archive_entries,
                                      max_chars=self.cfg.max_archive_chars, secrets=self.secrets,
                                      release_key=f"r{r}:{agent}")
        meta = {"shown": res.shown, "redacted": res.redacted, "withheld": [list(x) for x in res.withheld]}
        return res.text, meta, [list(x) for x in res.released]

    def _request(self, st: dict[str, Any]) -> TurnRequest:
        problem = next(p for p in self.cfg.problems if p.id == st["problem"])
        text = self.store.blobs.get(st["archive"]["ref"]).decode("utf-8")
        return TurnRequest(mode_id=self.mid, round=st["round"], agent=st["agent"], problem=problem.id,
                           task_dir=problem.task_dir, inputs=dict(problem.inputs), seed=st["seed"],
                           archive_context=text, limits=dict(st["limits"]),
                           campaign_id=self.campaign_id(st["agent"]), lane=st["lane"])

    def _ledger_seq(self) -> int:
        rows = self.store.ledger_rows()
        return int(rows[-1][0]) if rows else 0

    def _call(self, fn: Callable[[TurnRequest], TurnOutcome], req: TurnRequest, st: dict[str, Any]) -> TurnOutcome:
        try:
            out = fn(req)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:  # a crashing runner ends the turn; an outage leaves it open
            if infra_cause(e) is not None:
                self.log.append("turn_infra", {"round": st["round"], "agent": st["agent"], "run_id": None,
                                               "detail": type(e).__name__})
                raise StationInfraError(f"turn r{st['round']}/{st['agent']}: infrastructure failure "
                                        f"({type(e).__name__}); run the mode again to resume it") from e
            return TurnOutcome(None, "crashed", type(e).__name__)
        if out.status == RunStatus.INFRA_FAILED.value:
            self.log.append("turn_infra", {"round": st["round"], "agent": st["agent"], "run_id": out.run_id,
                                           "detail": out.detail[:200]})
            raise StationInfraError(f"turn r{st['round']}/{st['agent']}: run {out.run_id} ended INFRA_FAILED; run "
                                    f"the mode again to resume it")
        return out

    def _turn(self, r: int, agent: str, f: _Fold) -> None:
        st = f.started.get((r, agent))
        campaign = Campaign(self.store, self.campaign_id(agent))
        if st is None:
            alloc = self.allocation(agent)
            if alloc is None:
                self.log.append("turn_skip", {"round": r, "agent": agent, "reason": "no budget left"})
                return
            lane = f.lane(agent)
            text, meta, released = self._render(r, agent, lane, f)
            st = {"round": r, "agent": agent, "problem": self.problem_for(agent, f), "lane": lane,
                  "seed": self.turn_seed(r, agent), "limits": alloc, "seq_before": self._ledger_seq(),
                  "archive": {**meta, "ref": self.store.blobs.put(text.encode("utf-8"))}, "released": released}
            self.log.append("turn_start", st)
            st = _fold(self.log.events()).started[(r, agent)]
            out = self._call(self.runner.start, self._request(st), st)
        else:
            new = [m for m in campaign.members() if m not in f.runs(agent)]
            req = self._request(st)
            if not new:
                out = self._call(self.runner.start, req, st)
            else:
                run_id = new[-1]
                status, detail = self.store.run_status(run_id)
                if status in _RESUMABLE or (status == RunStatus.FAILED and detail.startswith("engine crash")):
                    out = self._call(lambda q: self.runner.resume(q, run_id), req, st)
                else:
                    out = TurnOutcome(run_id, status.value, "terminal before the mode resumed")
        if out.run_id:
            campaign.join(out.run_id)  # a runner that forgot to join still has its spend counted
        self._finish(st, out, campaign)

    def _finish(self, st: dict[str, Any], out: TurnOutcome, campaign: Campaign) -> None:
        r, agent = st["round"], st["agent"]
        f = _fold(self.log.events())
        seq_after = self._ledger_seq()
        w = TurnWindow(self.mid, agent, r, st["problem"], int(st["seq_before"]), seq_after)
        opened = f.open or {}
        exact = (opened.get("exact") or {}).get(st["problem"])
        policy = (opened.get("policies") or {}).get(agent) or {}
        pubs: list[Publication] = self.archive.publish_turn(
            w, prior_exposure=sorted(f.exposure(agent)), parents=list(st["archive"].get("shown", [])), exact=exact,
            requests=list(out.requests), author_policy=policy, secrets=self.secrets, ablated=self.ablated)
        certs = self.archive.turn_certificates(w)
        targets: set[str] = set()
        for h in certs:
            try:
                targets |= {str(t) for t in self.kernel.certificate(h).claim.get("generalization_targets", [])}
            except Exception:  # noqa: BLE001 - an unreadable certificate adds nothing (it was refused above)
                continue
        exposure_added = sorted(self.archive.turn_exposure(w) | targets)
        prereg = sum(1 for e in self.kernel.ledger.entries("prereg") if w.contains(e.seq))
        progress = [p for p in pubs if p.progress]
        signal = {"accepted": bool(progress), "proposed": int(prereg), "gated": len(certs), "rejected_on": {}}
        runs = [m for m in campaign.members() if m not in f.runs(agent)]
        spent = Cost()
        for run_id in runs:
            spent = spent + campaign.run_spent(run_id)
        self.log.append("turn_end", {
            "round": r, "agent": agent, "run_id": out.run_id, "runs": runs, "status": out.status,
            "detail": out.detail[:200], "seq_after": seq_after, "certs": certs,
            "publications": [p.record() for p in pubs], "signal": signal, "exposure_added": exposure_added,
            "spent": {u: _value(spent, u) for u in UNITS},
            "loop_label": loop_label([str(p.label) for p in progress])})

    # ------------------------------------------------------------------ stagnation lanes
    def _lanes(self, r: int, f: _Fold) -> None:
        if self.ablated:
            return
        from ...evolve import generations  # THE stall rule (control/islands.py uses the same function)
        for a in (x.id for x in self.cfg.agents):
            if (r, a) in f.lane_rounds:
                continue
            sigs = f.signals(a, r)
            if not sigs or (r, a) not in f.ended:
                continue
            ev = generations.stall_evidence(sigs, window=self.cfg.stall_window)
            history = f.lanes.get(a, [])
            stalls = [e for e in history if e["reason"] == "stalled"]
            if ev["stalled"]:
                if stalls and r - int(stalls[-1]["round"]) < self.cfg.stall_window:
                    continue  # cool-down: the window must refill with new rounds before the next stall lane
                lane = STALL_LANES[len(stalls) % len(STALL_LANES)]
                gate = self._shrink(a, r)
                self.log.append("lane", {"round": r, "agent": a, "lane": lane, "previous": f.lane(a),
                                         "reason": "stalled", "stall": ev, "gate": gate})
            elif f.lane(a) != DEFAULT_LANE and sigs[-1]["accepted"]:
                self.log.append("lane", {"round": r, "agent": a, "lane": DEFAULT_LANE, "previous": f.lane(a),
                                         "reason": "progress", "stall": ev, "gate": None})

    def _shrink(self, agent: str, r: int) -> dict[str, Any] | None:
        """Shrink the agent's primary cap through the downgrade-only gate (a code-written proposal; idempotent on
        resume: an identical proposal already decided under this round's controller id is not resubmitted)."""
        u = self.cfg.budget.unit
        caps = self.agent_caps(agent)
        used = _value(self.agent_spent(agent), u)
        old = caps[u]
        new = used + self.cfg.stall_shrink * (old - used)
        if not (old - used > 0 and math.isfinite(new) and 0 <= new < old):
            return None
        cid = f"{self._controller_prefix()}{r}"
        text = json.dumps({"actions": [{"type": "shrink", "target": self.budget_name(agent), "unit": u,
                                        "value": new}]}, sort_keys=True)
        sha = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        done = [rec for rec in self.store.records_where(ACTION_KIND, "controller_id", cid)
                if rec.get("proposal_sha256") == sha]
        if done:
            applied = any(rec.get("outcome") == "applied" for rec in done)
        else:
            gate = DowngradeGate(self.store, budgets={self.budget_name(agent): Budget(limits=dict(caps))},
                                 admissions=AdmissionRegister(), kill=_no_kill)
            applied = bool(gate.submit(cid, text).applied)
        return {"controller_id": cid, "proposal_sha256": sha, "applied": applied, "unit": u, "old_cap": old,
                "new_cap": new}

    # ------------------------------------------------------------------ reports
    def status(self) -> dict[str, Any]:
        f = _fold(self.log.events())
        opened = f.open or {}
        agents: dict[str, Any] = {}
        all_progress: list[str] = []
        for a in self.cfg.agents:
            ends = [e for (_, b), e in sorted(f.ended.items()) if b == a.id]
            pubs = [p for e in ends for p in e.get("publications", [])]
            progress = [str(p.get("label")) for p in pubs if p.get("progress")]
            all_progress += progress
            codes: dict[str, int] = {}
            for p in pubs:
                codes[p["code"]] = codes.get(p["code"], 0) + 1
            spent = self.agent_spent(a.id)
            caps = self.agent_caps(a.id)
            agents[a.id] = {
                "turns": len(ends), "skipped": sum(1 for (_, b) in f.skipped if b == a.id),
                "lane": f.lane(a.id), "lane_changes": len(f.lanes.get(a.id, [])),
                "publications": codes, "progress": len(progress), "loop_label": loop_label(progress),
                "caps": caps, "spent": {u: _value(spent, u) for u in caps},
                "invariant_5": ((opened.get("policies") or {}).get(a.id) or {}).get("invariant_5", "UNKNOWN")}
        return {"mode_id": self.cfg.mode_id, "format": FORMAT, "rounds_done": len(f.rounds_ended),
                "rounds": self.cfg.rounds, "stopped": f.stopped, "closed": f.closed,
                "loop_label": loop_label(all_progress), "invariant_5": opened.get("invariant_5", "UNKNOWN"),
                "exact_problems": {k: v is not None for k, v in (opened.get("exact") or {}).items()},
                "ablated": opened.get("ablated", []), "agents": agents,
                "archive": {"entries": len(self.archive.entries()), "releases": len(f.released),
                            "corroborations": self.archive.corroborations()},
                "log_intact": not self.log.verify(), "archive_log_intact": not self.archive.store.verify_log()}


__all__ = ["ABLATION_SWITCH", "AgentSpec", "BudgetSpec", "ModeConfig", "ProblemSpec", "PubCode", "StationError",
           "StationInfraError", "StationMode", "loop_label"]
