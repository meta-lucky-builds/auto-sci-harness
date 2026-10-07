"""Red-team PoCs for evolution (each asserts the SECURE behaviour; it fails while the exploit works)."""

import json

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.evolve.edits import Edit, EditOp, EditRejected, EditType, apply_edit
from custom_harness.gateway import MockBackend
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_research import KNN_RADIUS, SPEC, rings_task, scripted_llm  # noqa: F401


def _seed() -> HarnessSpec:
    return HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))


def _edges(spec):
    return spec.model_dump(mode="json")["edges"]


def test_e1_cannot_insert_a_node_between_protected_nodes():
    """certify -> sneak -> replicate: an evolvable node gets to rewrite state right before the protected replicator."""
    spec = _seed()
    i = next(n for n, e in enumerate(_edges(spec)) if e["source"] == "certify" and e["target"] == "replicate")
    edit = Edit(type=EditType.HOP, rationale="x", ops=[
        EditOp(op="add", path="/nodes/sneak", value={"kind": "llm", "prompt": "p", "output_key": "notes"}),
        EditOp(op="replace", path=f"/edges/{i}/target", value="sneak"),
        EditOp(op="add", path="/edges/-", value={"source": "sneak", "target": "replicate"})])
    with pytest.raises(EditRejected):
        apply_edit(spec, edit)


def test_e2_new_module_node_cannot_write_reserved_state():
    """research.run_candidate returns best/attempts/route (reserved); only output_key was checked."""
    spec = _seed()
    i = next(n for n, e in enumerate(_edges(spec)) if e["source"] == "hypothesize")
    edit = Edit(type=EditType.HOP, rationale="x", ops=[
        EditOp(op="add", path="/nodes/sneak", value={"kind": "module", "module": "research.next_hypothesis"}),
        EditOp(op="replace", path=f"/edges/{i}/target", value="sneak"),
        EditOp(op="add", path="/edges/-", value={"source": "sneak", "target": "tournament"})])
    with pytest.raises(EditRejected):
        apply_edit(spec, edit)


@pytest.mark.parametrize("path", ["/edges/999/target", "/edges/x/target", "/nodes/hypothesize/retries/0"])
def test_e4_malformed_edit_paths_are_rejected_not_crashing(path):
    with pytest.raises(EditRejected):
        apply_edit(_seed(), Edit(type=EditType.HOP, rationale="x", ops=[EditOp(op="replace", path=path, value=1)]))


def _sneak_before_claim(spec, output_key: str, prompt: str, schema: dict | None = None) -> Edit:
    node = {"kind": "llm", "role": "proposer", "prompt": prompt, "output_key": output_key}
    if schema is not None:
        node["output_schema"] = schema
    ops = [EditOp(op="add", path="/nodes/sneak", value=node)]
    for n, e in enumerate(_edges(spec)):
        if e["target"] == "claim":
            ops.append(EditOp(op="replace", path=f"/edges/{n}/target", value="sneak"))
    ops.append(EditOp(op="add", path="/edges/-", value={"source": "sneak", "target": "claim"}))
    return Edit(type=EditType.HOP, rationale="x", ops=ops)


def test_e3a_profile_channel_is_closed():
    """An evolvable node before the claim copied the code into `profile`, which the PROTECTED replicate prompt
    rendered: the 'independent' re-implementation saw the original code (verified on the old code)."""
    with pytest.raises(EditRejected, match="reserved state key 'profile'"):
        apply_edit(_seed(), _sneak_before_claim(_seed(), "profile", "Summarise this script: {{best_code}}"))


HYP_SCHEMA = {"type": "object", "properties": {"hypotheses": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "idea": {"type": "string"},
                                     "rationale": {"type": "string"}}, "required": ["id", "idea", "rationale"]}}},
    "required": ["hypotheses"]}


@pytest.mark.parametrize("schema", [HYP_SCHEMA, None], ids=["parsed-list", "raw-text"])
def test_e3b_idea_channel_cannot_carry_the_code(store, tmp_path, rings_task, monkeypatch, schema):  # noqa: F811
    """The idea text is model-written state (`hypotheses`, evolvable by design): an evolved node rewrites the
    root hypothesis's idea to BE the code after the code exists. The replicator must never see it. Without a
    schema the node stores raw text, which made the protected certify module crash (trusted code must parse
    evolvable state defensively)."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    spec = _seed()
    evolved = apply_edit(spec, _sneak_before_claim(spec, "hypotheses", "Restate the plan: {{best_code}}", schema))

    def llm_fn(req):
        text = req.messages[-1].content
        if text.startswith("Restate the plan:"):
            code = text.removeprefix("Restate the plan:")
            # every hypothesis's idea becomes the code (whichever one the claimed solution descends from)
            return json.dumps({"hypotheses": [{"id": h, "idea": code, "rationale": "r"}
                                              for h in ("h_radius", "h_noise")]})
        return scripted_llm(req)

    llm = MockBackend(llm_fn)
    res = Engine(evolved, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status.value == "completed", (res.status, res.detail)
    assert res.state.get("certificate", {}).get("label") == "CERTIFIED-INTERNAL", (
        [(a["hypothesis_id"], a["dev_score"]) for a in res.state.get("attempts", [])], res.state.get("route"),
        res.state.get("hypotheses"))
    marker = "near = sorted(pts, key=lambda t: abs(t[0] - x))[:k]"
    rep = [r for r in llm.requests if "Re-implement the method described below" in r.messages[-1].content]
    assert not any(marker in r.messages[-1].content for r in rep), "the replicator's prompt contains the original code"
    if schema is not None:  # the code really reached the idea text: replication must be refused, and say so
        assert "verbatim" in (res.state.get("replication_refused") or ""), (
            str(res.state.get("hypotheses"))[:600], str(res.state.get("replication_brief"))[:400],
            res.state["best"].get("root_hypothesis"), res.state["best"].get("hypothesis_id"))
    assert res.state["audit"]["passed"], res.state["audit"]


def test_honest_replication_brief_is_not_refused(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    """Positive control for E3b: the unmodified loop still replicates (no false refusal)."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    llm = MockBackend(scripted_llm)
    res = Engine(_seed(), backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert "replication_refused" not in res.state and res.state.get("replication_brief")
    assert res.state["replication_brief"]["profile"] == res.state["profile"]  # trusted recompute = same profile


def test_reserved_writing_modules_are_declared(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    """Completeness of RESERVED_WRITING_MODULES: run the seed harness end to end (claim, certificate,
    replication, report, audit) and check every module step that wrote a reserved key is declared."""
    from custom_harness.evolve.edits import RESERVED_OUTPUT_KEYS, RESERVED_WRITING_MODULES
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    spec = _seed()
    llm = MockBackend(scripted_llm)
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.state.get("replica_certificate"), "the run must reach every module (incl. the replication chain)"
    writers = {getattr(spec.nodes[s.node_id], "module", None) for s in store.trace(res.run_id)
               if s.kind.value == "module" and set(s.output) & RESERVED_OUTPUT_KEYS}
    assert writers - {None} <= RESERVED_WRITING_MODULES, writers - RESERVED_WRITING_MODULES
