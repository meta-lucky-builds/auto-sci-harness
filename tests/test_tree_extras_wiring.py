"""GOAL §5 L4.7 tree extras WIRED into research_v1/v2 (research/modules.py, spec flags run.backtrack,
run.curriculum, next.revive): exact backtracking via workspace snapshots, the tune -> creative -> ablation curriculum
with the stage decided by code from dev-racing evidence (never by a model), and revivable dead-ends under the
persistence rule, keeping every existing lifecycle rule (immutable answered nodes, one run contract, counted repairs,
re-measured promotion).

Unit tests drive research/tree.py directly; the loop tests run research_v1 end to end on the MockBackend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.records import RunStatus
from custom_harness.research import tree as T
from custom_harness.research.tree import Code, ExperimentTree, RunContract, RunOutcome, TreeRefusal, TreeTampered
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_research import KNN_RADIUS, rings_task, scripted_llm  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "examples/research_v1.yaml"
V2 = ROOT / "examples/research_v2.yaml"

N = 40
LABELS = ["a" if i % 2 else "b" for i in range(N)]
CONTRACT = RunContract.make(["python", "solution.py"], {}, {"train.csv": "1" * 64}, {"timeout_s": 600})


def _preds(errors: int, offset: int = 0) -> list[str]:
    return [("b" if y == "a" else "a") if offset <= i < offset + errors else y for i, y in enumerate(LABELS)]


def _ok(code: str, *, errors: int, seed: int, offset: int = 0) -> RunOutcome:
    p = _preds(errors, offset)
    acc = sum(a == b for a, b in zip(p, LABELS, strict=True)) / N
    return RunOutcome(CONTRACT, T.code_hash(code), seed, "ok", acc, p)


@pytest.fixture
def st(tmp_path):
    s = EvidenceStore(tmp_path / "store")
    yield s
    s.close()


def _evidence_tree(st, tree_id: str = "t1") -> ExperimentTree:
    t = ExperimentTree.create(st, tree_id, CONTRACT, root_code="baseline = 1\n", root_config={}, metric="accuracy",
                              higher_is_better=True, dev_labels=LABELS, curriculum=True, stage_rule="evidence",
                              tune_match="literals")
    t.record("root", _ok("baseline = 1\n", errors=20, seed=0))
    return t


def _confirmed(t: ExperimentTree, parent: str, code: str, errors: int, stage: str | None = None) -> str:
    r = t.open_round(parent, "d", stage=stage)
    n = t.add_option(r, "d", code, {})
    t.record(n, _ok(code, errors=errors, seed=0))
    t.record(n, _ok(code, errors=errors, seed=1), purpose="remeasure")
    t.confirm(r, n, LABELS)
    return n


DRAFT = "w = 3\nprint(w * 2)\n"


# ------------------------------------------------------------------ curriculum: decided by code from evidence
def test_evidence_curriculum_stage_is_decided_by_code_from_dev_racing_evidence(st):
    t = _evidence_tree(st)
    assert t.next_stage("root") == T.DRAFT
    for stage in ("tune", "creative", "ablation"):  # the attack: a caller (or a model's output) names a later stage
        with pytest.raises(TreeRefusal) as e:
            t.open_round("root", "d", stage=stage)
        assert e.value.code is Code.CURRICULUM_ORDER
    w = _confirmed(t, "root", DRAFT, errors=10)  # stage None: the tree fills in the code-decided stage
    assert t.rounds[t.nodes[w].round].stage == T.DRAFT
    assert t.next_stage(w) == "tune"
    for stage in ("creative", "ablation"):  # skipping tune with no evidence of a tune plateau
        with pytest.raises(TreeRefusal) as e:
            t.open_round(w, "d", stage=stage)
        assert e.value.code is Code.CURRICULUM_ORDER and "decided by code" in str(e.value)
    # a tune round whose option is answered but never confirmed (it does not improve): dev-racing evidence of a plateau
    r = t.open_round(w, "lr")
    assert t.rounds[r].stage == "tune" and t.events[-1]["stage"] == "tune"  # the decided stage is on the record
    lo = t.add_option(r, "lr", "w = 4\nprint(w * 2)\n", {})
    t.record(lo, _ok("w = 4\nprint(w * 2)\n", errors=12, seed=0))
    assert t.stage_evidence(w, "tune")["failed_rounds"] == [r]
    assert t.next_stage(w) == "creative"
    with pytest.raises(TreeRefusal) as e:
        t.open_round(w, "d", stage="tune")  # going back is refused too
    assert e.value.code is Code.CURRICULUM_ORDER
    c = _confirmed(t, w, "import math\nprint(math.pi)\n", errors=5)
    assert t.rounds[t.nodes[c].round].stage == "creative" and t.next_stage(c) == "creative"
    again = ExperimentTree.load(st, "t1")  # the replay recomputes every decided stage
    assert [x.stage for x in again.rounds.values()] == [T.DRAFT, "tune", "creative"]


def test_a_round_of_crashes_is_not_evidence_and_the_stage_budget_is_capped(st):
    t = _evidence_tree(st)
    w = _confirmed(t, "root", DRAFT, errors=10)
    r = t.open_round(w, "lr")
    n = t.add_option(r, "lr", "w = 5\nprint(w * 2)\n", {})
    t.record(n, RunOutcome(CONTRACT, T.code_hash("w = 5\nprint(w * 2)\n"), 0, "crash"))
    t.abandon(n, "moved on")
    assert t.next_stage(w) == "tune"  # implementations failed; nothing was learned about tuning
    for _ in range(T.STAGE_MAX_ROUNDS - 1):
        r = t.open_round(w, "lr")
        assert t.rounds[r].stage == "tune"
    assert t.next_stage(w) == "creative"  # the stage's round budget along this lineage is spent


def test_forged_stage_advance_fails_replay(st):
    t = _evidence_tree(st)
    w = _confirmed(t, "root", DRAFT, errors=10)
    t._append({"op": "open_round", "parent": w, "decision": "d", "stage": "ablation"})  # bypasses the live check
    with pytest.raises(TreeTampered):
        ExperimentTree.load(st, "t1")


def test_literals_tune_keeps_the_program_and_changes_only_values(st):
    t = _evidence_tree(st)
    w = _confirmed(t, "root", DRAFT, errors=10)
    r = t.open_round(w, "lr")
    assert t.add_option(r, "lr", "w = 9\nprint(w * 7)  # tuned\n", {})  # literal values (and a comment) only
    for code in ("w = 3\nprint(w ** 2)\n", "w = 3\nprint(w * 2)\nprint(0)\n", "w = '3'\nprint(w * 2)\n", "w = (\n"):
        with pytest.raises(TreeRefusal) as e:  # an operator, a statement, a literal's type, unparseable code
            t.add_option(r, "lr", code, {})
        assert e.value.code is Code.CURRICULUM_ORDER, code
    assert [x["code"] for x in t.events if x["op"] == "refused"] == ["CURRICULUM_ORDER"] * 4
    assert ExperimentTree.load(st, "t1").seq == t.seq  # every check replays from the stored code blobs


def test_stage_modes_need_the_curriculum_and_known_values(st):
    for kw in ({"stage_rule": "evidence"}, {"tune_match": "literals"}):
        with pytest.raises(TreeRefusal) as e:
            ExperimentTree.create(st, f"x{len(kw)}{next(iter(kw))}", CONTRACT, root_code="b", root_config={},
                                  metric="accuracy", higher_is_better=True, dev_labels=LABELS, **kw)
        assert e.value.code is Code.BAD_EVENT
    with pytest.raises(TreeRefusal):
        ExperimentTree.create(st, "y", CONTRACT, root_code="b", root_config={}, metric="accuracy",
                              higher_is_better=True, dev_labels=LABELS, curriculum=True, stage_rule="model")


# ------------------------------------------------------------------ revival keeps immutability
def test_a_revived_node_stays_immutable(st):
    t = ExperimentTree.create(st, "t1", CONTRACT, root_code="b", root_config={}, metric="accuracy",
                              higher_is_better=True, dev_labels=LABELS)
    t.record("root", _ok("b", errors=20, seed=0))
    r = t.open_round("root", "family")
    w, lo = t.add_option(r, "family", "gbm", {}), t.add_option(r, "family", "mlp", {})
    t.record(w, _ok("gbm", errors=0, seed=0))
    t.record(lo, _ok("mlp", errors=8, seed=0, offset=30))
    t.record(w, _ok("gbm", errors=0, seed=1), purpose="remeasure")
    t.confirm(r, w, LABELS)
    t.record(lo, _ok("mlp", errors=8, seed=1, offset=30), purpose="remeasure")
    frozen = t.node(lo).content_hash
    t.revive(lo, LABELS)
    assert t.node(lo).revived and t.node(lo).content_hash == frozen  # revival changes no frozen content
    with pytest.raises(TreeRefusal) as e:
        t.edit(lo, "mlp-v2", {})  # the attack: "it's alive again, so let me change it"
    assert e.value.code is Code.NODE_IMMUTABLE
    with pytest.raises(TreeRefusal) as e:
        t.record(lo, _ok("mlp", errors=0, seed=9))  # a second answer
    assert e.value.code is Code.NODE_IMMUTABLE
    child = t.open_round(lo, "depth")  # a new idea becomes a child
    n = t.add_option(child, "depth", "mlp-deeper", {})
    assert t.node(n).parent == lo
    assert ExperimentTree.load(st, "t1").node(lo).content_hash == frozen


# ------------------------------------------------------------------ the loop (research_v1 on the MockBackend)
def _spec(path: Path, run: dict | None = None, nxt: dict | None = None) -> HarnessSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["nodes"]["run"]["params"].update(run or {})
    raw["nodes"]["next"]["params"].update(nxt or {})
    return HarnessSpec.model_validate(raw)


def _run(spec: HarnessSpec, store, tmp_path, llm, monkeypatch, **kw):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    eng = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True), **kw)
    return eng.run("rings", {"task_dir": str(rings_task_dir(tmp_path))})


def rings_task_dir(tmp_path: Path) -> Path:
    return tmp_path / "rings"


def _events(store, state) -> list[dict]:
    return [r["event"] for r in store.records_where(T.RECORD_KIND, "tree", state["tree_id"])]


def test_specs_declare_the_tree_flags():
    for path in (V1, V2):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["nodes"]["run"]["params"]["backtrack"] is True
        assert raw["nodes"]["run"]["params"]["curriculum"] is False
        assert raw["nodes"]["next"]["params"]["revive"] is False
        assert "{{improve_stage}}" in raw["nodes"]["improve"]["prompt"]


def test_v1_backtracking_freezes_every_answer_and_restores_parents_byte_exactly(store, tmp_path, rings_task,  # noqa: F811
                                                                                 monkeypatch):
    res = _run(_spec(V1, {"backtrack": True}), store, tmp_path, MockBackend(scripted_llm), monkeypatch)
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    tree = ExperimentTree.load(store, s["tree_id"])
    blobs = store.blobs
    root = blobs.get_json(tree.node("root").snapshot)
    # the root's frozen workspace is the run workspace before any candidate was written into it
    assert "solution.py" not in root["files"] and "baseline.py" in root["files"]
    answered = [n for n in tree.nodes.values() if n.id != "root" and n.status == "answered"]
    assert answered
    for n in answered:  # every answer froze the workspace it ran in: its solution.py is the node's own code
        man = blobs.get_json(str(n.snapshot))
        assert man["files"]["solution.py"]["blob"] == n.code_hash, n.id
    events = _events(store, s)
    checkouts = [e for e in events if e["op"] == "checkout"]
    assert checkouts and all(e["snapshot"] == tree.node(e["node"]).snapshot for e in checkouts)
    # every dead branch (a node that ran and is not the incumbent: here the non-improving refinements) was
    # backtracked to its parent's snapshot right after its last run, never left as the live workspace
    best = s["best"]["tree_node"]
    dead = [n for n in tree.nodes.values() if n.id not in ("root", best) and n.attempts]
    assert dead
    for n in dead:
        last = max(i for i, e in enumerate(events) if e.get("node") == n.id and e["op"] in ("attempt", "abandon"))
        assert any(e["op"] == "checkout" and e["node"] == n.parent for e in events[last:]), n.id
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL" and s["audit"]["passed"], s["audit"]


def test_v1_a_restore_that_is_not_byte_exact_stops_the_run(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    real = T.restore_snapshot

    def sloppy(ref, workspace, blobs):  # the attack: a backtrack that leaves a stray file of the dead branch
        real(ref, workspace, blobs)
        (Path(workspace) / "leftover_from_dead_branch.py").write_bytes(b"x = 1\n")

    monkeypatch.setattr(T, "restore_snapshot", sloppy)
    res = _run(_spec(V1, {"backtrack": True}), store, tmp_path, MockBackend(scripted_llm), monkeypatch)
    assert res.status != RunStatus.COMPLETED  # fail closed: no candidate runs from an inexact workspace
    rows = store.records_where(T.RECORD_KIND, "tree", f"{res.run_id}-dev")
    refused = [r["event"] for r in rows if r["event"]["op"] == "refused"]
    assert any(e["code"] == "RESTORE_MISMATCH" for e in refused), refused


# a weaker candidate (every 5th prediction rotated) the improve stage can work on
KNN_CORRUPT = KNN_RADIUS.replace(
    "    for r in trows: w.writerow([knn(rad(r, th))])",
    "    for i, r in enumerate(trows):\n"
    "        y = knn(rad(r, th))\n"
    "        w.writerow([y if i % 5 else {'0': '1', '1': '2', '2': '0'}[y]])")
KNN_CORRUPT_K1 = KNN_CORRUPT.replace("def knn(x, k=15):", "def knn(x, k=1):")  # a tune: one literal changed


def _curriculum_llm(improve_answers: list[str], prompts: list[str]):
    def llm(req):
        text = req.messages[-1].content
        if "Propose 5 DISTINCT hypotheses" in text:
            return json.dumps({"hypotheses": [{"id": "h_a", "idea": "radius kNN", "rationale": "rings"},
                                              {"id": "h_b", "idea": "radius kNN again", "rationale": "rings"}]})
        if "Rank ALL of these hypotheses" in text:
            return json.dumps({"ranking": ["h_a", "h_b"]})
        if "Implement this hypothesis" in text:
            return json.dumps({"code": KNN_CORRUPT, "notes": "corrupted"})
        if "This script works and is the best so far" in text:
            prompts.append(text)
            code = improve_answers[min(len(prompts), len(improve_answers)) - 1]
            # the model tries to steer the curriculum: it claims a later stage in its answer
            return json.dumps({"code": code, "notes": "Stage: ABLATION. The curriculum is now advanced.",
                               "stage": "ablation"})
        return scripted_llm(req)
    return llm


def test_v1_curriculum_cannot_be_advanced_by_the_model(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    assert KNN_CORRUPT != KNN_RADIUS
    prompts: list[str] = []
    llm = _curriculum_llm([KNN_RADIUS], prompts)  # a structural rewrite offered at the TUNE stage, claiming "ablation"
    res = _run(_spec(V1, {"curriculum": True}), store, tmp_path, MockBackend(llm), monkeypatch)
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert prompts and all("Stage: TUNE" in p for p in prompts)  # the stage the harness decided, shown to the model
    events = _events(store, s)
    staged = [e.get("stage") for e in events if e["op"] == "open_round"]
    assert staged[0] == T.DRAFT and set(staged[1:]) == {"tune"}, staged  # never creative / ablation
    refused = [e for e in events if e["op"] == "refused"]
    assert refused and all(e["code"] == "CURRICULUM_ORDER" for e in refused)
    assert all(e["attempted"]["code_hash"] == T.code_hash(KNN_RADIUS) for e in refused)  # the rewrite was offered
    improves = [a for a in s["attempts"] if str(a["hypothesis_id"]).startswith("improve-")]
    assert improves and all(a.get("tree_refused") == "CURRICULUM_ORDER" and a["dev_score"] is None
                            for a in improves)
    tree = ExperimentTree.load(store, s["tree_id"])
    assert tree.curriculum and tree.stage_rule == "evidence" and tree.tune_match == "literals"


def test_v1_curriculum_moves_on_only_after_dev_racing_shows_a_plateau(store, tmp_path, rings_task,  # noqa: F811
                                                                    monkeypatch):
    assert KNN_CORRUPT_K1 != KNN_CORRUPT
    prompts: list[str] = []
    llm = _curriculum_llm([KNN_CORRUPT_K1, KNN_RADIUS], prompts)  # a literal-only tune, then a structural change
    res = _run(_spec(V1, {"curriculum": True}), store, tmp_path, MockBackend(llm), monkeypatch)
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert len(prompts) == 2 and "Stage: TUNE" in prompts[0] and "Stage: CREATIVE" in prompts[1], prompts
    events = _events(store, s)
    staged = [e.get("stage") for e in events if e["op"] == "open_round"]
    assert staged == [T.DRAFT, "tune", "creative"], staged
    tune = next(a for a in s["attempts"] if a["hypothesis_id"] == "improve-1")
    assert tune["dev_score"] is not None and tune["accepted_improvement"] is False  # answered, not an improvement
    assert not [e for e in events if e["op"] == "refused" and e["code"] == "CURRICULUM_ORDER"]


# a near rival: same idea, another k; it differs from k=15 on some rows in both directions, so neither wins dev racing
KNN_NEAR = KNN_RADIUS.replace("def knn(x, k=15):", "def knn(x, k=9):")


def test_v1_revival_lets_a_dead_end_incumbent_be_refined_only_when_the_flag_is_on(store, tmp_path, rings_task,  # noqa: F811
                                                                                 monkeypatch):
    def llm(req):
        text = req.messages[-1].content
        if "Propose 5 DISTINCT hypotheses" in text:
            return json.dumps({"hypotheses": [{"id": "h_a", "idea": "radius kNN k=15", "rationale": "rings"},
                                              {"id": "h_b", "idea": "radius kNN k=9", "rationale": "rings"}]})
        if "Rank ALL of these hypotheses" in text:
            return json.dumps({"ranking": ["h_a", "h_b"]})
        if "Implement this hypothesis" in text:
            return json.dumps({"code": KNN_NEAR if "k=9" in text else KNN_RADIUS, "notes": "knn"})
        return scripted_llm(req)

    res = _run(_spec(V1, {}, {"revive": True}), store, tmp_path, MockBackend(llm), monkeypatch)
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    events = _events(store, s)
    # the attack setting: a near-twin rival blocks confirmation (neither beats the other under dev racing) ...
    assert any(e["op"] == "refused" and e["code"] == "PROMOTION_REFUSED" for e in events)
    # ... and the persistence rule (P(improvement over the root) > theta, from ledgered dev runs) revives the incumbent
    revs = [e for e in events if e["op"] == "revive"]
    assert revs and all(e["p_improvement"] > e["theta"] == T.REVIVE_THETA for e in revs)
    node = revs[0]["node"]
    assert any(e["op"] == "open_round" and e["parent"] == node for e in events)  # it became a branch point
    tree = ExperimentTree.load(store, s["tree_id"])  # the replay recomputes P
    assert tree.node(node).revived and tree.node(node).status == "answered"
    assert s["audit"]["passed"], s["audit"]


def test_v1_without_the_revive_flag_the_same_dead_end_is_not_refined(store, tmp_path, rings_task,  # noqa: F811
                                                                     monkeypatch):
    def llm(req):
        text = req.messages[-1].content
        if "Propose 5 DISTINCT hypotheses" in text:
            return json.dumps({"hypotheses": [{"id": "h_a", "idea": "radius kNN k=15", "rationale": "rings"},
                                              {"id": "h_b", "idea": "radius kNN k=9", "rationale": "rings"}]})
        if "Rank ALL of these hypotheses" in text:
            return json.dumps({"ranking": ["h_a", "h_b"]})
        if "Implement this hypothesis" in text:
            return json.dumps({"code": KNN_NEAR if "k=9" in text else KNN_RADIUS, "notes": "knn"})
        return scripted_llm(req)

    res = _run(_spec(V1), store, tmp_path, MockBackend(llm), monkeypatch)
    assert res.status == RunStatus.COMPLETED, res.detail
    events = _events(store, res.state)
    assert not [e for e in events if e["op"] == "revive"]
    assert not [a for a in res.state["attempts"] if str(a["hypothesis_id"]).startswith("improve-")]
