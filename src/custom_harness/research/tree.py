"""Experiment tree: node lifecycle, run contract, repairs, rounds and shape diagnostics, enforced in code
(GOAL §5 L4.7 "Node lifecycle and tree shape"; design from OpenResearch, which states these rules only in prompts,
code_review/openresearch.md §3.1).

Rules enforced here (each refusal raises a `TreeRefusal` with a closed `Code` AND is written to the tree's log as a
`refused` event, so an attempted violation is itself on the record):

1. **Immutable answered nodes.** A node is *provisional* until a run produces its metric; that run *answers* it and
   the node is frozen: its content hash (parent, round, code hash, config, answering attempt) is fixed in the log,
   every later edit is refused (`NODE_IMMUTABLE`), and a new idea must become a child in a new round.
2. **One run contract.** The contract (command, environment, data input hashes, budget) is fixed when the tree is
   created. A run reported under any other contract is refused (`CONTRACT_MISMATCH`), as is a run of code other than
   the node's current committed code (`CODE_MISMATCH`: an unledgered change). Only code/config differ across nodes
   (K5 "identical conditions" inside the tree).
3. **Ledgered, capped repairs.** An implementation failure (crash, OOM, timeout, missing dependency) may be repaired
   only while the node is provisional, by an edit that cites that failed attempt; an edit after an attempt that does
   not cite the last, unrepaired failure is refused (`REPAIR_UNLEDGERED`). Repairs per node are capped
   (`MAX_REPAIRS`); a node that fails with no repair left is abandoned. Every attempt (failures, answers, re-measures,
   refusals) is in the log and in `selection_record()`.
4. **Rounds.** A round is up to `ROUND_MAX_OPTIONS` sibling options for ONE decision, all children of the round's
   parent. A round's winner is confirmed only if, against every answered rival and the round's parent, it wins dev
   racing (paired example-resampling interval, lower bound > 0) on its answering run AND on every fresh re-measure
   (a later run of the same frozen code with a seed it has not used). A new round may descend only from the root or a
   confirmed winner (`PROMOTION_REFUSED`).
5. **Shape diagnostics.** "flat fan" (most nodes hang off the root) and "noodle" (chains of only children) are
   computed by `shape()` and logged by `report()`.
6. **Exact backtracking** (L4.7 "exact backtracking via snapshots"). An answering run may carry the content-addressed
   workspace snapshot (sandbox/snapshot.py manifest hash) taken after it ran. The snapshot must be a stored workspace
   manifest (`BAD_EVENT` otherwise) and is bound into the node's frozen content hash. `checkout(node, workspace)`
   restores that workspace through `restore_snapshot` (preflight + staging + rollback: a corrupted blob raises
   `SnapshotError` with the live workspace untouched), then re-snapshots it and refuses unless every file's bytes and
   mode, every in-workspace symlink and every empty directory match (`RESTORE_MISMATCH`). The backtrack is logged
   (`checkout` event). A node with no frozen snapshot (provisional, abandoned, or answered without one) is refused
   (`NO_SNAPSHOT`). Exact for files, not for processes; symlinks the snapshot dropped (outside the workspace) are not
   restored (sandbox/snapshot.py).
7. **Revivable dead-ends** (L4.7 "persistence rule"). A dead-end is an answered, non-root node that is not its round's
   confirmed winner; it may not be a branch point (rule 4). `revive(node, dev_labels)` lifts that only if
   P(improvement) > `revive_theta`, where P(improvement) is, for the node's answering run AND every ok re-measure, the
   share of `REVIVE_N_BOOT` paired example resamples (fixed seed) in which the node's dev metric is strictly better than
   its round parent's answering run, and the MINIMUM over those runs is used (so extra re-measures can only lower it).
   No dev evidence (abandoned, never answered) or other dev labels than the tree's: refused (`REVIVAL_REFUSED`).
   θ is fixed in the create event; the revival is logged with P, θ and the reference node, and a replay recomputes P,
   so a forged revival is `TreeTampered`. A revived node may then open rounds.
8. **Curriculum** (L4.7 "tune → creative → ablation", AI-Scientist v2). A tree created with `curriculum=True` requires
   every round to declare its stage, one of `STAGES`. Along a lineage the stage never goes back and never skips:
   from the root only `tune`; from a `tune` node `tune` or `creative`; from `creative` `creative` or `ablation`; from
   `ablation` only `ablation` (`CURRICULUM_ORDER`). A `tune` option keeps its round parent's code (only the config
   differs), on `add_option` and on every `edit` (`CURRICULUM_ORDER`). A tree without the curriculum refuses stage
   labels (`ROUND_INVALID`): a label nobody enforces would claim an order that was not kept.

Storage: the log is an append-only, hash-chained event list in the evidence store's `records` table (kind
`exptree`), one record per event. `ExperimentTree.load` replays the log through the SAME checks, so a forged,
reordered or deleted record is detected (`TreeTampered`). The records table is used until workstream kstats adds a
Kernel ledger kind for tree events (requested in the build16 mailbox); this is NOT the Kernel ledger.

Logs written before rules 6-8 existed replay unchanged: a create event without `curriculum`/`revive_theta` gets the
defaults, and an answer without a snapshot hashes exactly as before (the `snapshot` key is added only when present).
Rules 6-8 are NOT yet used by research/modules.py (it opens untyped rounds, passes no snapshot and never revives).

Wired into the research loop by research/modules.py (`run_candidate`, `next_hypothesis`, `ensemble`) and into the
run report by research/claims.py (`report` logs `report()`, `render_report` includes `report_lines()`).
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..sandbox.snapshot import SnapshotError, restore_snapshot, take_snapshot

__all__ = ["SnapshotError"]  # re-exported: a corrupted snapshot surfaces from `checkout` as this error

# ---------------------------------------------------------------- parameters (one config place)
MAX_REPAIRS = 2  # UNCALIBRATED (owner decision, GOAL §13.8): repairs of implementation failures per node
ROUND_MAX_OPTIONS = 5  # UNCALIBRATED (owner decision, GOAL §13.8): sibling options in one round
RACE_ALPHA = 0.2  # UNCALIBRATED (owner decision, GOAL §13.8): two-sided level of the racing interval (as dev racing)
RACE_N_BOOT = 1000  # UNCALIBRATED (owner decision, GOAL §13.8): bootstrap resamples of the racing interval
FLAT_FAN_MIN_NODES = 5  # UNCALIBRATED (owner decision, GOAL §13.8): non-root nodes before "flat fan" can be flagged
FLAT_FAN_ROOT_SHARE = 0.8  # UNCALIBRATED (owner decision, GOAL §13.8): share of non-root nodes that are root children
NOODLE_MIN_CHAIN = 3  # UNCALIBRATED (owner decision, GOAL §13.8): consecutive only-children that make a "noodle"
REVIVE_THETA = 0.9  # UNCALIBRATED (owner decision, GOAL §13.8): P(improvement) a dead-end must exceed to be revived
REVIVE_N_BOOT = 2000  # UNCALIBRATED (owner decision, GOAL §13.8): paired example resamples behind P(improvement)
REVIVE_SEED = 0  # fixed, so a replay recomputes the same P(improvement)

STAGES = ("tune", "creative", "ablation")  # the curriculum, in order (L4.7; AI-Scientist v2)

RECORD_KIND = "exptree"  # records table, until a Kernel ledger kind exists (kstats)
GENESIS = "0" * 64

IMPLEMENTATION_FAILURES = frozenset({"crash", "oom", "timeout", "missing_dep"})
OUTCOMES = IMPLEMENTATION_FAILURES | {"ok"}


class Code(StrEnum):
    NODE_IMMUTABLE = "NODE_IMMUTABLE"
    NODE_ABANDONED = "NODE_ABANDONED"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"
    CODE_MISMATCH = "CODE_MISMATCH"
    REPAIR_UNLEDGERED = "REPAIR_UNLEDGERED"
    REPAIR_CAP = "REPAIR_CAP"
    REMEASURE_INVALID = "REMEASURE_INVALID"
    ROUND_INVALID = "ROUND_INVALID"
    PROMOTION_REFUSED = "PROMOTION_REFUSED"
    BAD_EVENT = "BAD_EVENT"
    STALE = "STALE"
    NO_SNAPSHOT = "NO_SNAPSHOT"
    RESTORE_MISMATCH = "RESTORE_MISMATCH"
    REVIVAL_REFUSED = "REVIVAL_REFUSED"
    CURRICULUM_ORDER = "CURRICULUM_ORDER"


class TreeRefusal(Exception):
    def __init__(self, code: Code, message: str):
        super().__init__(f"{code.value}: {message}")
        self.code = code


class TreeTampered(Exception):
    """The stored log does not replay: a record was forged, edited, reordered or removed."""


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


def code_hash(code: bytes | str) -> str:
    return hashlib.sha256(code.encode() if isinstance(code, str) else code).hexdigest()


@dataclass(frozen=True)
class RunContract:
    """What must be identical for every run in the tree. Data inputs are content hashes (name -> sha256)."""
    command: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    data_inputs: tuple[tuple[str, str], ...]
    budget: tuple[tuple[str, float], ...]

    @classmethod
    def make(cls, command: Sequence[str], env: dict[str, str], data_inputs: dict[str, str],
             budget: dict[str, float]) -> RunContract:
        return cls(tuple(str(c) for c in command), tuple(sorted((str(k), str(v)) for k, v in env.items())),
                   tuple(sorted((str(k), str(v)) for k, v in data_inputs.items())),
                   tuple(sorted((str(k), float(v)) for k, v in budget.items())))

    def to_json(self) -> dict[str, Any]:
        return {"command": list(self.command), "env": dict(self.env), "data_inputs": dict(self.data_inputs),
                "budget": dict(self.budget)}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> RunContract:
        return cls.make(d["command"], d["env"], d["data_inputs"], d["budget"])

    def digest(self) -> str:
        return _sha(self.to_json())


@dataclass(frozen=True)
class RunOutcome:
    """One run as observed by the runner: the contract it ACTUALLY ran under, the code it ran, the seed, the outcome
    (`ok` or an implementation failure), and for `ok` the metric and the dev predictions."""
    contract: RunContract
    code_hash: str
    seed: int
    outcome: str
    metric: float | None = None
    preds: list[str] | None = None
    snapshot: str | None = None  # workspace snapshot (sandbox/snapshot.py manifest hash) taken after the run


@dataclass
class _Attempt:
    idx: int
    purpose: str  # answer | remeasure
    seed: int
    outcome: str
    metric: float | None
    preds_ref: str | None
    code_hash: str
    snapshot: str | None = None


@dataclass
class _Node:
    id: str
    parent: str | None
    round: str | None
    code_hash: str
    config: dict[str, Any]
    status: str = "provisional"  # provisional | answered | abandoned
    attempts: list[_Attempt] = field(default_factory=list)
    repairs: int = 0
    repaired: set[int] = field(default_factory=set)  # attempt indices already cited by a repair
    content_hash: str | None = None
    snapshot: str | None = None  # frozen with the answer (rule 6)
    revived: bool = False  # rule 7


@dataclass
class _Round:
    id: str
    parent: str
    decision: str
    options: list[str] = field(default_factory=list)
    winner: str | None = None
    stage: str | None = None  # rule 8


@dataclass(frozen=True)
class NodeView:
    id: str
    parent: str | None
    round: str | None
    code_hash: str
    config: dict[str, Any]
    status: str
    content_hash: str | None
    n_attempts: int
    repairs: int
    metric: float | None
    snapshot: str | None = None
    revived: bool = False


def _content(n: _Node, answer: _Attempt) -> dict[str, Any]:
    c: dict[str, Any] = {"id": n.id, "parent": n.parent, "round": n.round, "code_hash": n.code_hash,
                         "config": n.config, "answer": {"idx": answer.idx, "seed": answer.seed,
                                                        "metric": answer.metric, "preds_ref": answer.preds_ref}}
    if answer.snapshot is not None:  # only when present: answers logged before rule 6 keep their hashes
        c["answer"]["snapshot"] = answer.snapshot
    return c


def _tree_of(manifest: dict[str, Any]) -> dict[str, Any]:
    """What a byte-exact restore must reproduce: each file's blob and mode, symlinks, empty directories (not the
    filesystem timestamps, which a restore cannot and need not keep)."""
    files = {rel: (v["blob"], v.get("mode")) if isinstance(v, dict) else (v, None)
             for rel, v in manifest.get("files", {}).items()}
    return {"files": files, "symlinks": dict(manifest.get("symlinks", {})), "dirs": sorted(manifest.get("dirs", []))}


Racer = Callable[[list[str], list[str], list[str]], bool]  # (labels, a_preds, b_preds) -> a beats b


def default_racer(metric: str, higher_is_better: bool) -> Racer:
    """Dev racing as in research.modules._dev_better: a paired example-resampling interval, lower bound > 0."""
    from ..verify import metrics as M
    from ..verify.stats import paired_example_interval

    def beats(labels: list[str], a: list[str], b: list[str]) -> bool:
        iv = paired_example_interval(M.METRICS[metric], labels, [a], [b], higher_is_better=higher_is_better,
                                     alpha=RACE_ALPHA, n_boot=RACE_N_BOOT)
        return not iv.degenerate and iv.lo > 0

    return beats


class ExperimentTree:
    """Event-sourced tree. Every mutation is checked by `_apply` and appended to the log before it takes effect."""

    def __init__(self, store: Any, tree_id: str):
        self.store, self.id = store, tree_id
        self.contract: RunContract | None = None
        self.metric, self.higher_is_better, self.labels_sha = "", True, ""
        self.max_repairs = MAX_REPAIRS
        self.curriculum, self.revive_theta = False, REVIVE_THETA
        self.nodes: dict[str, _Node] = {}
        self.rounds: dict[str, _Round] = {}
        self.root: str | None = None
        self.head, self.seq = GENESIS, 0
        self.events: list[dict[str, Any]] = []

    # ------------------------------------------------------------ construction / persistence
    @classmethod
    def create(cls, store: Any, tree_id: str, contract: RunContract, *, root_code: bytes | str,
               root_config: dict[str, Any], metric: str, higher_is_better: bool,
               dev_labels: list[str], max_repairs: int = MAX_REPAIRS, curriculum: bool = False,
               revive_theta: float = REVIVE_THETA) -> ExperimentTree:
        """`max_repairs` is the ONE repair cap of the tree (research_v1/v2 pass their `run` node's `max_fixes`);
        it is fixed in the create event, so a replay applies the same cap. `curriculum` (rule 8) and `revive_theta`
        (rule 7) are fixed there too."""
        if store.records_where(RECORD_KIND, "tree", tree_id):
            raise TreeRefusal(Code.BAD_EVENT, f"tree {tree_id!r} already exists")
        t = cls(store, tree_id)
        t._commit({"op": "create", "contract": contract.to_json(), "root_code_hash": code_hash(root_code),
                   "root_config": root_config, "metric": metric, "higher_is_better": higher_is_better,
                   "labels_sha": _sha(list(dev_labels)), "max_repairs": int(max_repairs),
                   "curriculum": bool(curriculum), "revive_theta": float(revive_theta)})
        return t

    @classmethod
    def load(cls, store: Any, tree_id: str) -> ExperimentTree:
        t = cls(store, tree_id)
        rows = store.records_where(RECORD_KIND, "tree", tree_id)
        if not rows:
            raise KeyError(tree_id)
        for r in rows:
            t._replay(r)
        return t

    def _replay(self, r: dict[str, Any]) -> None:
        ev = r.get("event")
        if r.get("seq") != self.seq + 1 or r.get("prev") != self.head or not isinstance(ev, dict):
            raise TreeTampered(f"tree {self.id}: record {r.get('seq')} breaks the chain (removed or reordered)")
        if r.get("hash") != _sha({"prev": self.head, "seq": self.seq + 1, "event": ev}):
            raise TreeTampered(f"tree {self.id}: record {r.get('seq')} was edited (hash mismatch)")
        try:
            if ev.get("op") != "refused":
                self._apply(ev)
        except TreeRefusal as e:
            raise TreeTampered(f"tree {self.id}: record {r['seq']} does not pass the tree's checks ({e})") from e
        self._advance(ev, r["hash"])

    def _advance(self, ev: dict[str, Any], h: str) -> None:
        self.seq, self.head = self.seq + 1, h
        self.events.append(ev)

    def _stale_check(self) -> None:
        rows = self.store.records_where(RECORD_KIND, "tree", self.id)
        if len(rows) != self.seq or (rows and rows[-1].get("hash") != self.head):
            raise TreeRefusal(Code.STALE, f"tree {self.id} changed in the store since it was loaded; reload it")

    def _append(self, ev: dict[str, Any]) -> None:
        h = _sha({"prev": self.head, "seq": self.seq + 1, "event": ev})
        self.store.append_record(RECORD_KIND, {"tree": self.id, "seq": self.seq + 1, "prev": self.head,
                                               "event": ev, "hash": h})
        self._advance(ev, h)

    def _commit(self, ev: dict[str, Any]) -> dict[str, Any]:
        """Check, log, apply. A refusal is logged as a `refused` event and re-raised."""
        if self.seq:
            self._stale_check()
        snapshot = _snapshot(self)
        try:
            result = self._apply(ev)
        except TreeRefusal as e:
            _restore(self, snapshot)
            if self.seq:  # a refused create has no tree to log into
                self._append({"op": "refused", "code": e.code.value, "attempted": ev})
            raise
        self._append({**ev, **result.pop("_log", {})})
        return result

    # ------------------------------------------------------------ the checks (shared by live calls and replay)
    def _node(self, node_id: str) -> _Node:
        if node_id not in self.nodes:
            raise TreeRefusal(Code.BAD_EVENT, f"unknown node {node_id!r}")
        return self.nodes[node_id]

    def _apply(self, ev: dict[str, Any]) -> dict[str, Any]:
        op = ev.get("op")
        handler = {"create": self._op_create, "open_round": self._op_open_round, "add_option": self._op_add_option,
                   "edit": self._op_edit, "attempt": self._op_attempt, "confirm": self._op_confirm,
                   "report": self._op_report, "abandon": self._op_abandon, "checkout": self._op_checkout,
                   "revive": self._op_revive}.get(str(op))
        if handler is None:
            raise TreeRefusal(Code.BAD_EVENT, f"unknown event {op!r}")
        if op != "create" and self.contract is None:
            raise TreeRefusal(Code.BAD_EVENT, "the first event must create the tree")
        return handler(ev)

    def _op_create(self, ev: dict[str, Any]) -> dict[str, Any]:
        if self.contract is not None:
            raise TreeRefusal(Code.BAD_EVENT, "tree already created")
        from ..verify import metrics as M
        if ev["metric"] not in M.METRICS:
            raise TreeRefusal(Code.BAD_EVENT, f"unknown metric {ev['metric']!r}")
        self.contract = RunContract.from_json(ev["contract"])
        self.metric, self.higher_is_better, self.labels_sha = ev["metric"], bool(ev["higher_is_better"]), \
            ev["labels_sha"]
        mr = ev.get("max_repairs", MAX_REPAIRS)  # logs written before the field existed used the module default
        if isinstance(mr, bool) or not isinstance(mr, int) or mr < 0:
            raise TreeRefusal(Code.BAD_EVENT, f"max_repairs must be a non-negative integer, not {mr!r}")
        self.max_repairs = mr
        cur = ev.get("curriculum", False)  # logs written before rule 8 had no curriculum
        th = ev.get("revive_theta", REVIVE_THETA)  # logs written before rule 7 used the module default
        if not isinstance(cur, bool):
            raise TreeRefusal(Code.BAD_EVENT, f"curriculum must be a boolean, not {cur!r}")
        if isinstance(th, bool) or not isinstance(th, int | float) or not 0.0 <= float(th) < 1.0:
            raise TreeRefusal(Code.BAD_EVENT, f"revive_theta must be a number in [0, 1), not {th!r}")
        self.curriculum, self.revive_theta = cur, float(th)
        self.root = "root"
        self.nodes["root"] = _Node("root", None, None, ev["root_code_hash"], dict(ev["root_config"]))
        return {"node": "root"}

    def _op_open_round(self, ev: dict[str, Any]) -> dict[str, Any]:
        parent = self._node(ev["parent"])
        if parent.status != "answered":
            raise TreeRefusal(Code.PROMOTION_REFUSED, f"{parent.id} has no metric yet; a round descends from an "
                                                      f"answered node")
        if parent.id != self.root and self.rounds[str(parent.round)].winner != parent.id and not parent.revived:
            raise TreeRefusal(Code.PROMOTION_REFUSED, f"{parent.id} is not the confirmed winner of its round "
                                                      f"(dev racing + fresh re-measure) nor a revived dead-end; a "
                                                      f"new round may descend only from the root, a confirmed winner "
                                                      f"or a revived node")
        if not str(ev.get("decision", "")).strip():
            raise TreeRefusal(Code.ROUND_INVALID, "a round names the ONE decision its options differ on")
        stage = ev.get("stage")
        self._check_stage(parent, stage)
        rid = f"r{len(self.rounds) + 1}"
        self.rounds[rid] = _Round(rid, parent.id, str(ev["decision"]), stage=stage)
        return {"round": rid}

    def _lineage_stage(self, node: _Node) -> int:
        """Index in STAGES of the round `node` belongs to (-1 for the root)."""
        if node.round is None:
            return -1
        stage = self.rounds[node.round].stage
        return STAGES.index(stage) if stage is not None else -1

    def _check_stage(self, parent: _Node, stage: Any) -> None:
        if not self.curriculum:
            if stage is not None:
                raise TreeRefusal(Code.ROUND_INVALID, "this tree was not created with the curriculum; a stage label "
                                                      "would claim an order nobody enforced")
            return
        if stage not in STAGES:
            raise TreeRefusal(Code.ROUND_INVALID, f"this tree enforces the curriculum {STAGES}; every round declares "
                                                  f"one of them, not {stage!r}")
        at, new = self._lineage_stage(parent), STAGES.index(stage)
        if not max(at, 0) <= new <= at + 1:
            here = STAGES[at] if at >= 0 else "the root"
            allowed = [STAGES[i] for i in range(max(at, 0), min(at + 2, len(STAGES)))]
            raise TreeRefusal(Code.CURRICULUM_ORDER, f"{parent.id} is at {here}; the curriculum {' -> '.join(STAGES)} "
                                                     f"allows only {allowed} next, not {stage!r}")

    def _check_tune_code(self, rnd: _Round, code: str) -> None:
        if rnd.stage == "tune" and code != self.nodes[rnd.parent].code_hash:
            raise TreeRefusal(Code.CURRICULUM_ORDER, f"round {rnd.id} is a tune round: its options keep the parent's "
                                                     f"code {self.nodes[rnd.parent].code_hash} and differ only in "
                                                     f"config; a code change belongs in a creative round")

    def _op_add_option(self, ev: dict[str, Any]) -> dict[str, Any]:
        rnd = self.rounds.get(str(ev.get("round")))
        if rnd is None:
            raise TreeRefusal(Code.ROUND_INVALID, f"unknown round {ev.get('round')!r}")
        if rnd.winner is not None:
            raise TreeRefusal(Code.ROUND_INVALID, f"round {rnd.id} is decided; a new option is a new round")
        if len(rnd.options) >= ROUND_MAX_OPTIONS:
            raise TreeRefusal(Code.ROUND_INVALID, f"round {rnd.id} already has {ROUND_MAX_OPTIONS} options")
        if ev.get("decision") != rnd.decision:
            raise TreeRefusal(Code.ROUND_INVALID, f"round {rnd.id} decides {rnd.decision!r}; an option for "
                                                  f"{ev.get('decision')!r} belongs in another round")
        self._check_tune_code(rnd, str(ev["code_hash"]))
        nid = f"n{len(self.nodes)}"
        self.nodes[nid] = _Node(nid, rnd.parent, rnd.id, str(ev["code_hash"]), dict(ev["config"]))
        rnd.options.append(nid)
        return {"node": nid}

    def _op_edit(self, ev: dict[str, Any]) -> dict[str, Any]:
        n = self._node(ev["node"])
        if n.status == "answered":
            raise TreeRefusal(Code.NODE_IMMUTABLE, f"{n.id} was answered by a run (content {n.content_hash}); "
                                                   f"it cannot change: put the new idea in a child round")
        if n.status == "abandoned":
            raise TreeRefusal(Code.NODE_ABANDONED, f"{n.id} was abandoned after {n.repairs} repair(s)")
        if n.attempts:
            last = n.attempts[-1]
            if ev.get("repair_of") != last.idx or last.outcome not in IMPLEMENTATION_FAILURES \
                    or last.idx in n.repaired:
                raise TreeRefusal(Code.REPAIR_UNLEDGERED, f"{n.id} has run; an edit must be a repair citing its "
                                                          f"last, unrepaired implementation failure "
                                                          f"(attempt {last.idx}: {last.outcome})")
            if n.repairs >= self.max_repairs:  # unreachable: a failure with no repair left abandons the node
                raise TreeRefusal(Code.REPAIR_CAP, f"{n.id} used its {self.max_repairs} repairs")
            n.repairs += 1
            n.repaired.add(last.idx)
        elif ev.get("repair_of") is not None:
            raise TreeRefusal(Code.REPAIR_UNLEDGERED, f"{n.id} has no failed attempt to repair")
        if n.round is not None:
            self._check_tune_code(self.rounds[n.round], str(ev["code_hash"]))
        n.code_hash, n.config = str(ev["code_hash"]), dict(ev["config"])
        return {"node": n.id, "repairs": n.repairs}

    def _op_attempt(self, ev: dict[str, Any]) -> dict[str, Any]:
        n = self._node(ev["node"])
        assert self.contract is not None
        if ev.get("contract") != self.contract.digest():
            raise TreeRefusal(Code.CONTRACT_MISMATCH, f"{n.id}: the run's contract {ev.get('contract')} differs "
                                                      f"from the tree's {self.contract.digest()} (command, "
                                                      f"environment, data inputs and budget are fixed)")
        if ev.get("code_hash") != n.code_hash:
            raise TreeRefusal(Code.CODE_MISMATCH, f"{n.id}: the run used code {ev.get('code_hash')}, not the node's "
                                                  f"committed code {n.code_hash} (unledgered change)")
        outcome, purpose = ev.get("outcome"), ev.get("purpose")
        if outcome not in OUTCOMES or purpose not in ("answer", "remeasure"):
            raise TreeRefusal(Code.BAD_EVENT, f"bad attempt outcome/purpose {outcome!r}/{purpose!r}")
        if outcome == "ok" and (ev.get("preds_ref") is None or ev.get("metric") is None):
            raise TreeRefusal(Code.BAD_EVENT, "an ok run carries its metric and dev predictions")
        snap = ev.get("snapshot")
        if snap is not None:
            self._check_snapshot(n, snap)
        if n.status == "abandoned":
            raise TreeRefusal(Code.NODE_ABANDONED, f"{n.id} was abandoned")
        if purpose == "answer" and n.status == "answered":
            raise TreeRefusal(Code.NODE_IMMUTABLE, f"{n.id} is already answered; a further run is a re-measure")
        if purpose == "remeasure":
            if n.status != "answered":
                raise TreeRefusal(Code.REMEASURE_INVALID, f"{n.id}: only an answered node is re-measured")
            if int(ev["seed"]) in {a.seed for a in n.attempts}:
                raise TreeRefusal(Code.REMEASURE_INVALID, f"{n.id}: a re-measure uses a fresh seed "
                                                          f"(seed {ev['seed']} already ran)")
        if purpose == "answer" and n.attempts and n.attempts[-1].outcome in IMPLEMENTATION_FAILURES \
                and n.attempts[-1].idx not in n.repaired:
            raise TreeRefusal(Code.REPAIR_UNLEDGERED, f"{n.id}: attempt {n.attempts[-1].idx} failed and was not "
                                                      f"repaired through the tree; re-running unchanged code is not "
                                                      f"a repair")
        a = _Attempt(len(n.attempts) + 1, str(purpose), int(ev["seed"]), str(outcome), ev.get("metric"),
                     ev.get("preds_ref"), n.code_hash, snap)
        n.attempts.append(a)
        if purpose == "answer":
            if outcome == "ok":
                n.status, n.content_hash, n.snapshot = "answered", _sha(_content(n, a)), snap
                if ev.get("content_hash") not in (None, n.content_hash):  # replay: the logged freeze must match
                    raise TreeRefusal(Code.BAD_EVENT, f"{n.id}: logged content hash does not match")
            elif n.repairs >= self.max_repairs:
                n.status = "abandoned"
        return {"node": n.id, "attempt": a.idx, "status": n.status, "content_hash": n.content_hash,
                "_log": {"content_hash": n.content_hash} if n.content_hash and purpose == "answer" else {}}

    def _op_abandon(self, ev: dict[str, Any]) -> dict[str, Any]:
        """The loop gives up on a provisional node (no repair attempted, or it moved on): ledgered, terminal. An
        answered node cannot be abandoned (its result stays on the record and in every racing comparison)."""
        n = self._node(ev["node"])
        if n.status == "answered":
            raise TreeRefusal(Code.NODE_IMMUTABLE, f"{n.id} was answered; an answered node is never withdrawn")
        if n.status == "abandoned":
            raise TreeRefusal(Code.NODE_ABANDONED, f"{n.id} is already abandoned")
        if not str(ev.get("reason", "")).strip():
            raise TreeRefusal(Code.BAD_EVENT, "an abandonment states its reason")
        n.status = "abandoned"
        return {"node": n.id}

    def _check_snapshot(self, n: _Node, snap: Any) -> None:
        try:
            manifest = self.store.blobs.get_json(str(snap))
        except (OSError, ValueError) as e:
            raise TreeRefusal(Code.BAD_EVENT, f"{n.id}: snapshot {snap!r} is not a readable stored blob ({e})") from e
        if not isinstance(manifest, dict) or manifest.get("kind") != "workspace_snapshot":
            raise TreeRefusal(Code.BAD_EVENT, f"{n.id}: {snap!r} is not a workspace snapshot manifest")

    def _frozen_snapshot(self, node_id: str) -> str:
        n = self._node(node_id)
        if n.status != "answered" or n.snapshot is None:
            raise TreeRefusal(Code.NO_SNAPSHOT, f"{n.id} ({n.status}) has no frozen workspace snapshot; only an "
                                                f"answered node whose answering run carried one can be checked out")
        return n.snapshot

    def _op_checkout(self, ev: dict[str, Any]) -> dict[str, Any]:
        snap = self._frozen_snapshot(str(ev.get("node")))
        if ev.get("snapshot") != snap:
            raise TreeRefusal(Code.BAD_EVENT, f"checkout of {ev.get('node')} names snapshot {ev.get('snapshot')}, "
                                              f"not the node's frozen {snap}")
        return {}

    def _p_improvement(self, n: _Node, labels: list[str]) -> tuple[float, str]:
        """min over the node's ok runs (answer + re-measures) of the bootstrap share of example resamples in which the
        node's dev metric is strictly better than its round parent's answering run."""
        from ..verify import metrics as M
        if n.id == self.root or n.status != "answered":
            raise TreeRefusal(Code.REVIVAL_REFUSED, f"{n.id} ({n.status}) has no dev evidence to revive on; only an "
                                                    f"answered non-root node can be a revivable dead-end")
        if _sha(list(labels)) != self.labels_sha:
            raise TreeRefusal(Code.REVIVAL_REFUSED, "these are not the dev labels the tree was created with")
        ref = self.nodes[str(n.parent)]
        ref_preds = self.store.blobs.get_json(self._ok_preds(ref, "answer")[0])
        sign = 1.0 if self.higher_is_better else -1.0
        k = len(labels)
        ps: list[float] = []
        for pref in [*self._ok_preds(n, "answer"), *self._ok_preds(n, "remeasure")]:
            preds = self.store.blobs.get_json(pref)
            va, vb = M.per_example(self.metric, labels, preds), M.per_example(self.metric, labels, ref_preds)
            rng = random.Random(REVIVE_SEED)
            wins = 0
            for _ in range(REVIVE_N_BOOT):
                idx = [rng.randrange(k) for _ in range(k)]
                if va is not None and vb is not None:
                    d = M.aggregate(self.metric, sum(va[i] for i in idx) / k) \
                        - M.aggregate(self.metric, sum(vb[i] for i in idx) / k)
                else:
                    t = [labels[i] for i in idx]
                    d = M.METRICS[self.metric](t, [preds[i] for i in idx]) \
                        - M.METRICS[self.metric](t, [ref_preds[i] for i in idx])
                wins += sign * d > 0
            ps.append(wins / REVIVE_N_BOOT)
        return min(ps), ref.id

    def _op_revive(self, ev: dict[str, Any]) -> dict[str, Any]:
        n = self._node(str(ev.get("node")))
        if n.revived or (n.round is not None and self.rounds[n.round].winner == n.id):
            raise TreeRefusal(Code.REVIVAL_REFUSED, f"{n.id} is not a dead-end (it is a confirmed winner or already "
                                                    f"revived)")
        if ev.get("labels_ref") is None:
            raise TreeRefusal(Code.BAD_EVENT, "revive needs the dev labels")
        labels = self.store.blobs.get_json(ev["labels_ref"])["tree_dev_labels"]
        p, ref = self._p_improvement(n, labels)
        if not p > self.revive_theta:
            raise TreeRefusal(Code.REVIVAL_REFUSED, f"{n.id}: P(improvement over {ref}) = {p:.4f} is not above "
                                                    f"theta = {self.revive_theta} (UNCALIBRATED); the dead-end stays "
                                                    f"dead")
        if "p_improvement" in ev and (ev["p_improvement"] != p or ev.get("theta") != self.revive_theta
                                      or ev.get("reference") != ref):  # replay: the logged numbers must recompute
            raise TreeRefusal(Code.BAD_EVENT, f"{n.id}: logged revival does not recompute")
        n.revived = True
        return {"node": n.id, "p_improvement": p, "theta": self.revive_theta, "reference": ref,
                "_log": {"p_improvement": p, "theta": self.revive_theta, "reference": ref}}

    def _ok_preds(self, n: _Node, purpose: str) -> list[str]:
        return [str(a.preds_ref) for a in n.attempts if a.purpose == purpose and a.outcome == "ok"]

    def _op_confirm(self, ev: dict[str, Any]) -> dict[str, Any]:
        rnd = self.rounds.get(str(ev.get("round")))
        if rnd is None or rnd.winner is not None:
            raise TreeRefusal(Code.ROUND_INVALID, f"round {ev.get('round')!r} is unknown or already decided")
        w = self._node(ev["winner"])
        if w.id not in rnd.options or w.status != "answered":
            raise TreeRefusal(Code.PROMOTION_REFUSED, f"{w.id} is not an answered option of round {rnd.id}")
        open_opts = [o for o in rnd.options if self.nodes[o].status == "provisional"]
        if open_opts:
            raise TreeRefusal(Code.PROMOTION_REFUSED, f"round {rnd.id} still has provisional options {open_opts}")
        remeasures = self._ok_preds(w, "remeasure")
        if not remeasures:
            raise TreeRefusal(Code.PROMOTION_REFUSED, f"{w.id} was measured by one run only; promotion needs a "
                                                      f"fresh re-measure (winner's curse)")
        if ev.get("labels_ref") is None:
            raise TreeRefusal(Code.BAD_EVENT, "confirm needs the dev labels")
        labels = self.store.blobs.get_json(ev["labels_ref"])["tree_dev_labels"]
        if _sha(list(labels)) != self.labels_sha:
            raise TreeRefusal(Code.PROMOTION_REFUSED, "these are not the dev labels the tree was created with")
        answered = [self.nodes[o] for o in rnd.options if o != w.id and self.nodes[o].status == "answered"]
        # A rival whose answering run made EXACTLY the winner's dev predictions is the same behaviour, not a rival:
        # no candidate can beat its own predictions under dev racing, so such a twin would block every round. The
        # twin is excluded from racing and listed in the selection record; any rival that differs on one row races.
        w_answer = self.store.blobs.get_json(self._ok_preds(w, "answer")[0])
        duplicates = [rv.id for rv in answered
                      if self.store.blobs.get_json(self._ok_preds(rv, "answer")[0]) == w_answer]
        rivals = [rv for rv in answered if rv.id not in duplicates]
        parent = self.nodes[rnd.parent]
        racer = default_racer(self.metric, self.higher_is_better)  # fixed, so a replay recomputes the same verdict
        losses: list[str] = []
        for rv in [parent, *rivals]:
            rv_preds = self.store.blobs.get_json(self._ok_preds(rv, "answer")[0])
            for ref in [*self._ok_preds(w, "answer"), *remeasures]:
                if not racer(labels, self.store.blobs.get_json(ref), rv_preds):
                    losses.append(f"{rv.id}@{ref[:12]}")
        if losses:
            raise TreeRefusal(Code.PROMOTION_REFUSED, f"{w.id} does not beat every rival and the parent under dev "
                                                      f"racing on its answer and every re-measure: {losses}")
        rnd.winner = w.id
        return {"round": rnd.id, "winner": w.id, "duplicates": duplicates, "selection": self.selection_record(rnd.id)}

    def _op_report(self, ev: dict[str, Any]) -> dict[str, Any]:
        if ev.get("shape") != self.shape():
            raise TreeRefusal(Code.BAD_EVENT, "logged shape does not match the tree")
        return {}

    # ------------------------------------------------------------ public API
    def open_round(self, parent: str, decision: str, *, stage: str | None = None) -> str:
        ev: dict[str, Any] = {"op": "open_round", "parent": parent, "decision": decision}
        if stage is not None:  # only when declared: rounds logged before rule 8 replay unchanged
            ev["stage"] = stage
        return str(self._commit(ev)["round"])

    def add_option(self, round_id: str, decision: str, code: bytes | str, config: dict[str, Any]) -> str:
        return str(self._commit({"op": "add_option", "round": round_id, "decision": decision,
                                 "code_hash": code_hash(code), "config": config})["node"])

    def edit(self, node_id: str, code: bytes | str, config: dict[str, Any], *, repair_of: int | None = None) -> None:
        self._commit({"op": "edit", "node": node_id, "code_hash": code_hash(code), "config": config,
                      "repair_of": repair_of})

    def record(self, node_id: str, run: RunOutcome, *, purpose: str = "answer") -> dict[str, Any]:
        """Log one run of a node. The runner reports the contract and code it actually used; both are checked."""
        preds_ref = self.store.blobs.put_json(run.preds) if run.preds is not None else None
        ev: dict[str, Any] = {"op": "attempt", "node": node_id, "purpose": purpose, "contract": run.contract.digest(),
                              "code_hash": run.code_hash, "seed": int(run.seed), "outcome": run.outcome,
                              "metric": run.metric, "preds_ref": preds_ref}
        if run.snapshot is not None:  # only when present: attempts logged before rule 6 replay unchanged
            ev["snapshot"] = run.snapshot
        return self._commit(ev)

    def run(self, node_id: str, runner: Callable[[RunContract, str, int], RunOutcome], *, seed: int,
            purpose: str = "answer") -> dict[str, Any]:
        """Run a node under the TREE's contract (the caller cannot pass another) and log the outcome."""
        assert self.contract is not None
        n = self._node(node_id)
        return self.record(node_id, runner(self.contract, n.code_hash, seed), purpose=purpose)

    def checkout(self, node_id: str, workspace: Path | str) -> str:
        """Exact backtracking (rule 6): restore `node_id`'s frozen workspace into `workspace`, verify it byte-exactly,
        log the backtrack and return the snapshot hash. Refusals (no frozen snapshot) are logged; a corrupted blob
        raises SnapshotError before the live workspace is touched."""
        self._stale_check()
        n = self.nodes.get(node_id)
        snap = n.snapshot if n is not None and n.status == "answered" else None
        ev = {"op": "checkout", "node": node_id, "snapshot": snap}
        if snap is None:
            self._commit(ev)  # refused (NO_SNAPSHOT / unknown node) and logged; raises
            raise AssertionError("unreachable: a checkout without a frozen snapshot is refused")  # pragma: no cover
        ws = Path(workspace)
        restore_snapshot(snap, ws, self.store.blobs)
        got = take_snapshot(ws, self.store.blobs)  # no `previous`: every file re-read, byte-exactness not trusted
        if _tree_of(self.store.blobs.get_json(got)) != _tree_of(self.store.blobs.get_json(snap)):
            self._append({"op": "refused", "code": Code.RESTORE_MISMATCH.value,
                          "attempted": {**ev, "restored": got}})
            raise TreeRefusal(Code.RESTORE_MISMATCH, f"{node_id}: the restored workspace differs from snapshot {snap}")
        self._commit(ev)
        return snap

    def p_improvement(self, node_id: str, dev_labels: list[str]) -> float:
        """P(improvement) of a node over its round parent (rule 7), without reviving it."""
        return self._p_improvement(self._node(node_id), list(dev_labels))[0]

    def revive(self, node_id: str, dev_labels: list[str]) -> dict[str, Any]:
        """Revive a dead-end if P(improvement) > theta (rule 7). Ledgered either way (a refusal is a `refused`
        event)."""
        ref = self.store.blobs.put_json({"tree_dev_labels": list(dev_labels)})
        return self._commit({"op": "revive", "node": node_id, "labels_ref": ref})

    def confirm(self, round_id: str, winner: str, dev_labels: list[str]) -> dict[str, Any]:
        """Promote `winner` (dev racing on its answer AND every fresh re-measure, against every answered rival and the
        round's parent). The dev labels are stored as a private blob so a replay recomputes the verdict."""
        ref = self.store.blobs.put_json({"tree_dev_labels": list(dev_labels)})
        return self._commit({"op": "confirm", "round": round_id, "winner": winner, "labels_ref": ref})

    def abandon(self, node_id: str, reason: str) -> None:
        self._commit({"op": "abandon", "node": node_id, "reason": reason})

    def node(self, node_id: str) -> NodeView:
        n = self._node(node_id)
        ans = next((a for a in n.attempts if a.purpose == "answer" and a.outcome == "ok"), None)
        return NodeView(n.id, n.parent, n.round, n.code_hash, dict(n.config), n.status, n.content_hash,
                        len(n.attempts), n.repairs, ans.metric if ans else None, n.snapshot, n.revived)

    def selection_record(self, round_id: str | None = None) -> dict[str, Any]:
        """Every attempt counts: options, runs (failures, answers, re-measures), repairs and refused events."""
        ids = self.rounds[round_id].options if round_id else [i for i in self.nodes if i != self.root]
        ns = [self.nodes[i] for i in ids]
        return {"round": round_id, "options": len(ns), "attempts": sum(len(n.attempts) for n in ns),
                "failed_attempts": sum(a.outcome != "ok" for n in ns for a in n.attempts),
                "remeasures": sum(a.purpose == "remeasure" for n in ns for a in n.attempts),
                "repairs": sum(n.repairs for n in ns),
                "abandoned": sum(n.status == "abandoned" for n in ns),
                "revivals": sum(n.revived for n in ns),
                "refused": sum(1 for e in self.events if e.get("op") == "refused"
                               and (round_id is None or e["attempted"].get("round") == round_id
                                    or e["attempted"].get("node") in ids))}

    def shape(self) -> dict[str, Any]:
        children: dict[str, list[str]] = {i: [] for i in self.nodes}
        for n in self.nodes.values():
            if n.parent is not None:
                children[n.parent].append(n.id)
        non_root = len(self.nodes) - 1
        root_children = len(children[str(self.root)])
        flat = non_root >= FLAT_FAN_MIN_NODES and root_children / non_root >= FLAT_FAN_ROOT_SHARE
        chains: list[list[str]] = []
        for nid, n in self.nodes.items():  # a chain starts at an only child whose parent is not itself an only child
            if n.parent is None or len(children[n.parent]) != 1:
                continue
            p = self.nodes[n.parent]
            if p.parent is not None and len(children[p.parent]) == 1:
                continue
            chain, cur = [nid], nid
            while len(children[cur]) == 1:
                cur = children[cur][0]
                chain.append(cur)
            chains.append(chain)
        noodles = [c for c in chains if len(c) >= NOODLE_MIN_CHAIN]

        def depth(i: str) -> int:
            return 1 + max((depth(c) for c in children[i]), default=0)

        return {"nodes": len(self.nodes), "depth": depth(str(self.root)) - 1, "root_children": root_children,
                "flat_fan": flat, "noodle": bool(noodles), "noodle_chains": noodles,
                "params": {"flat_fan_min_nodes": FLAT_FAN_MIN_NODES, "flat_fan_root_share": FLAT_FAN_ROOT_SHARE,
                           "noodle_min_chain": NOODLE_MIN_CHAIN}}

    def report(self) -> dict[str, Any]:
        """Log the shape diagnostics and the selection record (a `report` event in the tree log) and return them."""
        shape = self.shape()
        self._commit({"op": "report", "shape": shape})
        return {"tree": self.id, "head": self.head, "shape": shape, "selection": self.selection_record()}

    def report_lines(self) -> list[str]:
        """Markdown lines for the run report (included by research.claims.render_report)."""
        s, sel = self.shape(), self.selection_record()
        flags = [name for name, on in (("flat fan", s["flat_fan"]), ("noodle", s["noodle"])) if on]
        return ["## Experiment tree", "",
                f"- nodes: {s['nodes']}, depth: {s['depth']}, root children: {s['root_children']}",
                f"- shape warnings: {', '.join(flags) if flags else 'none'}",
                f"- attempts: {sel['attempts']} ({sel['failed_attempts']} failed, {sel['remeasures']} re-measures),"
                f" repairs: {sel['repairs']}, abandoned: {sel['abandoned']}, refused: {sel['refused']}",
                f"- log head: `{self.head}`", ""]


def _snapshot(t: ExperimentTree) -> Any:
    import copy
    return copy.deepcopy((t.contract, t.metric, t.higher_is_better, t.labels_sha, t.nodes, t.rounds, t.root,
                          t.max_repairs, t.curriculum, t.revive_theta))


def _restore(t: ExperimentTree, s: Any) -> None:
    (t.contract, t.metric, t.higher_is_better, t.labels_sha, t.nodes, t.rounds, t.root, t.max_repairs,
     t.curriculum, t.revive_theta) = s
