"""GOAL §5 L3 verifier synthesis, wired (s3-verifiers): agent-authored suites can never lift the T2 cap, and the
research loop's `research.synthesize_verifier` module writes, validates and tiers a verifier from a closed
vocabulary, with the tier recomputed (never trusted from state) and the sealed half never exposed."""

from __future__ import annotations

import json

import pytest

from custom_harness.budget import Budget
from custom_harness.gateway.mock import MockBackend
from custom_harness.kernel import ledger as ledger_mod
from custom_harness.kernel.ledger import Ledger
from custom_harness.runtime.modules import ModuleContext
from custom_harness.store import EvidenceStore
from custom_harness.tiers import Tier
from custom_harness.verify import synthesis as S

SORT_OK = [{"op": "sorted", "field": "out"}, {"op": "permutation_of", "field": "out", "of": "in"}]
SORT_TUNED = [{"op": "sorted", "field": "out"}, {"op": "sum_equals", "field": "out", "of": "in"}]


def spec(problem: str, kind: str, rules: list[dict]) -> str:
    return json.dumps({"format": S.SPEC_FORMAT, "problem": problem, "kind": kind, "rules": rules})


def backend(text: str, family: str = "fam-a", model: str = "scripted-1") -> MockBackend:
    b = MockBackend([text])
    b.family = family  # type: ignore[attr-defined]
    b.model = model  # type: ignore[attr-defined]
    return b


@pytest.fixture()
def store(tmp_path):
    assert "verifier_review" in ledger_mod.KINDS  # merged into the base (no stand-in needed)
    s = EvidenceStore(tmp_path / "store")
    yield s
    s.close()


def test_every_suite_in_code_is_agent_authored_never_the_owners():
    assert S.SUITES, "no suites: this test would check nothing"
    for name, suite in S.SUITES.items():
        assert suite.authorship == S.AGENT_AUTHORED, name
        assert suite.author.startswith("agent:"), name  # who wrote it: an agent, not the owner
        assert not hasattr(suite, "owner"), name  # the old field claimed the owner's name
        assert "owner (custom-harness)" not in json.dumps([suite.author, suite.description]), name
    # the author is part of the content hash: relabelling a suite (e.g. as the owner's) gives a new hash, so no
    # review of the agent-authored suite carries over to the relabelled one
    import dataclasses
    suite = S.SUITES["sort"]
    assert dataclasses.replace(suite, author="owner").hash() != suite.hash()


def test_an_agent_authored_suite_cannot_lift_the_cap(store):
    ledger = Ledger(store)
    v = S.synthesize(backend(spec("sort", "invariant", SORT_OK)), "sort")
    assert S.validate(v).passed
    assert S.suite_status("sort", ledger) == S.AGENT_AUTHORED
    S.record_review(ledger, v, reviewer="alice", approve=True, note="verifier reviewed")
    # the attack: a human-reviewed verifier, validated only against an agent-written suite, claims T1
    assert S.tier(v, ledger) == Tier.T2_STATISTICAL
    # an agent cannot approve the suite (nor the verifier) under an agent identity
    for who in ("agent:g-synth", "Agent-7", S.SUITES["sort"].author):
        with pytest.raises(S.SynthesisError, match="agent identity"):
            S.record_suite_review(ledger, "sort", reviewer=who, approve=True)
        with pytest.raises(S.SynthesisError, match="agent identity"):
            S.record_review(ledger, v, reviewer=who, approve=True)
    assert S.tier(v, ledger) == Tier.T2_STATISTICAL
    # a review of a DIFFERENT suite's hash does not count
    S.record_suite_review(ledger, "decay", reviewer="alice", approve=True)
    assert S.tier(v, ledger) == Tier.T2_STATISTICAL
    # only a ledgered human approval of this exact suite lifts it (together with the verifier review)
    S.record_suite_review(ledger, "sort", reviewer="alice", approve=True, note="read every case")
    assert S.suite_status("sort", ledger) == "human-approved" and S.tier(v, ledger) == Tier.T1_EXECUTABLE
    # suite approved but this verifier not reviewed: still capped
    v2 = S.synthesize(backend(spec("sort", "invariant", list(reversed(SORT_OK)))), "sort")
    assert S.tier(v2, ledger) == Tier.T2_STATISTICAL
    # a human rejection of the suite makes every verifier validated on it unusable
    S.record_suite_review(ledger, "sort", reviewer="bob", approve=False, note="faults too easy")
    assert S.suite_status("sort", ledger) == "human-rejected"
    assert S.tier(v, ledger) is None and S.tier(v2, ledger) is None
    assert ledger.verify() == []


def _ctx(store, text: str, family: str = "fam-a") -> ModuleContext:
    return ModuleContext(run_id="r-synth", store=store, backends={"verifier_author": backend(text, family)},
                         budget=Budget(), sandbox=None)


def test_research_module_writes_validates_and_caps_a_verifier(store):
    from custom_harness.research import verifier_synthesis as VS
    from custom_harness.runtime.modules import _REGISTRY
    assert _REGISTRY["research.synthesize_verifier"] is VS.synthesize_verifier  # registered with the loop
    ctx = _ctx(store, spec("sort", "invariant", SORT_OK))
    out = VS.synthesize_verifier({}, {"problem": "sort"}, ctx)["verifier_synthesis"]
    assert out["error"] is None and out["validated"] is True and out["tier"] == "T2_STATISTICAL", out
    assert out["suite_authorship"] == S.AGENT_AUTHORED
    assert len(ctx.llm_calls) == 1  # the call went through ModuleContext.llm (budgeted, traced)
    # the stored verifier round-trips through the vocabulary; the tier is recomputed from the ledger
    assert VS.usable_tier(ctx, out["ref"]) == Tier.T2_STATISTICAL
    # a forged blob (hash mismatch) is unusable, whatever state says
    rec = store.blobs.get_json(out["ref"])
    forged = store.blobs.put_json({**rec, "spec": spec("sort", "invariant", SORT_TUNED)})
    assert VS.usable_tier(ctx, forged) is None
    assert VS.usable_tier(ctx, store.blobs.put_json({"tier": "T1_EXECUTABLE"})) is None


def test_research_module_never_exposes_the_sealed_half(store):
    from custom_harness.research import verifier_synthesis as VS
    ctx = _ctx(store, spec("sort", "invariant", SORT_TUNED))
    out = VS.synthesize_verifier({}, {"problem": "sort"}, ctx)["verifier_synthesis"]
    assert out["validated"] is False and out["tier"] is None
    assert VS.usable_tier(ctx, out["ref"]) is None
    text = json.dumps(out)
    suite = S.SUITES["sort"]
    for case in (*suite.sealed_golden, *suite.sealed_faults, *suite.public_faults):
        assert case.id not in text  # which planted fault was missed is never shown to the loop
    # what the author model saw: description and public goldens only
    prompt = "\n".join(m["content"] for m in ctx.llm_calls[0]["request"])
    assert json.dumps(suite.public_golden[0].subject, sort_keys=True) in prompt  # the check below is not vacuous
    for case in (*suite.sealed_golden, *suite.sealed_faults, *suite.public_faults):
        assert json.dumps(case.subject, sort_keys=True) not in prompt


def test_research_module_refuses_out_of_vocabulary_specs(store):
    from custom_harness.research import verifier_synthesis as VS
    bad = spec("sort", "invariant", [{"op": "exec", "code": "import os"}])
    out = VS.synthesize_verifier({}, {"problem": "sort"}, _ctx(store, bad))["verifier_synthesis"]
    assert out["ref"] is None and out["tier"] is None and "refused" in out["error"]
