"""The multi-agent research mode: a deterministic orchestrator over research-loop agents (docs/multi_agent_mode.md).

Cognition is decentralised (each agent runs its own research loop with its own model bindings); control is not, as
in The Station's own code (code_review/station.md §3): one process schedules turns in a seeded order, one at a time,
holding the exclusive store-wide station lock (one OS lock per store for every mode, `store_lock`; plus a post-append
check of the log's chain), so two processes can never run turns on one store at once.

Per round r (0-based), agents take turns in the order sorted by sha256(mode|seed|order|r|agent). A turn:
  1. budget: the agent's turn caps = min(its cap left, the mode's cap left) per unit; on the primary unit, also at
     most its share of the mode's pool, proportional to the cap left of every agent (so shrinking a stalled agent's
     cap through the gate moves pool share to the others: the ONLY reallocation path). No cap left = turn skipped;
     no mode pool left = the mode stops (`budget_stop`).
  2. archive: the agent's view is rendered by trusted code (archive.render_for: other agents' entries, quoted data,
     journaled and ledgered releases); none in the isolation rounds, none in the `explore` lane, failures only in
     `revive`.
  3. run: the runner executes the turn (seed = sha256(mode|seed|seed|r|agent)); every run joins the agent's
     `Campaign`, so spend is derived from the store and survives crashes. The outcome (run id, status, publication
     requests, the window end, the turn's runs) is logged (`turn_outcome`) before anything is published.
  4. harvest: the certificates attributable to the turn are published through the archive and the result is logged
     (`turn_published`) before `turn_end`; the stall signal is {accepted: a NEW positive result was admitted,
     proposed: pre-registrations, gated: attributable certificates}. Claims registered but not certified in the
     turn stay OPEN and are published later, against their turn, when the Kernel certifies them (`deferred`).
After a round, each agent's signals go through THE stall rule (`evolve.generations.stall_evidence`, the rule of
control/islands.py). A stalled agent (outside a cool-down of `stall_window` rounds since its last stall lane) gets
the next stagnation lane (explore, revive, alternating) and its primary-unit cap is shrunk to spent + stall_shrink x
(cap - spent) by a code-written proposal through `control.downgrade.DowngradeGate`. The proposal is logged
(`lane_intent`) before it is submitted, and resubmitted after a crash only if the gate has no record of it; caps are
the minimum over every APPLIED gate shrink of the agent's budget, so a cap can only ever go down.

State: a hash-chained, append-only log in the store (records kind `station:<mode_id>`); every decision is folded
from it, so `run()` after a crash continues where it stopped. A broken chain refuses the mode (fail closed).
Reopening with another config, spec, model bindings, exact-adapter set or ablation is refused.

Labels (GOAL §6.1, invariant 10): every turn, every agent and the mode carry MECHANICAL or VALIDATED. VALIDATED only
when every result that counted as progress (and so steered budget) is CERTIFIED-VALIDATED (K6 on external or future
data) and there is at least one; exact (CERTIFIED-EXACT) and internal results are MECHANICAL.
Independence (invariant 5): each agent's bindings go through the runner's policy.model_policy at open; a violation
is refused unless the CONFIG allows it (`allow_same_family`), and then recorded as "invariant 5 NOT MET" for that
agent and for the mode.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ... import config
from ...ablation import MARKER, NO_ABLATION, AblationSet
from ...budget import CAMPAIGN_OPEN, UNITS, Budget, Campaign, _value
from ...control.downgrade import ACTION_KIND, AdmissionRegister, DowngradeGate
from ...errors import infra_cause
from ...kernel import Kernel, Label
from ...kernel.adapters import REGISTRY, resolve
from ...kernel.claim import EvidenceClass, canonical_json
from ...kerneld.releases import Releases
from ...kerneld.scope import ScopedKernel
from ...records import Cost, RunStatus
from .archive import ARCHIVE_KINDS, Kind, PubCode, Publication, StationArchive, TurnWindow
from .runner import AgentRunner, TurnOutcome, TurnRequest

FORMAT = "chh-station/1"
LOG_PREFIX = "station:"
ABLATED_SUFFIX = "-ABLATED"  # upper case: the mode_id pattern forbids it, so no production id can collide
DEFAULT_LANE = "exploit"
STALL_LANES = ("explore", "revive")  # assigned in turn to a stalled agent (Station's stagnation lanes, reduced)
ABLATION_SWITCH = "station_coordination"  # custom_harness/ablation.py, capabilities.yaml row the-station
ISOLATION_ROUNDS = 1  # UNCALIBRATED (owner decision, GOAL §13.8): rounds an agent works before it sees the archive
MAX_ARCHIVE_ENTRIES = 16  # UNCALIBRATED (owner decision, GOAL §13.8): entries rendered into one turn
MAX_ARCHIVE_CHARS = 12_000  # UNCALIBRATED (owner decision, GOAL §13.8): under the template's 20k truncation
MIN_SECRET = 6  # as memory/screen.py: shorter private values are not screened (too many false hits)
_SUPPORTED_CLASSES = (EvidenceClass.DATA, EvidenceClass.COMPUTATIONAL)
_RESUMABLE = (RunStatus.RUNNING, RunStatus.INFRA_FAILED, RunStatus.PAUSED)


class StationError(ValueError):
    pass


class StationInfraError(RuntimeError):
    """The environment failed during a turn: the turn stays open and `run()` resumes it later (infra != policy)."""


class StationLocked(StationError):
    """Another process holds this mode's lock."""


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
        """Append one record, then check it landed as record `seq` with nothing else appended (a compare-and-set
        after the fact: a concurrent writer breaks the chain and both see it; the mode lock prevents it)."""
        raw = self.raw()
        seq, prev = len(raw) + 1, (raw[-1]["hash"] if raw else "genesis")
        payload = json.loads(json.dumps(payload, sort_keys=True, allow_nan=False))  # plain JSON, as stored
        h = self._hash(seq, typ, payload, prev)
        self.store.append_record(self.kind, {"seq": seq, "type": typ, "payload": payload, "prev": prev, "hash": h})
        after = self.raw()
        if len(after) != seq or after[-1].get("hash") != h:
            raise StationError(f"station log {self.kind!r}: a concurrent writer appended record {seq} (fail closed)")


@dataclass
class _Fold:
    open: dict[str, Any] | None = None
    started: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    outcomes: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    published: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    ended: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    skipped: set[tuple[int, str]] = field(default_factory=set)
    lanes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lane_rounds: set[tuple[int, str]] = field(default_factory=set)
    lane_intents: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    released: set[tuple[str, str]] = field(default_factory=set)  # releases journaled `done`
    deferred: set[str] = field(default_factory=set)  # certificates published (or refused) as deferred
    rounds_ended: set[int] = field(default_factory=set)
    stopped: dict[str, Any] | None = None
    closed: bool = False

    def lane(self, agent: str) -> str:
        ev = self.lanes.get(agent, [])
        return str(ev[-1]["lane"]) if ev else DEFAULT_LANE

    def explores(self, agent: str) -> int:
        return sum(1 for e in self.lanes.get(agent, []) if e["lane"] == "explore")

    def exposure(self, agent: str, before_round: int | None = None) -> set[str]:
        return {t for (r, a), e in self.ended.items() if a == agent and (before_round is None or r < before_round)
                for t in e.get("exposure_added", [])}

    def runs(self, agent: str) -> set[str]:
        """Runs owned by a turn of `agent` that has an outcome (finished or being harvested)."""
        return {r for (_, a), e in self.outcomes.items() if a == agent for r in e.get("runs", [])}

    def signals(self, agent: str, upto: int) -> list[dict[str, Any]]:
        rounds = sorted(r for (r, a) in self.ended if a == agent and r <= upto)
        return [self.ended[(r, agent)]["signal"] for r in rounds]


def _fold(events: list[tuple[str, dict[str, Any]]]) -> _Fold:
    f = _Fold()
    for typ, p in events:
        rnd = p.get("round")
        key: tuple[int, str] = (rnd if isinstance(rnd, int) else -1, str(p.get("agent", "")))
        if typ == "open":
            f.open = f.open or p
        elif typ == "turn_start":
            f.started[key] = p
        elif typ == "turn_outcome":
            f.outcomes[key] = p
        elif typ == "turn_published":
            f.published[key] = p
        elif typ == "turn_end":
            f.ended[key] = p
        elif typ == "turn_skip":
            f.skipped.add(key)
        elif typ == "release" and p.get("event") == "done":
            f.released.add((str(p["entry"]), str(p["target"])))
        elif typ == "lane_intent":
            f.lane_intents[key] = p
        elif typ == "lane":
            f.lanes.setdefault(p["agent"], []).append(p)
            f.lane_rounds.add(key)
        elif typ == "deferred":
            f.deferred.add(str(p["cert"]))
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


STORE_LOCK_NAME = "station.lock"
NOTE_BYTES = 512  # the holder note (informational only) is written at offset 0, padded to this size
_LOCK_OFFSET = 1 << 20  # the locked byte lies past the note, so the note stays readable on Windows (msvcrt)


def store_lock_path(store_root: Path | str) -> Path:
    return Path(store_root) / STORE_LOCK_NAME


def _os_lock(fd: int) -> bool:
    """Non-blocking exclusive OS lock on the open handle `fd`; False when another handle holds it."""
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)  # LockFile may lock past the end of the file
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:  # flock: per open file description, so a second handle in the same process conflicts too
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _os_unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_note(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            doc = json.loads(fh.read(NOTE_BYTES).decode("utf-8", "replace").strip() or "{}")
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


@contextmanager
def store_lock(store_root: Path | str, holder: dict[str, Any]) -> Iterator[None]:
    """The exclusive station lock of one store (one file `<store>/station.lock` for every mode).

    It is an OS lock on a handle this process keeps open for the whole critical section (msvcrt.locking on
    Windows, fcntl.flock elsewhere), not the existence or content of a file: the OS releases it when the holder
    dies, so a crash never leaves a lock behind, and an empty, truncated or garbage lock file (a crash while the
    note was written, the old O_EXCL design's wedge) cannot block anyone. The file is never deleted (deleting a lock
    file lets a second process lock a new inode while the first still holds the old one). The JSON note at offset 0
    only names the holder in the refusal message; it decides nothing.
    Residual (stated): it binds processes that use this code on this filesystem; a tool writing Kernel entries to
    the store without it is not excluded (docs/multi_agent_mode.md)."""
    path = store_lock_path(store_root)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _os_lock(fd):
            raise StationLocked(f"the station lock {path} is held by {_read_note(path) or 'another process'}; "
                                "one station mode per store runs at a time: stop that process first")
        try:
            note = json.dumps(holder, sort_keys=True, default=str).encode("utf-8")[:NOTE_BYTES - 1]
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, note.ljust(NOTE_BYTES - 1) + b"\n")
            yield
        finally:
            _os_unlock(fd)
    finally:
        os.close(fd)


def run_secrets(store: Any, run_ids: Sequence[str]) -> list[str]:
    """String values of the `_private*` keys in each run's latest checkpoint state (the run's own secrets, e.g. its
    canary): what an archive entry or a rendered view must never contain. Unreadable checkpoints add nothing."""
    out: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if len(v) >= MIN_SECRET:
                out.add(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for rid in run_ids:
        try:
            steps = [s for s in store.trace(rid) if s.checkpoint_ref]
            if not steps:
                continue
            state = store.blobs.get_json(steps[-1].checkpoint_ref).get("state") or {}
        except (KeyError, OSError, ValueError, AttributeError):
            continue
        for k, v in state.items() if isinstance(state, dict) else ():
            if str(k).startswith("_private"):
                walk(v)
    return sorted(out)


class StationMode:
    """One multi-agent research mode over one evidence store. `runner` executes turns; `ablation` (an AblationSet,
    made only by the scorecard runner's entry point) must be the one the runner's Engine runs with. Any non-empty
    ablation marks the mode: its log goes to `station:<mode_id>:ABLATED`, its archive, campaigns, budgets and
    controller ids to `<mode_id>-ABLATED` (kept apart from production modes); `station_coordination` switches the
    coordination capability off (no archive shown, nothing published, no stagnation lanes): N independent loops."""

    def __init__(self, store: Any, cfg: ModeConfig, runner: AgentRunner, *, spec_hash: str,
                 kernel: Kernel | None = None, releases: Releases | None = None,
                 ablation: AblationSet = NO_ABLATION, secrets: Sequence[str] = ()):
        if not isinstance(ablation, AblationSet):
            raise StationError("ablation must be an AblationSet (custom_harness.ablation)")
        if getattr(runner, "ablation", NO_ABLATION) != ablation:
            raise StationError("the mode and its runner must run under one ablation set")
        runner_hash = getattr(runner, "spec_hash", None)
        if runner_hash is not None and str(runner_hash) != str(spec_hash):
            raise StationError("spec_hash differs from the spec the runner runs")
        self.store, self.cfg, self.runner = store, cfg, runner
        self.spec_hash = str(spec_hash)
        # host code never builds a bare Kernel (kerneld ratchet, tests/test_kerneld_boundary.py): ScopedKernel reads
        # private data only inside its certify/bundle grants
        self.kernel = kernel if kernel is not None else ScopedKernel(store)
        self.mid = f"{cfg.mode_id}{ABLATED_SUFFIX}" if ablation else cfg.mode_id
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

    def _controller_id(self, r: int) -> str:
        return f"station:{self.mid}:r{r}"

    def lock_path(self) -> Path:
        """ONE lock file for the whole store, shared by every mode (s4-station HIGH finding, fixed on int18): a
        turn's Kernel-ledger window is only this turn's when no other station mode can append to the store
        meanwhile, so a per-mode lock (the old `station-<mid>-<hash>.lock`) was not enough."""
        return store_lock_path(self.store.root)

    # ------------------------------------------------------------------ the exclusive store-wide lock
    @contextmanager
    def lock(self) -> Iterator[None]:
        """The store-wide station lock (`store_lock`): an OS lock on a held handle, released by the OS when the
        process dies. `run()` folds the mode log only AFTER taking it (`open()` re-reads every event), so a holder
        that ran before us is always seen. A live holder refuses (StationLocked) before anything is written."""
        with store_lock(self.store.root, {"pid": os.getpid(), "host": socket.gethostname(), "mode": self.mid,
                                          "at": time.time()}):
            yield

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
        if not_met and not self.cfg.allow_same_family:  # the CONFIG decides, whatever the runner allowed
            raise StationError(f"invariant 5 is not met for agents {not_met} and the config does not allow a "
                               f"same-family override (allow_same_family: false)")
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
    def _opened(self, agent: str) -> bool:
        return bool(self.store.records_where(CAMPAIGN_OPEN, "campaign_id", self.campaign_id(agent)))

    def agent_runs(self, agent: str) -> list[str]:
        return Campaign(self.store, self.campaign_id(agent)).members() if self._opened(agent) else []

    def agent_spent(self, agent: str) -> Cost:
        """Derived from the store (budget.Campaign). Before the mode is opened the campaign does not exist: nothing
        was spent (read-only: inspecting a mode never opens its campaigns)."""
        if not self._opened(agent):
            return Cost()
        return Campaign(self.store, self.campaign_id(agent)).spent

    def agent_caps(self, agent: str) -> dict[str, float]:
        """The agent's caps: the configured ones, lowered by EVERY applied shrink the downgrade gate recorded for its
        budget name (any controller). Only ever lower (min)."""
        caps = {k: float(v) for k, v in self.cfg.budget.agent.items()}
        for rec in self.store.records_where(ACTION_KIND, "target_id", self.budget_name(agent)):
            new = rec.get("new_cap")
            if (rec.get("action_type") == "shrink" and rec.get("outcome") == "applied" and rec.get("unit") in caps
                    and isinstance(new, (int, float)) and not isinstance(new, bool) and math.isfinite(new)):
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

    def mode_secrets(self, f: _Fold) -> list[str]:
        """The caller's secrets plus every `_private*` value of every run of this mode so far."""
        runs = sorted({r for e in f.outcomes.values() for r in e.get("runs", [])})
        return sorted(set(self.secrets) | set(run_secrets(self.store, runs)))

    # ------------------------------------------------------------------ the main loop
    def run(self) -> dict[str, Any]:
        """Open (or reopen) the mode under its exclusive lock and run every remaining round. Returns `status()`."""
        with self.lock():
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
                self._deferred(_fold(self.log.events()))
                self._lanes(r, _fold(self.log.events()))
                self.log.append("round_end", {"round": r})
            f = _fold(self.log.events())
            if not f.closed and (f.stopped is not None or len(f.rounds_ended) == self.cfg.rounds):
                self._deferred(f)
                self.log.append("close", {"rounds_done": len(f.rounds_ended)})
        return self.status()

    # ------------------------------------------------------------------ one turn
    def _journal(self, r: int, agent: str) -> Callable[[str, str, str, str], None]:
        def journal(event: str, entry: str, target: str, alphabet: str) -> None:
            self.log.append("release", {"round": r, "agent": agent, "event": event, "entry": entry,
                                        "target": target, "alphabet": alphabet})
        return journal

    def _render(self, r: int, agent: str, lane: str, f: _Fold) -> tuple[str, dict[str, Any]]:
        empty: dict[str, Any] = {"shown": [], "redacted": [], "withheld": []}
        if self.ablated:
            return (f"(the station archive is switched off for this run: ablation {ABLATION_SWITCH})", empty)
        if r < self.cfg.isolation_rounds:
            return (f"(isolation phase: no archive entries are shown before round {self.cfg.isolation_rounds})",
                    empty)
        if lane == "explore":
            return ("(exploration lane: this turn works without the archive)", empty)
        kinds = (Kind.NEGATIVE,) if lane == "revive" else ARCHIVE_KINDS
        res = self.archive.render_for(agent, released=set(f.released), kinds=kinds,
                                      max_entries=self.cfg.max_archive_entries, max_chars=self.cfg.max_archive_chars,
                                      secrets=self.mode_secrets(f), release_key=f"r{r}:{agent}",
                                      journal=self._journal(r, agent))
        meta = {"shown": res.shown, "redacted": res.redacted, "withheld": [list(x) for x in res.withheld]}
        return res.text, meta

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
        key = (r, agent)
        if key in f.outcomes:  # the run finished before a crash: harvest (or finish harvesting) its outcome
            self._finish(f.started[key], f.outcomes[key])
            return
        st = f.started.get(key)
        campaign = Campaign(self.store, self.campaign_id(agent))
        if st is None:
            alloc = self.allocation(agent)
            if alloc is None:
                self.log.append("turn_skip", {"round": r, "agent": agent, "reason": "no budget left"})
                return
            lane = f.lane(agent)
            text, meta = self._render(r, agent, lane, f)
            st = {"round": r, "agent": agent, "problem": self.problem_for(agent, f), "lane": lane,
                  "seed": self.turn_seed(r, agent), "limits": alloc, "seq_before": self._ledger_seq(),
                  "archive": {**meta, "ref": self.store.blobs.put(text.encode("utf-8"))}}
            self.log.append("turn_start", st)
            st = _fold(self.log.events()).started[key]
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
        f = _fold(self.log.events())
        outcome = {"round": r, "agent": agent, "run_id": out.run_id, "status": out.status,
                   "detail": out.detail[:200], "requests": [dict(x) for x in out.requests if isinstance(x, dict)],
                   "seq_after": self._ledger_seq(),
                   "runs": [m for m in campaign.members() if m not in f.runs(agent)]}
        self.log.append("turn_outcome", outcome)
        self._finish(st, _fold(self.log.events()).outcomes[key])

    def _window(self, st: dict[str, Any], outcome: dict[str, Any], *, runs: Sequence[str] | None = None) -> TurnWindow:
        return TurnWindow(self.mid, st["agent"], int(st["round"]), st["problem"], int(st["seq_before"]),
                          int(outcome["seq_after"]), frozenset(runs if runs is not None else outcome["runs"]))

    def _finish(self, st: dict[str, Any], outcome: dict[str, Any]) -> None:
        r, agent = int(st["round"]), str(st["agent"])
        key = (r, agent)
        f = _fold(self.log.events())
        w = self._window(st, outcome)
        if key not in f.published:
            opened = f.open or {}
            exact = (opened.get("exact") or {}).get(st["problem"])
            policy = (opened.get("policies") or {}).get(agent) or {}
            secrets = self.mode_secrets(f)
            pubs: list[Publication] = self.archive.publish_turn(
                w, prior_exposure=sorted(f.exposure(agent, before_round=r)),
                parents=list(st["archive"].get("shown", [])), exact=exact, requests=list(outcome.get("requests", [])),
                author_policy=policy, secrets=secrets, ablated=self.ablated)
            certs = self.archive.turn_certificates(w)
            unattributed = [h for h in self.archive.window_certificates(w) if h not in certs]
            targets: set[str] = set()
            for h in certs:
                try:
                    targets |= {str(t) for t in self.kernel.certificate(h).claim.get("generalization_targets", [])}
                except Exception:  # noqa: BLE001 - an unreadable certificate adds nothing (it was refused above)
                    continue
            prereg = len(self.archive.turn_preregs(w))  # only the turn's runs' pre-registrations (run_id)
            progress = [p for p in pubs if p.progress]
            spent = Cost()
            camp = Campaign(self.store, self.campaign_id(agent))
            for run_id in outcome["runs"]:
                spent = spent + camp.run_spent(run_id)
            self.log.append("turn_published", {
                "round": r, "agent": agent, "certs": certs, "unattributed": unattributed,
                "open_preregs": self.archive.open_preregs(w, certs),
                "publications": [p.record() for p in pubs],
                "signal": {"accepted": bool(progress), "proposed": int(prereg), "gated": len(certs),
                           "rejected_on": {}},
                "exposure_added": sorted(self.archive.turn_exposure(w) | targets),
                "spent": {u: _value(spent, u) for u in UNITS},
                "loop_label": loop_label([str(p.label) for p in progress])})
            f = _fold(self.log.events())
        self.log.append("turn_end", {**outcome, **f.published[key]})

    # ------------------------------------------------------------------ deferred certificates
    def _deferred(self, f: _Fold) -> None:
        """Publish certificates issued AFTER their turn for claims that turn pre-registered (future data, deferred
        resolutions), against that turn: its author, parents and exposure frozen at the pre-registration. Each is
        logged once (`deferred`), published or refused. They never feed a stall signal."""
        if self.ablated:
            return
        opened = f.open or {}
        for key, end in sorted(f.ended.items()):
            open_claims = set(end.get("open_preregs") or [])
            if not open_claims:
                continue
            r, agent = key
            st = f.started[key]
            for e in self.kernel.ledger.entries("certificate"):
                h = e.body.get("cert_hash")
                if (e.body.get("claim_hash") not in open_claims or not isinstance(h, str) or h in f.deferred
                        or e.seq <= int(end["seq_after"])):
                    continue
                w = self._window(st, end, runs=self.agent_runs(agent))
                pub = self.archive.publish_deferred(
                    h, w, prior_exposure=sorted(f.exposure(agent, before_round=r)),
                    parents=list(st["archive"].get("shown", [])), exact=(opened.get("exact") or {}).get(st["problem"]),
                    author_policy=(opened.get("policies") or {}).get(agent) or {}, secrets=self.mode_secrets(f))
                self.log.append("deferred", {"round": r, "agent": agent, "cert": h, "publication": pub.record()})
                f = _fold(self.log.events())

    # ------------------------------------------------------------------ stagnation lanes
    def _lanes(self, r: int, f: _Fold) -> None:
        if self.ablated:
            return
        from ...evolve import generations  # THE stall rule (control/islands.py uses the same function)
        for a in (x.id for x in self.cfg.agents):
            key = (r, a)
            if key in f.lane_rounds:
                continue
            intent = f.lane_intents.get(key)
            if intent is None:
                sigs = f.signals(a, r)
                if not sigs or key not in f.ended:
                    continue
                ev = generations.stall_evidence(sigs, window=self.cfg.stall_window)
                stalls = [e for e in f.lanes.get(a, []) if e["reason"] == "stalled"]
                if ev["stalled"]:
                    if stalls and r - int(stalls[-1]["round"]) < self.cfg.stall_window:
                        continue  # cool-down: the window must refill with new rounds before the next stall lane
                    intent = {"round": r, "agent": a, "lane": STALL_LANES[len(stalls) % len(STALL_LANES)],
                              "previous": f.lane(a), "reason": "stalled", "stall": ev, "proposal": self._proposal(a)}
                elif f.lane(a) != DEFAULT_LANE and sigs[-1]["accepted"]:
                    intent = {"round": r, "agent": a, "lane": DEFAULT_LANE, "previous": f.lane(a),
                              "reason": "progress", "stall": ev, "proposal": None}
                else:
                    continue
                self.log.append("lane_intent", intent)
                intent = _fold(self.log.events()).lane_intents[key]
            gate = self._submit_shrink(r, intent["proposal"]) if intent["proposal"] else None
            self.log.append("lane", {**intent, "gate": gate})

    def _proposal(self, agent: str) -> dict[str, Any] | None:
        """The shrink of the agent's primary cap to spent + stall_shrink x (cap - spent), or None when nothing is
        left to shrink. Computed ONCE and logged in the lane intent, so a resume submits exactly the same text."""
        u = self.cfg.budget.unit
        caps = self.agent_caps(agent)
        used = _value(self.agent_spent(agent), u)
        old = caps[u]
        new = used + self.cfg.stall_shrink * (old - used)
        if not (old - used > 0 and math.isfinite(new) and 0 <= new < old):
            return None
        text = json.dumps({"actions": [{"type": "shrink", "target": self.budget_name(agent), "unit": u,
                                        "value": new}]}, sort_keys=True)
        return {"text": text, "budget": self.budget_name(agent), "unit": u, "old_cap": old, "new_cap": new,
                "caps": caps}

    def _submit_shrink(self, r: int, prop: dict[str, Any]) -> dict[str, Any]:
        """Submit the logged proposal through the downgrade-only gate unless the gate already decided it (same
        controller id and proposal hash): idempotent across crashes."""
        cid = self._controller_id(r)
        text = str(prop["text"])
        sha = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        done = [rec for rec in self.store.records_where(ACTION_KIND, "controller_id", cid)
                if rec.get("proposal_sha256") == sha]
        if done:
            applied = any(rec.get("outcome") == "applied" for rec in done)
        else:
            caps = {k: float(v) for k, v in dict(prop["caps"]).items()}
            gate = DowngradeGate(self.store, budgets={str(prop["budget"]): Budget(limits=dict(caps))},
                                 admissions=AdmissionRegister(), kill=_no_kill)
            applied = bool(gate.submit(cid, text).applied)
        return {"controller_id": cid, "proposal_sha256": sha, "applied": applied, "unit": prop["unit"],
                "old_cap": prop["old_cap"], "new_cap": prop["new_cap"]}

    # ------------------------------------------------------------------ reports (read-only)
    def status(self) -> dict[str, Any]:
        """Read-only: writes nothing (no campaign is opened, no retraction applied, no read logged)."""
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
                "open_preregs": sorted({c for e in ends for c in e.get("open_preregs", [])}),
                "caps": caps, "spent": {u: _value(spent, u) for u in caps},
                "invariant_5": ((opened.get("policies") or {}).get(a.id) or {}).get("invariant_5", "UNKNOWN")}
        entries = self.archive.entries()
        return {"mode_id": self.cfg.mode_id, "format": FORMAT, "rounds_done": len(f.rounds_ended),
                "rounds": self.cfg.rounds, "stopped": f.stopped, "closed": f.closed,
                "loop_label": loop_label(all_progress), "invariant_5": opened.get("invariant_5", "UNKNOWN"),
                "exact_problems": {k: v is not None for k, v in (opened.get("exact") or {}).items()},
                "ablated": opened.get("ablated", []), "agents": agents,
                "archive": {"entries": len(entries),
                            "retraction_pending": sum(e["retraction_pending"] for e in entries),
                            "releases": len(f.released), "deferred": len(f.deferred),
                            "corroborations": self.archive.corroborations()},
                "log_intact": not self.log.verify(), "archive_log_intact": not self.archive.store.verify_log()}


__all__ = ["ABLATED_SUFFIX", "ABLATION_SWITCH", "AgentSpec", "BudgetSpec", "ModeConfig", "ProblemSpec", "PubCode",
           "StationError", "StationInfraError", "StationLocked", "StationMode", "loop_label", "run_secrets"]
