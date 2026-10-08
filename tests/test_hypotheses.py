"""L4.4 hypothesis diversity (GOAL §5 L4 stage 4; research/hypotheses.py): error correlation of out-of-fold dev
predictions (not vendor count), declared topic quotas, and distance-3 inspiration as a prompt-side option only."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from custom_harness.research.hypotheses import (
    DEFAULT_TAXONOMY,
    HypothesisRun,
    TopicQuota,
    apply_topic_quota,
    diversity_report,
    domain_distance,
    error_correlation,
    error_vector,
    inspiration,
)


def _labels(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [rng.choice("abc") for _ in range(n)]


def _with_errors(labels: list[str], wrong: set[int]) -> tuple[str, ...]:
    return tuple(("b" if y == "a" else "a") if i in wrong else y for i, y in enumerate(labels))


def test_near_duplicate_hypotheses_are_flagged_and_deduplicated_whatever_the_vendor():
    n = 300
    labels = _labels(n, 0)
    rng = random.Random(1)
    base = set(rng.sample(range(n), 60))
    # near-duplicate of h1: the same errors except 4 rows swapped (error correlation ~0.9), from ANOTHER vendor
    swap_out = set(sorted(base)[:4])
    swap_in = set(rng.sample(sorted(set(range(n)) - base), 4))
    near = (base - swap_out) | swap_in
    other = set(rng.sample(range(n), 60))  # an independent behaviour from the SAME vendor as h1
    runs = [HypothesisRun(id="h1", preds=_with_errors(labels, base), vendor="vendorA"),
            HypothesisRun(id="h2", preds=_with_errors(labels, near), vendor="vendorB"),
            HypothesisRun(id="h3", preds=_with_errors(labels, other), vendor="vendorA")]
    r12 = error_correlation(error_vector("accuracy", labels, runs[0].preds),
                            error_vector("accuracy", labels, runs[1].preds))
    assert r12 is not None and r12 > 0.8  # the planted pair really is a near-duplicate by error correlation
    rep = diversity_report("accuracy", labels, runs)
    assert [(a, b) for a, b, _ in rep.flagged] == [("h1", "h2")]
    assert rep.kept == ("h1", "h3")
    assert [(d.id, d.duplicate_of) for d in rep.dropped] == [("h2", "h1")] and rep.dropped[0].r == pytest.approx(r12)
    # vendor count would call h1/h2 diverse (2 vendors) and h1/h3 not; the measure says the opposite
    assert rep.vendors == 2 and rep.r["h1"]["h3"] is not None and rep.r["h1"]["h3"] < 0.8
    assert rep.measure.startswith("error correlation")
    assert rep.effective_n is not None and 1 <= rep.effective_n <= 3


def test_regression_errors_use_signed_residuals_and_order_decides_which_duplicate_is_kept():
    rng = random.Random(4)
    y = [rng.gauss(0, 1) for _ in range(200)]
    noise = [rng.gauss(0, 1) for _ in range(200)]
    labels = [f"{v:.6f}" for v in y]
    a = tuple(f"{v + e:.6f}" for v, e in zip(y, noise, strict=True))
    b = tuple(f"{v + 1.02 * e + 0.05 * rng.gauss(0, 1):.6f}" for v, e in zip(y, noise, strict=True))
    c = tuple(f"{v + rng.gauss(0, 1):.6f}" for v in y)
    rep = diversity_report("rmse", labels, [HypothesisRun(id="b", preds=b), HypothesisRun(id="a", preds=a),
                                            HypothesisRun(id="c", preds=c)])
    assert rep.kept == ("b", "c") and rep.dropped[0].id == "a" and rep.dropped[0].duplicate_of == "b"


def test_constant_error_vectors_are_reported_undefined_not_silently_merged():
    labels = _labels(50, 2)
    perfect = HypothesisRun(id="p", preds=tuple(labels))
    perfect2 = HypothesisRun(id="p2", preds=tuple(labels))
    some = HypothesisRun(id="s", preds=_with_errors(labels, {1, 5, 9}))
    rep = diversity_report("accuracy", labels, [perfect, some, perfect2])
    assert ("p", "s") in rep.undefined and rep.kept == ("p", "s")  # identical vectors are duplicates (r = 1)
    assert [(d.id, d.duplicate_of) for d in rep.dropped] == [("p2", "p")]
    with pytest.raises(ValueError):
        diversity_report("accuracy", labels, [perfect, perfect])  # ids must be unique


def test_topic_quota_caps_each_declared_topic_and_drops_undeclared_topics():
    quota = TopicQuota(topics=("features", "models", "regularisation"), cap=2, caps={"models": 1})
    hyps = [{"id": f"h{i}", "topic": t} for i, t in enumerate(
        ["features", "Features ", "features", "models", "models", "regularisation", "data leakage", None])]
    kept, dropped = apply_topic_quota(hyps, quota)
    assert [h["id"] for h in kept] == ["h0", "h1", "h3", "h5"]
    assert {d["id"]: d["reason"] for d in dropped} == {
        "h2": "over the topic cap (2)", "h4": "over the topic cap (1)", "h6": "undeclared topic",
        "h7": "undeclared topic"}
    with pytest.raises(ValueError):
        apply_topic_quota(hyps, TopicQuota(topics=("features",), caps={"models": 1}))


def test_distance_3_inspiration_is_a_far_domain_prompt_option_and_never_evidence():
    assert domain_distance("tabular learning", "tabular learning") == 0
    assert domain_distance("tabular learning", "ensemble methods") == 1
    assert domain_distance("tabular learning", "streaming algorithms") == 2
    assert domain_distance("tabular learning", "ecology") == 3
    picks = {inspiration("tabular learning", seed=s).source_area for s in range(40)}
    assert len(picks) > 3
    for s in range(40):
        ins = inspiration("tabular learning", seed=s)
        assert ins.distance == 3 and domain_distance("tabular learning", ins.source_area) == 3
        assert ins.is_evidence is False and "NOT evidence" in ins.prompt
    assert inspiration("tabular learning", seed=7) == inspiration("tabular learning", seed=7)  # deterministic
    with pytest.raises(ValidationError):  # it cannot be marked as evidence
        type(ins).model_validate({**ins.model_dump(), "is_evidence": True})
    with pytest.raises(ValueError):
        inspiration("not an area", seed=0)
    # inspiration does not reach judging: dedup and quotas ignore it
    labels = _labels(40, 3)
    run = HypothesisRun(id="h", preds=tuple(labels))
    assert diversity_report("accuracy", labels, [run]).kept == ("h",)
    kept, _ = apply_topic_quota([{"id": "h", "topic": "models", "inspired_by": ins.source_area}],
                                TopicQuota(topics=("models",)))
    assert kept[0]["id"] == "h"
    assert all(len(ds) >= 1 for ds in DEFAULT_TAXONOMY.values())
