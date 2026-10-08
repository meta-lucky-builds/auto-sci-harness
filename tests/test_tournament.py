"""L4.5 tournament (GOAL §5 L4 stage 5, §1.1): BT/PL intervals, both-order pairwise mode with position bias,
Improve-weakest / Combine operators, pinned incumbent. Synthetic judges with KNOWN true strengths; no model calls."""

import json
import math
import random
from itertools import combinations

import pytest

from custom_harness.research.tournament import (
    PinnedIncumbentError,
    combine,
    improve_weakest,
    kway_rounds,
    pairwise_rounds,
    position_bias_report,
    run_tournament,
    tournament_module,
)
from custom_harness.verify.ranking import Choice, binom_two_sided_p, fit_bt, pair_choice, ranking_to_choices

ITEMS = ["a", "b", "c", "d"]
_RAW = [-0.9, -0.3, 0.2, 1.0]
TRUE = {i: t - sum(_RAW) / len(_RAW) for i, t in zip(ITEMS, _RAW, strict=True)}  # centred, as fit_bt reports
N_SIMS = 1000


def _p(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def pl_sample(rng: random.Random, items, theta) -> list[str]:
    rem, out = list(items), []
    while rem:
        ws = [math.exp(theta[i]) for i in rem]
        r, acc = rng.random() * sum(ws), 0.0
        pick = rem[-1]
        for i, w in zip(rem, ws, strict=True):
            acc += w
            if r <= acc:
                pick = i
                break
        out.append(pick)
        rem.remove(pick)
    return out


def pair_judge(theta, rng, gamma=0.0):
    """Synthetic pairwise judge: P(first wins) = logistic(theta_first - theta_second + gamma)."""
    def judge(first, second):
        return first["id"] if rng.random() < _p(theta[first["id"]] - theta[second["id"]] + gamma) else second["id"]
    return judge


def kway_judge(theta, rng, first_bonus=0.0):
    """Synthetic K-way judge: Plackett-Luce on theta, plus (optionally) extra log-strength for the first-listed."""
    def judge(presented, dimension=None):
        th = {h["id"]: theta[h["id"]] for h in presented}
        th[presented[0]["id"]] += first_bonus
        return pl_sample(rng, list(th), th)
    return judge


def _coverage(choice_maker, level=0.95):
    rng = random.Random(12345)
    hits = {i: 0 for i in ITEMS}
    for _ in range(N_SIMS):
        fit = fit_bt(ITEMS, choice_maker(rng))
        for i in ITEMS:
            lo, hi = fit.interval(i, level)
            hits[i] += lo <= TRUE[i] <= hi
    return {i: h / N_SIMS for i, h in hits.items()}


# ---------------------------------------------------------------- interval coverage at the nominal rate
# 1000 simulations: the binomial SE of a 95% coverage estimate is 0.0069, so [0.93, 0.97] is about +-3 SE.
def _pairwise_choices(rng):
    out = []
    for a, b in combinations(ITEMS, 2):
        for _ in range(8):
            for f, s in ((a, b), (b, a)):
                out.append(pair_choice(f, s, f if rng.random() < _p(TRUE[f] - TRUE[s]) else s))
    return out


def _kway_choices(rng):
    return [c for _ in range(30) for c in ranking_to_choices(pl_sample(rng, ITEMS, TRUE))]


def test_bt_intervals_cover_truth_at_nominal_rate_pairwise():
    cov = _coverage(_pairwise_choices)
    assert all(0.93 <= c <= 0.97 for c in cov.values()), cov


def test_pl_intervals_cover_truth_at_nominal_rate_kway():
    cov = _coverage(_kway_choices)
    assert all(0.93 <= c <= 0.97 for c in cov.values()), cov


def test_coverage_check_has_teeth_naive_rank_breaking_undercovers():
    """The same coverage check rejects the obvious wrong method: breaking each K-way ranking into independent pairs
    (what `rankings_to_wins` does) overstates the information, so its intervals are too narrow."""
    def naive(rng):
        out = []
        for _ in range(30):
            r = pl_sample(rng, ITEMS, TRUE)
            out += [Choice(options=(r[i], r[j]), chosen=r[i]) for i, j in combinations(range(len(r)), 2)]
        return out
    cov = _coverage(naive)
    assert sum(cov.values()) / len(cov) < 0.92, cov


# ---------------------------------------------------------------- the fit itself
def test_fit_recovers_order_and_flags_separation():
    rng = random.Random(3)
    fit = fit_bt(ITEMS, _pairwise_choices(rng))
    assert sorted(ITEMS, key=lambda i: -fit.theta[i]) == ["d", "c", "b", "a"]
    assert abs(sum(fit.theta.values())) < 1e-9 and fit.mle_exists
    unbeaten = [pair_choice("a", "b", "a"), pair_choice("b", "a", "a"), pair_choice("b", "c", "b"),
                pair_choice("c", "b", "c")]
    sep = fit_bt(["a", "b", "c"], unbeaten)
    assert not sep.mle_exists  # 'a' never lost: Ford's condition fails, the intervals are prior-dominated


@pytest.mark.parametrize("bad", [Choice(options=("a", "b"), chosen="z"), Choice(options=("a", "a"), chosen="a"),
                                 Choice(options=("a", "q"), chosen="a"), Choice(options=("a",), chosen="a"),
                                 Choice(options=("a", "b"), chosen="a", weight=float("nan"))])
def test_fit_fails_closed_on_malformed_choices(bad):
    with pytest.raises(ValueError):
        fit_bt(["a", "b"], [bad])


def test_binomial_p_value_matches_known_values():
    assert binom_two_sided_p(0, 10, 0.5) == pytest.approx(2 / 1024)
    assert binom_two_sided_p(5, 10, 0.5) == pytest.approx(1.0)
    assert binom_two_sided_p(8, 10, 0.5) == pytest.approx(112 / 1024)


# ---------------------------------------------------------------- position bias: measured, reported, detected
POOL = [{"id": i, "idea": f"idea {i}"} for i in ITEMS]


def test_position_biased_pairwise_judge_is_detected_and_corrected():
    rng = random.Random(7)
    res = pairwise_rounds(POOL, pair_judge(TRUE, rng, gamma=1.0), repeats=10, rng=rng)
    assert res.first_hits / res.first_trials > 0.6  # the attack happened: first-shown wins far more than half
    fit = fit_bt(ITEMS, res.choices, position_effect=True)
    rep = position_bias_report(res, fit, mode="pairwise")
    assert rep["detected"] and rep["p_value"] < 1e-4
    lo, hi = rep["gamma_ci"]
    assert lo <= 1.0 <= hi and lo > 0
    assert sorted(ITEMS, key=lambda i: -fit.theta[i]) == ["d", "c", "b", "a"]  # strengths still recovered
    assert rep["order_consistency"] < 0.8 and rep["pairs_judged_both_orders"] == 60


def test_tournament_pairwise_mode_fits_the_position_term():
    rng = random.Random(9)
    out = run_tournament(POOL, mode="pairwise", judge_pair=pair_judge(TRUE, rng, gamma=1.0), repeats=10, rng=rng)
    pb = out["position_bias"]
    assert pb["detected"] and pb["gamma_ci"][0] <= 1.0 <= pb["gamma_ci"][1] and pb["gamma_ci"][0] > 0
    assert out["fit"]["position"] == pb["gamma"] and out["order"][0] == "d"  # a vs b (gap 0.6) is noise-level here


def test_unbiased_pairwise_judge_rarely_flagged():
    rng = random.Random(11)
    flagged = 0
    for _ in range(200):
        res = pairwise_rounds(POOL, pair_judge(TRUE, rng), repeats=3, rng=rng)
        flagged += position_bias_report(res, None, mode="pairwise")["detected"]
    assert flagged <= 6  # alpha = 0.01 and the test is conservative under a balanced both-order design


def test_order_dependent_refusals_do_not_fake_position_bias():
    """A judge with NO position preference that refuses whenever the weaker item is shown first: counting single
    answers would show 'first always wins'. Only pairs judged in both orders are tested and fitted."""
    def judge(first, second):
        if TRUE[first["id"]] < TRUE[second["id"]]:
            return None
        return first["id"]
    res = pairwise_rounds(POOL, judge, repeats=5, rng=random.Random(0))
    assert res.valid == 30 and res.rejected == 30  # the attack happened: every refusal depends on the order
    rep = position_bias_report(res, None, mode="pairwise")
    assert rep["trials"] == 0 and not rep["detected"] and rep["unpaired_answers_dropped"] == 30
    assert res.choices == []


def test_kway_rejections_are_reported_by_first_listed():
    def judge(presented, dim):
        return None if presented[0]["id"] == "a" else [h["id"] for h in presented]
    res = kway_rounds(POOL, judge, rounds=40, rng=random.Random(0))
    rep = position_bias_report(res, None, mode="kway")
    assert set(rep["rejected_by_first_listed"]) == {"a"} and rep["caveat"]


def test_backend_errors_are_not_counted_as_rejected_rounds():
    class NoBackend:
        def llm(self, role, req):
            raise ValueError(f"no backend bound to role '{role}'")
    with pytest.raises(ValueError, match="no backend"):
        tournament_module(_state(), {}, NoBackend())


def test_position_biased_kway_judge_is_detected():
    rng = random.Random(5)
    biased = kway_rounds(POOL, kway_judge(TRUE, rng, first_bonus=2.0), rounds=60, rng=rng)
    assert biased.first_hits / biased.first_trials > 0.45  # attack happened (null rate 1/4)
    assert position_bias_report(biased, None, mode="kway")["detected"]
    clean = kway_rounds(POOL, kway_judge(TRUE, rng), rounds=60, rng=rng)
    assert not position_bias_report(clean, None, mode="kway")["detected"]


def test_both_orders_are_always_judged():
    seen = []

    def judge(first, second):
        seen.append((first["id"], second["id"]))
        return first["id"]
    pairwise_rounds(POOL, judge, repeats=2, rng=random.Random(0))
    for a, b in combinations(ITEMS, 2):
        assert seen.count((a, b)) == 2 and seen.count((b, a)) == 2


# ---------------------------------------------------------------- pinned incumbent
def _children_ok(h, dim, best):
    return {"idea": h["idea"] + " (improved)", "rationale": f"dim={dim}"}


def _combined(a, b, da, db):
    return {"idea": f"{a['idea']} + {b['idea']}"}


@pytest.mark.parametrize("mode", ["kway", "pairwise"])
def test_pinned_incumbent_is_never_dropped(mode):
    """The incumbent is the WEAKEST item, operators always succeed and the pool cap forces drops every generation:
    without the pin it would be the first to go (asserted by the control below)."""
    for seed in range(30):
        rng = random.Random(seed)
        theta = dict(TRUE)
        theta["inc"] = -3.0
        theta.update({f"g{g}-imp": 0.0 for g in range(1, 8)})
        theta.update({f"g{g}-comb": 0.5 for g in range(1, 8)})
        pool = POOL + [{"id": "inc", "idea": "incumbent"}]
        kw = dict(judge_k=kway_judge(theta, rng), k=3) if mode == "kway" else dict(judge_pair=pair_judge(theta, rng))
        out = run_tournament(pool, mode=mode, rounds=6, repeats=2, generations=5, improve=_children_ok,
                             combiner=_combined, max_pool=4, pinned="inc", rng=rng, **kw)
        assert "inc" in out["order"]
        for gen in out["history"]:
            assert "inc" in gen["pool"]
            assert all(r.get("target") != "inc" for r in gen["operators"])
            assert "inc" not in gen["pruned"]
        dropped = [d for gen in out["history"] for d in gen["pruned"]]
        assert dropped  # the cap really forced drops (the attack on the incumbent happened)
        first_fit = out["history"][0]
        assert first_fit["pool"] and "inc" in first_fit["pool"]


def test_pinned_incumbent_in_every_kway_block():
    rng = random.Random(1)
    pool = POOL + [{"id": "inc", "idea": "incumbent"}, {"id": "e", "idea": "e"}, {"id": "f", "idea": "f"}]
    th = dict(TRUE, inc=0.0, e=0.0, f=0.0)
    res = kway_rounds(pool, kway_judge(th, rng), rounds=5, k=3, rng=rng, pinned="inc")
    assert res.records and all("inc" in r["presented"] and len(r["presented"]) == 3 for r in res.records)
    covered = {i for r in res.records for i in r["presented"]}
    assert covered == {h["id"] for h in pool}


def test_control_unpinned_weak_incumbent_is_dropped():
    rng = random.Random(0)
    theta = dict(TRUE, inc=-3.0, **{"g1-imp": 0.0, "g1-comb": 0.5})
    pool = POOL + [{"id": "inc", "idea": "incumbent"}]
    out = run_tournament(pool, mode="pairwise", judge_pair=pair_judge(theta, rng), repeats=3, generations=2,
                         improve=_children_ok, max_pool=4, pinned=None, rng=rng)
    assert "inc" not in out["order"]


def test_pinned_must_be_in_pool_and_violation_raises(monkeypatch):
    with pytest.raises(ValueError):
        run_tournament(POOL, mode="pairwise", judge_pair=pair_judge(TRUE, random.Random(0)), pinned="nope")
    import custom_harness.research.tournament as T

    def evil_prune(pool, fit, max_size, *, pinned):
        return [h for h in pool if h["id"] != pinned], [pinned]
    monkeypatch.setattr(T, "prune", evil_prune)
    with pytest.raises(PinnedIncumbentError):
        run_tournament(POOL, mode="pairwise", judge_pair=pair_judge(TRUE, random.Random(0)), generations=2,
                       max_pool=3, pinned="a")


# ---------------------------------------------------------------- operators
def _fit_for(theta):
    rng = random.Random(2)
    pool = [{"id": i, "idea": i} for i in theta]
    res = pairwise_rounds(pool, pair_judge(theta, rng), repeats=20, rng=rng)
    return pool, fit_bt(list(theta), res.choices, position_effect=True)


def test_improve_weakest_targets_the_weakest_non_pinned_along_its_weakest_dimension():
    pool, fit = _fit_for(TRUE)
    dim_fits = {"novelty": fit_bt(ITEMS, [pair_choice("a", "b", "a")] * 5 + [pair_choice("b", "a", "b")]),
                "feasibility": fit_bt(ITEMS, [pair_choice("b", "a", "b")] * 5 + [pair_choice("a", "b", "a")])}
    calls = []

    def imp(h, dim, best):
        calls.append((h["id"], dim, best["id"]))
        return {"idea": "better a"}
    new, rec = improve_weakest(pool, fit, imp, pinned=None, new_id="a2", dim_fits=dim_fits)
    assert calls == [("a", "feasibility", "d")] and rec["applied"]
    assert [h["id"] for h in new] == ["a2", "b", "c", "d"] and new[0]["parent"] == "a"
    _, rec2 = improve_weakest(pool, fit, imp, pinned="a", new_id="b2")
    assert rec2["target"] == "b"  # the pinned weakest is skipped


def test_combine_children_of_top_two():
    pool, fit = _fit_for(TRUE)
    new, rec = combine(pool, fit, _combined, new_id="cd")
    assert rec["parents"] == ["d", "c"] and new[-1]["parents"] == ["d", "c"] and len(new) == 5


@pytest.mark.parametrize("bad", [None, {}, {"idea": ""}, {"idea": 3}, "text", ["idea"]])
def test_operators_fail_closed_on_invalid_output(bad):
    pool, fit = _fit_for(TRUE)
    new, rec = improve_weakest(pool, fit, lambda *a: bad, pinned=None, new_id="x")
    assert new == pool and not rec["applied"]
    new, rec = combine(pool, fit, lambda *a: bad, new_id="y")
    assert new == pool and not rec["applied"]


def test_invalid_judge_rounds_are_rejected_not_guessed():
    answers = iter([["a", "b"], None, ["a", "b", "c", "d", "d"], 7, ["d", "c", "b", "a"]])
    res = kway_rounds(POOL, lambda presented, dim: next(answers), rounds=5, rng=random.Random(0))
    assert res.valid == 1 and res.rejected == 4


# ---------------------------------------------------------------- the research.tournament module
class _Ctx:
    def __init__(self, responder):
        from custom_harness.gateway import MockBackend
        self.backend = MockBackend(responder)

    def llm(self, role, req):
        assert role in ("judge", "proposer")
        return self.backend.complete(req)


def _state():
    return {"hypotheses": {"hypotheses": [dict(h) for h in POOL]}, "task": {"description": "d", "metric": "acc"},
            "profile": {}, "_seed": 0}


def _ids_in(prompt):
    return [ln.split("id=")[1].split(":")[0] for ln in prompt.splitlines() if ln.startswith("- id=")]


def test_module_pairwise_mode_reports_position_bias_and_intervals():
    def first_wins(req):  # a maximally position-biased judge
        return json.dumps({"winner": _ids_in(req.messages[0].content)[0]})
    out = tournament_module(_state(), {"mode": "pairwise", "repeats": 3}, _Ctx(first_wins))
    rb = out["ranking"]
    assert rb["position_bias"]["detected"] and rb["position_bias"]["rate"] == 1.0
    assert set(rb["fit"]["ci"]) == set(ITEMS) and rb["mode"] == "pairwise"
    assert [h["id"] for h in out["queue"]] == rb["order"]


def test_module_default_kway_matches_judge_and_reports_intervals():
    def fixed(req):
        return json.dumps({"ranking": ["d", "c", "b", "a"]})
    out = tournament_module(_state(), {"rounds": 4}, _Ctx(fixed))
    rb = out["ranking"]
    assert rb["order"] == ["d", "c", "b", "a"] and rb["valid_rounds"] == 4 and rb["rejected_rounds"] == 0
    assert rb["fit"]["ci_level"] == 0.95 and "position_bias" in rb


def test_module_single_hypothesis_needs_no_judge():
    st = _state()
    st["hypotheses"]["hypotheses"] = st["hypotheses"]["hypotheses"][:1]
    ctx = _Ctx(lambda req: pytest.fail("no judge call expected"))
    out = tournament_module(st, {}, ctx)
    assert out["ranking"]["order"] == ["a"] and out["queue"] == st["hypotheses"]["hypotheses"]


def test_module_operators_call_proposer_and_keep_incumbent():
    roles = []

    class Ctx(_Ctx):
        def llm(self, role, req):
            roles.append(role)
            return super().llm(role, req)

    def resp(req):
        text = req.messages[0].content
        if "Return JSON {\"idea\"" in text:
            return json.dumps({"idea": "new idea", "rationale": "r"})
        ids = _ids_in(text)
        return json.dumps({"ranking": sorted(ids, key=lambda i: (i != "d", i))})
    out = tournament_module(_state(), {"rounds": 2, "generations": 3, "operators": ["improve_weakest", "combine"],
                                       "max_pool": 4, "incumbent": "a"}, Ctx(resp))
    assert "proposer" in roles and "a" in out["ranking"]["order"]
    ops = [r for g in out["ranking"]["history"] for r in g["operators"]]
    assert any(r["operator"] == "improve_weakest" and r["applied"] for r in ops)
    assert any(r["operator"] == "combine" and r["applied"] for r in ops)
    with pytest.raises(ValueError):
        tournament_module(_state(), {"operators": ["mutate"]}, Ctx(resp))
