"""Router over branches (GOAL §5 L6.9): a select/materialize seam that fails closed, built only from released
gate verdict bits, and tested on the sealed split only through the one-shot `finalize_routed`.

Building (`build_router`):
  1. reads the `branch` records of the CURRENT trunk (branches made against an older trunk compared against a
     harness that is no longer the default: stale, ignored) and re-checks each against the whitelist
     (branches.check_record);
  1b. refuses a record whose routed harness has no passing leave-one-family/domain-out bit, re-checked against the
     release ledger like the cluster bits (invariant 16: routing never bypasses LOFO/LODO; branches.py);
  2. re-checks every tested cluster bit against the release ledger: each of its targets must carry a release with
     the record's note, channel `evolution_score`, alphabet `verdict_bit` (kerneld/releases.released). A bit the
     ledger does not account for is refused (RouteRefused), never silently dropped;
  3. hands a route policy a `GateEvidenceView`: an object holding ONLY those verified bits (no store, no scores, no
     task ids). Any other attribute raises EmbargoViolation and is counted (`attempts`);
  4. checks the table the policy returns: every route must be a (cluster -> branch) pair whose released bit is
     True. A route keyed by anything else (a task id, a cluster whose bit was False or never released) is refused:
     a policy that learned something the embargo forbids (e.g. per-task gate values read straight from the store)
     can only express it through such a route, and that is what this check refuses.
The default policy routes a cluster to the one branch that won it; two winners on one cluster are refused (a
head-to-head comparison between branches is not built), so the router never guesses.

Seam: `select(task)` -> spec hash, from the task's public descriptor alone (`cluster_key` attribute); no descriptor
-> RouteRefused. Unrouted clusters go to the trunk. `materialize(spec_hash)` -> HarnessSpec, only for the trunk or
a routed branch, and only if the stored spec re-hashes to the hash (fail closed on any mismatch).

Held-out test: `finalize_routed` follows evolve/finalize.py exactly (named human approver, the hash-chained ledger
decision BEFORE any sealed run, refused if any sealed task or its held-out data was finalized before by either
command) and compares the routed harness with the trunk, paired per sealed unit, pooled and per cluster.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..gateway import Backend
from ..kernel import Ledger
from ..kerneld.releases import released
from ..runtime.engine import approval_item_hash
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .branches import BRANCH_KIND, CLUSTER_KEYS, BranchRefused, check_record, cluster_of
from .evaluate import _SEALED_ACCESS, RunOutcome, SandboxFactory, SuiteTask, run_harness
from .finalize import FINALIZE_NODE, FinalizeRefused, prior_finalizations
from .multiplicity import held_out_target_id
from .selection import paired_ci


class RouteRefused(RuntimeError):
    pass


class EmbargoViolation(RuntimeError):
    """A route policy asked the evidence view for something other than released verdict bits."""


class GateEvidenceView:
    """Released (branch, cluster) verdict bits, and nothing else. Built from verified bits only, so even
    introspection finds no score, interval, task id or store handle in it."""

    __slots__ = ("_bits", "attempts")

    def __init__(self, bits: Mapping[tuple[str, str], bool]):
        for k, v in bits.items():
            if type(v) is not bool or not (isinstance(k, tuple) and len(k) == 2 and all(isinstance(x, str) for x in k)):
                raise EmbargoViolation("the evidence view holds (branch, cluster) -> bit only")
        object.__setattr__(self, "_bits", dict(bits))
        object.__setattr__(self, "attempts", [])

    def verdicts(self) -> list[tuple[str, str, bool]]:
        """(branch spec hash, cluster, released bit), sorted."""
        return sorted((b, c, v) for (b, c), v in self._bits.items())

    def winners(self, cluster: str) -> list[str]:
        return sorted(b for (b, c), v in self._bits.items() if c == cluster and v)

    def clusters(self) -> list[str]:
        return sorted({c for _, c in self._bits})

    def __getattr__(self, name: str) -> Any:  # only reached for attributes that do not exist
        self.attempts.append(name)
        raise EmbargoViolation(f"the gate evidence view has no {name!r}: only released verdict bits are routable")

    def __setattr__(self, name: str, value: Any) -> None:
        raise EmbargoViolation("the gate evidence view is read-only")


RoutePolicy = Callable[[GateEvidenceView], Mapping[str, str]]


def default_policy(view: GateEvidenceView) -> dict[str, str]:
    out = {}
    for c in view.clusters():
        w = view.winners(c)
        if len(w) > 1:
            raise RouteRefused(f"cluster {c!r} is won by {len(w)} branches: a head-to-head gate is not built, "
                               "so the router refuses to choose")
        if w:
            out[c] = w[0]
    return out


@dataclass(frozen=True)
class Router:
    trunk: str
    cluster_key: str
    routes: tuple[tuple[str, str], ...]  # (cluster, branch spec hash), sorted
    specs: tuple[tuple[str, str], ...]  # (spec hash, canonical spec json): the trunk and every routed branch

    def table(self) -> dict[str, Any]:
        return {"trunk": self.trunk, "cluster_key": self.cluster_key, "routes": dict(self.routes)}

    def table_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.table(), sort_keys=True).encode()).hexdigest()

    def select(self, task: SuiteTask) -> str:
        try:
            c = cluster_of(task, self.cluster_key)
        except BranchRefused as e:
            raise RouteRefused(str(e)) from None
        return dict(self.routes).get(c, self.trunk)

    def materialize(self, spec_hash: str) -> HarnessSpec:
        doc = dict(self.specs).get(spec_hash)
        if doc is None:
            raise RouteRefused(f"spec {spec_hash[:12]} is neither the trunk nor a routed branch")
        spec = HarnessSpec.model_validate(json.loads(doc))
        if spec.spec_hash() != spec_hash:
            raise RouteRefused(f"stored spec does not re-hash to {spec_hash[:12]} (fail closed)")
        return spec

    def route(self, task: SuiteTask) -> HarnessSpec:
        return self.materialize(self.select(task))


def _ledgered(store: EvidenceStore, target: str, note: str) -> bool:
    return any(b.get("note") == note and b.get("channel") == "evolution_score" and b.get("alphabet") == "verdict_bit"
               for b in released(store, target))


def verified_bits(store: EvidenceStore, trunk_hash: str, cluster_key: str) -> dict[tuple[str, str], bool]:
    """(branch, cluster) -> bit for every tested cluster of every branch on `trunk_hash`, each re-checked against
    the release ledger (RouteRefused on any record or bit the ledger does not account for)."""
    bits: dict[tuple[str, str], bool] = {}
    for rec in store.records(BRANCH_KIND):
        if not isinstance(rec, dict) or rec.get("trunk") != trunk_hash:
            continue
        try:
            check_record(rec)
        except BranchRefused as err:
            raise RouteRefused(f"branch record {str(rec.get('branch_id'))[:40]!r} refused: {err}") from None
        if rec["cluster_key"] != cluster_key:
            raise RouteRefused("branch records on this trunk use different cluster keys")
        # invariant 16: the routed harness's leave-one-family/domain-out bit must be a ledgered, passing release
        lofo = rec["lofo"]
        missing = [t for t in lofo["targets"] if not _ledgered(store, t, lofo["note"])]
        if missing:
            raise RouteRefused(f"branch {rec['branch_id']}: LOFO/LODO bit not in the release ledger for "
                               f"{len(missing)} target(s) (fail closed)")
        for c, e in rec["clusters"].items():
            if not e["tested"]:
                continue
            missing = [t for t in e["targets"] if not _ledgered(store, t, e["note"])]
            if missing:
                raise RouteRefused(f"branch {rec['branch_id']} cluster {c!r}: verdict bit not in the release ledger "
                                   f"for {len(missing)} target(s) (embargo; fail closed)")
            key = (rec["spec_hash"], c)
            if key in bits and bits[key] != e["wins"]:
                raise RouteRefused(f"conflicting released bits for branch {rec['branch_id']} cluster {c!r}")
            bits[key] = e["wins"]
    return bits


def build_router(store: EvidenceStore, trunk: HarnessSpec, *, cluster_key: str = "family",
                 policy: RoutePolicy = default_policy) -> Router:
    if cluster_key not in CLUSTER_KEYS:
        raise RouteRefused(f"cluster key {cluster_key!r} is not a public task attribute {CLUSTER_KEYS}")
    trunk_hash = trunk.spec_hash()
    bits = verified_bits(store, trunk_hash, cluster_key)
    view = GateEvidenceView(bits)
    table = policy(view)
    if not isinstance(table, Mapping):
        raise RouteRefused("a route policy returns a cluster -> branch mapping")
    routes = []
    for c, b in table.items():
        if not isinstance(c, str) or not isinstance(b, str):
            raise RouteRefused("route keys and values are strings")
        if bits.get((b, c)) is not True:
            raise RouteRefused(f"route {c!r} -> {b[:12]} is not backed by a released winning verdict bit "
                               "(the embargo forbids routing on anything else)")
        routes.append((c, b))
    specs = {trunk_hash: json.dumps(trunk.model_dump(mode="json"), sort_keys=True)}
    for _, b in routes:
        try:
            specs[b] = json.dumps(store.harness_spec(b), sort_keys=True)
        except Exception:  # noqa: BLE001 - any read failure: the branch cannot be materialized (fail closed)
            raise RouteRefused(f"branch spec {b[:12]} is not in the store") from None
    router = Router(trunk_hash, cluster_key, tuple(sorted(routes)), tuple(sorted(specs.items())))
    for h in specs:
        router.materialize(h)  # every routed spec must re-hash now, not first at finalize
    return router


# ---------------------------------------------------------------------------------- one-shot sealed test
Runner = Callable[[HarnessSpec, SuiteTask, int], RunOutcome]


def finalize_routed(router: Router, suite: list[SuiteTask], *, approver: str, store: EvidenceStore,
                    backends: Mapping[str, Backend] | None = None, sandbox_factory: SandboxFactory | None = None,
                    seeds: tuple[int, ...] = (0,), runner: Runner | None = None) -> dict[str, Any]:
    """HUMAN action (the pattern of evolve/finalize.py, sharing its once-per-sealed-split record): the routed
    harness and the trunk on every sealed task x seed, paired. `runner` defaults to `run_harness` with the sealed
    capability; tests pass a scripted runner (it still consumes the one-shot)."""
    approver = (approver or "").strip()
    if not approver:
        raise FinalizeRefused("finalize is a human action: --approver must name the human")
    sealed = sorted((t for t in suite if t.split == "sealed"), key=lambda t: t.task_id)
    if not sealed:
        raise FinalizeRefused("the suite has no sealed task")
    if not seeds or len(set(seeds)) != len(seeds):
        raise FinalizeRefused("seeds must be non-empty and distinct")
    if runner is None:
        if backends is None or sandbox_factory is None:
            raise FinalizeRefused("backends and a sandbox factory are needed to run the sealed split")
        bk, sf = dict(backends), sandbox_factory

        def runner(spec: HarnessSpec, task: SuiteTask, seed: int) -> RunOutcome:
            return run_harness(spec, task, seed, backends=bk, store=store, sandbox_factory=sf,
                               sealed_access=_SEALED_ACCESS)
    # routes are chosen from public descriptors BEFORE anything is recorded or run (and bound by the ledger entry)
    chosen = {t.task_id: router.select(t) for t in sealed}
    tasks = [{"task_id": t.task_id, "target": held_out_target_id(t.task_dir), "route": chosen[t.task_id],
              "cluster": cluster_of(t, router.cluster_key)} for t in sealed]
    used_ids, used_targets = set(), set()
    for p in prior_finalizations(store):
        for t in p.get("sealed") or []:
            used_ids.add(t.get("task_id"))
            used_targets.add(t.get("target"))
    again = [t["task_id"] for t in tasks if t["task_id"] in used_ids or t["target"] in used_targets]
    if again:
        raise FinalizeRefused(f"sealed task(s) {again} were already finalized: the sealed split is touched once "
                              "(a second look would make it a training signal)")
    trunk = router.materialize(router.trunk)
    specs = {h: router.materialize(h) for h in set(chosen.values())}
    item = {"spec_hash": f"router:{router.table_hash()}", "route_table": router.table(),
            "baseline_hash": router.trunk, "sealed": tasks, "seeds": list(seeds)}
    item_hash = approval_item_hash(item)
    entry = Ledger(store).append("approval", {
        "run_id": f"{FINALIZE_NODE}:{item_hash[:16]}", "node_id": FINALIZE_NODE, "item_hash": item_hash,
        "decision": "approved", "approver": approver, "note": "one-shot sealed-split evaluation of a router",
        "finalize": item})
    store.append_record("finalize", {"ledger_seq": entry.seq, "item_hash": item_hash, "approver": approver,
                                     "started_at": time.time(), "status": "started", **item})
    rows: list[dict[str, Any]] = []
    diffs: dict[str, list[float]] = {}
    for t in sealed:
        c = cluster_of(t, router.cluster_key)
        for s in seeds:
            r = runner(specs[chosen[t.task_id]], t, s)
            b = r if chosen[t.task_id] == router.trunk else runner(trunk, t, s)
            rows.append({"task_id": t.task_id, "seed": s, "cluster": c, "route": chosen[t.task_id],
                         "run_id": r.run_id, "score": r.score, "status": r.status,
                         "baseline_run_id": b.run_id, "baseline_score": b.score})
            if r.score is not None and b.score is not None:
                diffs.setdefault(c, []).append(r.score - b.score)
    pooled = [d for ds in diffs.values() for d in ds]
    m, lo, hi = paired_ci(pooled)
    per_cluster = {}
    for c, ds in sorted(diffs.items()):
        cm, clo, chi = paired_ci(ds)
        per_cluster[c] = {"n": len(ds), "mean_diff": cm, "lo": clo, "hi": chi}
    summary = {"ledger_seq": entry.seq, "item_hash": item_hash, "approver": approver, **item, "units": rows,
               "paired": {"n": len(pooled), "mean_diff": m, "lo": lo, "hi": hi}, "per_cluster": per_cluster}
    store.append_record("finalize", {**summary, "status": "done", "ended_at": time.time()})
    return summary
