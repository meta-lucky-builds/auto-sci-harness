"""Search over search (GOAL §5 L7; MILO islands, SIA lever choice): islands with different proposer and mutator
mixes, and a deterministic orchestrator that detects stalls and reallocates budget across islands, edit types and
levers, logging the lever it chose and the evidence for it.

What the orchestrator may read (verdict embargo, invariant 18): one `GenerationSignal` per island generation,
built ONLY from proposer-visible fields: the lineage/stats whitelists of evolve/export.py (accept bit, counters,
the rejecting stage `rejected_on`, the edit types proposed). No gate mean, CI, per-task value or score enters it;
a signal with any other field, or a non-count number, is refused (`GenerationSignal.from_view`).

Decisions per round (`Orchestrator.step`), for each island in id order:
  - stalled = at least `stall_window` generations observed and no accept in the last `stall_window`;
  - lever for a stalled island: `more_evaluation` (one more seed per unit, fewer edits for the same budget) when at
    least `more_eval_share` of its gated rejections in the window were on the evolve re-measurement (the
    candidates may be real but underpowered), otherwise `harness_edit` with its mutator mix diversified (the edit
    types it used most in the window are halved) and its budget weight shrunk by `stall_shrink`;
    `weights` (L8) is a declared lever that is not built: it is never chosen;
  - budget: shares proportional to the island weights with a floor of `min_island_share`, rounded by largest
    remainder (ties by island id) so the shares sum to the total exactly;
  - edit-type quota: a planned batch has at most floor(prompt_max_share * n) prompt-only edits and, when n allows,
    at least `min_edit_types` distinct types (`plan_edit_types`); `admit` applies the same quota to a proposal.
Every decision is returned and (with a store) appended as an `orchestrator_decision` record carrying the policy
version and hash, the lever and the evidence counts it rested on.

The policy is versioned (`OrchestratorPolicy.version` + a hash of its parameters) and may change only on a slower
cadence: `set_policy` refuses a reused version, the same version with different parameters, and a change less
than `cadence` rounds after the previous one. Not built: gating a policy change on measured meta-level gain.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import config
from ..evolve.edits import EditType

LEVERS = ("harness_edit", "more_evaluation", "weights")
BUILT_LEVERS = ("harness_edit", "more_evaluation")  # weights = L8, not built
PROMPT_TYPE = EditType.PROMPT_TEXT.value
EDIT_TYPES = tuple(e.value for e in EditType)
REJECT_STAGES = ("evolve", "held_out_gate", "cost", "exclusion_asymmetry")
SIGNAL_FIELDS = frozenset({"island", "generation", "accepted", "proposed", "gated", "rejected_on", "edit_types"})
DECISION_KIND = "orchestrator_decision"
POLICY_KIND = "orchestrator_policy"


class IslandError(ValueError):
    pass


@dataclass(frozen=True)
class OrchestratorPolicy:
    version: str
    stall_window: int = config.ISLAND_STALL_WINDOW
    more_eval_share: float = config.ISLAND_MORE_EVAL_SHARE
    stall_shrink: float = config.ISLAND_STALL_SHRINK
    min_island_share: float = config.ISLAND_MIN_SHARE
    prompt_max_share: float = config.ISLAND_PROMPT_MAX_SHARE
    min_edit_types: int = config.ISLAND_MIN_EDIT_TYPES
    cadence: int = config.ISLAND_POLICY_CADENCE
    max_seeds: int = 4  # bound on the more-evaluation lever (machine budget, not claim policy)

    def __post_init__(self) -> None:
        if not self.version:
            raise IslandError("a policy needs a version")
        if (self.stall_window < 1 or not 0 < self.more_eval_share <= 1 or not 0 < self.stall_shrink <= 1
                or not 0 <= self.min_island_share < 1 or not 0 <= self.prompt_max_share <= 1
                or self.min_edit_types < 1 or self.cadence < 1 or self.max_seeds < 1):
            raise IslandError("orchestrator policy parameter out of range")

    def policy_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Island:
    island_id: str
    proposer_mix: Mapping[str, float]  # proposer (backend) name -> weight
    mutator_mix: Mapping[str, float]  # EditType value -> weight

    def __post_init__(self) -> None:
        for name, mix in (("proposer", self.proposer_mix), ("mutator", self.mutator_mix)):
            if not mix or any(not isinstance(w, (int, float)) or isinstance(w, bool) or not math.isfinite(w)
                              or w < 0 for w in mix.values()) or sum(mix.values()) <= 0:
                raise IslandError(f"island {self.island_id}: {name} mix needs non-negative weights, not all zero")
        bad = sorted(set(self.mutator_mix) - set(EDIT_TYPES))
        if bad:
            raise IslandError(f"island {self.island_id}: unknown edit types {bad}")

    def key(self) -> str:
        return json.dumps([_norm(self.proposer_mix), _norm(self.mutator_mix)], sort_keys=True)


@dataclass(frozen=True)
class GenerationSignal:
    island: str
    generation: int
    accepted: bool
    proposed: int
    gated: int
    rejected_on: tuple[str, ...]  # one stage per gated, rejected candidate
    edit_types: tuple[str, ...]  # edit type of each proposed edit

    @classmethod
    def from_view(cls, doc: Mapping[str, Any]) -> GenerationSignal:
        """From a proposer-visible summary (export.stats_view/lineage_view fields). Fail closed on any other field
        or on a value outside its alphabet (a continuous number is never accepted)."""
        extra = set(doc) - SIGNAL_FIELDS
        if extra or SIGNAL_FIELDS - set(doc):
            raise IslandError(f"generation signal fields are not the whitelist (extra: {sorted(extra)})")
        if type(doc["accepted"]) is not bool:
            raise IslandError("accepted must be a bit")
        for k in ("generation", "proposed", "gated"):
            if type(doc[k]) is not int or doc[k] < 0:
                raise IslandError(f"{k} must be a count")
        rej, types = tuple(doc["rejected_on"]), tuple(doc["edit_types"])
        if any(r not in REJECT_STAGES for r in rej) or any(t not in EDIT_TYPES for t in types):
            raise IslandError("rejected_on / edit_types outside their closed sets")
        if len(rej) > doc["gated"] or len(types) > doc["proposed"]:
            raise IslandError("more rejections or edit types than gated / proposed candidates")
        if not isinstance(doc["island"], str) or not doc["island"]:
            raise IslandError("island id")
        return cls(doc["island"], doc["generation"], doc["accepted"], doc["proposed"], doc["gated"], rej, types)


def signal_from_generation(island: str, generation: int, stats: Mapping[str, Any],
                           lineage: Iterable[Mapping[str, Any]]) -> GenerationSignal:
    """A signal from one generation's `generation_stats` record and its `lineage` records, read ONLY through the
    proposer-visible views of evolve/export.py (stats_view, lineage_view)."""
    from ..evolve.export import lineage_view, stats_view
    st = stats_view(dict(stats))
    rows = [lineage_view(dict(r)) for r in lineage]
    gated = [r for r in rows if r.get("status") in ("accepted", "rejected_gate")]
    types = [r["edit"]["type"] for r in rows if r.get("status") == "raced" and isinstance(r.get("edit"), dict)]
    types += [r["edit"]["type"] for r in rows if r.get("status") in ("rejected_static", "rejected_novelty",
                                                                     "rejected_smoke", "no_op_or_duplicate")
              and isinstance(r.get("edit"), dict) and "type" in r["edit"]]
    return GenerationSignal.from_view({
        "island": island, "generation": generation, "accepted": bool(st.get("accepted", 0)),
        "proposed": int(st.get("proposed", 0)), "gated": len(gated),
        "rejected_on": [r["rejected_on"] for r in gated if r.get("status") == "rejected_gate" and r.get("rejected_on")],
        "edit_types": types[: int(st.get("proposed", 0))]})


@dataclass
class IslandState:
    budget: int = 0
    n_seeds: int = 1
    mutator_mix: dict[str, float] = field(default_factory=dict)
    signals: list[GenerationSignal] = field(default_factory=list)


def _norm(mix: Mapping[str, float]) -> dict[str, float]:
    tot = sum(mix.values())
    return {k: round(v / tot, 12) for k, v in sorted(mix.items()) if v > 0}


def largest_remainder(weights: Mapping[str, float], total: int) -> dict[str, int]:
    """Integer shares of `total` proportional to `weights` (sum exact; ties broken by key)."""
    tot = sum(weights.values())
    if total < 0 or tot <= 0:
        raise IslandError("largest_remainder needs a non-negative total and positive weights")
    raw = {k: total * w / tot for k, w in weights.items()}
    out = {k: math.floor(v) for k, v in raw.items()}
    rest = total - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), k))[:rest]:
        out[k] += 1
    return out


def plan_edit_types(mix: Mapping[str, float], n: int, policy: OrchestratorPolicy) -> list[str]:
    """`n` edit-type slots from `mix` under the quota: at most floor(prompt_max_share * n) prompt-only slots and,
    when n >= min_edit_types, at least min_edit_types distinct types (filled from the non-prompt types of the mix,
    then the remaining edit types in enum order). Deterministic."""
    if n <= 0:
        return []
    counts = largest_remainder({k: v for k, v in mix.items() if v > 0}, n)
    cap = math.floor(policy.prompt_max_share * n)
    excess = max(0, counts.get(PROMPT_TYPE, 0) - cap)
    others = sorted((k for k in counts if k != PROMPT_TYPE), key=lambda k: (-mix[k], k))
    others += [t for t in EDIT_TYPES if t != PROMPT_TYPE and t not in others]
    if excess:
        counts[PROMPT_TYPE] -= excess
        for i in range(excess):  # spread over the highest-weighted non-prompt types
            k = others[i % min(len(others), max(1, policy.min_edit_types))]
            counts[k] = counts.get(k, 0) + 1
    need = min(policy.min_edit_types, n)
    while sum(1 for v in counts.values() if v > 0) < need:
        donor = max((k for k, v in counts.items() if v > 1), key=lambda k: (counts[k], k), default=None)
        new = next((t for t in others if counts.get(t, 0) == 0), None)
        if donor is None or new is None:
            break
        counts[donor] -= 1
        counts[new] = 1
    return [k for k in sorted(counts, key=lambda k: (-counts[k], k)) for _ in range(counts[k])]


def admit(edit_types: Iterable[str], policy: OrchestratorPolicy) -> tuple[list[int], list[int]]:
    """Indices of a proposal's edits admitted / refused by the prompt-only quota (in proposal order: prompt-only
    edits beyond floor(prompt_max_share * n) are refused)."""
    types = list(edit_types)
    cap = math.floor(policy.prompt_max_share * len(types))
    ok, refused, used = [], [], 0
    for i, t in enumerate(types):
        if t == PROMPT_TYPE:
            if used >= cap:
                refused.append(i)
                continue
            used += 1
        ok.append(i)
    return ok, refused


class Orchestrator:
    def __init__(self, islands: list[Island], policy: OrchestratorPolicy, *, total_budget: int,
                 store: Any | None = None, initial_seeds: int = 1):
        if len(islands) < 2:
            raise IslandError("islands need at least two members")
        ids = [i.island_id for i in islands]
        if len(set(ids)) != len(ids) or not all(ids):
            raise IslandError("island ids must be unique and non-empty")
        keys = [i.key() for i in islands]
        if len(set(keys)) != len(keys):
            raise IslandError("two islands share the same proposer and mutator mix (islands must differ)")
        if total_budget < len(islands):
            raise IslandError("the total budget must give every island at least one edit")
        self.islands = {i.island_id: i for i in islands}
        self.policy, self.total, self.store = policy, total_budget, store
        self.round = 0
        self.policy_changed_at = 0
        self.versions = [policy.version]
        self.state = {i.island_id: IslandState(mutator_mix=dict(_norm(i.mutator_mix)), n_seeds=initial_seeds)
                      for i in islands}
        for k, b in largest_remainder({i: 1.0 for i in self.islands}, total_budget).items():
            self.state[k].budget = b
        self._log(POLICY_KIND, {"round": 0, "version": policy.version, "policy_hash": policy.policy_hash(),
                                "params": asdict(policy)})

    def _log(self, kind: str, body: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.append_record(kind, body)

    def observe(self, signal: GenerationSignal | Mapping[str, Any]) -> None:
        s = signal if isinstance(signal, GenerationSignal) else GenerationSignal.from_view(signal)
        if s.island not in self.islands:
            raise IslandError(f"unknown island {s.island!r}")
        self.state[s.island].signals.append(s)

    def set_policy(self, policy: OrchestratorPolicy) -> None:
        if policy.version in self.versions:
            raise IslandError(f"policy version {policy.version!r} was used before: a change needs a new version")
        if self.round - self.policy_changed_at < self.policy.cadence:
            raise IslandError(f"policy changes are allowed every {self.policy.cadence} rounds (last at round "
                              f"{self.policy_changed_at}, now {self.round})")
        self.policy, self.policy_changed_at = policy, self.round
        self.versions.append(policy.version)
        self._log(POLICY_KIND, {"round": self.round, "version": policy.version, "policy_hash": policy.policy_hash(),
                                "params": asdict(policy)})

    def evidence(self, island: str) -> dict[str, Any]:
        """The counts a decision rests on: the island's last `stall_window` generations."""
        w = self.state[island].signals[-self.policy.stall_window:]
        types: dict[str, int] = {}
        for s in w:
            for t in s.edit_types:
                types[t] = types.get(t, 0) + 1
        rej: dict[str, int] = {}
        for s in w:
            for r in s.rejected_on:
                rej[r] = rej.get(r, 0) + 1
        n_types = sum(types.values())
        return {"window": len(w), "observed": len(self.state[island].signals), "accepted": sum(s.accepted for s in w),
                "proposed": sum(s.proposed for s in w), "gated": sum(s.gated for s in w), "rejected_on": rej,
                "edit_types": dict(sorted(types.items())),
                "prompt_share": (types.get(PROMPT_TYPE, 0) / n_types) if n_types else 0.0}

    def step(self) -> list[dict[str, Any]]:
        """One orchestrator round: decide every island's lever, budget, seeds and edit-type plan."""
        self.round += 1
        p = self.policy
        decisions: dict[str, dict[str, Any]] = {}
        weights: dict[str, float] = {}
        for iid in sorted(self.islands):
            st, ev = self.state[iid], self.evidence(iid)
            stalled = ev["observed"] >= p.stall_window and ev["accepted"] == 0
            lever, why = "harness_edit", "not stalled"
            weight = 1.0
            if stalled:
                rej_evolve = ev["rejected_on"].get("evolve", 0)
                if ev["gated"] and rej_evolve / ev["gated"] >= p.more_eval_share and st.n_seeds < p.max_seeds:
                    lever = "more_evaluation"
                    why = (f"stalled; {rej_evolve}/{ev['gated']} gated rejections on the evolve re-measurement "
                           f">= {p.more_eval_share}: underpowered, evaluate more")
                    st.n_seeds += 1
                else:
                    why = "stalled; rejections not dominated by the evolve re-measurement: diversify edits"
                    weight = p.stall_shrink
                    used = ev["edit_types"]
                    if used:
                        top = max(used.values())
                        st.mutator_mix = _norm({k: (v * 0.5 if used.get(k, 0) == top else v)
                                                for k, v in st.mutator_mix.items()} | {
                            t: 1.0 / len(EDIT_TYPES) for t in EDIT_TYPES if t not in st.mutator_mix})
            if ev["prompt_share"] > p.prompt_max_share and st.mutator_mix.get(PROMPT_TYPE, 0) > p.prompt_max_share:
                # collapse into prompt-only edits: cap the prompt weight at the quota
                mix = {k: v for k, v in st.mutator_mix.items() if k != PROMPT_TYPE and v > 0}
                if not mix:  # a prompt-only mix: open every other edit type equally
                    mix = {t: 1.0 for t in EDIT_TYPES if t != PROMPT_TYPE}
                if p.prompt_max_share > 0:
                    mix[PROMPT_TYPE] = p.prompt_max_share * sum(mix.values()) / max(1e-12, 1 - p.prompt_max_share)
                st.mutator_mix = _norm(mix)
                why += "; prompt-only share above quota: prompt weight capped"
            if lever not in BUILT_LEVERS:  # pragma: no cover - defensive: no rule above chooses `weights`
                raise IslandError(f"lever {lever!r} is not built")
            weights[iid] = weight
            decisions[iid] = {"island": iid, "stalled": stalled, "lever": lever, "why": why, "evidence": ev}
        floor = p.min_island_share
        n = len(weights)
        tot = sum(weights.values())
        shares = {k: floor + (1 - floor * n) * w / tot for k, w in weights.items()} if floor * n < 1 else \
            {k: 1.0 for k in weights}
        budgets = largest_remainder(shares, self.total)
        out = []
        for iid in sorted(decisions):
            st = self.state[iid]
            st.budget = budgets[iid]
            n_edits = st.budget // st.n_seeds  # budget = harness runs per unit set: edits x seeds
            d = decisions[iid] | {"round": self.round, "policy_version": p.version, "policy_hash": p.policy_hash(),
                                  "budget": st.budget, "n_seeds": st.n_seeds, "n_edits": n_edits,
                                  "mutator_mix": dict(st.mutator_mix),
                                  "proposer_mix": _norm(self.islands[iid].proposer_mix),
                                  "edit_plan": plan_edit_types(st.mutator_mix, n_edits, p)}
            self._log(DECISION_KIND, d)
            out.append(d)
        return out
