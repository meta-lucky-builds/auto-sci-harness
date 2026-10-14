"""Publication gate (GOAL §1.3, VG Table 10: "every report auto-fills the checklist; a missing item blocks
publication"; invariant 10: every loop is labelled MECHANICAL or VALIDATED).

Paths that publish a report, each tested here end to end:
  * `chh audit publish` (audit/gate.py publish), the ONLY code path that writes an export package. Until 2026-10-09
    it shipped report.md with no reporting checklist at all (no traces, cost, selection policy or human-in-the-loop
    points), and published without the Reproducibility Bundle hash and without a certificate.
  * the research report (research.certify -> research.report): it is not exported anywhere, but it carries the
    `publishable` bit and is the source of every headline result. Until 2026-10-09 a blank checklist answer counted
    as answered, the loop label was not a checklist item, the report never said whether it was publishable, and
    `headline()` ignored the publication blockers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from custom_harness.audit import claim_audit as CA
from custom_harness.audit import gate as G
from custom_harness.gateway import MockBackend
from custom_harness.records import RunStatus
from custom_harness.research.claims import headline
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_claim_audit import honest  # noqa: F401  (fixture re-export)
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401  (fixture re-export)

VG_ITEMS = ("seeds", "traces", "selection_policy", "human_in_the_loop", "cost", "reproducibility_bundle")


def _answered(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _approve(a, out) -> None:
    rh = out["report_hash"]
    G.record_reply(a.store, audit_id=out["audit_id"], report_hash=rh, recorded_by="h", notified="author, email",
                   notified_on=date.today() - timedelta(days=30), reply_text=None)
    G.approve_publication(a.store, audit_id=out["audit_id"], report_hash=rh, approver="pytest-human",
                          reviewed_hash=rh)


# ------------------------------------------------------------------ the audit export path
def test_audit_publish_ships_a_complete_checklist_with_the_report(honest, tmp_path):  # noqa: F811
    a, out = honest
    _approve(a, out)
    bpath = CA.bundle(a.store, out, tmp_path / "b.zip")
    dest = tmp_path / "pub"
    man = G.publish(a.store, out, CA.current_report(a.store, out), dest, bundle_path=bpath)
    cl = man.get("checklist")
    assert isinstance(cl, dict), "the published package carries no reporting checklist"
    from custom_harness.kernel.checklist import ITEMS
    assert all(_answered(cl.get(k)) for k, _ in ITEMS), cl
    assert cl["loop_label"].startswith("not a loop:")
    assert hashlib.sha256(bpath.read_bytes()).hexdigest() in cl["reproducibility_bundle"]
    assert "publication approved by pytest-human" in cl["human_in_the_loop"]
    md = (dest / "checklist.md").read_text(encoding="utf-8")
    assert man["checklist_sha256"] == hashlib.sha256(md.encode("utf-8")).hexdigest()
    assert "MISSING" not in md and "Loop label" in md


def test_audit_publish_without_the_bundle_is_refused_for_the_missing_checklist_item(honest, tmp_path):  # noqa: F811
    a, out = honest
    _approve(a, out)
    dest = tmp_path / "pub"
    with pytest.raises(G.AuditRefused, match="checklist item missing: reproducibility_bundle"):
        G.publish(a.store, out, CA.current_report(a.store, out), dest, bundle_path=None)
    assert not dest.exists()


@pytest.mark.parametrize("item", VG_ITEMS[:5])
def test_audit_publish_with_a_blank_checklist_item_is_refused(honest, tmp_path, monkeypatch, item):  # noqa: F811
    import custom_harness.audit.checklist as AC
    real = AC.fill

    def blank(*args, **kw):
        cl = real(*args, **kw)
        cl[item] = "   "  # truthy, so the Kernel's `missing` counts it as answered: here it is no answer
        return cl
    monkeypatch.setattr(AC, "fill", blank)
    a, out = honest
    _approve(a, out)
    dest = tmp_path / "pub"
    with pytest.raises(G.AuditRefused, match=f"checklist item missing: {item}"):
        G.publish(a.store, out, CA.current_report(a.store, out), dest, bundle_path=CA.bundle(a.store, out,
                                                                                               tmp_path / "b.zip"))
    assert not dest.exists()


def test_audit_publish_refuses_a_loop_label_on_an_audit(honest, tmp_path, monkeypatch):  # noqa: F811
    import custom_harness.audit.checklist as AC
    monkeypatch.setattr(AC, "NOT_A_LOOP", "VALIDATED")  # an audit is not a loop: it may not claim a loop label
    a, out = honest
    _approve(a, out)
    with pytest.raises(G.AuditRefused, match="not of a loop"):
        G.publish(a.store, out, CA.current_report(a.store, out), tmp_path / "pub",
                  bundle_path=CA.bundle(a.store, out, tmp_path / "b.zip"))


def test_audit_without_a_certificate_is_not_published(honest, tmp_path):  # noqa: F811
    a, real = honest
    report = CA.render_could_not_run(real["audit_id"], real["claim_hash"], "fixture: data unavailable", False)
    rh = hashlib.sha256(report.encode()).hexdigest()
    out = {"audit_id": real["audit_id"], "claim_hash": real["claim_hash"], "verdict": "COULD NOT RUN",
           "reason": "fixture: data unavailable", "needs_owner": False, "cert_hash": None, "report_hash": rh}
    _approve(a, out)
    with pytest.raises(G.AuditRefused, match="no certificate"):
        G.publish(a.store, out, report, tmp_path / "pub")
    assert not (tmp_path / "pub").exists()


# ------------------------------------------------------------------ the research report
def _state(**kw):
    s = {"certificate": {"label": "CERTIFIED-INTERNAL"}, "replica_certificate": {"label": "CERTIFIED-INTERNAL"},
         "model_policy": {"invariant_5": "met"}, "publishable": True, "replica_publishable": True}
    return {**s, **kw}


def test_headline_needs_both_claims_publishable():
    assert headline(_state())
    assert not headline(_state(publishable=False))
    assert not headline(_state(replica_publishable=False))
    assert not headline({k: v for k, v in _state().items() if k != "publishable"})  # no record: fail closed


def test_gate_rules():
    from custom_harness import publication as P
    full = {k: "answer" for k, _ in P.ALL_ITEMS}
    assert P.blockers({**full, "loop_label": "MECHANICAL"}, is_loop=True) == []
    assert P.blockers({**full, "loop_label": "VALIDATED"}, is_loop=True) == []
    assert P.blockers({**full, "loop_label": "mechanical-ish"}, is_loop=True)
    assert P.blockers({k: v for k, v in full.items() if k != "loop_label"}, is_loop=True) == [
        "checklist item missing: loop_label (GOAL §6.1, invariant 10)"]
    assert P.blockers({**full, "cost": " \n"}, is_loop=True)[0] == "checklist item missing: cost"
    assert P.blockers({**full, "seeds": True, "loop_label": "MECHANICAL"}, is_loop=True) == [
        "checklist item missing: seeds"]
    assert P.blockers(None, is_loop=True) == ["reporting checklist missing entirely (VG Table 10)"]
    assert P.blockers({**full, "loop_label": "not a loop: one confirmation"}, is_loop=False) == []
    assert P.blockers({**full, "loop_label": "not a loop:  "}, is_loop=False)  # the reason is required


def test_audit_checklist_without_the_human_steps_has_no_human_in_the_loop_answer(honest, tmp_path):  # noqa: F811
    from custom_harness.audit.checklist import audit_checklist
    a, out = honest
    cl = audit_checklist(a.store, out, "b" * 64, None, None)  # no right-of-reply, no publication approval
    assert cl["human_in_the_loop"] is None


def _research(store, tmp_path, task, monkeypatch, blank: str | None):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    if blank is not None:
        import custom_harness.research.claims as RC
        real = RC.fill

        def fill(*args, **kw):
            cl = real(*args, **kw)
            cl[blank] = "  "
            return cl
        monkeypatch.setattr(RC, "fill", fill)
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(scripted_llm)
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    return res.state


def test_research_report_with_a_blank_checklist_item_is_not_publishable(store, tmp_path, rings_task,  # noqa: F811
                                                                        monkeypatch):
    s = _research(store, tmp_path, rings_task, monkeypatch, blank="selection_policy")
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL", s["certificate"]
    assert s["publishable"] is False
    assert "checklist item missing: selection_policy" in s["publish_blockers"], s["publish_blockers"]
    assert "**Publication: BLOCKED (NOT PUBLISHABLE)**" in s["report"]
    assert "checklist item missing: selection_policy" in s["report"]
    assert s["audit"]["passed"], s["audit"]  # the re-render (I4) agrees with the blocked report


def test_research_report_states_its_loop_label_and_publication(store, tmp_path, rings_task,  # noqa: F811
                                                                monkeypatch):
    s = _research(store, tmp_path, rings_task, monkeypatch, blank=None)
    assert s["publishable"] is True, s["publish_blockers"]
    assert s["checklist"]["loop_label"] == "MECHANICAL"  # internal data only (GOAL §6.1)
    assert "**Publication: allowed**" in s["report"]
    assert "Loop label (GOAL §6.1): MECHANICAL or VALIDATED**: MECHANICAL" in s["report"]
    assert s["audit"]["passed"], s["audit"]
    json.dumps(s["checklist"])  # stays plain JSON (stored in the run state)


def test_every_publish_path_uses_the_gate():
    """Static: every module that exports or certifies a report as publishable calls publication.blockers (a new
    path that forgets it fails here)."""
    root = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
    for rel, call in (("audit/gate.py", "checklist_blockers(checklist, is_loop=False)"),
                      ("research/claims.py", "publication_blockers(checklist, is_loop=True)")):
        assert call in (root / rel).read_text(encoding="utf-8"), rel
    # the only writers of an exported report file: a new one must be added above with its gate call
    writers = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py")
                     if '"report.md"' in p.read_text(encoding="utf-8"))
    assert writers == ["audit/gate.py"], writers
