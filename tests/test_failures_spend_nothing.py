"""GOAL 18.6 "failures never spend truth" (UPGRADES N2): a crash before the first label read keeps the target and its
statistical budget, and is shown as COULD NOT RUN with its reason. After a label read the look is spent, and that is
shown too. `labels_read` is a fact read from the ledger, never an assumption made at the failure site.

Every route that can spend is covered: the minted Court route, the Claim Audit route and the research route's shared
prediction check. Each test pins a case found by tracing the routes (docs/court_failures.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from _court_world import BEACON, Case, part
from custom_harness.audit import claim_audit as CA
from custom_harness.audit import gate
from custom_harness.court import minted as C
from custom_harness.kernel import Kernel
from custom_harness.kerneld import client as KC
from custom_harness.kerneld import ingest as I
from custom_harness.kerneld.client import KernelDError, KernelDUnavailable, RunHandle
from custom_harness.verify import metrics as M
from test_claim_audit import Audit


def _unavailable(*a, **k):
    raise KernelDUnavailable()


def _refused(*a, **k):
    raise KernelDError("internal")


def _once_then_gone():
    real, calls = RunHandle.held_out_score, []

    def once(self, *a, **k):
        if calls:
            raise KernelDUnavailable()
        calls.append(1)
        return real(self, *a, **k)
    return once


def _broken_sandbox(key):
    raise OSError("disk full")


@pytest.fixture
def case(tmp_path):
    c = Case(tmp_path / "c")
    yield c
    c.close()


@pytest.fixture
def audit(tmp_path):
    a = Audit(tmp_path / "a")
    a.approve_fidelity()
    a.prepare()
    a.approve_roles()
    yield a
    a.close()


def test_kerneld_failing_to_start_keeps_the_target_and_a_retry_certifies(case, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(KC.KernelD, "__init__", _unavailable)
        out = case.run()
    assert out["verdict"] == "COULD NOT RUN" and out["labels_read"] is False and "kerneld" in out["reason"]
    assert case.evaluate_reads() == 0
    assert not C.first_test_started(case.store, case.case_id)
    again = case.run()
    assert again["cert_hash"] and again["labels_read"] is True, again
    assert str(again["label"]).startswith("CERTIFIED"), again


@pytest.mark.parametrize("fail", [_unavailable, _refused], ids=["kerneld-gone", "kerneld-refused"])
def test_a_failure_at_the_first_score_call_reads_no_label_and_keeps_the_target(case, monkeypatch, fail):
    with monkeypatch.context() as m:
        m.setattr(RunHandle, "held_out_score", fail)
        out = case.run()
    assert out["verdict"] == "COULD NOT RUN" and out["labels_read"] is False
    assert case.run()["cert_hash"]


def test_a_failure_after_a_label_read_is_shown_as_spent(case, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(RunHandle, "held_out_score", _once_then_gone())
        out = case.run()
    assert out["verdict"] == "COULD NOT RUN" and out["labels_read"] is True and case.evaluate_reads() == 1
    with pytest.raises(C.CourtRefused, match="one look"):
        case.run()


def test_a_sandbox_that_cannot_start_is_could_not_run(case):
    out = C.run(case.store, case.case_id, _broken_sandbox)
    assert out["verdict"] == "COULD NOT RUN" and out["labels_read"] is False and "disk full" in out["reason"]
    assert case.run()["cert_hash"]


def test_unscorable_predictions_are_stopped_before_any_label_is_read(tmp_path):
    arm = (b"class M:\n    def fit(self, X, y): pass\n    def predict(self, X): return ['abc'] * len(X)\n"
           b"def build_model(seed):\n    return M()\n")
    mean = (b"class M:\n    def fit(self, X, y): self.m = sum(map(float, y)) / len(y)\n"
            b"    def predict(self, X): return [self.m] * len(X)\n"
            b"def build_model(seed):\n    return M()\n")  # the trivial comparator for an error metric
    c = Case(tmp_path / "c", method=arm, baseline=mean, family="friedman", claim=part("rmse", 0.1))
    try:
        out = c.run()
        assert out["verdict"] == "COULD NOT RUN" and out["labels_read"] is False and "number" in out["reason"]
        assert c.evaluate_reads() == 0
    finally:
        c.close()


@pytest.mark.parametrize("metric,preds,bad", [("accuracy", ["a", "b"], False), ("f1_macro", ["a"], False),
                                              ("rmse", ["1.5", "2"], False), ("mae", ["1", "x"], True),
                                              ("rmse", ["nan"], True), ("rmse", ["inf"], True)])
def test_prediction_problem_needs_no_label(metric, preds, bad):
    assert (M.predictions_problem(metric, preds) is not None) == bad


def test_a_crash_inside_the_mint_after_its_ledger_entry_can_finish(tmp_path, monkeypatch):
    """The ledger entry is written, the process dies before the ingest is published: nothing was read, so the target
    must not be lost. The same beacon finishes it; another beacon is still beacon shopping."""
    c = Case(tmp_path / "c", mint=False)
    try:
        vault = Kernel(c.store).vault
        args = dict(family="rings", n_train=300, n_test=600, beacon_round="r1", beacon_value=BEACON,
                    anchor_file=str(c.body["anchor_file"]))

        def die(*a, **k):
            raise SystemExit("crash")

        with monkeypatch.context() as m:
            m.setattr(I, "_publish", die)
            with pytest.raises(SystemExit):
                I.minted_heldout(vault, "run1", c.target, c.claim_hash, **args)
        assert len(Kernel(c.store).ledger.entries("mint")) == 1
        with pytest.raises(I.IngestRefused) as e:
            I.minted_heldout(vault, "run1", c.target, c.claim_hash, **{**args, "beacon_value": "another-beacon-0002"})
        assert e.value.code == "already_registered"
        res = I.minted_heldout(vault, "run1", c.target, c.claim_hash, **args)
        assert res["n_test"] >= 1 and len(Kernel(c.store).ledger.entries("mint")) == 1
        assert I.minted_heldout(vault, "run1", c.target, c.claim_hash, **args) == res
    finally:
        c.close()


def test_a_mint_with_kerneld_gone_is_a_refusal_and_can_be_repeated(tmp_path, monkeypatch):
    c = Case(tmp_path / "c", mint=False)
    try:
        with monkeypatch.context() as m:
            m.setattr(KC.KernelD, "__init__", _unavailable)
            with pytest.raises(C.CourtRefused, match="kerneld"):
                c.mint()
        assert c.mint()["n_test"] >= 1
    finally:
        c.close()


# ------------------------------------------------------------------------------------- the Claim Audit route
@pytest.mark.parametrize("fail,where", [(_unavailable, "start"), (_unavailable, "score"), (_refused, "score")],
                         ids=["kerneld-no-start", "kerneld-gone", "kerneld-refused"])
def test_claim_audit_failure_before_a_read_keeps_the_target(audit, monkeypatch, fail, where):
    with monkeypatch.context() as m:
        if where == "start":
            m.setattr(KC.KernelD, "__init__", fail)
        else:
            m.setattr(RunHandle, "held_out_score", fail)
        out = CA.run(audit.store, audit.loaded, audit.factory)
    assert out["verdict"] == "COULD NOT RUN" and out["labels_read"] is False
    again = CA.run(audit.store, audit.loaded, audit.factory)
    assert again["cert_hash"], again


def test_claim_audit_failure_after_a_read_is_shown_as_spent(audit, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(RunHandle, "held_out_score", _once_then_gone())
        out = CA.run(audit.store, audit.loaded, audit.factory)
    assert out["verdict"] == "COULD NOT RUN" and out["labels_read"] is True
    with pytest.raises(gate.AuditRefused, match="one look"):
        CA.run(audit.store, audit.loaded, audit.factory)


def test_claim_audit_sandbox_that_cannot_start_is_could_not_run(audit):
    out = CA.run(audit.store, audit.loaded, _broken_sandbox)
    assert out["verdict"] == "COULD NOT RUN" and "disk full" in out["reason"]
    assert CA.run(audit.store, audit.loaded, audit.factory)["cert_hash"]


def test_every_host_route_checks_predictions_before_scoring() -> None:
    """Static: each route that calls held_out_score also calls predictions_problem (the generalised bug class)."""
    src = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
    routes = [p for p in src.rglob("*.py") if "held_out_score(" in p.read_text(encoding="utf-8")
              and p.name not in ("client.py", "trace.py", "store.py")]
    assert {p.name for p in routes} == {"claim_audit.py", "minted.py", "claims.py"}
    for p in routes:
        assert "predictions_problem(" in p.read_text(encoding="utf-8"), p
