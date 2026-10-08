"""GOAL §5 L8 (data side only; no training here): trajectory export certified by BOTH of two disjoint verifier sets,
licence filter (invariant 11, fail closed), SFT rows filtered by outcome, coupled-Goodhart indicator and task selection
by reward variance (src/custom_harness/weights/).

Attack tests assert that the attack really happened (the trajectory really was certified by A, the output really is
from a provider whose flag is false, the sets really overlap...) and that it was excluded or refused."""

from __future__ import annotations

import json
import math

import pytest

from custom_harness.records import AccessMode, Provenance
from custom_harness.tiers import Tier
from custom_harness.weights import config as C
from custom_harness.weights.export import ExportRefused, Reason, export_trajectories, select_for_training
from custom_harness.weights.goodhart import GapPoint, goodhart_indicator
from custom_harness.weights.licence import LicenceConfigError, LicencePolicy
from custom_harness.weights.selection import select_by_reward_variance
from custom_harness.weights.trajectory import (
    Step,
    Trajectory,
    Verdict,
    VerifierRef,
    VerifierSet,
    certified_by,
    declare_disjoint,
)

H = {k: (str(k) * 64)[:64] for k in "0123456789abcdef"}  # distinct 64-hex adapter hashes


def prov(provider: str = "local", mode: AccessMode = AccessMode.OPEN_WEIGHTS, *, terms: str = "allowed",
         replayed: bool = False) -> Provenance:
    return Provenance(backend=f"{provider}-be", provider=provider, model="m1", mode=mode, training_on_outputs=terms,
                      replayed=replayed)


def traj(tid: str = "t1", *, outcome: str = "certified", p: Provenance | None = None,
         answer: str = "x = 1") -> Trajectory:
    return Trajectory(id=tid, task_id=f"task-{tid}", outcome=outcome, steps=(
        Step("system", "You write code."),
        Step("user", "Solve the task."),
        Step("assistant", answer, p if p is not None else prov()),
        Step("tool", "exit 0"),
    ))


SET_A = VerifierSet("A", (VerifierRef("unit-tests", H["1"], Tier.T1_EXECUTABLE),
                          VerifierRef("paired-ci", H["2"], Tier.T2_STATISTICAL)))
SET_B = VerifierSet("B", (VerifierRef("sealed-replay", H["3"], Tier.T1_EXECUTABLE),))
PAIR = declare_disjoint(SET_A, SET_B)
OPEN = LicencePolicy.from_mapping({"local/ow": True})  # the owner allowed training on this local open-weights model


def verdicts_for(t: Trajectory, *sets: VerifierSet, passed: bool = True) -> list[Verdict]:
    return [Verdict(m.id, m.adapter_hash, t.digest(), passed, 1.0 if passed else 0.0) for s in sets for m in s.members]


# ------------------------------------------------------------------ disjoint verifier sets
@pytest.mark.parametrize("b_member", [
    VerifierRef("unit-tests", H["9"], Tier.T1_EXECUTABLE),  # same verifier id, other adapter hash
    VerifierRef("other-name", H["1"], Tier.T1_EXECUTABLE),  # renamed, but the SAME adapter code
    VerifierRef("  Unit-Tests ", H["9"], Tier.T1_EXECUTABLE),  # same id up to case/whitespace
    VerifierRef("other-name", H["1"].upper(), Tier.T1_EXECUTABLE),  # same hash, upper-case hex
])
def test_overlapping_verifier_sets_are_refused(b_member):
    b = VerifierSet("B", (VerifierRef("sealed-replay", H["3"], Tier.T1_EXECUTABLE), b_member))
    a_ids = {m.id for m in SET_A.members}
    a_hashes = {m.adapter_hash for m in SET_A.members}
    # the attack: B really shares a verifier (by id or by adapter code) with A
    assert b_member.id.strip().lower() in a_ids or b_member.adapter_hash.lower() in a_hashes
    with pytest.raises(ExportRefused) as e:
        declare_disjoint(SET_A, b)
    assert e.value.code == "SETS_OVERLAP"


def test_verifier_set_declarations_fail_closed():
    with pytest.raises(ExportRefused) as e:
        declare_disjoint(SET_A, VerifierSet("B", ()))
    assert e.value.code == "SET_INVALID"
    with pytest.raises(ExportRefused) as e:
        VerifierSet("B", (VerifierRef("x", "not-a-hash", Tier.T1_EXECUTABLE),))
    assert e.value.code == "SET_INVALID"
    judge = VerifierSet("B", (VerifierRef("llm-judge", H["4"], Tier.T5_LLM_JUDGE),))
    assert judge.members[0].tier > C.EXPORT_TIER_FLOOR  # the attack: an LLM judge as a certifying set
    with pytest.raises(ExportRefused) as e:
        declare_disjoint(SET_A, judge)
    assert e.value.code == "TIER_BELOW_FLOOR"
    with pytest.raises(ExportRefused) as e:
        declare_disjoint(SET_A, VerifierSet("A", SET_B.members))
    assert e.value.code == "SET_INVALID"  # two sets with one name


# ------------------------------------------------------------------ both sets must certify
def test_trajectory_certified_only_by_set_a_is_excluded():
    t = traj()
    vs = verdicts_for(t, SET_A)
    assert certified_by(t, SET_A, vs) and not certified_by(t, SET_B, vs)  # really certified by A, not by B
    sel = select_for_training([t], vs, PAIR, OPEN)
    assert sel.kept == [] and sel.excluded == {"t1": [Reason.NOT_CERTIFIED_B]}
    both = select_for_training([t], verdicts_for(t, SET_A, SET_B), PAIR, OPEN)
    assert [x.id for x in both.kept] == ["t1"]  # with B's verdicts too, the same trajectory is kept


def test_set_certifies_only_if_every_member_passed_on_this_exact_trajectory():
    t = traj()
    vs = verdicts_for(t, SET_A, SET_B)
    partial = [v for v in vs if v.verifier_id != "paired-ci"]  # one A member silent
    assert not certified_by(t, SET_A, partial)
    failed = [v if v.verifier_id != "paired-ci" else Verdict(v.verifier_id, v.adapter_hash, v.trajectory_digest,
                                                             False, 0.0) for v in vs]
    assert not certified_by(t, SET_A, failed)
    assert not certified_by(t, SET_A, vs + failed)  # a failing verdict vetoes the passing one (no verifier shopping)
    other_code = [v if v.verifier_id != "paired-ci" else Verdict(v.verifier_id, H["7"], v.trajectory_digest, True,
                                                                 1.0) for v in vs]
    assert not certified_by(t, SET_A, other_code)  # a verdict by another adapter version does not count
    edited = traj(answer="x = 2  # edited after certification")
    assert edited.id == t.id and edited.digest() != t.digest()
    sel = select_for_training([edited], vs, PAIR, OPEN)  # the verdicts are for the unedited trajectory
    assert sel.kept == [] and set(sel.excluded["t1"]) == {Reason.NOT_CERTIFIED_A, Reason.NOT_CERTIFIED_B}


# ------------------------------------------------------------------ licence filter (invariant 11), fail closed
def test_output_whose_licence_flag_is_false_is_excluded():
    p = prov("acme", AccessMode.API_KEY, terms="allowed")
    t = traj(p=p)
    vs = verdicts_for(t, SET_A, SET_B)
    policy = LicencePolicy.from_mapping({"acme/api": False, "local/ow": True})
    assert not policy.permits(p)  # the owner flag for this provider is really false
    sel = select_for_training([t], vs, PAIR, policy)
    assert sel.kept == [] and sel.excluded == {"t1": [Reason.LICENCE]}


def test_licence_defaults_to_false_for_every_provider():
    assert C.PROVIDER_TRAINING_PERMITTED == {}  # nothing is permitted until the owner sets it
    policy = LicencePolicy.default()
    for p in (prov(), prov("openai", AccessMode.API_KEY), prov("anthropic", AccessMode.SUBSCRIPTION)):
        assert not policy.permits(p)
    t = traj()
    sel = select_for_training([t], verdicts_for(t, SET_A, SET_B), PAIR, policy)
    assert sel.excluded == {"t1": [Reason.LICENCE]}


@pytest.mark.parametrize("p,why", [
    (prov(terms="forbidden"), "the provider's terms forbid training even though the owner flag is true"),
    (prov(terms="unknown"), "unknown terms are not permission"),
    (prov(replayed=True), "a replayed (cassette) output is not a fresh model output"),
    (prov("mock", AccessMode.MOCK), "mock output is test scaffolding"),
])
def test_licence_filter_other_fail_closed_cases(p, why):
    policy = LicencePolicy.from_mapping({"local/ow": True, "mock/mock": True})
    assert not policy.permits(p), why


def test_assistant_step_without_provenance_is_excluded():
    t = Trajectory(id="t1", task_id="task", outcome="certified", steps=(Step("user", "q"), Step("assistant", "a")))
    sel = select_for_training([t], verdicts_for(t, SET_A, SET_B), PAIR, OPEN)
    assert sel.excluded == {"t1": [Reason.LICENCE]}


def test_licence_config_values_must_be_literal_booleans(tmp_path):
    for bad in ({"local/ow": "true"}, {"local/ow": 1}, {"local": True}, ["local/ow"]):
        with pytest.raises(LicenceConfigError):
            LicencePolicy.from_mapping(bad)
    f = tmp_path / "training_licence.json"
    f.write_text(json.dumps({"permitted": {"local/ow": True}}), encoding="utf-8")
    assert LicencePolicy.load(f).permits(prov())
    # keys match exactly: a differently-cased owner key grants nothing (folding could only widen permission)
    assert LicencePolicy.from_mapping({"local/ow": True}).permits(prov())
    assert not LicencePolicy.from_mapping({"LOCAL/ow": True}).permits(prov())
    assert not LicencePolicy.from_mapping({"local/ow": True}).permits(prov("Local"))
    with pytest.raises(LicenceConfigError):
        LicencePolicy.load(tmp_path / "missing.json")


# ------------------------------------------------------------------ SFT format filtered by outcome
def test_sft_rows_only_from_allowed_outcomes(tmp_path):
    ts = [traj("good"), traj("neg", outcome="negative"), traj("bad", outcome="rejected")]
    vs = [v for t in ts for v in verdicts_for(t, SET_A, SET_B)]
    res = export_trajectories(ts, vs, PAIR, OPEN, tmp_path / "out")
    assert C.SFT_OUTCOMES == frozenset({"certified"})
    assert [r["id"] for r in res.rows] == ["good"]
    assert res.excluded == {"neg": [Reason.OUTCOME], "bad": [Reason.OUTCOME]}
    row = res.rows[0]
    assert row["messages"] == [{"role": "system", "content": "You write code."},
                               {"role": "user", "content": "Solve the task."},
                               {"role": "assistant", "content": "x = 1"}, {"role": "tool", "content": "exit 0"}]
    assert row["outcome"] == "certified" and row["trajectory_digest"] == ts[0].digest()
    assert row["verifier_sets"] == {"A": SET_A.digest(), "B": SET_B.digest()}
    assert row["sources"] == [{"provider": "local", "mode": "ow", "model": "m1", "backend": "local-be"}]
    lines = (tmp_path / "out" / "sft.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["id"] for x in lines] == ["good"]
    man = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert man["kept"] == 1 and man["excluded"] == {"OUTCOME": 2}
    assert man["licence_policy"] == OPEN.digest() and man["pair"] == PAIR.digest()
    with pytest.raises(ExportRefused) as e:  # never overwrite an export
        export_trajectories(ts, vs, PAIR, OPEN, tmp_path / "out")
    assert e.value.code == "DEST_EXISTS"


def test_unknown_outcome_is_refused():
    with pytest.raises(ExportRefused) as e:
        traj(outcome="probably-fine")
    assert e.value.code == "BAD_TRAJECTORY"


# ------------------------------------------------------------------ coupled-Goodhart indicator
def test_goodhart_indicator_fires_on_a_planted_widening_gap():
    # A-score climbs, the sealed score stays flat: the harness is fitting verifier set A, not the task
    pts = [GapPoint(t, 0.60 + 0.03 * t, 0.58 + 0.002 * ((-1) ** t)) for t in range(8)]
    gaps = [p.a_score - p.sealed_score for p in pts]
    assert gaps[-1] - gaps[0] > 0.15  # the planted widening really is there
    rep = goodhart_indicator(pts)
    assert rep.status == "alarm" and rep.slope > C.GOODHART_SLOPE_ALARM


@pytest.mark.parametrize("name,pts", [
    ("both improve together", [GapPoint(t, 0.6 + 0.03 * t, 0.58 + 0.03 * t) for t in range(8)]),
    ("noisy constant gap", [GapPoint(t, 0.7 + 0.01 * ((-1) ** t), 0.65) for t in range(8)]),
    ("gap narrows", [GapPoint(t, 0.8, 0.6 + 0.02 * t) for t in range(8)]),
])
def test_goodhart_indicator_quiet_without_widening(name, pts):
    assert goodhart_indicator(pts).status == "ok", name


def test_goodhart_indicator_fails_closed_on_bad_input():
    assert goodhart_indicator([GapPoint(0, 0.5, 0.5), GapPoint(1, 0.9, 0.5)]).status == "insufficient_data"
    with pytest.raises(ValueError):
        goodhart_indicator([GapPoint(t, 0.5, math.nan if t == 2 else 0.5) for t in range(6)])
    with pytest.raises(ValueError):
        goodhart_indicator([GapPoint(t % 3, 0.5, 0.5) for t in range(6)])  # time does not increase


# ------------------------------------------------------------------ task selection by reward variance
def test_tasks_selected_by_reward_variance():
    rewards = {
        "solved": [1, 1, 1, 1, 1, 1],  # zero variance: nothing to learn
        "hopeless": [0, 0, 0, 0, 0, 0],  # zero variance
        "boundary": [1, 0, 1, 0, 1, 0],  # p = .5: maximal variance
        "mostly": [1, 1, 1, 1, 1, 0],
        "few": [1, 0],  # too few rollouts to estimate a variance
    }
    sel = select_by_reward_variance(rewards)
    assert [s.task_id for s in sel.selected] == ["boundary", "mostly"]
    assert sel.selected[0].variance == pytest.approx(0.25)
    assert sel.excluded == {"solved": "LOW_VARIANCE", "hopeless": "LOW_VARIANCE", "few": "TOO_FEW_ROLLOUTS"}
    assert [s.task_id for s in select_by_reward_variance(rewards, k=1).selected] == ["boundary"]
    with pytest.raises(ValueError):
        select_by_reward_variance({"x": [1, 0, math.inf, 0]})


def test_parameters_are_marked_uncalibrated():
    import inspect
    src = inspect.getsource(C)
    for name in ("SFT_OUTCOMES", "EXPORT_TIER_FLOOR", "GOODHART_WINDOW", "GOODHART_MIN_POINTS", "GOODHART_SLOPE_ALARM",
                 "VARIANCE_MIN_ROLLOUTS", "VARIANCE_MIN"):
        line = next(ln for ln in src.splitlines() if ln.startswith(f"{name} = "))
        assert "# UNCALIBRATED (owner decision, GOAL §13.8)" in line, name
