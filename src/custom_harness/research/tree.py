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

Storage: the log is an append-only, hash-chained event list in the evidence store's `records` table (kind
`exptree`), one record per event. `ExperimentTree.load` replays the log through the SAME checks, so a forged,
reordered or deleted record is detected (`TreeTampered`). The records table is used until workstream kstats adds a
Kernel ledger kind for tree events (requested in the build16 mailbox); this is NOT the Kernel ledger.

Not done here: wiring into the research loop's modules (research/modules.py, owned by workstream yield) and into the
rendered run report (research/claims.py `render_report`, also yield's). `report_lines()` is the text to include.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------- parameters (one config place)
MAX_REPAIRS = 2  # UNCALIBRATED (owner decision, GOAL §13.8): repairs of implementation failures per node
ROUND_MAX_OPTIONS = 5  # UNCALIBRATED (owner decision, GOAL §13.8): sibling options in one round
RACE_ALPHA = 0.2  # UNCALIBRATED (owner decision, GOAL §13.8): two-sided level of the racing interval (as dev racing)
RACE_N_BOOT = 1000  # UNCALIBRATED (owner decision, GOAL §13.8): bootstrap resamples of the racing interval
FLAT_FAN_MIN_NODES = 5  # UNCALIBRATED (owner decision, GOAL §13.8): non-root nodes before "flat fan" can be flagged
FLAT_FAN_ROOT_SHARE = 0.8  # UNCALIBRATED (owner decision, GOAL §13.8): share of non-root nodes that are root children
NOODLE_MIN_CHAIN = 3  # UNCALIBRATED (owner decision, GOAL §13.8): consecutive only-children that make a "noodle"

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


@dataclass
class _Attempt:
    idx: int
    purpose: str  # answer | remeasure
    seed: int
    outcome: str
    metric: float | None
    preds_ref: str | None
    code_hash: str


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


@dataclass
class _Round:
    id: str
    parent: str
    decision: str
    options: list[str] = field(default_factory=list)
    winner: str | None = None


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


def _content(n: _Node, answer: _Attempt) -> dict[str, Any]:
    return {"id": n.id, "parent": n.parent, "round": n.round, "code_hash": n.code_hash, "config": n.config,
            "answer": {"idx": answer.idx, "seed": answer.seed, "metric": answer.metric, "preds_ref": answer.preds_ref}}


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
        self.nodes: dict[str, _Node] = {}
        self.rounds: dict[str, _Round] = {}
        self.root: str | None = None
        self.head, self.seq = GENESIS, 0
        self.events: list[dict[str, Any]] = []

    # ------------------------------------------------------------ construction / persistence
    @classmethod
    def create(cls, store: Any, tree_id: str, contract: RunContract, *, root_code: bytes | str,
               root_config: dict[str, Any], metric: str, higher_is_better: bool,
               dev_labels: list[str]) -> ExperimentTree:
        if store.records_where(RECORD_KIND, "tree", tree_id):
            raise TreeRefusal(Code.BAD_EVENT, f"tree {tree_id!r} already exists")
        t = cls(store, tree_id)
        t._commit({"op": "create", "contract": contract.to_json(), "root_code_hash": code_hash(root_code),
                   "root_config": root_config, "metric": metric, "higher_is_better": higher_is_better,
                   "labels_sha": _sha(list(dev_labels))})
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
                   "report": self._op_report}.get(str(op))
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
        self.root = "root"
        self.nodes["root"] = _Node("root", None, None, ev["root_code_hash"], dict(ev["root_config"]))
        return {"node": "root"}

    def _op_open_round(self, ev: dict[str, Any]) -> dict[str, Any]:
        parent = self._node(ev["parent"])
        if parent.status != "answered":
            raise TreeRefusal(Code.PROMOTION_REFUSED, f"{parent.id} has no metric yet; a round descends from an "
                                                      f"answered node")
        if parent.id != self.root and self.rounds[str(parent.round)].winner != parent.id:
            raise TreeRefusal(Code.PROMOTION_REFUSED, f"{parent.id} is not the confirmed winner of its round "
                                                      f"(dev racing + fresh re-measure); a new round may descend "
                                                      f"only from the root or a confirmed winner")
        if not str(ev.get("decision", "")).strip():
            raise TreeRefusal(Code.ROUND_INVALID, "a round names the ONE decision its options differ on")
        rid = f"r{len(self.rounds) + 1}"
        self.rounds[rid] = _Round(rid, parent.id, str(ev["decision"]))
        return {"round": rid}

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
            if n.repairs >= MAX_REPAIRS:  # unreachable: a failure with no repair left abandons the node
                raise TreeRefusal(Code.REPAIR_CAP, f"{n.id} used its {MAX_REPAIRS} repairs")
            n.repairs += 1
            n.repaired.add(last.idx)
        elif ev.get("repair_of") is not None:
            raise TreeRefusal(Code.REPAIR_UNLEDGERED, f"{n.id} has no failed attempt to repair")
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
                     ev.get("preds_ref"), n.code_hash)
        n.attempts.append(a)
        if purpose == "answer":
            if outcome == "ok":
                n.status, n.content_hash = "answered", _sha(_content(n, a))
                if ev.get("content_hash") not in (None, n.content_hash):  # replay: the logged freeze must match
                    raise TreeRefusal(Code.BAD_EVENT, f"{n.id}: logged content hash does not match")
            elif n.repairs >= MAX_REPAIRS:
                n.status = "abandoned"
        return {"node": n.id, "attempt": a.idx, "status": n.status, "content_hash": n.content_hash,
                "_log": {"content_hash": n.content_hash} if n.content_hash and purpose == "answer" else {}}

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
        rivals = [self.nodes[o] for o in rnd.options if o != w.id and self.nodes[o].status == "answered"]
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
        return {"round": rnd.id, "winner": w.id, "selection": self.selection_record(rnd.id)}

    def _op_report(self, ev: dict[str, Any]) -> dict[str, Any]:
        if ev.get("shape") != self.shape():
            raise TreeRefusal(Code.BAD_EVENT, "logged shape does not match the tree")
        return {}

    # ------------------------------------------------------------ public API
    def open_round(self, parent: str, decision: str) -> str:
        return str(self._commit({"op": "open_round", "parent": parent, "decision": decision})["round"])

    def add_option(self, round_id: str, decision: str, code: bytes | str, config: dict[str, Any]) -> str:
        return str(self._commit({"op": "add_option", "round": round_id, "decision": decision,
                                 "code_hash": code_hash(code), "config": config})["node"])

    def edit(self, node_id: str, code: bytes | str, config: dict[str, Any], *, repair_of: int | None = None) -> None:
        self._commit({"op": "edit", "node": node_id, "code_hash": code_hash(code), "config": config,
                      "repair_of": repair_of})

    def record(self, node_id: str, run: RunOutcome, *, purpose: str = "answer") -> dict[str, Any]:
        """Log one run of a node. The runner reports the contract and code it actually used; both are checked."""
        preds_ref = self.store.blobs.put_json(run.preds) if run.preds is not None else None
        return self._commit({"op": "attempt", "node": node_id, "purpose": purpose, "contract": run.contract.digest(),
                             "code_hash": run.code_hash, "seed": int(run.seed), "outcome": run.outcome,
                             "metric": run.metric, "preds_ref": preds_ref})

    def run(self, node_id: str, runner: Callable[[RunContract, str, int], RunOutcome], *, seed: int,
            purpose: str = "answer") -> dict[str, Any]:
        """Run a node under the TREE's contract (the caller cannot pass another) and log the outcome."""
        assert self.contract is not None
        n = self._node(node_id)
        return self.record(node_id, runner(self.contract, n.code_hash, seed), purpose=purpose)

    def confirm(self, round_id: str, winner: str, dev_labels: list[str]) -> dict[str, Any]:
        """Promote `winner` (dev racing on its answer AND every fresh re-measure, against every answered rival and the
        round's parent). The dev labels are stored as a private blob so a replay recomputes the verdict."""
        ref = self.store.blobs.put_json({"tree_dev_labels": list(dev_labels)})
        return self._commit({"op": "confirm", "round": round_id, "winner": winner, "labels_ref": ref})

    def node(self, node_id: str) -> NodeView:
        n = self._node(node_id)
        ans = next((a for a in n.attempts if a.purpose == "answer" and a.outcome == "ok"), None)
        return NodeView(n.id, n.parent, n.round, n.code_hash, dict(n.config), n.status, n.content_hash,
                        len(n.attempts), n.repairs, ans.metric if ans else None)

    def selection_record(self, round_id: str | None = None) -> dict[str, Any]:
        """Every attempt counts: options, runs (failures, answers, re-measures), repairs and refused events."""
        ids = self.rounds[round_id].options if round_id else [i for i in self.nodes if i != self.root]
        ns = [self.nodes[i] for i in ids]
        return {"round": round_id, "options": len(ns), "attempts": sum(len(n.attempts) for n in ns),
                "failed_attempts": sum(a.outcome != "ok" for n in ns for a in n.attempts),
                "remeasures": sum(a.purpose == "remeasure" for n in ns for a in n.attempts),
                "repairs": sum(n.repairs for n in ns),
                "abandoned": sum(n.status == "abandoned" for n in ns),
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
        """Markdown lines for the run report (research.claims.render_report, owned by yield, is not wired yet)."""
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
    return copy.deepcopy((t.contract, t.metric, t.higher_is_better, t.labels_sha, t.nodes, t.rounds, t.root))


def _restore(t: ExperimentTree, s: Any) -> None:
    t.contract, t.metric, t.higher_is_better, t.labels_sha, t.nodes, t.rounds, t.root = s
