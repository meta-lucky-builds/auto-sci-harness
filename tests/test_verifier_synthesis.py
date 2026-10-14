"""GOAL §5 L3 verifier synthesis: scripted authors, fault-injection validation against a fixed human-owned suite,
T2 cap until a ledgered human review, and no self-certification without an independent second verifier.

Attack tests assert that the attack HAPPENED (the bad verifier really accepts what it should not, on the cases the
author could see) before asserting that validation stops it."""

from __future__ import annotations

import json

import pytest

from custom_harness.gateway.mock import MockBackend
from custom_harness.kernel import ledger as ledger_mod
from custom_harness.kernel.ledger import Ledger
from custom_harness.store import EvidenceStore
from custom_harness.tiers import Tier
from custom_harness.verify import synthesis as S

SORT_OK = [{"op": "sorted", "field": "out"}, {"op": "permutation_of", "field": "out", "of": "in"}]
# tuned to the golden cases: every golden case passes, but "same sum" is weaker than "same multiset"
SORT_TUNED = [{"op": "sorted", "field": "out"}, {"op": "sum_equals", "field": "out", "of": "in"}]
DECAY_A = [["1/2", "0"], ["1/4", "1/2"]]
DECAY_OK = [{"op": "simulate_linear", "A": DECAY_A, "x0": "x0", "trajectory": "traj", "tolerance": "1/1000000"}]


def spec(problem: str, kind: str, rules: list[dict]) -> str:
    return json.dumps({"format": S.SPEC_FORMAT, "problem": problem, "kind": kind, "rules": rules})


def backend(text: str, family: str = "fam-a", model: str = "scripted-1") -> MockBackend:
    b = MockBackend([text])
    b.family = family  # type: ignore[attr-defined]  # read by policy.model_family
    b.model = model  # type: ignore[attr-defined]
    return b


def make(problem: str, kind: str, rules: list[dict], family: str = "fam-a", model: str = "scripted-1"):
    return S.synthesize(backend(spec(problem, kind, rules), family, model), problem)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    if "verifier_review" not in ledger_mod.KINDS:
        # TEST STAND-IN until kstats' commit 6701279 (adds the kind to kernel/ledger.py) is merged into this base
        monkeypatch.setattr(ledger_mod, "KINDS", ledger_mod.KINDS | {"verifier_review"})
    store = EvidenceStore(tmp_path / "store")
    yield Ledger(store)
    store.close()


def test_scripted_author_sees_only_the_public_half_and_its_verifier_validates():
    b = backend(spec("sort", "invariant", SORT_OK))
    v = S.synthesize(b, "sort")
    assert v.author.startswith("fam-a/") and v.family == "fam-a" and v.kind == "invariant"
    prompt = " ".join(m.content for m in b.requests[0].messages)
    suite = S.SUITES["sort"]
    for case in suite.public_golden:
        assert json.dumps(case.subject, sort_keys=True) in prompt
    for case in (*suite.sealed_golden, *suite.public_faults, *suite.sealed_faults):  # never shown to the author
        assert case.id not in prompt and json.dumps(case.subject, sort_keys=True) not in prompt
    val = S.validate(v)
    assert val.passed, val
    assert val.golden_rejected == () and val.faults_missed == ()
    assert val.suite_hash == suite.hash() and val.verifier_hash == v.hash()


@pytest.mark.parametrize("rules", [[], [{"op": "range", "field": "out", "lo": "-1000000000", "hi": "1000000000"}]])
def test_vacuous_verifier_is_rejected(rules):
    v = make("sort", "property_test", rules)
    suite = S.SUITES["sort"]
    faults = (*suite.public_faults, *suite.sealed_faults)
    assert all(S.run_rules(v, f.subject) == [] for f in faults)  # the attack: it accepts every planted fault
    val = S.validate(v)
    assert not val.passed and set(val.faults_missed) == {f.id for f in faults}
    assert S.tier(v, None) is None  # unusable


def test_golden_tuned_verifier_that_misses_a_planted_fault_is_rejected():
    v = make("sort", "invariant", SORT_TUNED)
    suite = S.SUITES["sort"]
    assert all(S.run_rules(v, g.subject) == [] for g in (*suite.public_golden, *suite.sealed_golden))  # tuned
    val = S.validate(v)
    assert not val.passed and val.golden_rejected == () and val.faults_missed == ("sum-preserving-change",)
    assert S.tier(v, None) is None


def test_verifier_memorising_the_public_goldens_is_rejected_by_the_sealed_half():
    suite = S.SUITES["sort"]
    memo = [{"op": "one_of", "field": "out", "values": [g.subject["out"] for g in suite.public_golden]}]
    v = make("sort", "property_test", memo)
    # the attack: perfect on everything the author could see (public goldens accepted, public faults rejected)
    assert all(S.run_rules(v, g.subject) == [] for g in suite.public_golden)
    assert all(S.run_rules(v, f.subject) != [] for f in suite.public_faults)
    val = S.validate(v)
    assert not val.passed and set(val.golden_rejected) == {g.id for g in suite.sealed_golden}


def test_simulator_verifier_validates_and_a_loose_or_wrong_simulator_is_rejected():
    assert S.validate(make("decay", "simulator", DECAY_OK)).passed
    loose = [{**DECAY_OK[0], "tolerance": "1"}]
    v = make("decay", "simulator", loose)
    drift = next(f for f in S.SUITES["decay"].sealed_faults if f.id == "late-drift")
    assert S.run_rules(v, drift.subject) == []  # the attack: a loose tolerance accepts the planted drift
    assert "late-drift" in S.validate(v).faults_missed
    wrong = [{**DECAY_OK[0], "A": [["1/2", "0"], ["0", "1/2"]]}]  # ignores the coupling term
    assert S.validate(make("decay", "simulator", wrong)).golden_rejected != ()


def test_unreviewed_verifier_is_capped_at_t2_and_the_human_review_is_ledgered(ledger):
    v = make("sort", "invariant", SORT_OK)
    assert S.tier(v, ledger) == Tier.T2_STATISTICAL  # validated, not reviewed: capped
    with pytest.raises(S.SynthesisError, match="author"):
        S.record_review(ledger, v, reviewer=v.author, approve=True)
    with pytest.raises(S.SynthesisError, match="human reviewer"):
        S.record_review(ledger, v, reviewer="  ", approve=True)
    bad = make("sort", "invariant", SORT_TUNED)
    with pytest.raises(S.SynthesisError, match="validation"):
        S.record_review(ledger, bad, reviewer="alice", approve=True)
    assert ledger.entries("verifier_review") == []
    e = S.record_review(ledger, v, reviewer="alice", approve=True, note="read the rules")
    (stored,) = ledger.entries("verifier_review")
    assert stored.hash == e.hash and stored.body["verifier_hash"] == v.hash() and stored.body["reviewer"] == "alice"
    assert stored.body["suite_hash"] == S.SUITES["sort"].hash() and stored.body["approve"] is True
    assert ledger.verify() == []
    # s3-verifiers (tightened): the suite is AGENT-AUTHORED, so a verifier review alone does not lift the cap; a
    # ledgered human approval of the suite is also needed
    assert S.tier(v, ledger) == Tier.T2_STATISTICAL
    S.record_suite_review(ledger, "sort", reviewer="alice", approve=True, note="read every case")
    assert S.tier(v, ledger) == Tier.T1_EXECUTABLE
    # any content change voids the review: same meaning, different bytes -> capped again
    edited = make("sort", "invariant", list(reversed(SORT_OK)))
    assert edited.hash() != v.hash() and S.validate(edited).passed
    assert S.tier(edited, ledger) == Tier.T2_STATISTICAL
    # a human rejection makes the verifier unusable
    S.record_review(ledger, edited, reviewer="bob", approve=False, note="too weak")
    assert S.tier(edited, ledger) is None


def test_a_verifier_never_certifies_its_own_authors_claim_alone(ledger):
    v = make("sort", "invariant", SORT_OK, family="fam-a", model="m1")
    good = S.SUITES["sort"].public_golden[0].subject
    own = S.Author(id=v.author, family=v.family)
    other = S.Author(id="fam-z/other", family="fam-z")
    # the attack: the verifier accepts its author's (correct) output, and alone that would be enough
    assert S.run_rules(v, good) == []
    alone = S.verdict("sort", good, own, [v], ledger)
    assert not alone.accepted and any("independent" in r for r in alone.reasons)
    # same family, different model: not independent
    sibling = make("sort", "invariant", list(reversed(SORT_OK)), family="fam-a", model="m2")
    assert not S.verdict("sort", good, own, [v, sibling], ledger).accepted
    # a claim author sharing the verifier's family (different model) is also conflicted
    cousin = S.Author(id="fam-a/m9", family="fam-a")
    assert not S.verdict("sort", good, cousin, [v], ledger).accepted
    # an independent, validated second verifier from another family lifts the block; tier = weakest link (T2)
    indep = make("sort", "invariant", SORT_OK + [{"op": "range", "field": "out", "lo": "-100", "hi": "100"}],
                 family="fam-b", model="m3")
    ok = S.verdict("sort", good, own, [v, indep], ledger)
    assert ok.accepted and ok.tier == Tier.T2_STATISTICAL, ok.reasons
    # a human-owned check is an independent second verifier too
    human = S.HumanVerifier(name="sort-check", owner="alice", tier=Tier.T1_EXECUTABLE,
                            check=lambda s: [] if s["out"] == sorted(s["in"]) else ["not sorted"])
    assert S.verdict("sort", good, own, [v, human], ledger).accepted
    # but only if it agrees
    nay = S.HumanVerifier(name="nay", owner="alice", tier=Tier.T1_EXECUTABLE, check=lambda s: ["no"])
    assert not S.verdict("sort", good, own, [v, nay], ledger).accepted
    # someone else's claim: a single validated verifier is enough
    single = S.verdict("sort", good, other, [v], ledger)
    assert single.accepted and single.tier == Tier.T2_STATISTICAL


def test_an_unvalidated_verifier_cannot_take_part_in_a_verdict(ledger):
    tuned = make("sort", "invariant", SORT_TUNED, family="fam-b")
    fault = next(f for f in S.SUITES["sort"].sealed_faults if f.id == "sum-preserving-change").subject
    assert S.run_rules(tuned, fault) == []  # it would accept this wrong output
    res = S.verdict("sort", fault, S.Author(id="fam-z/x", family="fam-z"), [tuned], ledger)
    assert not res.accepted and res.tier is None and any("validation" in r for r in res.reasons)
    other_problem = make("decay", "simulator", DECAY_OK, family="fam-b")
    res = S.verdict("sort", fault, S.Author(id="fam-z/x", family="fam-z"), [other_problem], ledger)
    assert not res.accepted and any("problem" in r for r in res.reasons)
    assert not S.verdict("sort", fault, S.Author(id="fam-z/x", family="fam-z"), [], ledger).accepted


def test_suite_below_the_minimum_size_or_unknown_problem_cannot_validate(monkeypatch):
    v = make("sort", "invariant", SORT_OK)
    small = S.SUITES["sort"]
    monkeypatch.setitem(S.SUITES, "sort", S.FaultSuite(
        problem="sort", description=small.description, author=small.author, public_golden=small.public_golden,
        sealed_golden=small.sealed_golden, public_faults=small.public_faults, sealed_faults=small.sealed_faults[:1]))
    val = S.validate(v)
    assert not val.passed and any("planted faults" in e for e in val.errors)
    assert not S.validate(make("nosuch", "invariant", SORT_OK)).passed


@pytest.mark.parametrize("text, why", [
    ("not json", "JSON"),
    (json.dumps({"format": "other", "problem": "sort", "kind": "invariant", "rules": []}), "format"),
    (spec("sort", "oracle", SORT_OK), "kind"),
    (spec("sort", "invariant", [{"op": "exec", "code": "import os"}]), "op"),
    (spec("sort", "invariant", [{"op": "sorted"}]), "field"),
    (spec("sort", "invariant", [{"op": "range", "field": "out", "lo": 0.1, "hi": "1"}]), "rational"),
    (spec("decay", "invariant", DECAY_OK), "problem"),
])
def test_malformed_or_out_of_vocabulary_specs_are_refused(text, why):
    with pytest.raises(S.SynthesisError, match=why):
        S.synthesize(backend(text), "sort")


def test_a_crashing_or_unreadable_subject_is_a_rejection_not_an_acceptance():
    v = make("sort", "invariant", SORT_OK)
    assert S.run_rules(v, {"in": [1, 2]}) != []  # missing field
    assert S.run_rules(v, {"in": [1, 2], "out": "12"}) != []  # wrong type
    assert S.run_rules(v, {"in": [1, 2], "out": [1, float("nan")]}) != []
