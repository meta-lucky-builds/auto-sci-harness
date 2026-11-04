"""Three routes to a false VALIDATED, each closed in the Court (the Kernel is untouched): a comparator sabotaged on the
real test only, a family retried until something passes, and public data put through the sealed route."""

from __future__ import annotations

from datetime import date

import pytest

from custom_harness.audit.spec import Budget, ClaimPart
from custom_harness.court import comparator as K
from custom_harness.court import display as D
from custom_harness.court import health as H
from custom_harness.court import levels as L
from custom_harness.court import minted as C
from custom_harness.court import sealed as S
from custom_harness.experience import words as W
from custom_harness.kernel.vault import DatasetRole
from custom_harness.kerneld.scope import ScopedKernel
from test_court_rehearsal_health import _factory
from test_sealed_project import BEACON, Project

_SWITCH = b'''
class Switch:
    def fit(self, X, y):
        self.top = max(sorted(set(y)), key=y.count)
    def predict(self, X):
        return [self.top if len(X) < 500 else "never-a-label"] * len(X)
def build_model(seed):
    return Switch()
'''


# ------------------------------------------------------------------------- a comparator sabotaged on the real test
@pytest.mark.parametrize("metric,held_out,public,trivial,against_public,collapsed", [
    ("accuracy", 0.60, 0.60, 0.40, True, False),
    ("accuracy", 0.46, 0.60, 0.40, True, False),  # inside the allowed drop
    ("accuracy", 0.44, 0.60, 0.40, True, True),  # beyond it
    ("accuracy", 0.44, 0.60, 0.40, False, False),  # future data may honestly differ from the public file
    ("accuracy", 0.20, 0.60, 0.40, False, True),  # below the trivial answer on any route
    ("accuracy", 0.20, None, 0.40, True, True),  # it would not run publicly: the trivial bound still holds
    ("accuracy", 0.20, None, None, True, False),  # nothing to hold it against
    ("rmse", 1.00, 1.00, 2.00, True, False),
    ("rmse", 1.29, 1.00, 2.00, True, False),
    ("rmse", 1.31, 1.00, 2.00, True, True),  # error metrics: a relative rise
    ("rmse", 2.20, None, 2.00, True, True),
])
def test_held_out_problem(metric, held_out, public, trivial, against_public, collapsed):
    got = K.held_out_problem(metric, held_out, public, trivial, against_public=against_public)
    assert got == (K.COLLAPSED if collapsed else None)


def test_a_reference_with_no_labelled_rows_is_not_measured():
    assert K.comparator_reference(b"", "accuracy", b"x,label\n", None, 0, 1.0) == (None, None)


def test_a_reference_with_labels_that_cannot_be_scored_is_not_measured(monkeypatch):
    monkeypatch.setattr(K, "run_program", lambda *a, **k: (None, None))
    train = "x,label\n" + "".join(f"{i},not-a-number\n" for i in range(12))
    assert K.comparator_reference(b"", "rmse", train.encode(), None, 0, 1.0) == (None, None)


def test_the_reason_carries_no_number():
    assert not any(c.isdigit() for c in K.COLLAPSED)  # it reaches the viewer: it must not hand back a held-out score


def test_a_comparator_sabotaged_on_the_real_test_loses_its_verdict_and_validated(tmp_path):
    p = Project(tmp_path / "p", parts=1, rows=700)  # public split about 300 rows, the real test 700
    try:
        p.session()
        body = C.register_sealed(p.store, p.freeze(baseline=_SWITCH), p.project, anchor_file=p.anchor())
        out = p.run(body)
        assert "strawman" not in str(out.get("reason"))  # it got past the public check, which is the whole trick
        assert out["label"].startswith("CERTIFIED")  # the Kernel saw a large effect and said so
        assert out["comparator_held_out"] == K.COLLAPSED and out["verdict"] == "NOT CONFIRMED"
        res = L.level(p.store, out["cert_hash"])
        assert not res.held["VALIDATED"] and K.COLLAPSED in res.missing["VALIDATED"]
        v = D.view(p.store, body["case_id"], "researcher")
        assert v["verdict"] == "NOT CERTIFIED" and v["reason_class"] == D.COLLAPSED_CLASS
    finally:
        p.close()


def test_an_honest_comparator_is_not_flagged(tmp_path):
    p = Project(tmp_path / "p", parts=1, rows=700)
    try:
        p.session()
        body = C.register_sealed(p.store, p.freeze(), p.project, anchor_file=p.anchor())
        out = p.run(body)
        assert out["verdict"] == "CERTIFIED" and out["comparator_held_out"] is None
        assert L.level(p.store, out["cert_hash"]).held["VALIDATED"]
        assert "verdict" not in D.view(p.store, body["case_id"], "researcher")
    finally:
        p.close()


def test_every_released_reason_has_words():
    assert set(D.REASON_RELEASE) <= set(W.REASONS)
    assert D.REASON_RELEASE[-1] == D.COLLAPSED_CLASS  # the index is in the ledger: classes are added at the end only


def test_a_plant_stopped_for_the_wrong_reason_does_not_count():
    plant = next(p for p in H.PLANTS if p.name == "size_switched_baseline")
    assert H._correct(plant, {"cert_hash": "c", "comparator_held_out": K.COLLAPSED}, False)
    assert not H._correct(plant, {"cert_hash": "c", "comparator_held_out": None}, False)  # a plain rejection
    assert not H._correct(plant, {"reason": "infrastructure: docker went away"}, False)


# ------------------------------------------------------------------------- a family retried until something passes
def _register(store, root, n: int, *, family: str = "rings", alpha: float = 0.05, role: str = "promotion",
              of_case: str | None = None) -> dict:
    claim = ClaimPart(metric="accuracy", min_effect=0.1, probability=0.6, seeds=(0, 1, 2), n_boot=1000, alpha=alpha,
                      resolve_by=date(2030, 1, 1))
    return C.register_case(store, role=role, promotion_id=f"try-{n}", session_id="s", title="t", sentence="a sentence",
                           part=claim, budget=Budget(timeout_s=60), method=f"# method {n}\n".encode() + H._KNN.encode(),
                           baseline=H._MAJORITY.encode(), lock=b"x==1\n", family=family, beacon_round=f"r{n}",
                           sizing={"n_test": 800}, n_train=400, anchor_file=root / "anchors" / "a.jsonl",
                           of_case=of_case)


def _claim_alpha(store, body) -> float:
    claim = next(e.body["claim"] for e in ScopedKernel(store).ledger.entries("prereg")
                 if e.body.get("claim_hash") == body["claim_hash"])
    return claim["analysis_plan"]["alpha"]


@pytest.fixture
def world(tmp_path):
    p = Project(tmp_path / "p", parts=1)
    yield p.store, tmp_path
    p.close()


def test_each_try_on_a_family_spends_less(world):
    store, root = world
    bodies = [_register(store, root, n) for n in range(4)]
    assert [b["attempt"] for b in bodies] == [1, 2, 3, 4]
    assert [_claim_alpha(store, b) for b in bodies] == [0.05, 0.05 / 4, 0.05 / 9, 0.05 / 16]
    assert sum(0.05 / k ** 2 for k in range(1, 100_000)) < 0.05 * 1.65  # the bound the docstring states


def test_another_family_starts_again(world):
    store, root = world
    _register(store, root, 0)
    assert _register(store, root, 1, family="noise")["attempt"] == 1


def test_a_run_that_never_read_a_label_is_no_try(world):
    store, root = world
    body = _register(store, root, 0)
    store.append_record(C.RECORD_KIND, {"case_id": body["case_id"], "event": "outcome", "labels_read": False,
                                        "verdict": "COULD NOT RUN"})
    assert C.attempt_on_family(store, "rings") == 1
    store.append_record(C.RECORD_KIND, {"case_id": body["case_id"], "event": "outcome", "labels_read": True,
                                        "verdict": "COULD NOT RUN"})
    assert C.attempt_on_family(store, "rings") == 2


def test_a_declared_level_above_the_cap_is_capped_and_recorded(world):
    store, root = world
    body = _register(store, root, 0, alpha=0.2)  # the Kernel alone would allow this
    assert _claim_alpha(store, body) == 0.05 and body["alpha"] == 0.05 and body["alpha_declared"] == 0.2


def test_a_lower_declared_level_is_kept(world):
    store, root = world
    assert _claim_alpha(store, _register(store, root, 0, alpha=0.01)) == 0.01


def test_a_second_test_keeps_the_level_of_the_case_it_follows(world):
    store, root = world
    _register(store, root, 0)
    first = _register(store, root, 1)
    second = _register(store, root, 2, role="revalidation", of_case=first["case_id"])
    assert second["attempt"] is None and _claim_alpha(store, second) == _claim_alpha(store, first) == 0.05 / 4
    assert C.attempt_on_family(store, "rings") == 3  # and it is no try of its own


# ------------------------------------------------------------------------- public data put through the sealed route
def test_a_seal_must_say_where_the_data_comes_from(tmp_path):
    p = Project(tmp_path / "p", parts=1)
    try:
        for bad in ("", "unknown", "PRIVATE"):
            with pytest.raises(S.SealRefused, match="provenance"):
                S.seal_project(p.store, "other-one", p.data, label_column="outcome", rows=100, parts=1,
                               beacon_round="r2", beacon_value=BEACON, provenance=bad)
        assert S.sealed_record(p.store, "other-one") is None
    finally:
        p.close()


def test_classify_target_caps_public_data():
    assert L.classify_target("sealed_split", False, False, DatasetRole.EXTERNAL, True, True) == "retrospective"
    assert L.classify_target("sealed_split", False, False, DatasetRole.EXTERNAL, True, False) == "sealed-at-start"


def _sealed_run(tmp_path, provenance: str | None):
    p = Project(tmp_path / "p", parts=1)
    if provenance != "private":  # the fixture seals as private; rewrite what the record states
        rows = p.store.records(S.RECORD_KIND)
        assert rows[0]["provenance"] == "private"
        original = p.store.records

        def rewritten(kind, *a, **k):
            out = original(kind, *a, **k)
            if kind != S.RECORD_KIND:
                return out
            return [{**r, "provenance": provenance} if r.get("event") == "sealed" else r for r in out]
        p.store.records = rewritten  # type: ignore[method-assign]
    p.session()
    body = C.register_sealed(p.store, p.freeze(), p.project, anchor_file=p.anchor())
    return p, p.run(body)


def test_private_data_can_be_validated_and_says_what_it_rests_on(tmp_path):
    p, out = _sealed_run(tmp_path, "private")
    try:
        res = L.level(p.store, out["cert_hash"])
        assert res.held["VALIDATED"] and not res.retrospective
        assert any("as the researcher stated" in a for a in res.facts["untested_assumptions"])
    finally:
        p.close()


@pytest.mark.parametrize("provenance", ["public", None])  # None: a seal from before the statement existed
def test_public_or_unstated_data_is_capped(tmp_path, provenance):
    p, out = _sealed_run(tmp_path, provenance)
    try:
        res = L.level(p.store, out["cert_hash"])
        assert out["label"].startswith("CERTIFIED")  # the Kernel is not what stops this
        assert res.retrospective and not res.held["VALIDATED"] and "RETROSPECTIVE" in res.badge
    finally:
        p.close()


# ------------------------------------------------------------------------- the same three as health-run plants
@pytest.mark.parametrize("name", ["size_switched_baseline", "retried_claim", "sealed_public_data"])
def test_the_plant_gets_its_right_answer(tmp_path, name):
    plant = next(p for p in H.PLANTS if p.name == name)
    r = H.run_plant(plant, tmp_path, _factory)
    assert r["correct"], r
    assert not r["certified"]
