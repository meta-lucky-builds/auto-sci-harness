"""Invariant 8 extended to figures, in the research audit: a run whose figure does not re-render from its artifacts
fails its audit (I6), and a figure may only plot the run's certified evidence."""

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.budget import Budget
from custom_harness.gateway import MockBackend
from custom_harness.records import Evidence, EvidenceKind, RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.runtime.modules import ModuleContext
from custom_harness.sandbox import LocalSandbox
from custom_harness.tiers import Tier
from test_figures import HONEST
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401


@pytest.fixture
def finished(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    llm = MockBackend(scripted_llm)
    eng = Engine(HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text())),
                 backends=dict.fromkeys(("proposer", "executor", "judge", "replicator"), llm), store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED and res.state["audit"]["passed"], res.detail
    ctx = ModuleContext(run_id=res.run_id, store=store, backends={}, budget=Budget(),
                        sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    return res.state, ctx, store, tmp_path


def _with_figure(state, ctx, store, tmp_path, refs, name="fig"):
    from custom_harness.figures import make_figure
    from custom_harness.research.claims import render_report
    d = tmp_path / name
    meta = make_figure(d, HONEST, refs, store, LocalSandbox(tmp_path / f"mk-{name}", i_understand_no_isolation=True))
    s = {**state, "figures": [{"dir": str(d), "meta_sha256": meta}]}
    s["report"] = render_report(s, ctx)
    return s, d, meta


def _arm_refs(state, store):
    import json
    out = {}
    for eid in state["evidence_ids"]:
        arm = json.loads(store.blobs.get(store.get_evidence(eid).payload_ref))["arm"]
        key = "ours" if arm == "subject" else "base"
        out.setdefault(key, {"evidence": eid, "pointer": "/score"})
    return out


def test_figures_in_the_audit(finished):
    from custom_harness.research.claims import audit
    state, ctx, store, tmp_path = finished
    plain = audit(state, {}, ctx)["audit"]
    s, d, meta = _with_figure(state, ctx, store, tmp_path, _arm_refs(state, store))
    good = audit(s, {}, ctx)["audit"]

    p = d / "figure.svg"  # the attack: a bar label edited after the figure was made
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace(">ours<", ">ours (best)<"), encoding="utf-8", newline="\n")
    assert p.read_text(encoding="utf-8") != text  # the attack is in place
    bad = audit(s, {}, ctx)["audit"]
    assert not bad["passed"], bad  # without I6 the run's audit passes with an edited figure
    assert bad["I6"] is False and any("figure.svg differs" in x for x in bad["problems"]), bad["problems"]
    assert plain["I6"] == "n/a" and plain["passed"], plain
    assert f"`{meta}`" in s["report"] and "## Figures" in s["report"]
    assert good["I6"] is True and good["passed"], good


def test_a_figure_may_only_plot_the_runs_certified_evidence(finished):
    from custom_harness.research.claims import audit
    state, ctx, store, tmp_path = finished
    ev = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T2_STATISTICAL, verifier_id="x", produced_by_run=None,
                  payload_ref=store.blobs.put_json({"score": 0.99}))
    store.add_evidence(ev)  # real evidence in the store, but not this run's certificate's
    refs = {**_arm_refs(state, store), "ours": {"evidence": ev.id, "pointer": "/score"}}
    s, _, _ = _with_figure(state, ctx, store, tmp_path, refs)
    res = audit(s, {}, ctx)["audit"]
    assert not res["passed"], res  # without I6 the run's audit passes with a figure of foreign evidence
    assert res["I6"] is False
    assert any("not among the evidence this figure may plot" in x for x in res["problems"]), res["problems"]


def test_an_edit_cannot_write_figures():
    """`figures` is audit input: an added node writing it could swap a run's figures (reserved state)."""
    from custom_harness.evolve import Edit, EditOp, EditRejected, EditType, apply_edit
    from test_evolve import seed_spec
    spec = seed_spec()
    i = next(n for n, e in enumerate(spec.model_dump(mode="json")["edges"]) if e["source"] == "socratic")

    def sneak(key):
        return Edit(type=EditType.HOP, rationale="x", ops=[
            EditOp(op="add", path="/nodes/sneak", value={"kind": "llm", "role": "proposer", "prompt": "p",
                                                         "output_key": key}),
            EditOp(op="replace", path=f"/edges/{i}/target", value="sneak"),
            EditOp(op="add", path="/edges/-", value={"source": "sneak", "target": "next"})])
    apply_edit(spec, sneak("notes"))  # positive control: the same wiring with a harmless key is accepted
    with pytest.raises(EditRejected, match="reserved state key"):
        apply_edit(spec, sneak("figures"))
