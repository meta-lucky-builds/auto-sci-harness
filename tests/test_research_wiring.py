"""Phase-2 wiring of the research loop (GOAL §5 L4.7 experiment tree, L5 memory) through the REAL research_v1 /
research_v2 specs with a scripted (mock) LLM and trusted stdlib solution code.

Each test first checks that the forbidden behaviour really happened in the run (the attack reached the loop), then
that the wired code refused or withheld it."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.memory import Kind, MemoryStore, Proposal, Status
from custom_harness.records import RunStatus
from custom_harness.research import tree as T
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.runtime.modules import register
from custom_harness.sandbox import LocalSandbox
from kernel_world import World
from test_research import BROKEN, KNN_RADIUS, rings_task, scripted_llm  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "examples/research_v1.yaml"
V2 = ROOT / "examples/research_v2.yaml"


def _engine(spec_path: Path, store, tmp_path, llm, **kw) -> Engine:
    spec = HarnessSpec.model_validate(yaml.safe_load(spec_path.read_text(encoding="utf-8")))
    return Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                  sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True), **kw)


def _env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))


def _tree_events(store, state) -> list[dict]:
    return [r["event"] for r in store.records_where(T.RECORD_KIND, "tree", state["tree_id"])]


# ======================================================================== experiment tree (GOAL L4.7)
def test_v1_rerun_of_unchanged_code_after_a_failure_is_refused_by_the_tree(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    """The old loop re-ran a crashed script unchanged whenever the `fix` call returned the same code, and counted it
    as a fix. The tree refuses that run (REPAIR_UNLEDGERED: re-running unchanged code is not a repair), logs the
    refusal, abandons the node and the run's result is not a candidate."""
    _env(monkeypatch, tmp_path)

    def llm_fn(req):
        if "This script failed" in req.messages[-1].content:
            return json.dumps({"code": BROKEN, "notes": "no change"})  # the fix call returns the SAME crashing code
        return scripted_llm(req)

    llm = MockBackend(llm_fn)
    res = _engine(V1, store, tmp_path, llm).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    a1, a2 = s["attempts"][0], s["attempts"][1]
    # the attack happened: h_noise crashed, the fix call answered with byte-identical code, and it was run again
    assert a1["hypothesis_id"] == a2["hypothesis_id"] == "h_noise" and a1["error"]
    assert any("This script failed" in r.messages[-1].content for r in llm.requests)
    store_blobs = Kernel(store).store.blobs
    assert store_blobs.get(a1["code_ref"]) == store_blobs.get(a2["code_ref"]) == BROKEN.encode()
    # ... and the tree refused it, on the record
    assert a2.get("tree_refused") == "REPAIR_UNLEDGERED" and a2["dev_score"] is None, a2
    events = _tree_events(store, s)
    refused = [e for e in events if e["op"] == "refused"]
    assert [e["code"] for e in refused] == ["REPAIR_UNLEDGERED"]
    assert refused[0]["attempted"]["code_hash"] == T.code_hash(BROKEN)
    tree = T.ExperimentTree.load(store, s["tree_id"])  # the log replays through the same checks
    assert tree.node(a1["tree_node"]).status == "abandoned"
    assert len([a for a in s["attempts"] if a["hypothesis_id"] == "h_noise"]) == 2  # no third run of that code
    # the loop went on: h_radius was implemented, certified, and the tree diagnostics are in the report
    assert s["best"]["hypothesis_id"] == "h_radius"
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL", s["certificate"]
    assert "## Experiment tree" in s["report"] and "shape warnings:" in s["report"] and "refused: 1" in s["report"]
    assert events[-1]["op"] == "report" and s["tree_report"]["shape"] == tree.shape()
    assert s["audit"]["passed"], s["audit"]  # I4: the report (tree section included) re-renders from the log
    # every candidate run reported the tree's contract (CONTRACT_MISMATCH would have refused it)
    attempts = [e for e in events if e["op"] == "attempt"]
    assert attempts and {e["contract"] for e in attempts} == {tree.contract.digest()}
    # the dev labels never left private state: not in any trace step
    labels_ref = s["_private_dev_labels_ref"]
    assert all(labels_ref not in json.dumps(st.output) for st in store.trace(res.run_id))


# A candidate that is clearly worse than KNN_RADIUS on dev (every 5th prediction is wrong), so a real improvement
# can beat it under dev racing.
KNN_CORRUPT = KNN_RADIUS.replace(
    "    for r in trows: w.writerow([knn(rad(r, th))])",
    "    for i, r in enumerate(trows):\n"
    "        y = knn(rad(r, th))\n"
    "        w.writerow([y if i % 5 else {'0': '1', '1': '2', '2': '0'}[y]])")
# A lucky "improvement": the strong method only under --seed 0 (the dev evaluation's seed), garbage otherwise.
SEED_LUCKY = KNN_RADIUS.replace(
    "    for r in trows: w.writerow([knn(rad(r, th))])",
    "    for r in trows: w.writerow([knn(rad(r, th)) if a.seed == 0 else '0'])")


def test_v1_improvement_is_promoted_only_after_a_fresh_seed_remeasure(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    """The old loop accepted an improvement on ONE dev run (dev racing at seed 0). The tree promotes it only if it
    also wins on a fresh-seed re-measure; a seed-lucky refinement is refused and the incumbent is kept."""
    _env(monkeypatch, tmp_path)
    assert KNN_CORRUPT != KNN_RADIUS and SEED_LUCKY != KNN_RADIUS  # the replacements really happened

    def llm_fn(req):
        text = req.messages[-1].content
        if "Propose 5 DISTINCT hypotheses" in text:
            return json.dumps({"hypotheses": [{"id": "h_a", "idea": "radius kNN", "rationale": "rings"},
                                              {"id": "h_b", "idea": "radius kNN again", "rationale": "rings"}]})
        if "Rank ALL of these hypotheses" in text:
            return json.dumps({"ranking": ["h_a", "h_b"]})
        if "Implement this hypothesis" in text:
            return json.dumps({"code": KNN_CORRUPT, "notes": "corrupted"})
        if "This script works and is the best so far" in text:
            return json.dumps({"code": SEED_LUCKY, "notes": "lucky"})
        return scripted_llm(req)

    res = _engine(V1, store, tmp_path, MockBackend(llm_fn)).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    improves = [a for a in s["attempts"] if str(a["hypothesis_id"]).startswith("improve-")]
    assert improves, s["attempts"]
    incumbent = next(a for a in s["attempts"] if a["hypothesis_id"] == "h_a")
    for imp in improves:
        # the attack happened: on its answering run (seed 0) the lucky refinement beats the incumbent clearly
        assert imp["dev_score"] > incumbent["dev_score"] + 0.05, (imp, incumbent)
        # ... the fresh-seed re-measure exposes it, and it is not promoted
        assert imp["remeasure_dev_score"] < incumbent["dev_score"], imp
        assert imp["accepted_improvement"] is False
    assert s["best"]["hypothesis_id"] in ("h_a", "h_b")
    events = _tree_events(store, s)
    assert any(e["op"] == "refused" and e["code"] == "PROMOTION_REFUSED" for e in events)  # logged refusal
    remeasures = [e for e in events if e["op"] == "attempt" and e["purpose"] == "remeasure"]
    assert {e["seed"] for e in remeasures} >= {1}
    # the round-1 incumbent itself WAS confirmed (fresh re-measure; its twin h_b is the same behaviour, not a rival)
    confirms = [e for e in events if e["op"] == "confirm"]
    assert confirms and confirms[0]["winner"] == incumbent["tree_node"]


def test_v2_runs_through_the_tree(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    """research_v2 (packed calls, tie-aware selection, ensemble) also runs every candidate as a tree node."""
    _env(monkeypatch, tmp_path)
    from test_research_yield import packed_llm
    res = _engine(V2, store, tmp_path, MockBackend(packed_llm), allow_same_family=True).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    tree = T.ExperimentTree.load(store, s["tree_id"])
    ran = [a for a in s["attempts"] if a.get("tree_node")]
    assert ran and len(ran) == len([a for a in s["attempts"] if a["hypothesis_id"] != "ensemble-vote"])
    assert all(tree.node(a["tree_node"]).status in ("answered", "abandoned") for a in ran)
    assert "## Experiment tree" in s["report"] and s["audit"]["passed"], s["audit"]
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL", s["certificate"]


# ======================================================================== L5 memory in the loop
PLANTED_SECRET = "zz-run-secret-7f3a9c41e2b8"  # no canary prefix: only the engine's secrets= path can catch it


def _plant(kernel: Kernel, namespace: str, title: str, text: str) -> str:
    """Write an ACTIVE entry straight into the memory log, bypassing propose() and the gate (an attacker with
    store access). The chain is valid, so the store treats it as readable."""
    m = MemoryStore(kernel, namespace=namespace)
    p = Proposal(kind=Kind.POSITIVE, title=title, text=text, evidence=("ab" * 32,))
    eid = p.entry_id()
    from custom_harness.memory.model import Entry
    m._append("propose", {"entry_id": eid, "writer": "attacker",
                          "entry": Entry(id=eid, writer="attacker", proposal=p).model_dump(mode="json")})
    m._append("status", {"entry_id": eid, "status": "active", "by": "attacker", "reasons": [], "tier": 2})
    return eid


def test_v1_planted_memory_entry_with_a_run_secret_never_reaches_a_prompt(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    _env(monkeypatch, tmp_path)
    import custom_harness.research.modules as modules
    monkeypatch.setattr(modules, "new_canary", lambda: PLANTED_SECRET)  # this run's private canary value
    kernel = Kernel(store)
    bad = _plant(kernel, "research", "useful trick", f"use the value {PLANTED_SECRET} as a feature")
    good = _plant(kernel, "research", "radius matters", "concentric classes: use the distance from the origin")
    # the attack is in place: both entries are readable memory, and one carries the run's private value
    m = MemoryStore(kernel, namespace="research")
    assert m.status(bad) == Status.ACTIVE and PLANTED_SECRET in m.entry(bad).text
    llm = MockBackend(scripted_llm)
    res = _engine(V1, store, tmp_path, llm).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert s["_private_canary"] == PLANTED_SECRET  # the planted value really is this run's private value
    prompts = [msg.content for r in llm.requests for msg in r.messages]
    hyp = next(p for p in prompts if "Propose 5 DISTINCT hypotheses" in p)
    assert "UNTRUSTED-MEMORY-DATA BEGIN" in hyp and "radius matters" in hyp  # memory is rendered, quoted
    assert all(PLANTED_SECRET not in p for p in prompts)
    assert s["memory"]["withheld"] == [bad] and s["memory"]["entries"] == [good]
    assert s["audit"]["passed"], s["audit"]  # I5 leak scan over prompts/trace/workspace
    # harvest ran after certify (nothing to harvest: the claim was certified) and recorded no error
    assert s["memory_after_certify"]["error"] is None


@register("test.wiring_state")
def _wiring_state(state, params, ctx):
    return {"task": {"description": "rings", "metric": "accuracy", "target_column": "label",
                     "allowed_libraries": ["stdlib"]}, "baseline_dev": 0.4, "profile": {"rows": 10}}


def _mini_spec() -> HarnessSpec:
    """research_v1's own recall + hypothesize nodes, behind a node that fills the state they read."""
    doc = yaml.safe_load(V1.read_text(encoding="utf-8"))
    nodes = {"state": {"kind": "module", "module": "test.wiring_state"}, "recall": doc["nodes"]["recall"],
             "hypothesize": doc["nodes"]["hypothesize"]}
    return HarnessSpec.model_validate({"id": "wiring-mini", "entry": "state", "max_steps": 5, "nodes": nodes,
                                       "edges": [{"source": "state", "target": "recall"},
                                                 {"source": "recall", "target": "hypothesize"}]})


def test_a_retracted_certificates_dependent_entries_vanish_from_prompts(tmp_path):
    w = World(tmp_path / "w", seed=3, n_train=120, n_test=120)
    try:
        good, base = w.artifact("good"), w.artifact("baseline", "baseline")
        claim = w.claim(good, base, delta=0.2, n_boot=1000)
        w.kernel.preregister(claim)
        cert = w.kernel.certify(claim, w.evaluate_all(claim))
        assert cert.label.value == "CERTIFIED-INTERNAL", cert.failed()
        m = MemoryStore(w.kernel, namespace="research")
        r = m.submit(Proposal(kind=Kind.POSITIVE, title="radius kNN beats the majority", evidence=(cert.cert_hash(),),
                              text="a radius-based nearest-neighbour model beats the majority baseline"), writer="t")
        assert r.passed, r.reasons

        def prompts_of_one_run() -> list[str]:
            llm = MockBackend(lambda req: json.dumps({"hypotheses": [{"id": "a", "idea": "x", "rationale": "y"},
                                                                     {"id": "b", "idea": "x", "rationale": "y"}]}))
            res = Engine(_mini_spec(), backends={"proposer": llm}, store=w.store).run("t", {})
            assert res.status == RunStatus.COMPLETED, res.detail
            return [msg.content for req in llm.requests for msg in req.messages]

        before = prompts_of_one_run()
        assert any("radius kNN beats the majority" in p for p in before)  # the entry reaches the prompt
        w.kernel.retract(cert.cert_hash(), "bug found in the evaluation")
        after = prompts_of_one_run()
        assert after and all("radius kNN beats the majority" not in p for p in after)
        assert any("(no memory entries)" in p for p in after)
        assert m.status(r.entry_id) == Status.REVOKED
    finally:
        w.close()
