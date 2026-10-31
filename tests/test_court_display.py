"""The one display path and the feedback split (GOAL 18.5, court/display.py), on real minted cases."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from _court_world import ARMS, Case, part
from custom_harness.court import display as D
from custom_harness.court import minted as C
from custom_harness.kernel import Kernel
from custom_harness.kernel.certificate import CHECK_IDS
from custom_harness.kerneld.client import KernelDUnavailable, RunHandle
from custom_harness.kerneld.releases import released

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def won(tmp_path):
    c = Case(tmp_path / "c")
    out = c.run()
    assert str(out["label"]).startswith("CERTIFIED")
    yield c
    c.close()


@pytest.fixture
def lost(tmp_path):
    """A claim that fails on the effect: the method is the baseline with a comment."""
    same = (ARMS / "majority.py").read_bytes() + b"\n# a method with no effect\n"
    c = Case(tmp_path / "c", method=same)
    out = c.run()
    assert out["cert_hash"] and not str(out["label"]).startswith("CERTIFIED")
    yield c
    c.close()


def test_the_table_covers_every_check_once():
    assert sorted(D.TRUTH_INDEPENDENT + D.TRUTH_DEPENDENT) == sorted(CHECK_IDS)
    assert set(D.REASON_CLASS) == set(D.TRUTH_DEPENDENT)
    assert len(D.REASON_CLASS) + 1 <= 16  # one `count` release may tell 16 values


def test_a_view_records_a_release_and_each_viewer_counts_once(won):
    before = len(released(won.store, won.target))
    v = D.view(won.store, won.case_id, "owner")
    assert v["label"].startswith("CERTIFIED") and v["reason_class"] is None
    after = released(won.store, won.target)
    assert len(after) == before + 1 and after[-1]["note"] == "court:view" and after[-1]["channel"] == "report"
    D.view(won.store, won.case_id, "owner")
    assert len(released(won.store, won.target)) == before + 1
    D.view(won.store, won.case_id, "someone-else")
    assert len(released(won.store, won.target)) == before + 2


def test_a_failed_claim_shows_a_bit_and_a_closed_class_and_no_number(lost):
    v = D.view(lost.store, lost.case_id, "owner")
    assert v["reason_class"] in D.REASON_CLASS.values()
    assert set(v["checks"]) == set(D.TRUTH_INDEPENDENT)
    assert "autopsy" not in v
    cert = Kernel(lost.store).certificate(C.latest(lost.store, lost.case_id, "outcome")["cert_hash"])
    text = json.dumps(v)
    for k in D.TRUTH_DEPENDENT:  # no reason text of a truth-dependent check
        for r in cert.checks[k].reasons:
            assert str(r) not in text
    assert any(cert.checks[k].reasons for k in D.TRUTH_DEPENDENT)  # there was something to withhold
    notes = [r["note"] for r in released(lost.store, lost.target)]
    assert "court:view" in notes and "court:view_reason" in notes


def test_the_autopsy_opens_only_after_retirement_and_marks_the_target_exposed(lost):
    assert "autopsy" not in D.view(lost.store, lost.case_id, "owner")
    with pytest.raises(D.DisplayRefused):
        D.retire(lost.store, lost.target, " ")
    D.retire(lost.store, lost.target, "owner")
    v = D.view(lost.store, lost.case_id, "owner")
    assert v["retired"] and v["autopsy"]["checks"]["K6"] and v["looks_remaining"] == 0 and v["exposed"]
    rel = [r for r in released(lost.store, lost.target) if r["note"] == "court:autopsy"]
    assert len(rel) == 1 and rel[0]["alphabet"] == "continuous"
    D.view(lost.store, lost.case_id, "owner")
    assert len([r for r in released(lost.store, lost.target) if r["note"] == "court:autopsy"]) == 1


def test_looks_remaining_before_and_after(tmp_path):
    c = Case(tmp_path / "c")
    try:
        assert D.view(c.store, c.case_id, "owner")["looks_remaining"] == 1
        assert D.view(c.store, c.case_id, "owner")["state"] == "no outcome yet"
        c.run()
        assert D.view(c.store, c.case_id, "owner")["looks_remaining"] == 0
    finally:
        c.close()


def test_an_alpha_account_shows_its_free_slots(tmp_path):
    from test_sealed_project import Project
    p = Project(tmp_path / "p", parts=1)
    try:
        p.session()
        t = p.seal["targets"][0]
        k = Kernel(p.store)
        k.open_alpha_account(t, 0.05, 3, "fixture-human")
        body = C.register_sealed(p.store, p.freeze(), p.project, anchor_file=p.anchor())
        assert D.looks_remaining(p.store, body) == 2
        k.retire_alpha_account(t, "fixture-human")
        assert D.retired(p.store, t) and D.looks_remaining(p.store, body) == 0
    finally:
        p.close()


def test_could_not_run_shows_the_full_reason_when_nothing_was_read(tmp_path):
    c = Case(tmp_path / "c", method=b"def build_model(seed):\n    raise ValueError('typo in my code')\n")
    try:
        c.run()
        v = D.view(c.store, c.case_id, "owner")
        assert v["state"] == "COULD NOT RUN" and "typo in my code" in v["reason"] and v["labels_read"] is False
        assert v["looks_remaining"] == 1
    finally:
        c.close()


def test_could_not_run_after_a_read_hides_the_detail(tmp_path, monkeypatch):
    c = Case(tmp_path / "c")
    try:
        real, calls = RunHandle.held_out_score, []

        def once(self, *a, **k):
            if calls:
                raise KernelDUnavailable("detail that must stay")
            calls.append(1)
            return real(self, *a, **k)

        with monkeypatch.context() as m:
            m.setattr(RunHandle, "held_out_score", once)
            c.run()
        v = D.view(c.store, c.case_id, "owner")
        assert v["labels_read"] is True and "detail that must stay" not in json.dumps(v)
    finally:
        c.close()


def test_a_view_needs_a_viewer(won):
    with pytest.raises(D.DisplayRefused):
        D.view(won.store, won.case_id, " ")


def test_only_the_display_module_emits_outcome_releases():
    """Ratchet: a new surface cannot show a Court outcome without going through court/display.py."""
    bad = []
    for p in [*(ROOT / "src/custom_harness/court").glob("*.py"), *(ROOT / "src/custom_harness/lab").glob("*.py")]:
        if p.name == "display.py":
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "emit":
                bad.append(f"{p.name}:{n.lineno}")
    assert bad == [], bad


def test_the_court_cli_prints_cases_only_through_the_view():
    src = (ROOT / "src/custom_harness/court/cli.py").read_text(encoding="utf-8")
    assert "level_of_case" not in src and "C.summary" not in src and "D.view(" in src


def test_sealed_target_second_case_needs_a_budget(tmp_path):
    from test_sealed_project import Project
    p = Project(tmp_path / "p", parts=1)
    try:
        p.session()
        C.register_sealed(p.store, p.freeze(), p.project, anchor_file=p.anchor())
        with pytest.raises(C.CourtRefused, match="one look"):
            C.register_sealed(p.store, p.freeze(method=b"def build_model(seed):\n    return 1\n"), p.project,
                              anchor_file=p.anchor())
    finally:
        p.close()
    q = Project(tmp_path / "q", parts=1)
    try:
        q.session()
        t = q.seal["targets"][0]
        Kernel(q.store).open_alpha_account(t, 0.05, 2, "fixture-human")
        C.register_sealed(q.store, q.freeze(), q.project, anchor_file=q.anchor())
        assert C.sealed_route(q.store, q.project, 1)["target"] == t  # a budget was opened first: a second case may use it
        D.retire(q.store, t, "owner")
        with pytest.raises(C.CourtRefused, match="retired"):
            C.sealed_route(q.store, q.project, 1)
    finally:
        q.close()


def test_part_helper_has_defaults():
    assert part().metric == "accuracy"
