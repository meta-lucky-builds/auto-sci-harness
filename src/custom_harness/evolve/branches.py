"""Branches (GOAL §5 L6.9; Mixture of Branches, Adaptive Auto-Harness): a candidate that wins on a CLUSTER of gate
tasks but not globally becomes a branch, routed only on the clusters it won.

What a branch is built from (verdict embargo, GOAL §16.5.3, invariant 18):
  - Clusters are values of ONE public task attribute from a closed set (`CLUSTER_KEYS`: the task family or domain),
    so a sealed or future task can be routed from its metadata alone, without its labels.
  - Per cluster, host code computes the paired Student-t interval of candidate - trunk over the cluster's GATE units
    (Bonferroni over the clusters tested for this candidate). That interval never leaves this function. Only the
    win BIT is kept, and it is a typed release (`Releases.emit`, channel `evolution_score`, alphabet `verdict_bit`)
    on every target the cluster's runs read, so K4 counts it; in a replayed campaign the bit is pinned.
  - A cluster with fewer than BRANCH_MIN_CLUSTER_TASKS gate tasks is not tested (its bit would be a per-task gate
    value) and is never routed.
  - The `branch` record is a whitelist: ids, hashes, cluster names, task COUNTS (campaign structure), target ids,
    release notes and the bits. No mean, CI, score or task id of a gate task is stored in it (`check_record`).
The router (router.py) reads branch records only and re-checks every bit against the release ledger.

Leave-one-family/domain-out (invariant 16; a route never bypasses it): a branch is recorded only if the ROUTED
harness it would create (the candidate on its won clusters, the trunk everywhere else) passes the same non-regression
check as the global gate's LOFO/LODO (selection.non_regression: paired-CI lower bound of routed - trunk >= -eps) on
EACH held-out family and EACH held-out domain of the campaign (evaluate.check_held_out) separately. The check is
needed because a won cluster can contain a held-out group the cluster win does not speak for (cluster key `domain`:
a domain won pooled while one held-out family inside it regresses; or a family spanning a held-out domain). Its
result is ONE more verdict bit, released like the cluster bits (note `branch_lofo:<branch id>`, on every target the
held-out groups' runs read) and carried in the record as `lofo` {passed, targets, note}; the router refuses a record
without a released, passing `lofo` bit. eps = min(the branch config's eps, the loop's `non_regression_eps`): never
looser than the global gate.

Sealed tasks are refused here (invariant 6): the router's held-out test is `router.finalize_routed`, the one-shot
human command, never this function.

What is NOT controlled (stated): the per-cluster tests are Bonferroni-corrected within one candidate, not across
generations; a branch's cluster win is a selection on the gate split, which is exactly why its worth is only
claimed from `finalize_routed` on the sealed split.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import config
from ..kerneld.releases import Releases, note_code, private_reads
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .evaluate import RunOutcome, SuiteTask, check_held_out
from .selection import Unit, exclusion_check, non_regression, paired_ci, paired_sample

BRANCH_KIND = "branch"
POLICY_VERSION = "branch-v1"
CLUSTER_KEYS = ("family", "domain")  # closed set of PUBLIC task attributes a route may depend on
# the only fields a branch record may carry, and per cluster entry (check_record)
RECORD_FIELDS = frozenset({"branch_id", "trunk", "spec_hash", "cluster_key", "clusters", "won", "lofo",
                           "policy_version", "at"})
CLUSTER_FIELDS = frozenset({"tested", "wins", "n_tasks", "targets", "note", "why"})
LOFO_FIELDS = frozenset({"passed", "targets", "note"})
UNTESTED_WHY = ("too_few_tasks", "no_targets")


class BranchRefused(ValueError):
    pass


@dataclass(frozen=True)
class BranchConfig:
    cluster_key: str = "family"
    alpha: float = config.BRANCH_ALPHA
    min_cluster_tasks: int = config.BRANCH_MIN_CLUSTER_TASKS
    min_units: int = config.BRANCH_MIN_UNITS
    max_excluded_frac: float = config.BRANCH_MAX_EXCLUDED_FRAC
    # LOFO/LODO non-regression margin of the routed harness (EvolveConfig.non_regression_eps's default; the loop
    # passes its own and the smaller one is used)
    non_regression_eps: float = 0.0

    def __post_init__(self) -> None:
        if self.cluster_key not in CLUSTER_KEYS:
            raise BranchRefused(f"cluster key {self.cluster_key!r} is not a public task attribute {CLUSTER_KEYS}")
        if not 0 < self.alpha < 1 or self.min_cluster_tasks < 2 or self.min_units < 2:
            raise BranchRefused("branch config out of range (alpha in (0,1), min_cluster_tasks >= 2, "
                                "min_units >= 2)")
        if not (math.isfinite(self.non_regression_eps) and self.non_regression_eps >= 0):
            raise BranchRefused("non_regression_eps must be finite and >= 0")


def cluster_of(task: SuiteTask, key: str) -> str:
    """The public descriptor of a task (fail closed on an unknown key or an empty value)."""
    if key not in CLUSTER_KEYS:
        raise BranchRefused(f"cluster key {key!r} is not a public task attribute {CLUSTER_KEYS}")
    value = getattr(task, key, "")
    if not isinstance(value, str) or not value:
        raise BranchRefused(f"task {task.task_id} has no {key}: it cannot be clustered or routed")
    return value


def branch_id(trunk: str, spec_hash: str) -> str:
    return f"{trunk[:12]}-{spec_hash[:12]}"


def cluster_note(bid: str, cluster: str) -> str:
    return note_code("branch", bid, cluster)


def lofo_note(bid: str) -> str:
    """A different prefix from cluster_note, so no cluster name can collide with it."""
    return note_code("branch_lofo", bid)


def _default_targets(outcome: Callable[[HarnessSpec, Unit], RunOutcome], store: EvidenceStore,
                     arms: tuple[HarnessSpec, HarnessSpec]) -> Callable[[list[Unit]], list[str]]:
    def targets(units: list[Unit]) -> list[str]:
        runs = {outcome(x, u).run_id for x in arms for u in units}
        return sorted({t for r in runs if r for t in private_reads(store, run_id=r)})
    return targets


def propose_branch(trunk: HarnessSpec, candidate: HarnessSpec, tasks: list[SuiteTask], *, seeds: tuple[int, ...],
                   outcome: Callable[[HarnessSpec, Unit], RunOutcome], store: EvidenceStore, releases: Releases,
                   globally_accepted: bool, cfg: BranchConfig | None = None,
                   targets_for: Callable[[list[Unit]], list[str]] | None = None,
                   lofo_eps: float | None = None) -> dict[str, Any] | None:
    """Test `candidate` against `trunk` on each gate cluster; record and return a `branch` record if it wins on at
    least one cluster and was NOT accepted globally (a global winner becomes the trunk instead). None otherwise.

    `outcome(spec, unit)`: the evaluator's cached run (Evaluator.outcome). `targets_for(units)`: the held-out
    targets those units' runs read (default: the Kernel's private-read log of both arms' runs)."""
    cfg = cfg or BranchConfig()
    sealed = sorted(t.task_id for t in tasks if t.split == "sealed")
    if sealed:
        raise BranchRefused(f"sealed task(s) {sealed[:5]} cannot inform a branch (finalize only)")
    if globally_accepted:
        return None
    if not seeds:
        raise BranchRefused("no seeds")
    trunk_hash, cand_hash = trunk.spec_hash(), candidate.spec_hash()
    if trunk_hash == cand_hash:
        raise BranchRefused("a branch must differ from its trunk")
    bid = branch_id(trunk_hash, cand_hash)
    targets_for = targets_for or _default_targets(outcome, store, (candidate, trunk))
    by_cluster: dict[str, list[SuiteTask]] = {}
    for t in tasks:
        if t.split == "gate":
            by_cluster.setdefault(cluster_of(t, cfg.cluster_key), []).append(t)
    testable = sorted(c for c, ts in by_cluster.items() if len(ts) >= cfg.min_cluster_tasks)
    level = cfg.alpha / max(1, len(testable))  # Bonferroni over this candidate's cluster tests
    clusters: dict[str, dict[str, Any]] = {}
    for c in sorted(by_cluster):
        ts = sorted(by_cluster[c], key=lambda t: t.task_id)
        entry: dict[str, Any] = {"n_tasks": len(ts)}
        if c not in testable:
            clusters[c] = entry | {"tested": False, "wins": False, "why": "too_few_tasks"}
            continue
        units = [(t.task_id, s) for t in ts for s in seeds]
        targets = sorted(set(targets_for(units)))
        if not targets:  # a bit no ledger entry can account for is never computed into a route (fail closed)
            clusters[c] = entry | {"tested": False, "wins": False, "why": "no_targets"}
            continue
        win = _cluster_win(units, candidate, trunk, outcome, cfg, level)  # host-only interval, reduced to a bit
        note = cluster_note(bid, c)
        bit = releases.emit(win, targets=targets, channel="evolution_score", alphabet="verdict_bit", note=note,
                            key=f"{trunk_hash}|{cand_hash}|{c}")
        if not isinstance(bit, bool):  # a pinned value that is not a bit: fail closed
            raise BranchRefused(f"cluster {c}: released verdict is not a bit")
        clusters[c] = entry | {"tested": True, "wins": bit, "targets": targets, "note": note}
    won = sorted(c for c, e in clusters.items() if e["wins"])
    if not won:
        return None
    # invariant 16: the ROUTED harness (candidate on the won clusters, trunk elsewhere) must pass LOFO/LODO on every
    # held-out family and domain, host-only, reduced to one released bit
    eps = cfg.non_regression_eps if lofo_eps is None else min(cfg.non_regression_eps, lofo_eps)
    groups = _held_out_groups(tasks, seeds)
    lofo_units = sorted({u for us in groups.values() for u in us})
    lofo_targets = sorted(set(targets_for(lofo_units)))
    if not lofo_targets:  # the LOFO bit could not be ledgered: no branch (fail closed)
        return None
    passed = _routed_lofo(groups, set(won), {t.task_id: t for t in tasks}, candidate, trunk, outcome, cfg, eps)
    lnote = lofo_note(bid)
    lbit = releases.emit(passed, targets=lofo_targets, channel="evolution_score", alphabet="verdict_bit",
                         note=lnote, key=f"{trunk_hash}|{cand_hash}|lofo")
    if not isinstance(lbit, bool):
        raise BranchRefused("released LOFO verdict is not a bit")
    if not lbit:
        return None
    rec = {"branch_id": bid, "trunk": trunk_hash, "spec_hash": cand_hash, "cluster_key": cfg.cluster_key,
           "clusters": clusters, "won": won, "lofo": {"passed": True, "targets": lofo_targets, "note": lnote},
           "policy_version": POLICY_VERSION, "at": time.time()}
    check_record(rec)
    store.add_harness_version(candidate.id, candidate.model_dump(mode="json"), parent_hash=trunk_hash)
    store.append_record(BRANCH_KIND, rec)
    return rec


def _held_out_groups(tasks: list[SuiteTask], seeds: tuple[int, ...]) -> dict[str, list[Unit]]:
    """'family:<f>' / 'domain:<d>' -> gate units of each held-out group (evaluate.check_held_out: refused, fail
    closed, when the campaign has no held-out family, or no held-out domain once it spans >= 2 domains)."""
    try:
        families, domains = check_held_out(tasks)
    except ValueError as e:
        raise BranchRefused(f"no LOFO/LODO gate for this campaign: {e}") from None
    out: dict[str, list[Unit]] = {}
    for kind, names in (("family", families), ("domain", domains)):
        for name in names:
            out[f"{kind}:{name}"] = [(t.task_id, s) for t in sorted(tasks, key=lambda t: t.task_id)
                                     if t.split == "gate" and getattr(t, kind) == name for s in seeds]
    return out


def _routed_lofo(groups: dict[str, list[Unit]], won: set[str], by_id: dict[str, SuiteTask], cand: HarnessSpec,
                 trunk: HarnessSpec, outcome: Callable[[HarnessSpec, Unit], RunOutcome], cfg: BranchConfig,
                 eps: float) -> bool:
    """Every held-out group non-regressing for the routed harness vs the trunk (selection.non_regression, the
    global LOFO gate's check). On a unit of an unrouted cluster the routed harness IS the trunk (difference 0)."""
    def routed(u: Unit) -> float | None:
        on_branch = cluster_of(by_id[u[0]], cfg.cluster_key) in won
        return outcome(cand if on_branch else trunk, u).score

    for units in groups.values():
        r = non_regression(paired_sample(units, routed, lambda u: outcome(trunk, u).score),
                           non_regression_eps=eps, max_excluded_frac=cfg.max_excluded_frac)
        if not r["passed"]:
            return False
    return True


def _cluster_win(units: list[Unit], cand: HarnessSpec, trunk: HarnessSpec,
                 outcome: Callable[[HarnessSpec, Unit], RunOutcome], cfg: BranchConfig, level: float) -> bool:
    s = paired_sample(units, lambda u: outcome(cand, u).score, lambda u: outcome(trunk, u).score)
    if not all(math.isfinite(d) for d in s.diffs) or len(s.diffs) < cfg.min_units:
        return False
    if s.excluded_frac > cfg.max_excluded_frac or not exclusion_check(s)["passed"]:
        return False
    _, lo, _ = paired_ci(s.diffs, alpha=level)
    return bool(lo > 0)


def check_record(rec: Any) -> None:
    """Fail closed unless `rec` is a well-formed branch record carrying nothing beyond the whitelist: every value
    is an id/name/code string, a bool, a task count or a target-id list. A continuous number anywhere (a mean, a CI
    bound, a score) or an unknown field is refused."""
    if not isinstance(rec, dict) or set(rec) - RECORD_FIELDS or not RECORD_FIELDS - {"at"} <= set(rec):
        raise BranchRefused("branch record fields are not the whitelist")
    for k in ("branch_id", "trunk", "spec_hash", "policy_version"):
        if not isinstance(rec[k], str) or not rec[k]:
            raise BranchRefused(f"branch record {k} is not a string")
    if rec["cluster_key"] not in CLUSTER_KEYS:
        raise BranchRefused("branch record cluster key is not a public attribute")
    if "at" in rec and not isinstance(rec["at"], (int, float)):
        raise BranchRefused("branch record time is not a number")
    clusters = rec["clusters"]
    if not isinstance(clusters, dict):
        raise BranchRefused("branch record clusters is not a mapping")
    for c, e in clusters.items():
        if not isinstance(c, str) or not c or not isinstance(e, dict) or set(e) - CLUSTER_FIELDS:
            raise BranchRefused(f"branch record cluster {c!r} carries fields beyond the whitelist")
        if type(e.get("tested")) is not bool or type(e.get("wins")) is not bool:
            raise BranchRefused(f"cluster {c!r}: tested/wins must be bits")
        if type(e.get("n_tasks")) is not int or e["n_tasks"] < 0:
            raise BranchRefused(f"cluster {c!r}: n_tasks must be a count")
        if e["tested"]:
            if "why" in e or not isinstance(e.get("note"), str) or not isinstance(e.get("targets"), list) or \
                    not e["targets"] or not all(isinstance(t, str) and t for t in e["targets"]):
                raise BranchRefused(f"cluster {c!r}: a tested cluster needs its release note and targets")
        elif e["wins"] or e.get("why") not in UNTESTED_WHY or "note" in e or "targets" in e:
            raise BranchRefused(f"cluster {c!r}: an untested cluster carries no verdict")
    won = rec["won"]
    if not isinstance(won, list) or sorted(won) != sorted(c for c, e in clusters.items() if e["wins"]) or not won:
        raise BranchRefused("branch record `won` disagrees with its cluster bits")
    lofo = rec["lofo"]
    if not isinstance(lofo, dict) or set(lofo) != LOFO_FIELDS or lofo["passed"] is not True \
            or lofo["note"] != lofo_note(rec["branch_id"]) or not isinstance(lofo["targets"], list) \
            or not lofo["targets"] or not all(isinstance(t, str) and t for t in lofo["targets"]):
        raise BranchRefused("branch record has no passing leave-one-family/domain-out bit (invariant 16)")
