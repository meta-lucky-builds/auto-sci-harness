"""Evidence embargo: an evolution campaign whose evolve split never touches held-out truth (docs/evolve_embargo.md).

Why (R2-2, docs/r2-2_transcript_bound.md, docs/alpha_accounts.md): an evolve-split run registers claims on its target
and releases verdict bits to the proposer. K4 multiplies a claim's term by every release logged before it, so a
campaign's family grows exponentially in its runs and `multiplicity.check_run` halts it. The transcript bound is honest
worst-case accounting and is not loosened here. `alpha_accounts.md` names the way out: release fewer bits.

What this mode does (`EvolveConfig.evidence = "embargo"`, opt-in):
  1. EVOLVE-split runs execute `dev_view(spec)`: the harness cut before its first claim. No claim, no held-out read, no
     certificate, no verdict bit, no family growth. The score is the public dev term alone (`evaluate.dev_only_score`).
     A private read attributed to such a run stops the campaign (`evaluate.EmbargoBroken`, fail closed).
  2. Selection evidence is public and spends nothing: the fresh re-measurement (one difference per TASK, not per seed)
     and the cost cap decide first. A candidate rejected there never reaches a gate run.
  3. GATE-split runs execute `gate_view(spec, n)`: replication off (its brief is 3 verdict-bit releases per certified
     run) and n_comparisons raised to the campaign's bound. Raising it only makes K4 stricter.
  4. Acceptance rests on Kernel verdicts only (`flips_gate`): an exact one-sided sign test on the gate TASKS where the
     candidate earned more certificates than the parent against those where it earned fewer. A dev gain alone can
     never be accepted (the audit finding: an edit raising attempts lifted max-CV with no new certificate).
  5. Each gated generation releases ONE verdict bit (accept/reject). The rejecting stage among gate evidence is not
     released: a rejection after the gate always reads `held_out_gate`, and the real stage stays host-only.
  6. Each held-out family must show no net loss of certificates (`lofo_flips`). This is "no regression observed", not
     "non-regression shown": the old rule (t-interval lower bound >= 0) needs t = 12.7 at n = 2 and rejected real gains.

The bound (`gate_family_bound`): one verdict bit per generation means a claim of generation g carries 2^g on top of the
transcripts already reachable, and only gate runs register claims, so N is known before the campaign starts. It counts
every gate task as one family (over-counting: a stricter level, never a laxer one). `check_run` still verifies every run
against the Kernel's own number, so an unforeseen release halts the campaign rather than loosening anything.

Limits, stated:
  - The per-generation level is gate_alpha / generations: a union bound over generations. It is not a bound over the
    transcripts of the accept bits, so a proposer that adapts to them is only controlled through K4's own accounting of
    each claim, not through this sign test.
  - Power is low by design: with m gate tasks the test can only pass when at least ceil(log2(1 / level)) tasks flip, and
    a campaign whose gate cannot reach that is refused. A parent already certified on most gate tasks leaves no room to
    win, so such a suite accepts nothing.
  - The level, the cost cap and the task-count floor are UNCALIBRATED (owner decision, GOAL §13.8).
  - Nothing here has run with a real model.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..kernel import alpha as A
from ..kernel.checks import related_targets, releases_on
from ..kernel.releases import Alphabet, transcript_bound
from ..kerneld.scope import ScopedKernel
from ..runtime.spec import END, HarnessSpec, MapNode, ModuleNode
from ..store import EvidenceStore
from ..verify.exclusion import sign_test_p
from .multiplicity import CLAIM_MODULE, held_out_target_id, kernel_family_size
from .selection import PairedSample, Unit, paired_ci

# the modules of a harness that read held-out truth or depend on it. An evolve-split run stops before all of them.
TRUTH_MODULES = frozenset({CLAIM_MODULE, "research.confirm", "research.certify", "research.replica_dev"})
REPORT_MODULE = "research.report"
AUDIT_MODULE = "research.audit"  # its leak scan reads private fingerprints: left out of the dev view
CERTIFY_MODULE = "research.certify"
PLAN_KIND = "evolve_embargo"  # host-only store record: the bound a campaign declared, reused on resume

EMBARGO_OBJECTIVE = """OBJECTIVE per task run you see: the relative improvement of the best candidate over the baseline
on the public dev estimate, clipped to [-1, 1]; -1 if the run fails. The runs you see stop before any held-out data is
touched: they make no claim and earn no certificate.
Edits are accepted only if the improvement holds on fresh seeds across the tasks you see, the edit costs about the same
as its parent, AND on hidden tasks you never see the harness earns Kernel certificates its parent did not, with no net
loss on any held-out family. You are told accept or reject, nothing else. A dev gain alone is never accepted, and extra
compute alone does not help."""


class EmbargoRefused(ValueError):
    """The campaign cannot run under the evidence embargo (fail closed, before any run)."""


@dataclass(frozen=True)
class EmbargoPlan:
    """What a campaign fixed before its first run: the n_comparisons every gate claim declares, the generations it
    was computed for, and the sign-test level per generation."""

    n_comparisons: int
    generations: int
    level: float


# ---------------------------------------------------------------------------------------------- spec views
def dev_view(spec: HarnessSpec) -> HarnessSpec:
    """`spec` cut before its first claim: every edge into a truth module goes to the report instead, the report ends
    the run, and the nodes no longer reachable are dropped. Pure. Fails closed when the spec has no single report node
    or a truth module survives the cut. A spec with no truth module is returned unchanged."""
    truth = {nid for nid, n in spec.nodes.items() if isinstance(n, ModuleNode) and n.module in TRUTH_MODULES}
    if not truth:
        return spec
    if spec.entry in truth:
        raise EmbargoRefused("the harness starts at a held-out step: it has no dev-only part")
    reports = [nid for nid, n in spec.nodes.items() if isinstance(n, ModuleNode) and n.module == REPORT_MODULE]
    if len(reports) != 1:
        raise EmbargoRefused(f"need exactly one {REPORT_MODULE} node to end a dev-only run, found {len(reports)}")
    report = reports[0]
    audits = {nid for nid, n in spec.nodes.items() if isinstance(n, ModuleNode) and n.module == AUDIT_MODULE}
    doc = spec.model_dump(mode="json")
    edges = []
    for e in doc["edges"]:
        if e["source"] in truth or e["source"] in audits:
            continue
        if e["target"] in audits:
            edges.append({**e, "target": END})
        elif e["target"] in truth:
            edges.append({**e, "target": report})
        else:
            edges.append(e)
    if not any(e["source"] == report for e in edges):
        edges.append({"source": report, "target": END, "when": None})
    keep = _reachable(spec, edges)
    survived = sorted(nid for nid in keep if nid in truth or nid in audits)
    if survived:  # unreachable by construction; kept fail closed
        raise EmbargoRefused(f"held-out step(s) {survived} are still reachable in the dev view")
    nodes = {nid: doc["nodes"][nid] for nid in spec.nodes if nid in keep}
    # the report's figure plots the held-out effect: there is none here
    nodes[report] = {**nodes[report], "params": {**nodes[report].get("params", {}), "figure": False}}
    return HarnessSpec.model_validate({**doc, "nodes": nodes, "edges": [e for e in edges if e["source"] in keep]})


def _reachable(spec: HarnessSpec, edges: list[dict[str, Any]]) -> set[str]:
    """Nodes reachable from the entry over `edges`, plus the body of every reachable map node."""
    keep: set[str] = set()
    todo = [spec.entry]
    while todo:
        nid = todo.pop()
        if nid in keep or nid == END:
            continue
        keep.add(nid)
        node = spec.nodes[nid]
        if isinstance(node, MapNode):
            todo.append(node.body)
        todo += [e["target"] for e in edges if e["source"] == nid]
    return keep


def gate_view(spec: HarnessSpec, n_comparisons: int) -> HarnessSpec:
    """`spec` with replication off and every claim node declaring at least `n_comparisons`. Both changes are applied
    by trusted host code to protected nodes; neither can loosen a check (a larger n_comparisons only lowers K4's level,
    and replication only ever added credit)."""
    doc = spec.model_dump(mode="json")
    for node in doc["nodes"].values():
        if node.get("kind") != "module":
            continue
        params = dict(node.get("params") or {})
        if node.get("module") == CLAIM_MODULE:
            params["n_comparisons"] = max(int(params.get("n_comparisons", 1)), int(n_comparisons))
        elif node.get("module") == CERTIFY_MODULE:
            params["replicate"] = False
        else:
            continue
        node["params"] = params
    return HarnessSpec.model_validate(doc)


# ---------------------------------------------------------------------------------------------- the bound
def gate_family_bound(*, generations: int, n_gate_tasks: int, n_claim_nodes: int, n_seeds: int, infra_retries: int,
                      base: int, reach: int) -> int:
    """Worst-case K4 transcript bound on a gate target at the end of the campaign. `base`: K4's N on the target now.
    `reach`: transcripts already reachable from its logged releases. Generation g adds its gate claims, each carrying
    reach x 2^g: g earlier accept/reject bits. The parent is new only in generation 0 (a later parent is an accepted
    candidate, already evaluated), hence 2 arms there and 1 after."""
    per_arm = n_gate_tasks * n_claim_nodes * n_seeds * (1 + infra_retries)
    return base + sum((2 if g == 0 else 1) * per_arm * reach * 2 ** g for g in range(generations))


def min_gate_tasks(level: float) -> int:
    """The fewest gate tasks with which the sign test can pass at `level`: all of m tasks flipping gives p = 2^-m."""
    return max(1, math.ceil(math.log2(1 / level)))


def plan_campaign(spec: HarnessSpec, store: EvidenceStore, gate_tasks: Sequence[Any], *, generations: int, n_seeds: int,
                  infra_retries: int, gate_alpha: float) -> EmbargoPlan:
    """The campaign's plan, or EmbargoRefused: a gate too small to ever accept; a target with an alpha-account (its
    slots are charged per release: not covered by this bound); a target already adaptively exposed (K6 would fail every
    claim on it)."""
    if generations < 1:
        raise EmbargoRefused("a campaign needs at least one generation")
    if not 0 < gate_alpha < 1:
        raise EmbargoRefused("gate_alpha must be in (0, 1)")
    level = gate_alpha / generations
    need = min_gate_tasks(level)
    if len(gate_tasks) < need:
        raise EmbargoRefused(f"{len(gate_tasks)} gate task(s) can never reach level {level:.3g}: the sign test needs "
                             f"at least {need} (add gate tasks, run fewer generations or raise gate_alpha)")
    primary = [n for n in spec.nodes.values() if isinstance(n, ModuleNode) and n.module == CLAIM_MODULE
               and not n.params.get("prefix")]
    if not primary:
        raise EmbargoRefused("the harness has no claim node: a gate of Kernel verdicts has nothing to compare")
    kernel = ScopedKernel(store)
    base = reach = 0
    for t in gate_tasks:
        target = held_out_target_id(t.task_dir)
        rel = related_targets(kernel.vault, target)
        if A.accounts(kernel.ledger, rel):
            raise EmbargoRefused(f"gate target {target} has an alpha-account: this bound does not cover its slots")
        rels = releases_on(kernel.ledger, rel)
        if any(a == Alphabet.CONTINUOUS for _, a in rels):
            raise EmbargoRefused(f"gate target {target} is adaptively exposed: no claim on it can certify")
        after_all = max((at for at, _ in rels), default=0) + 1
        reach = max(reach, transcript_bound([after_all], rels, kernel.config.count_alphabet))
        base = max(base, kernel_family_size(store, target))
    bound = gate_family_bound(generations=generations, n_gate_tasks=len(gate_tasks), n_claim_nodes=len(primary),
                              n_seeds=n_seeds, infra_retries=infra_retries, base=base, reach=reach)
    return EmbargoPlan(bound, generations, level)


def resolve_plan(store: EvidenceStore, campaign: str | None, completed: int, generations: int,
                 compute: Callable[[], EmbargoPlan]) -> EmbargoPlan:
    """The plan of a campaign. A resumed campaign reuses its declared plan, so both arms of a comparison keep one
    n_comparisons, and may not run past the generations that plan was computed for. Without a campaign nothing is
    persisted."""
    if campaign is None:
        return compute()
    rows = store.records_where(PLAN_KIND, "campaign", campaign)
    if not rows:
        plan = compute()
        store.append_record(PLAN_KIND, {"campaign": campaign, "n_comparisons": plan.n_comparisons,
                                        "generations": plan.generations, "level": plan.level})
        return plan
    if len(rows) != 1:
        raise EmbargoRefused(f"campaign {campaign!r} has {len(rows)} embargo plans (need 1)")
    plan = EmbargoPlan(int(rows[0]["n_comparisons"]), int(rows[0]["generations"]), float(rows[0]["level"]))
    if completed + generations > plan.generations:
        raise EmbargoRefused(f"campaign {campaign!r} was planned for {plan.generations} generation(s), {completed} "
                             f"done: {generations} more would pass the multiplicity bound it declared")
    return plan


# ---------------------------------------------------------------------------------------------- evidence
def task_level(sample: PairedSample) -> PairedSample:
    """One difference per task (the mean over its valid seeds). Seeds of one task are not independent units."""
    gone = set(sample.excluded)
    valid = [u for u in sample.units if u not in gone]
    by: dict[str, list[float]] = {}
    for u, d in zip(valid, sample.diffs, strict=True):
        by.setdefault(u[0], []).append(d)
    return PairedSample([(t, 0) for t in sorted(by)], [sum(by[t]) / len(by[t]) for t in sorted(by)])


def evolve_evidence(sample: PairedSample, *, require_ci: bool, max_excluded_frac: float, min_tasks: int = 2
                    ) -> dict[str, Any]:
    """The fresh re-measurement as acceptance evidence, at task level. Public data only: nothing here is released."""
    tasks = task_level(sample)
    out: dict[str, Any] = {"n": len(tasks.diffs), "excluded": len(sample.excluded), "mean_diff": None, "lo": None,
                           "hi": None, "passed": False, "infra_aborted": False}
    if sample.excluded_frac > max_excluded_frac:
        return out | {"infra_aborted": True, "reason": f"{len(sample.excluded)}/{sample.n_units} fresh units excluded "
                                                       f"for infrastructure failures (> {max_excluded_frac:.0%})"}
    if not all(math.isfinite(d) for d in tasks.diffs):
        return out | {"reason": "non-finite score difference (fail closed)"}
    if len(tasks.diffs) < min_tasks:
        return out | {"reason": f"only {len(tasks.diffs)} valid task(s) (< {min_tasks})"}
    m, lo, hi = paired_ci(tasks.diffs)
    out |= {"mean_diff": m, "lo": lo, "hi": hi}
    if not m > 0:
        return out | {"reason": f"no improvement on the fresh re-measurement (mean={m:.4g})"}
    if require_ci and not lo > 0:
        return out | {"reason": f"improvement not significant across tasks (CI lo={lo:.4g})"}
    return out | {"passed": True, "reason": "improved on the fresh re-measurement"}


class Outcome(Protocol):
    score: float | None
    verified: bool


@dataclass(frozen=True)
class Flips:
    """Gate tasks by Kernel verdicts: won = the candidate earned more certificates than the parent over the task's valid
    seeds, lost = fewer, tied = as many, excluded = no valid seed."""

    won: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    tied: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    excluded_units: int = 0
    units: int = 0
    per_task: dict[str, tuple[int, int, int]] = field(default_factory=dict)  # (candidate, parent, valid seeds)

    @property
    def p(self) -> float:
        return sign_test_p(len(self.won), len(self.lost))

    @property
    def excluded_frac(self) -> float:
        return self.excluded_units / self.units if self.units else 1.0


def verdict_flips(units: Sequence[Unit], candidate: Callable[[Unit], Outcome],
                  parent: Callable[[Unit], Outcome]) -> Flips:
    counts: dict[str, list[int]] = {}
    lost_units = 0
    for u in units:
        c, p = candidate(u), parent(u)
        row = counts.setdefault(u[0], [0, 0, 0])
        if c.score is None or p.score is None:  # infrastructure failure on either arm: the pair says nothing
            lost_units += 1
            continue
        row[0] += c.verified is True
        row[1] += p.verified is True
        row[2] += 1
    pick = lambda test: tuple(sorted(t for t, r in counts.items() if r[2] and test(r)))  # noqa: E731
    return Flips(pick(lambda r: r[0] > r[1]), pick(lambda r: r[0] < r[1]), pick(lambda r: r[0] == r[1]),
                 tuple(sorted(t for t, r in counts.items() if not r[2])), lost_units, len(units),
                 {t: (r[0], r[1], r[2]) for t, r in sorted(counts.items())})


def flips_gate(flips: Flips, *, level: float, max_excluded_frac: float) -> dict[str, Any]:
    """Exact one-sided sign test over gate tasks (ties carry no direction and are dropped, as in McNemar's test)."""
    out: dict[str, Any] = {"won": len(flips.won), "lost": len(flips.lost), "tied": len(flips.tied),
                           "excluded": len(flips.excluded), "p": flips.p, "level": level, "passed": False,
                           "infra_aborted": False}
    if flips.excluded_frac > max_excluded_frac:
        return out | {"infra_aborted": True, "reason": f"{flips.excluded_units}/{flips.units} gate units excluded for "
                                                       f"infrastructure failures (> {max_excluded_frac:.0%})"}
    if not flips.won:
        return out | {"reason": "the candidate earned no certificate the parent did not (a dev gain is not evidence)"}
    if len(flips.won) <= len(flips.lost) or flips.p > level:
        return out | {"reason": f"{len(flips.won)} task(s) won, {len(flips.lost)} lost: sign test p={flips.p:.3g} > "
                                f"{level:.3g}"}
    return out | {"passed": True, "reason": f"{len(flips.won)} task(s) won, {len(flips.lost)} lost: sign test "
                                            f"p={flips.p:.3g} <= {level:.3g}"}


def lofo_flips(tasks: Sequence[Any], families: Sequence[str], domains: Sequence[str], seeds: Sequence[int],
               candidate: Callable[[Unit], Outcome], parent: Callable[[Unit], Outcome], *,
               max_excluded_frac: float) -> dict[str, Any]:
    """Each held-out family and domain separately: no net loss of certificates on its gate tasks. Same outcomes as the
    pooled gate. `ood_drop` is won minus lost per group (negative: worse out of distribution). This says no regression
    was OBSERVED; with two or three tasks in a group it cannot show there is none."""
    out: dict[str, Any] = {"families": {}, "domains": {}, "ood_drop": {}, "rule": "no_net_certificate_loss"}
    failed: list[str] = []
    aborted: list[bool] = []
    for kind, names, attr in (("family", families, "family"), ("domain", domains, "domain")):
        for name in names:
            units = [(t.task_id, s) for t in tasks if t.split == "gate" and getattr(t, attr) == name for s in seeds]
            f = verdict_flips(units, candidate, parent)
            net = len(f.won) - len(f.lost)
            r: dict[str, Any] = {"won": len(f.won), "lost": len(f.lost), "tied": len(f.tied), "n": f.units,
                                 "excluded": f.excluded_units, "mean_diff": net, "passed": False}
            if f.excluded_frac > max_excluded_frac:
                r |= {"infra_aborted": True, "reason": f"{f.excluded_units}/{f.units} units excluded for "
                                                       f"infrastructure failures (> {max_excluded_frac:.0%})"}
            elif net < 0:
                r["reason"] = f"net loss of certificates ({len(f.won)} task(s) won, {len(f.lost)} lost)"
            else:
                r |= {"passed": True, "reason": "no net loss of certificates"}
            out["families" if kind == "family" else "domains"][name] = r
            out["ood_drop"][f"{kind}:{name}"] = net
            if not r["passed"]:
                failed.append(f"held-out {kind} {name}: {r['reason']}")
                aborted.append(bool(r.get("infra_aborted")))
    out["passed"] = not failed
    out["reason"] = "; ".join(failed) if failed else "no net loss on any held-out family or domain"
    out["infra_aborted"] = bool(failed) and all(aborted)  # a policy failure wins, as in loop._lofo
    return out
