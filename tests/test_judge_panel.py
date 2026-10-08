"""L3 judge panel (T5): >= 3 families, both orders, ensemble + meta-review, rubric decomposition; validated against
human labels before use; capped at T5 and never grants. Scripted fake backends only (no model calls)."""

import dataclasses
import json
import random
import re
from pathlib import Path

import pytest

from custom_harness.gateway import MockBackend
from custom_harness.tiers import PolicyDecision, Tier, chain_tier
from custom_harness.verify.judge import (
    NEEDS_OWNER,
    Criterion,
    HumanLabel,
    JudgePanel,
    LabelSet,
    PanelMember,
    PanelNotValidated,
    PanelRefused,
    calibration,
    cap_tier,
    cohen_kappa,
    load_labels,
    panel_gate,
    panel_status,
    validate_panel,
)

FIXTURE = Path(__file__).parent / "fixtures" / "judge" / "labels_fixture.json"
RUBRIC = [Criterion("soundness", "Which plan is methodologically sounder?", 2.0),
          Criterion("feasibility", "Which plan is more feasible on this data?", 1.0)]
_Q = re.compile(r"Option A:\n.*\[q=([0-9.]+)\].*\n\nOption B:\n.*\[q=([0-9.]+)\]", re.S)


def fake(family, kind="good", seed=0, replayed=False):
    """A scripted judge. good: picks the higher [q=..] marker (a little noise); contrarian: the lower; first:
    always "A" (pure position bias); random: coin flip; prose: answers in prose (must not be substring-matched)."""
    rng = random.Random(seed)

    def respond(req):
        text = req.messages[0].content
        if "meta-reviewer" in text:
            return json.dumps({"downgrade": False, "concerns": []})
        m = _Q.search(text)
        qa, qb = (float(m.group(1)), float(m.group(2))) if m else (0.5, 0.5)
        if kind == "good":
            w = "A" if qa + rng.gauss(0, 0.02) > qb else "B"
        elif kind == "contrarian":
            w = "B" if qa > qb else "A"
        elif kind == "first":
            w = "A"
        elif kind == "prose":
            return 'The winner is "A" because it is better.'
        else:
            w = rng.choice("AB")
        return json.dumps({"winner": w})
    b = MockBackend(respond)
    b.family = family
    if replayed:
        orig = b.complete

        def complete(req):
            r = orig(req)
            return r.model_copy(update={"replayed": True})
        b.complete = complete
    return b


def meta(family="metafam", text=None):
    b = MockBackend(lambda req: text if text is not None else json.dumps({"downgrade": False, "concerns": []}))
    b.family = family
    return PanelMember("meta", b)


def members(*specs):
    return [PanelMember(f"m{i}", fake(f, k, seed=i)) for i, (f, k) in enumerate(specs)]


GOOD3 = [("fam1", "good"), ("fam2", "good"), ("fam3", "good")]


def panel(specs=GOOD3, meta_member=None, rubric=RUBRIC):
    return JudgePanel(members(*specs), rubric, meta=meta_member or meta(), author_family="openai")


def human_set(n=60, seed=1):
    """A label set marked human for TESTS ONLY (a stand-in for owner-supplied labels)."""
    rng = random.Random(seed)
    labs = []
    for i in range(n):
        qa, qb = rng.uniform(0.05, 0.95), rng.uniform(0.05, 0.95)
        if abs(qa - qb) < 0.05:
            qb = min(0.95, qa + 0.1) if qa < 0.5 else qa - 0.1
        labs.append(HumanLabel(f"h{i}", f"plan [q={qa:.2f}]", f"plan [q={qb:.2f}]", "a" if qa > qb else "b"))
    return LabelSet(source="human", labels=tuple(labs), note="test stand-in")


# ---------------------------------------------------------------- refusal: families (invariant 5 NOT MET today)
def test_panel_refuses_with_fewer_than_three_families_before_any_call():
    ms = members(("fam1", "good"), ("fam2", "good"), ("fam2", "good"), ("fam1", "good"))
    with pytest.raises(PanelRefused, match=r">= 3 distinct model families.*needs owner"):
        JudgePanel(ms, RUBRIC, meta=meta(), author_family="openai")
    assert all(not m.backend.requests for m in ms)  # refused before any model call


@pytest.mark.parametrize("specs,msg", [
    ([("fam1", "good"), ("fam2", "good"), ("openai", "good")], "author's family"),
    ([("fam1", "good"), ("fam2", "good"), ("mock", "good")], "unknown or mock"),
])
def test_panel_refuses_author_family_and_mock_members(specs, msg):
    with pytest.raises(PanelRefused, match=msg):
        panel(specs)


def test_panel_refuses_bad_meta_reviewer_and_bad_rubric():
    with pytest.raises(PanelRefused, match="meta-reviewer"):
        panel(meta_member=meta(family="openai"))
    with pytest.raises(PanelRefused, match="meta-reviewer is required"):
        JudgePanel(members(*GOOD3), RUBRIC, meta=None, author_family="openai")
    with pytest.raises(PanelRefused, match="rubric"):
        panel(rubric=[])


def test_todays_configuration_is_refused_and_says_needs_owner():
    class Codex:
        name, provider, model = "codex", "openai", "gpt-5"
    st = panel_status({"proposer": Codex(), "judge": Codex(), "executor": Codex()})
    assert st["status"].startswith("REFUSED") and NEEDS_OWNER in st["status"] and st["other_families"] == []


# ---------------------------------------------------------------- validation against human labels before use
def test_unvalidated_panel_refuses_to_judge_before_any_call():
    p = panel()
    with pytest.raises(PanelNotValidated, match="needs owner"):
        p.evaluate("plan [q=0.9]", "plan [q=0.1]", validation=None)
    with pytest.raises(PanelNotValidated):
        p.rank({"x": "plan [q=0.9]", "y": "plan [q=0.1]"}, validation=None)
    assert all(not m.backend.requests for m in p.members)


def test_fixture_labels_are_measured_but_can_never_validate():
    labels = load_labels(FIXTURE)
    assert labels.source == "synthetic-fixture" and len(labels.labels) == 60
    p = panel()
    v = validate_panel(p, labels)
    assert v.agreement > 0.9 and v.kappa > 0.8 and v.n == 57 and v.ties_skipped == 3  # the harness measured it
    assert not v.passed and any("not human" in r for r in v.reasons)
    with pytest.raises(PanelNotValidated, match="not human"):
        p.evaluate("plan [q=0.9]", "plan [q=0.1]", validation=v)
    forged = dataclasses.replace(v, passed=True, reasons=())  # a copied record claiming a pass is refused
    with pytest.raises(PanelNotValidated, match="not issued"):
        p.evaluate("plan [q=0.9]", "plan [q=0.1]", validation=forged)
    p._issued.append(forged)  # even if it were slipped into the registry, non-human labels are refused
    with pytest.raises(PanelNotValidated, match="not human"):
        p.evaluate("plan [q=0.9]", "plan [q=0.1]", validation=forged)


def test_hand_built_validation_record_is_refused():
    from custom_harness.verify.judge import PanelValidation
    p = panel()
    fake_pass = PanelValidation(p.config_hash, "human", "x", 100, 0, 1.0, 1.0, 0.0, 0.0, 0.0, True, ())
    with pytest.raises(PanelNotValidated, match="not issued"):
        p.evaluate("plan [q=0.9]", "plan [q=0.1]", validation=fake_pass)
    assert all(not m.backend.requests for m in p.members)


def test_family_floor_and_thresholds_can_only_be_made_stricter():
    with pytest.raises(PanelRefused, match="below the floor"):
        JudgePanel(members(("fam1", "good")), RUBRIC, meta=meta(), author_family="openai", min_families=1)
    p = panel()
    for loose in ({"min_labels": 1}, {"min_agreement": 0.0}, {"min_kappa": -1.0}, {"max_ece": 1.0}):
        with pytest.raises(ValueError, match="stricter"):
            validate_panel(p, human_set(), **loose)
    assert not validate_panel(p, human_set(), min_labels=1000).passed  # stricter is allowed


def test_validation_rejects_a_bad_panel():
    bad = panel([("fam1", "random"), ("fam2", "contrarian"), ("fam3", "first")])
    v = validate_panel(bad, human_set())
    assert not v.passed and any("agreement" in r for r in v.reasons) and any("kappa" in r for r in v.reasons)
    with pytest.raises(PanelNotValidated, match="failed"):
        bad.evaluate("plan [q=0.9]", "plan [q=0.1]", validation=v)


def test_validation_requires_enough_labels():
    v = validate_panel(panel(), human_set(n=10))
    assert not v.passed and any("< 50" in r for r in v.reasons)


def test_validation_is_bound_to_the_exact_panel_configuration():
    v = validate_panel(panel(), human_set())
    assert v.passed, v.reasons
    other = panel(rubric=RUBRIC[:1])
    with pytest.raises(PanelNotValidated, match="different panel configuration"):
        other.evaluate("plan [q=0.9]", "plan [q=0.1]", validation=v)


def test_kappa_and_calibration_known_values():
    # 50 items: rater1 yes 25 (agree yes 20), rater2 yes 30; po = 0.7, pe = 0.5*0.6 + 0.5*0.4 = 0.5 -> kappa 0.4
    r1 = ["y"] * 20 + ["y"] * 5 + ["n"] * 10 + ["n"] * 15
    r2 = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert cohen_kappa(r1, r2) == pytest.approx(0.4)
    assert cohen_kappa(["a", "a"], ["a", "a"]) != cohen_kappa(["a", "a"], ["a", "a"])  # NaN: undefined
    c = calibration([0.9, 0.9, 0.1, 0.1], [1, 0, 0, 0], bins=10)
    assert c["ece"] == pytest.approx(0.5 * 0.4 + 0.5 * 0.1)
    assert c["brier"] == pytest.approx((0.01 + 0.81 + 0.01 + 0.01) / 4)
    with pytest.raises(ValueError):
        calibration([1.2], [1])


def test_label_loader_fails_closed(tmp_path):
    for bad in ({"labels": []}, {"source": "human", "labels": [{"id": "x", "a": "1", "b": "2", "human": "A"}]},
                {"source": "human", "labels": [{"id": "x", "a": "1", "b": "2", "human": "a"}] * 2}):
        f = tmp_path / "l.json"
        f.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_labels(f)


# ---------------------------------------------------------------- judging: both orders, ensemble, meta-review
def _validated(specs=GOOD3, meta_member=None):
    p = panel(specs, meta_member=meta_member)
    v = validate_panel(p, human_set())
    assert v.passed, v.reasons
    return p, v


def test_validated_panel_supports_better_candidate_and_is_capped_at_t5():
    p, v = _validated()
    good = p.evaluate("plan [q=0.90]", "plan [q=0.20]", validation=v)
    assert good.supported and good.tier == Tier.T5_LLM_JUDGE and good.grants is False
    assert set(good.judgement.by_criterion) == {"soundness", "feasibility"}  # rubric decomposition reported
    assert chain_tier([Tier.T1_EXECUTABLE, good.tier]) == Tier.T5_LLM_JUDGE
    assert cap_tier(Tier.T0_FORMAL) == Tier.T5_LLM_JUDGE and cap_tier(Tier.T6_NONE) == Tier.T6_NONE
    worse = p.evaluate("plan [q=0.20]", "plan [q=0.90]", validation=v)
    assert not worse.supported


def test_every_member_judges_every_criterion_in_both_orders():
    p, v = _validated()
    for m in p.members:
        m.backend.requests.clear()
    p.evaluate("plan [q=0.90]", "plan [q=0.20]", validation=v)
    for m in p.members:
        prompts = [r.messages[0].content for r in m.backend.requests]
        for c in RUBRIC:
            mine = [t for t in prompts if t.startswith(f"Criterion: {c.name}.")]
            assert len(mine) == 2 and mine[0].index("q=0.90") < mine[0].index("q=0.20")
            assert mine[1].index("q=0.20") < mine[1].index("q=0.90")


def test_pure_position_bias_is_neutralised_by_both_orders():
    p = panel([("fam1", "first"), ("fam2", "first"), ("fam3", "first")])
    j = p.compare("plan [q=0.90]", "plan [q=0.20]")
    assert j.first_wins == j.first_trials == 12  # the attack happened: every answer was "first shown"
    assert j.prob_a == 0.5 and j.prefers is None and j.order_dependent == 6 and j.position_p_value < 0.001


def test_one_family_cannot_outvote_the_others():
    specs = [("fam1", "good"), ("fam2", "good")] + [("fam3", "contrarian")] * 5
    j = panel(specs).compare("plan [q=0.90]", "plan [q=0.20]")
    assert j.by_family["fam3"] == 0.0 and j.prob_a == pytest.approx(2 / 3) and j.prefers == "a"


@pytest.mark.parametrize("kind", ["prose", "replayed"])
def test_unparseable_or_replayed_answers_abstain_and_never_support(kind):
    first = fake("fam1", "prose") if kind == "prose" else fake("fam1", "good", replayed=True)
    ms = [PanelMember("x0", first)] + members(("fam2", "good"), ("fam3", "good"))
    p = JudgePanel(ms, RUBRIC, meta=meta(), author_family="openai")
    j = p.compare("plan [q=0.90]", "plan [q=0.20]")
    assert j.abstentions == 2 and j.families_voting == 2 and j.prob_a is None  # no quorum without fam1
    assert j.prefers is None


def test_meta_review_can_only_downgrade():
    veto = meta(text=json.dumps({"downgrade": True, "concerns": ["criteria disagree"]}))
    p, v = _validated(meta_member=veto)
    out = p.evaluate("plan [q=0.90]", "plan [q=0.20]", validation=v)
    assert not out.supported and "criteria disagree" in " ".join(out.reasons)
    garbled = meta(text="looks fine to me")
    p, v = _validated(meta_member=garbled)
    out = p.evaluate("plan [q=0.90]", "plan [q=0.20]", validation=v)
    assert not out.supported and not out.meta.valid  # unparseable meta-review = downgrade (fail closed)
    p, v = _validated()  # an approving meta-review cannot rescue a candidate the panel does not prefer
    assert not p.evaluate("plan [q=0.20]", "plan [q=0.90]", validation=v).supported


def test_panel_gate_never_raises_a_decision():
    p, v = _validated()
    yes = p.evaluate("plan [q=0.90]", "plan [q=0.20]", validation=v)
    no = p.evaluate("plan [q=0.20]", "plan [q=0.90]", validation=v)
    order = {PolicyDecision.DENY: 0, PolicyDecision.NEEDS_HUMAN: 1, PolicyDecision.ALLOW: 2}
    for d in PolicyDecision:
        for verdict in (yes, no, None):
            assert order[panel_gate(d, verdict)] <= order[d]
        assert panel_gate(d, yes) == d
    assert panel_gate(PolicyDecision.ALLOW, no) == PolicyDecision.NEEDS_HUMAN
    assert panel_gate(PolicyDecision.DENY, yes) == PolicyDecision.DENY


def test_rank_aggregates_by_family_with_quorum(monkeypatch):
    """Aggregation only (validation is bypassed here: a 5-contrarian family makes the panel miscalibrated, so it
    could not pass validation; the point is that member count cannot outvote families, and quorum is enforced)."""
    specs = [("fam1", "good"), ("fam2", "good")] + [("fam3", "contrarian")] * 5
    p2 = panel(specs)
    monkeypatch.setattr(JudgePanel, "_require_validation", lambda self, v: v)
    fit = p2.rank({"lo": "plan [q=0.10]", "hi": "plan [q=0.90]"}, validation=None)
    assert fit.theta["hi"] > fit.theta["lo"]  # 2 families vs 1, not 2 members vs 5
    assert fit.n_choices == 6  # 2 orders x 3 families (not 7 members)
    p3 = JudgePanel([PanelMember("x0", fake("fam1", "prose"))] + members(("fam2", "good"), ("fam3", "good")),
                    RUBRIC, meta=meta(), author_family="openai")
    with pytest.raises(PanelRefused, match="quorum"):  # only 2 families answer: no pair counts
        p3.rank({"lo": "plan [q=0.10]", "hi": "plan [q=0.90]"}, validation=None)


def test_rank_fits_bt_with_position_term_and_intervals():
    p, v = _validated()
    fit = p.rank({"w": "plan [q=0.10]", "x": "plan [q=0.40]", "y": "plan [q=0.60]", "z": "plan [q=0.90]"},
                 validation=v)
    assert sorted(fit.theta, key=lambda i: -fit.theta[i]) == ["z", "y", "x", "w"]
    assert fit.position is not None and fit.position_interval() is not None
