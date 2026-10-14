"""Search over search (GOAL §5 L7; MILO islands, SIA lever choice): islands with different proposer and mutator
mixes, and a deterministic orchestrator that detects stalls and reallocates budget across islands, edit types and
levers, logging the lever it chose and the evidence for it.

What the orchestrator may read (verdict embargo, invariant 18): one `GenerationSignal` per island generation,
built ONLY from proposer-visible fields: the lineage/stats whitelists of evolve/export.py (accept bit, counters,
the rejecting stage `rejected_on`, the edit types proposed). No gate mean, CI, per-task value or score enters it;
a signal with any other field, or a non-count number, is refused (`GenerationSignal.from_view`).

Decisions per round (`Orchestrator.step`), for each island in id order:
  - stalled: decided by ONE function, `evolve.generations.stall_evidence` (at least `stall_window` generations
    observed and no accept in the last `stall_window`), called with the island's signals; the orchestrator has no
    stall rule of its own (tests/test_islands.py pins this);
  - lever for a stalled island: `more_evaluation` (one more seed per unit, fewer edits for the same budget) when at
    least `more_eval_share` of its gated rejections in the window were on the evolve re-measurement (the
    candidates may be real but underpowered), otherwise `harness_edit` with its mutator mix diversified (the edit
    types it used most in the window are halved) and its budget weight shrunk by `stall_shrink`;
    `weights` (L8) is chosen only when the orchestrator was built with `weights_lever=True` (a caller that routes it
    to the L8 data export, evolve/search.py) and the island is stalled, not underpowered, and its last
    `weights_after` decisions were already stalled harness-edit diversifications. It ROUTES to the trajectory export
    only: nothing is trained (no GPU); the island runs no harness generation that round (n_edits = 0);
  - budget: shares proportional to the island weights with a floor of `min_island_share`, rounded by largest
    remainder (ties by island id) so the shares sum to the total exactly; an optional `cap` (the downgrade-only
    gate's shrunk caps, evolve/search.py) can only LOWER an island's budget (min, enforced here);
  - edit-type quota: a planned batch has at most floor(prompt_max_share * n) prompt-only edits and, when n allows,
    at least `min_edit_types` distinct types (`plan_edit_types`); `admit` applies the same quota to a proposal
    (enforced in the loop through evolve_generation's `edit_admission`).
Every decision is returned and (with a store) appended as an `orchestrator_decision` record carrying the policy
version and hash, the lever and the evidence counts it rested on.

The policy is versioned (`OrchestratorPolicy.version` + a hash of its parameters) and may change only on a slower
cadence and only on measured gain: `set_policy` refuses a reused version, the same version with different
parameters, a change less than `cadence` rounds after the previous one, and any change that does not carry the
passing `policy_trial_decision` this orchestrator recorded for exactly that challenger (version AND hash). A trial
(control/policy_gate.py) is declared with `declare_trial` (refused while another is open, for a version used or
trialled before, and less than `cadence` rounds after the last change); during its window `step` runs every island
under the arm the declared order coins name, and the round outcomes (number of islands that accepted) feed an exact
one-sided sign test.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import config
from ..evolve.edits import EditType
from . import policy_gate as pg

LEVERS = ("harness_edit", "more_evaluation", "weights")
BUILT_LEVERS = ("harness_edit", "more_evaluation")  # always available; `weights` only with a routing caller
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
    weights_after: int = config.ISLAND_WEIGHTS_AFTER

    def __post_init__(self) -> None:
        if not self.version:
            raise IslandError("a policy needs a version")
        if (self.stall_window < 1 or not 0 < self.more_eval_share <= 1 or not 0 < self.stall_shrink <= 1
                or not 0 <= self.min_island_share < 1 or not 0 <= self.prompt_max_share <= 1
                or self.min_edit_types < 1 or self.cadence < 1 or self.max_seeds < 1 or self.weights_after < 1):
            raise IslandError("orchestrator policy parameter out of range")

    def param_hash(self) -> str:
        """Hash of the parameters WITHOUT the version: the same policy under a new name has the same param_hash."""
        return hashlib.sha256(json.dumps({k: v for k, v in asdict(self).items() if k != "version"},
                                         sort_keys=True).encode()).hexdigest()

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
        if len(rej) + doc["accepted"] > doc["gated"] or len(types) > doc["proposed"]:
            raise IslandError("more rejections/accepts or edit types than gated / proposed candidates")
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
    diversify_streak: int = 0  # consecutive stalled harness-edit diversifications (the weights lever's evidence)


def _ledger_signal(s: GenerationSignal) -> dict[str, Any]:
    """A signal in the shape of the generation ledger's (generations.generation_signal): accept bit and counts."""
    rej: dict[str, int] = {}
    for r in s.rejected_on:
        rej[r] = rej.get(r, 0) + 1
    return {"accepted": s.accepted, "proposed": s.proposed, "gated": s.gated, "rejected_on": dict(sorted(rej.items()))}


def stall_evidence(signals: list[GenerationSignal], window: int) -> dict[str, Any]:
    """THE stall rule: `evolve.generations.stall_evidence`, looked up at call time (no copy of the rule lives here,
    and no import cycle through generations -> loop is created when this module is imported)."""
    from ..evolve import generations
    return dict(generations.stall_evidence([_ledger_signal(s) for s in signals], window=window))


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
                 store: Any | None = None, initial_seeds: int = 1, weights_lever: bool = False,
                 entropy: Callable[[], str] | None = None):
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
        self.weights_lever = weights_lever  # True only when a caller routes the lever (evolve/search.py)
        self.round = 0
        self.policy_changed_at = 0
        self.changes = 0  # accepted policy changes so far
        self.versions = [policy.version]
        self.round_policy = policy  # the policy the current round runs under (a trial arm during a trial)
        self.trial: pg.PolicyTrial | None = None
        self._trial_challenger = policy  # the open trial's challenger (set by declare_trial)
        self.trialled: list[str] = []  # challenger versions declared in a trial (a version is trialled once)
        # parameter hashes (version excluded) declared in a trial or adopted: the same parameters under a new version
        # name are not trialled again (red-team finding: re-trial after peeking or a refusal)
        self.trialled_params: set[str] = {policy.param_hash()}
        self._entropy = entropy  # coin entropy of policy trials (None = secrets.token_hex)
        self._round_seen: dict[int, set[str]] = {}  # islands that reported (or were explicitly skipped) per round
        self.trial_decisions: dict[str, dict[str, Any]] = {}
        self._trial_decls: dict[str, pg.TrialDeclaration] = {}
        self._round_accepts: dict[int, int] = {}
        self._closed = 0  # rounds up to this one are closed (no more signals for them)
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
        # a GenerationSignal built directly is re-validated through the same whitelist as a mapping
        s = GenerationSignal.from_view(asdict(signal) if isinstance(signal, GenerationSignal) else signal)
        if s.island not in self.islands:
            raise IslandError(f"unknown island {s.island!r}")
        if self.round and self._closed >= self.round:
            raise IslandError(f"round {self.round} is closed: a signal for it can no longer be counted")
        seen = self._round_seen.setdefault(self.round, set())
        if self.trial is not None and self.trial.index(self.round) is not None and s.island in seen:
            raise IslandError(f"island {s.island!r} already reported in trial round {self.round}")
        seen.add(s.island)
        self.state[s.island].signals.append(s)
        self._round_accepts[self.round] = self._round_accepts.get(self.round, 0) + int(s.accepted)

    def skip(self, island: str) -> None:
        """Record that `island` deliberately ran no generation this round (0 edits, or the weights lever): it counts
        as reported. An island neither observed nor skipped in a trial round voids the trial (close_round)."""
        if island not in self.islands:
            raise IslandError(f"unknown island {island!r}")
        if self.round and self._closed >= self.round:
            raise IslandError(f"round {self.round} is closed")
        self._round_seen.setdefault(self.round, set()).add(island)

    # ------------------------------------------------------------------ policy trial and change (policy_gate.py)
    def trial_refusal(self, challenger: OrchestratorPolicy) -> str | None:
        """Why a trial of `challenger` cannot be declared now (None = it can)."""
        if self.trial is not None:
            return f"trial {self.trial.decl.trial_id!r} is still open"
        if challenger.version in self.versions or challenger.version in self.trialled:
            return f"policy version {challenger.version!r} was used before (adopted or trialled)"
        if challenger.param_hash() in self.trialled_params:
            return "these policy parameters were adopted or trialled before (under another version name)"
        # the cadence counts from the last CHANGE; the initial policy is not a change (see docs/search_over_search.md)
        if self.changes and self.round - self.policy_changed_at < self.policy.cadence:
            return (f"a trial may start {self.policy.cadence} rounds after the last change (round "
                    f"{self.policy_changed_at}, now {self.round})")
        return None

    def declare_trial(self, challenger: OrchestratorPolicy, *, order_seed: str, trial_id: str | None = None,
                      n_pairs: int = config.POLICY_TRIAL_PAIRS, alpha: Any = config.POLICY_TRIAL_ALPHA,
                      min_nonzero: int = config.POLICY_TRIAL_MIN_NONZERO) -> pg.TrialDeclaration:
        """Declare (and ledger) a trial of `challenger` against the current policy; its window starts next round."""
        why = self.trial_refusal(challenger)
        if why is not None:
            raise IslandError(f"policy trial refused: {why}")
        tid = trial_id or f"{self.policy.version}->{challenger.version}@{self.round}"
        if tid in self._trial_decls:
            raise IslandError(f"trial id {tid!r} was used before")
        try:
            decl = pg.declare(trial_id=tid, incumbent_version=self.policy.version,
                              incumbent_hash=self.policy.policy_hash(), challenger_version=challenger.version,
                              challenger_hash=challenger.policy_hash(), round_now=self.round, order_seed=order_seed,
                              n_pairs=n_pairs, alpha=alpha, min_nonzero=min_nonzero, drawn=True)
        except pg.PolicyGateError as e:
            raise IslandError(f"policy trial refused: {e}") from e
        self._closed = max(self._closed, self.round)  # signals after the declaration belong to trial rounds only
        self.trial = pg.PolicyTrial(decl, self._entropy)
        self._trial_challenger = challenger
        self._trial_decls[tid] = decl
        self.trialled.append(challenger.version)
        self.trialled_params.add(challenger.param_hash())
        self._log(pg.TRIAL_KIND, decl.as_record() | {"challenger_params": asdict(challenger),
                                                     "incumbent_params": asdict(self.policy)})
        return decl

    def close_round(self) -> None:
        """Close the current round: a trial round's outcome (islands that accepted) is recorded; when the window is
        complete the decision is computed, recorded and kept for `set_policy`. Idempotent."""
        if self._closed >= self.round:
            return
        self._closed = self.round
        t = self.trial
        if t is None or t.index(self.round) is None:
            return
        missing = sorted(set(self.islands) - self._round_seen.get(self.round, set()))
        if missing:  # a round whose islands did not all report is not a measurement: the trial is void
            self._log(pg.ROUND_KIND, {"trial_id": t.decl.trial_id, "round": self.round, "void": True,
                                      "missing": missing})
            self._finish_trial()
            return
        rec = t.record(self.round, self._round_accepts.get(self.round, 0))
        self._log(pg.ROUND_KIND, rec | {"policy_version": self.round_policy.version,
                                        "policy_hash": self.round_policy.policy_hash()})
        if t.complete:
            self._finish_trial()

    def abandon_trial(self) -> dict[str, Any] | None:
        """End an open trial before its window completed: recorded as not accepted."""
        if self.trial is None:
            return None
        return self._finish_trial()

    def _finish_trial(self) -> dict[str, Any]:
        assert self.trial is not None
        dec = self.trial.decision()
        self.trial_decisions[dec["trial_id"]] = dec
        self.trial = None
        self._log(pg.DECISION_KIND, dict(dec))
        return dec

    def set_policy(self, policy: OrchestratorPolicy, *, trial_decision: Mapping[str, Any] | None = None) -> None:
        if policy.version in self.versions:
            raise IslandError(f"policy version {policy.version!r} was used before: a change needs a new version")
        if self.round - self.policy_changed_at < self.policy.cadence:
            raise IslandError(f"policy changes are allowed every {self.policy.cadence} rounds (last at round "
                              f"{self.policy_changed_at}, now {self.round})")
        if trial_decision is None:
            raise IslandError("a policy change needs the passing policy_trial_decision of a declared trial "
                              "(control/policy_gate.py)")
        doc = dict(trial_decision)
        tid = doc.get("trial_id")
        mine = self.trial_decisions.get(tid) if isinstance(tid, str) else None
        if mine is None or mine != doc:
            raise IslandError("trial decision is not one this orchestrator's policy gate recorded")
        if doc["challenger_version"] != policy.version or doc["challenger_hash"] != policy.policy_hash():
            raise IslandError("trial decision is for another challenger (version and parameter hash must match)")
        if doc["incumbent_hash"] != self.policy.policy_hash() or doc["incumbent_version"] != self.policy.version:
            raise IslandError("trial decision was measured against another incumbent")
        problems = pg.verify_decision(doc, self._trial_decls[str(tid)])
        if problems or doc["accepted"] is not True:
            raise IslandError("trial decision does not accept this challenger: " + "; ".join(problems or [
                str(doc.get("reason"))]))
        self.policy, self.policy_changed_at = policy, self.round
        self.changes += 1
        self.versions.append(policy.version)
        self._log(POLICY_KIND, {"round": self.round, "version": policy.version, "policy_hash": policy.policy_hash(),
                                "params": asdict(policy), "trial_id": tid})

    # ------------------------------------------------------------------ decisions
    def evidence(self, island: str, policy: OrchestratorPolicy | None = None) -> dict[str, Any]:
        """The counts a decision rests on: the stall evidence of `stall_evidence` (the one stall rule) over the
        island's signals, plus the edit types it proposed in the last `stall_window` generations."""
        p = policy or self.policy
        sigs = self.state[island].signals
        ev = stall_evidence(sigs, p.stall_window)
        types: dict[str, int] = {}
        for s in sigs[-p.stall_window:]:
            for t in s.edit_types:
                types[t] = types.get(t, 0) + 1
        n_types = sum(types.values())
        return ev | {"edit_types": dict(sorted(types.items())),
                     "prompt_share": (types.get(PROMPT_TYPE, 0) / n_types) if n_types else 0.0}

    def step(self, *, cap: Callable[[dict[str, int]], Mapping[str, float | None]] | None = None
             ) -> list[dict[str, Any]]:
        """One orchestrator round: decide every island's lever, budget, seeds and edit-type plan. During a declared
        trial window every island runs under the arm the order coins name. `cap(budgets)` may return a cap per
        island (the downgrade-only gate's); an island's budget becomes min(its budget, floor(cap)), never more."""
        self.close_round()
        self.round += 1
        if self.trial is not None:  # the coin of a starting pair is drawn now and ledgered before the pair runs
            coin = self.trial.start(self.round)
            if coin is not None:
                self._log(pg.COIN_KIND, coin)
        arm = self.trial.arm(self.round) if self.trial is not None else None
        p = self._trial_challenger if arm == "challenger" else self.policy
        self.round_policy = p
        decisions: dict[str, dict[str, Any]] = {}
        weights: dict[str, float] = {}
        for iid in sorted(self.islands):
            st, ev = self.state[iid], self.evidence(iid, p)
            stalled = bool(ev["stalled"])
            lever, why = "harness_edit", "not stalled"
            weight = 1.0
            if stalled:
                rej_evolve = ev["rejected_on"].get("evolve", 0)
                if ev["gated"] and rej_evolve / ev["gated"] >= p.more_eval_share and st.n_seeds < p.max_seeds:
                    lever = "more_evaluation"
                    why = (f"stalled; {rej_evolve}/{ev['gated']} gated rejections on the evolve re-measurement "
                           f">= {p.more_eval_share}: underpowered, evaluate more")
                    st.n_seeds += 1
                    st.diversify_streak = 0
                elif self.weights_lever and st.diversify_streak >= p.weights_after:
                    lever = "weights"
                    why = (f"stalled; not underpowered; the last {st.diversify_streak} decisions were already "
                           "stalled harness-edit diversifications: route to the L8 trajectory export (nothing is "
                           "trained)")
                    weight = p.stall_shrink
                    ev = ev | {"diversify_streak": st.diversify_streak}
                    st.diversify_streak = 0
                else:
                    why = "stalled; rejections not dominated by the evolve re-measurement: diversify edits"
                    weight = p.stall_shrink
                    st.diversify_streak += 1
                    used = ev["edit_types"]
                    if used:
                        top = max(used.values())
                        st.mutator_mix = _norm({k: (v * 0.5 if used.get(k, 0) == top else v)
                                                for k, v in st.mutator_mix.items()} | {
                            t: 1.0 / len(EDIT_TYPES) for t in EDIT_TYPES if t not in st.mutator_mix})
            else:
                st.diversify_streak = 0
            if ev["prompt_share"] > p.prompt_max_share and st.mutator_mix.get(PROMPT_TYPE, 0) > p.prompt_max_share:
                # collapse into prompt-only edits: cap the prompt weight at the quota
                mix = {k: v for k, v in st.mutator_mix.items() if k != PROMPT_TYPE and v > 0}
                if not mix:  # a prompt-only mix: open every other edit type equally
                    mix = {t: 1.0 for t in EDIT_TYPES if t != PROMPT_TYPE}
                if p.prompt_max_share > 0:
                    mix[PROMPT_TYPE] = p.prompt_max_share * sum(mix.values()) / max(1e-12, 1 - p.prompt_max_share)
                st.mutator_mix = _norm(mix)
                why += "; prompt-only share above quota: prompt weight capped"
            if lever not in BUILT_LEVERS and not (lever == "weights" and self.weights_lever):
                raise IslandError(f"lever {lever!r} is not available")  # pragma: no cover - defensive
            weights[iid] = weight
            decisions[iid] = {"island": iid, "stalled": stalled, "lever": lever, "why": why, "evidence": ev}
        floor = p.min_island_share
        n = len(weights)
        tot = sum(weights.values())
        shares = {k: floor + (1 - floor * n) * w / tot for k, w in weights.items()} if floor * n < 1 else \
            {k: 1.0 for k in weights}
        budgets = largest_remainder(shares, self.total)
        capped = self._apply_cap(cap, budgets) if cap is not None else None
        out = []
        for iid in sorted(decisions):
            st = self.state[iid]
            st.budget = capped[iid] if capped is not None else budgets[iid]
            # budget = harness runs per unit set: edits x seeds; the weights lever runs no harness generation
            n_edits = 0 if decisions[iid]["lever"] == "weights" else st.budget // st.n_seeds
            d = decisions[iid] | {"round": self.round, "policy_version": p.version, "policy_hash": p.policy_hash(),
                                  "budget": st.budget, "n_seeds": st.n_seeds, "n_edits": n_edits,
                                  "mutator_mix": dict(st.mutator_mix),
                                  "proposer_mix": _norm(self.islands[iid].proposer_mix),
                                  "edit_plan": plan_edit_types(st.mutator_mix, n_edits, p)}
            if capped is not None:
                d["budget_orchestrator"] = budgets[iid]
            if arm is not None and self.trial is not None:
                d["trial"] = {"trial_id": self.trial.decl.trial_id, "arm": arm}
            self._log(DECISION_KIND, d)
            out.append(d)
        return out

    @staticmethod
    def _apply_cap(cap: Callable[[dict[str, int]], Mapping[str, float | None]], budgets: dict[str, int]
                   ) -> dict[str, int]:
        """min(budget, floor(cap)) per island; a missing or None cap leaves the budget; a cap that is not a finite
        number >= 0 is refused (fail closed: the round does not run on an unchecked cap)."""
        caps = cap(dict(budgets))
        out = {}
        for iid, b in budgets.items():
            c = caps.get(iid) if isinstance(caps, Mapping) else None
            if c is None:
                out[iid] = b
                continue
            if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or c < 0:
                raise IslandError(f"island {iid}: budget cap {c!r} is not a finite number >= 0")
            out[iid] = min(b, math.floor(c))
        return out
