"""L4.5 hypothesis tournament (GOAL §5 L4 stage 5, §1.1 "relative judgment beats absolute").

What it does (all decisions here are deterministic code; the judge only answers "which is better"):
- **K-way mode**: shuffled K-way rounds. Every round reshuffles the pool and presents blocks of k items in a fresh
  random order; the judge returns a best-first ranking of the block, or the round is rejected (never guessed).
  Rankings are fitted as Plackett-Luce choices (BT's K-way form) with Wald intervals (`verify.ranking.fit_bt`).
- **Both-order pairwise mode**: every pair is judged in BOTH presentation orders. The position effect is measured
  two ways and reported: an exact binomial test of "the first-shown option wins" against 1/2 (valid and conservative
  for a balanced both-order design whatever the true strengths: the two orders' win chances sum to 1), and a jointly
  fitted position log-odds gamma with its interval. The strengths are fitted with gamma in the model, so a biased
  judge does not distort them.
  In K-way mode the analogous statistic is "the first-listed item is ranked top", tested against 1/k (exact under
  random presentation: the first-listed item is a uniform draw from the block).
- **Operators (SciDER, code_review/scider.md §4.2-4.3)**: Improve-weakest (rewrite the lowest-rated, non-pinned
  hypothesis along its weakest rubric dimension, when per-dimension rankings exist) and Combine (a child of the two
  highest-rated hypotheses, carrying each parent's strongest dimension in its rationale). Their outputs are
  validated and fail closed (an invalid output changes nothing and is counted). Ratings are BT fits over the whole
  generation, not SciDER's per-call rank normalisation.
- **Pinned incumbent**: the incumbent is in every K-way block, is never replaced by Improve-weakest and is never
  pruned. This is checked again at the end of every generation (a violation raises; it is not a soft warning).

What it does NOT do: a tournament rating is a T5-class soft signal for ORDERING what to try. It certifies nothing and
grants nothing; the downstream T1/T2 metric decides (GOAL §1.1 "an LLM may only downgrade, never grant").
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, cast

from ..verify.ranking import BTFit, Choice, binom_two_sided_p, fit_bt, pair_choice, ranking_to_choices, wilson_interval

Hyp = dict[str, Any]
# judge_k(presented hypotheses in presentation order, dimension or None) -> best-first ranking of exactly their ids,
# or None (the round is then rejected, never guessed)
JudgeK = Callable[[list[Hyp], str | None], Sequence[str] | None]
# judge_pair(first-shown hypothesis, second-shown hypothesis) -> the winner's id, or None
JudgePair = Callable[[Hyp, Hyp], str | None]
# improve(weakest hypothesis, weakest dimension or None, best hypothesis) -> {"idea": ..., "rationale": ...} or None
ImproveFn = Callable[[Hyp, str | None, Hyp], Mapping[str, Any] | None]
# combine(parent a, parent b, strongest dimension of a or None, of b or None) -> {"idea": ..., ...} or None
CombineFn = Callable[[Hyp, Hyp, str | None, str | None], Mapping[str, Any] | None]


class PinnedIncumbentError(RuntimeError):
    """The pinned incumbent left the pool: a code bug, never a judge outcome."""


@dataclass
class RoundsResult:
    choices: list[Choice] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    valid: int = 0
    rejected: int = 0
    first_hits: int = 0      # K-way: first-listed ranked top; pairwise: first-shown won
    first_trials: int = 0
    expected_rate: float = 0.5
    consistent_pairs: int = 0  # pairwise: both orders valid and agree
    judged_pairs: int = 0      # pairwise: both orders valid


def _valid_permutation(ranking: Any, presented: Sequence[str]) -> list[str] | None:
    if not isinstance(ranking, (list, tuple)) or not all(isinstance(x, str) for x in ranking):
        return None
    r = list(ranking)
    return r if sorted(r) == sorted(presented) else None


def _blocks(ids: Sequence[str], k: int, rng: random.Random, pinned: str | None) -> list[list[str]]:
    """One round's blocks. k >= len(ids): one block of everything. Otherwise shuffle the non-pinned items into
    blocks of k (k-1 plus the pinned incumbent), padding the last block with other items so every block has size k
    (so the K-way position test has one null rate, 1/k)."""
    if k >= len(ids):
        return [list(ids)]
    others = [i for i in ids if i != pinned]
    rng.shuffle(others)
    per = k - 1 if pinned is not None else k
    blocks = []
    for s in range(0, len(others), per):
        blk = others[s:s + per]
        if len(blk) < per:
            pool = [i for i in others if i not in blk]
            blk = blk + rng.sample(pool, per - len(blk))
        blocks.append(([pinned] if pinned is not None else []) + blk)
    return blocks


def kway_rounds(pool: Sequence[Hyp], judge: JudgeK, *, rounds: int, k: int | None = None,
                rng: random.Random, pinned: str | None = None, dimension: str | None = None) -> RoundsResult:
    by_id = {h["id"]: h for h in pool}
    ids = list(by_id)
    k = len(ids) if k is None else k
    if k < 2:
        raise ValueError("a K-way round needs k >= 2")
    res = RoundsResult(expected_rate=1.0 / min(k, len(ids)))
    for rnd in range(rounds):
        for blk in _blocks(ids, k, rng, pinned):
            presented = blk[:]
            rng.shuffle(presented)
            try:
                ranking = _valid_permutation(judge([by_id[i] for i in presented], dimension), presented)
            except (ValueError, KeyError, TypeError):
                ranking = None
            res.records.append({"round": rnd, "dimension": dimension, "presented": presented, "ranking": ranking})
            if ranking is None:
                res.rejected += 1
                continue
            res.valid += 1
            res.choices.extend(ranking_to_choices(ranking))
            res.first_trials += 1
            res.first_hits += ranking[0] == presented[0]
    return res


def pairwise_rounds(pool: Sequence[Hyp], judge: JudgePair, *, repeats: int = 1, rng: random.Random) -> RoundsResult:
    """Every unordered pair, `repeats` times, in BOTH orders; the schedule order is shuffled."""
    by_id = {h["id"]: h for h in pool}
    ids = list(by_id)
    res = RoundsResult(expected_rate=0.5)
    schedule = [(a, b, r) for a, b in combinations(ids, 2) for r in range(repeats)]
    rng.shuffle(schedule)
    for a, b, r in schedule:
        orders = [(a, b), (b, a)]
        rng.shuffle(orders)
        winners = {}
        for first, second in orders:
            try:
                w = judge(by_id[first], by_id[second])
            except (ValueError, KeyError, TypeError):
                w = None
            ok = isinstance(w, str) and w in (first, second)
            res.records.append({"first": first, "second": second, "repeat": r, "winner": w if ok else None})
            if not ok:
                res.rejected += 1
                continue
            res.valid += 1
            winners[first] = w
            res.choices.append(pair_choice(first, second, w))  # type: ignore[arg-type]
            res.first_trials += 1
            res.first_hits += w == first
        if len(winners) == 2:
            res.judged_pairs += 1
            res.consistent_pairs += winners[a] == winners[b]
    return res


def position_bias_report(res: RoundsResult, fit: BTFit | None, *, mode: str, alpha: float | None = None,
                         level: float | None = None) -> dict[str, Any]:
    from ..config import BT_CI_LEVEL, POSITION_BIAS_ALPHA
    alpha = POSITION_BIAS_ALPHA if alpha is None else alpha
    level = BT_CI_LEVEL if level is None else level
    n, k = res.first_trials, res.first_hits
    p = binom_two_sided_p(k, n, res.expected_rate) if n else 1.0
    out: dict[str, Any] = {
        "mode": mode, "statistic": "first-shown wins" if mode == "pairwise" else "first-listed ranked top",
        "hits": k, "trials": n, "expected_rate": res.expected_rate, "rate": (k / n) if n else None,
        "rate_ci": list(wilson_interval(k, n, level)), "p_value": p, "alpha": alpha,
        "detected": bool(n) and p < alpha}
    if mode == "pairwise":
        out["order_consistency"] = (res.consistent_pairs / res.judged_pairs) if res.judged_pairs else None
        out["pairs_judged_both_orders"] = res.judged_pairs
        if fit is not None and fit.position is not None:
            out["gamma"] = fit.position
            out["gamma_ci"] = list(fit.position_interval(level) or ())
    return out


# ---------------------------------------------------------------- operators
def _ok_hyp(out: Any) -> bool:
    return isinstance(out, Mapping) and isinstance(out.get("idea"), str) and bool(out["idea"].strip())


def _ranked(fit: BTFit, ids: Sequence[str]) -> list[str]:
    """Best first; ties broken by id so the order is deterministic."""
    return sorted(ids, key=lambda i: (-fit.theta[i], i))


def _dim_extreme(dim_fits: Mapping[str, BTFit] | None, hid: str, *, lowest: bool) -> str | None:
    if not dim_fits:
        return None
    scored = [(f.theta[hid], d) for d, f in sorted(dim_fits.items()) if hid in f.theta]
    if not scored:
        return None
    return (min(scored) if lowest else max(scored))[1]


def improve_weakest(pool: list[Hyp], fit: BTFit, improve: ImproveFn, *, pinned: str | None, new_id: str,
                    dim_fits: Mapping[str, BTFit] | None = None) -> tuple[list[Hyp], dict[str, Any]]:
    """Replace the lowest-rated NON-PINNED hypothesis by an improved version (child keeps a `parent` link)."""
    by_id = {h["id"]: h for h in pool}
    rated = [i for i in by_id if i in fit.theta]
    cands = [i for i in reversed(_ranked(fit, rated)) if i != pinned]
    if not cands:
        return pool, {"operator": "improve_weakest", "applied": False, "reason": "no non-pinned rated hypothesis"}
    weakest = cands[0]
    best = by_id[_ranked(fit, rated)[0]]
    dim = _dim_extreme(dim_fits, weakest, lowest=True)
    try:
        out = improve(by_id[weakest], dim, best)
    except (ValueError, KeyError, TypeError):
        out = None
    if not _ok_hyp(out) or new_id in by_id:
        return pool, {"operator": "improve_weakest", "applied": False, "target": weakest, "reason": "invalid output"}
    assert out is not None
    child = {"id": new_id, "idea": str(out["idea"]), "rationale": str(out.get("rationale", "")),
             "parent": weakest, "operator": "improve_weakest", "dimension": dim}
    new_pool = [child if h["id"] == weakest else h for h in pool]
    return new_pool, {"operator": "improve_weakest", "applied": True, "target": weakest, "dimension": dim,
                      "child": new_id}


def combine(pool: list[Hyp], fit: BTFit, combiner: CombineFn, *, new_id: str,
            dim_fits: Mapping[str, BTFit] | None = None) -> tuple[list[Hyp], dict[str, Any]]:
    """Add a child of the two highest-rated hypotheses (parents stay in the pool)."""
    by_id = {h["id"]: h for h in pool}
    rated = _ranked(fit, [i for i in by_id if i in fit.theta])
    if len(rated) < 2:
        return pool, {"operator": "combine", "applied": False, "reason": "fewer than 2 rated hypotheses"}
    a, b = rated[0], rated[1]
    da, db = _dim_extreme(dim_fits, a, lowest=False), _dim_extreme(dim_fits, b, lowest=False)
    try:
        out = combiner(by_id[a], by_id[b], da, db)
    except (ValueError, KeyError, TypeError):
        out = None
    if not _ok_hyp(out) or new_id in by_id:
        return pool, {"operator": "combine", "applied": False, "parents": [a, b], "reason": "invalid output"}
    assert out is not None
    child = {"id": new_id, "idea": str(out["idea"]), "rationale": str(out.get("rationale", "")),
             "parents": [a, b], "operator": "combine", "strongest": {a: da, b: db}}
    return pool + [child], {"operator": "combine", "applied": True, "parents": [a, b], "child": new_id}


def prune(pool: list[Hyp], fit: BTFit, max_size: int, *, pinned: str | None) -> tuple[list[Hyp], list[str]]:
    """Drop the lowest-rated non-pinned RATED hypotheses until the pool fits. Unrated (new) items are kept: they
    have not been judged yet, so dropping them would be a guess."""
    ids = [h["id"] for h in pool]
    droppable = [i for i in reversed(_ranked(fit, [i for i in ids if i in fit.theta])) if i != pinned]
    drop: list[str] = []
    while len(ids) - len(drop) > max_size and droppable:
        drop.append(droppable.pop(0))
    return [h for h in pool if h["id"] not in drop], drop


# ---------------------------------------------------------------- the tournament
def run_tournament(pool: Sequence[Hyp], *, mode: str = "kway", judge_k: JudgeK | None = None,
                   judge_pair: JudgePair | None = None, rounds: int = 3, k: int | None = None, repeats: int = 1,
                   generations: int = 1, improve: ImproveFn | None = None, combiner: CombineFn | None = None,
                   dimensions: Sequence[str] = (), max_pool: int | None = None, pinned: str | None = None,
                   rng: random.Random | None = None, level: float | None = None) -> dict[str, Any]:
    from ..config import BT_CI_LEVEL
    level = BT_CI_LEVEL if level is None else level
    rng = rng or random.Random(0)
    pool = [dict(h) for h in pool]
    ids = [h["id"] for h in pool]
    if len(set(ids)) != len(ids):
        raise ValueError("hypothesis ids must be unique")
    if len(ids) < 2:
        raise ValueError("a tournament needs >= 2 hypotheses")
    if pinned is not None and pinned not in ids:
        raise ValueError(f"pinned incumbent {pinned!r} is not in the pool")
    if mode not in ("kway", "pairwise"):
        raise ValueError(f"unknown tournament mode {mode!r}")
    if (mode == "kway" and judge_k is None) or (mode == "pairwise" and judge_pair is None):
        raise ValueError(f"mode {mode!r} needs its judge")
    if dimensions and judge_k is None:
        raise ValueError("per-dimension rankings need a K-way judge")
    if generations < 1:
        raise ValueError("generations must be >= 1")
    history: list[dict[str, Any]] = []
    fit: BTFit | None = None
    res = RoundsResult()
    total_valid = total_rejected = 0
    for gen in range(generations):
        cur = [h["id"] for h in pool]
        if mode == "kway":
            assert judge_k is not None
            res = kway_rounds(pool, judge_k, rounds=rounds, k=k, rng=rng, pinned=pinned)
        else:
            assert judge_pair is not None
            res = pairwise_rounds(pool, judge_pair, repeats=repeats, rng=rng)
        total_valid += res.valid
        total_rejected += res.rejected
        if not res.choices:
            raise RuntimeError("judge produced no valid rankings")
        fit = fit_bt(cur, res.choices, position_effect=(mode == "pairwise"))
        dim_fits: dict[str, BTFit] = {}
        for d in dimensions:
            assert judge_k is not None
            dr = kway_rounds(pool, judge_k, rounds=rounds, k=k, rng=rng, pinned=pinned, dimension=d)
            if dr.choices:
                dim_fits[d] = fit_bt(cur, dr.choices)
        gen_rec: dict[str, Any] = {"generation": gen, "pool": cur, "valid": res.valid, "rejected": res.rejected,
                                   "position_bias": position_bias_report(res, fit, mode=mode, level=level),
                                   "operators": [], "pruned": []}
        if gen < generations - 1:
            if improve is not None:
                pool, rec = improve_weakest(pool, fit, improve, pinned=pinned, new_id=f"g{gen + 1}-imp",
                                            dim_fits=dim_fits)
                gen_rec["operators"].append(rec)
            if combiner is not None:
                pool, rec = combine(pool, fit, combiner, new_id=f"g{gen + 1}-comb", dim_fits=dim_fits)
                gen_rec["operators"].append(rec)
            if max_pool is not None:
                pool, dropped = prune(pool, fit, max_pool, pinned=pinned)
                gen_rec["pruned"] = dropped
        if pinned is not None and pinned not in {h["id"] for h in pool}:
            raise PinnedIncumbentError(f"pinned incumbent {pinned!r} left the pool in generation {gen}")
        history.append(gen_rec)
    assert fit is not None
    final_ids = [h["id"] for h in pool]
    ordered = _ranked(fit, final_ids)
    by_id = {h["id"]: h for h in pool}
    return {"order": ordered, "bt": fit.strengths, "fit": fit.summary(level), "mle_exists": fit.mle_exists,
            "position_bias": history[-1]["position_bias"], "valid_rounds": total_valid,
            "rejected_rounds": total_rejected, "pinned": pinned, "mode": mode, "history": history,
            "pool": [by_id[i] for i in ordered], "_fit": fit,
            "method": ("Plackett-Luce (K-way BT) over shuffled K-way rounds" if mode == "kway" else
                       "Bradley-Terry with a position term over both-order pairs") + "; Wald intervals"}


# ---------------------------------------------------------------- the `research.tournament` module body
def tournament_module(state: dict[str, Any], params: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Body of `research.tournament` (registered in research/modules.py, which delegates here). Defaults reproduce
    the original module: K-way over the whole pool, 3 shuffled rounds, judge role `judge`, rejected rounds counted.
    Params: mode (kway|pairwise), rounds, k, repeats, incumbent (pinned id), generations, max_pool, operators
    (list of "improve_weakest" / "combine"; they call role `proposer`), dimensions (rubric names for Improve)."""
    from ..gateway import ChatRequest, Message
    from ..runtime.engine import parse_json_output
    hyps = state["hypotheses"]["hypotheses"]
    task = state["task"]
    head = (f"Task: {task['description']}\nMetric: {task['metric']}\n"
            f"Data profile: {json.dumps(state['profile'])[:6000]}\n\n")

    def _ask(role: str, prompt: str, schema: dict[str, Any]) -> Any:
        resp = ctx.llm(role, ChatRequest(messages=[Message(role="user", content=prompt)], json_schema=schema))
        return parse_json_output(resp.text)

    def _listing(hs: Sequence[Hyp]) -> str:
        return "\n".join(f"- id={h['id']}: {h['idea']} (rationale: {h.get('rationale', '')})" for h in hs)

    def judge_k(presented: list[Hyp], dimension: str | None) -> Sequence[str] | None:
        crit = (f"on this criterion only: {dimension}" if dimension else
                "from most to least likely to improve the held-out metric over the baseline, considering "
                "feasibility given the data")
        prompt = (head + f"Rank ALL of these hypotheses {crit}. Use only the ids.\n{_listing(presented)}\n"
                  f'Return JSON {{"ranking": [ids best-first]}}.')
        schema = {"type": "object", "properties": {"ranking": {"type": "array", "items": {"type": "string"}}},
                  "required": ["ranking"]}
        out = _ask("judge", prompt, schema)
        return cast("Sequence[str] | None", out["ranking"])  # validated by kway_rounds

    def judge_pair(first: Hyp, second: Hyp) -> str | None:
        prompt = (head + "Which hypothesis is more likely to improve the held-out metric over the baseline, "
                  f"considering feasibility given the data?\n{_listing([first, second])}\n"
                  'Return JSON {"winner": id}.')
        schema = {"type": "object", "properties": {"winner": {"type": "string"}}, "required": ["winner"]}
        return cast("str | None", _ask("judge", prompt, schema)["winner"])  # validated by pairwise_rounds

    def _gen(prompt: str) -> Mapping[str, Any] | None:
        schema = {"type": "object", "properties": {"idea": {"type": "string"}, "rationale": {"type": "string"}},
                  "required": ["idea"]}
        out = _ask("proposer", head + prompt + '\nReturn JSON {"idea": ..., "rationale": ...}.', schema)
        return out if isinstance(out, Mapping) else None

    def improve(h: Hyp, dim: str | None, best: Hyp) -> Mapping[str, Any] | None:
        focus = f" Improve it specifically along: {dim}." if dim else ""
        return _gen(f"This hypothesis was rated weakest: {h['idea']} (rationale: {h.get('rationale', '')}).{focus}"
                    f" The strongest one is: {best['idea']}. Write an improved version.")

    def comb(a: Hyp, b: Hyp, da: str | None, db: str | None) -> Mapping[str, Any] | None:
        return _gen(f"Combine these two hypotheses into one, keeping each one's strongest aspect "
                    f"({da or 'unspecified'} / {db or 'unspecified'}):\nA: {a['idea']}\nB: {b['idea']}")

    ops = set(params.get("operators", ()))
    unknown = ops - {"improve_weakest", "combine"}
    if unknown:
        raise ValueError(f"unknown tournament operators: {sorted(unknown)}")
    out = run_tournament(hyps, mode=params.get("mode", "kway"), judge_k=judge_k, judge_pair=judge_pair,
                         rounds=int(params.get("rounds", 3)), k=params.get("k"),
                         repeats=int(params.get("repeats", 1)), generations=int(params.get("generations", 1)),
                         improve=improve if "improve_weakest" in ops else None,
                         combiner=comb if "combine" in ops else None,
                         dimensions=tuple(params.get("dimensions", ())), max_pool=params.get("max_pool"),
                         pinned=params.get("incumbent"), rng=random.Random(state.get("_seed", 0)))
    out.pop("_fit", None)
    queue = out.pop("pool")
    return {"ranking": out, "queue": queue, "current": None}
