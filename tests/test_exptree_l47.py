"""GOAL §5 L4.7 extras in research/tree.py: exact backtracking via content-addressed workspace snapshots, revivable
dead-ends under a persistence rule (P(improvement) > θ from dev evidence, ledgered), and the tune → creative →
ablation curriculum as declared round types.

Attack tests assert that the attack reached the tree (the forbidden input was really offered) AND that it was refused
and logged as a `refused` event."""

from __future__ import annotations

import os

import pytest

from custom_harness.research import tree as T
from custom_harness.research.tree import Code, ExperimentTree, RunContract, RunOutcome, TreeRefusal
from custom_harness.sandbox.snapshot import take_snapshot
from custom_harness.store import EvidenceStore

N = 40
LABELS = ["a" if i % 2 else "b" for i in range(N)]
CONTRACT = RunContract.make(["python", "solution.py"], {"PYTHONHASHSEED": "0"}, {"train.csv": "1" * 64},
                            {"timeout_s": 600})


def preds_with_errors(k: int, offset: int = 0) -> list[str]:
    """Dev predictions with exactly k errors, on rows offset..offset+k-1."""
    return [("b" if y == "a" else "a") if offset <= i < offset + k else y for i, y in enumerate(LABELS)]


def ok(code: str, *, errors: int, seed: int, offset: int = 0, snapshot: str | None = None) -> RunOutcome:
    p = preds_with_errors(errors, offset)
    acc = sum(a == b for a, b in zip(p, LABELS, strict=True)) / N
    return RunOutcome(CONTRACT, T.code_hash(code), seed, "ok", acc, p, snapshot=snapshot)


@pytest.fixture
def st(tmp_path):
    s = EvidenceStore(tmp_path / "store")
    yield s
    s.close()


def refused(t: ExperimentTree) -> list[dict]:
    return [e for e in t.events if e["op"] == "refused"]


def new_tree(st, *, curriculum: bool = False, tree_id: str = "t1", root_errors: int = 20) -> ExperimentTree:
    t = ExperimentTree.create(st, tree_id, CONTRACT, root_code="baseline", root_config={}, metric="accuracy",
                              higher_is_better=True, dev_labels=LABELS, curriculum=curriculum)
    t.record("root", ok("baseline", errors=root_errors, seed=0))
    return t


# ------------------------------------------------------------------ (1) exact backtracking
def _write_workspace(ws, files: dict[str, bytes], empty_dirs=()) -> None:
    for rel, data in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for d in empty_dirs:
        (ws / d).mkdir(parents=True, exist_ok=True)


def _read_tree(ws) -> tuple[dict[str, bytes], list[str]]:
    files, dirs = {}, []
    for root, dirnames, filenames in os.walk(ws):
        for f in filenames:
            full = os.path.join(root, f)
            files[os.path.relpath(full, ws).replace("\\", "/")] = open(full, "rb").read()
        for d in dirnames:
            full = os.path.join(root, d)
            if not os.listdir(full):
                dirs.append(os.path.relpath(full, ws).replace("\\", "/"))
    return files, sorted(dirs)


def test_checkout_restores_an_answered_nodes_workspace_byte_exactly(st, tmp_path):
    ws = tmp_path / "ws"
    v1 = {"solution.py": b"print('gbm')\r\n", "data/feat.bin": bytes(range(256)) * 3, "notes.txt": b"\x00\xff\n"}
    _write_workspace(ws, v1, empty_dirs=["out/empty"])
    expected = _read_tree(ws)
    snap = take_snapshot(ws, st.blobs)

    t = new_tree(st)
    r = t.open_round("root", "model family")
    n = t.add_option(r, "model family", "gbm", {"model": "gbm"})
    t.record(n, ok("gbm", errors=5, seed=0, snapshot=snap))
    assert t.node(n).snapshot == snap

    # later work trashes the workspace: edits, deletions, new files, a removed empty dir
    (ws / "solution.py").write_bytes(b"print('mlp')\n")
    (ws / "data/feat.bin").unlink()
    (ws / "extra.py").write_bytes(b"x = 1\n")
    os.rmdir(ws / "out/empty")
    assert _read_tree(ws) != expected  # the backtrack has something to undo

    got = t.checkout(n, ws)
    assert got == snap
    assert _read_tree(ws) == expected  # byte-exact: every file's bytes, no extra file, the empty dir is back
    assert t.events[-1] == {"op": "checkout", "node": n, "snapshot": snap}

    # the same backtrack works from a fresh replay of the log
    (ws / "solution.py").write_bytes(b"changed again")
    ExperimentTree.load(st, "t1").checkout(n, ws)
    assert _read_tree(ws) == expected


def test_answer_binds_its_snapshot_into_the_content_hash(st, tmp_path):
    ws = tmp_path / "ws"
    _write_workspace(ws, {"solution.py": b"a"})
    s1 = take_snapshot(ws, st.blobs)
    _write_workspace(ws, {"solution.py": b"b"})
    s2 = take_snapshot(ws, st.blobs)
    assert s1 != s2
    t = new_tree(st)
    r = t.open_round("root", "model family")
    a = t.add_option(r, "model family", "same", {"k": 1})
    run = ok("same", errors=5, seed=0, snapshot=s1)
    t.record(a, run)
    ans = t.events[-1]
    unbound = {"id": a, "parent": "root", "round": r, "code_hash": T.code_hash("same"), "config": {"k": 1},
               "answer": {"idx": 1, "seed": 0, "metric": run.metric, "preds_ref": ans["preds_ref"]}}
    bound = {**unbound, "answer": {**unbound["answer"], "snapshot": s1}}
    # the frozen content hash covers the workspace snapshot: another workspace would be another content hash
    assert t.node(a).content_hash == T._sha(bound) != T._sha(unbound)
    assert T._sha({**unbound, "answer": {**unbound["answer"], "snapshot": s2}}) != T._sha(bound)
    assert ExperimentTree.load(st, "t1").node(a).content_hash == t.node(a).content_hash


def test_attack_answer_with_a_non_snapshot_blob_is_refused(st):
    t = new_tree(st)
    r = t.open_round("root", "model family")
    n = t.add_option(r, "model family", "gbm", {})
    fake = st.blobs.put_json({"kind": "not_a_snapshot", "files": {}})
    assert st.blobs.has(fake)  # the attack: a real blob, but not a workspace snapshot
    with pytest.raises(TreeRefusal) as e:
        t.record(n, ok("gbm", errors=5, seed=0, snapshot=fake))
    assert e.value.code is Code.BAD_EVENT
    assert refused(t)[-1]["attempted"]["snapshot"] == fake
    assert t.node(n).status == "provisional"


def test_checkout_refused_for_a_node_without_a_frozen_snapshot(st, tmp_path):
    t = new_tree(st)  # root answered WITHOUT a snapshot
    r = t.open_round("root", "model family")
    n = t.add_option(r, "model family", "gbm", {})  # provisional
    for node in ("root", n):
        with pytest.raises(TreeRefusal) as e:
            t.checkout(node, tmp_path / "ws")
        assert e.value.code is Code.NO_SNAPSHOT
    assert [x["attempted"]["node"] for x in refused(t)] == ["root", n]
    assert not (tmp_path / "ws").exists()


def test_checkout_of_corrupted_snapshot_fails_and_leaves_workspace_untouched(st, tmp_path):
    ws = tmp_path / "ws"
    _write_workspace(ws, {"solution.py": b"original", "big.bin": b"z" * 1000})
    snap = take_snapshot(ws, st.blobs)
    t = new_tree(st)
    r = t.open_round("root", "model family")
    n = t.add_option(r, "model family", "gbm", {})
    t.record(n, ok("gbm", errors=5, seed=0, snapshot=snap))
    blob = st.blobs.get_json(snap)["files"]["big.bin"]["blob"]
    st.blobs._path(blob).write_bytes(b"tampered")  # the attack: a stored blob no longer matches its hash
    (ws / "solution.py").write_bytes(b"current")
    with pytest.raises(T.SnapshotError):
        t.checkout(n, ws)
    assert (ws / "solution.py").read_bytes() == b"current"  # nothing half-restored


# ------------------------------------------------------------------ (2) revivable dead-ends
def _round_with_loser(t: ExperimentTree, *, loser_errors: int, loser_offset: int = 0) -> tuple[str, str, str]:
    """A round off the root whose winner (0 errors) is confirmed; the loser is answered and left behind."""
    r = t.open_round("root", "model family")
    w = t.add_option(r, "model family", "gbm", {"model": "gbm"})
    lo = t.add_option(r, "model family", "mlp", {"model": "mlp"})
    t.record(w, ok("gbm", errors=0, seed=0))
    t.record(lo, ok("mlp", errors=loser_errors, seed=0, offset=loser_offset))
    t.record(w, ok("gbm", errors=0, seed=1), purpose="remeasure")
    t.confirm(r, w, LABELS)
    return r, w, lo


def test_dead_end_cannot_branch_without_revival(st):
    t = new_tree(st)
    _, _, lo = _round_with_loser(t, loser_errors=12)
    with pytest.raises(TreeRefusal) as e:
        t.open_round(lo, "depth")
    assert e.value.code is Code.PROMOTION_REFUSED


def test_revival_refused_below_theta_and_ledgered(st):
    # the root makes 20 errors; this dead-end makes 19 on the SAME rows plus nothing else: it improves on the parent on
    # only one example, so P(improvement) from dev resampling is well below the default θ
    t = new_tree(st)
    _, _, lo = _round_with_loser(t, loser_errors=19)
    p = t.p_improvement(lo, LABELS)
    assert 0 < p < T.REVIVE_THETA  # the attack really offers a node with some, but too little, evidence
    with pytest.raises(TreeRefusal) as e:
        t.revive(lo, LABELS)
    assert e.value.code is Code.REVIVAL_REFUSED
    (rec,) = refused(t)
    assert rec["code"] == "REVIVAL_REFUSED" and rec["attempted"] == {"op": "revive", "node": lo,
                                                                   "labels_ref": rec["attempted"]["labels_ref"]}
    with pytest.raises(TreeRefusal):
        t.open_round(lo, "depth")  # still a dead-end
    assert ExperimentTree.load(st, "t1").selection_record()["revivals"] == 0


def test_revival_above_theta_is_ledgered_and_lets_the_dead_end_branch(st):
    t = new_tree(st)
    _, _, lo = _round_with_loser(t, loser_errors=8, loser_offset=30)  # clearly better than the 20-error parent
    out = t.revive(lo, LABELS)
    assert out["p_improvement"] > T.REVIVE_THETA
    ev = t.events[-1]
    assert ev["op"] == "revive" and ev["node"] == lo and ev["theta"] == T.REVIVE_THETA
    assert ev["p_improvement"] == out["p_improvement"] and ev["reference"] == "root"
    r2 = t.open_round(lo, "depth")  # the revived dead-end may now be a branch point
    assert r2
    again = ExperimentTree.load(st, "t1")  # the replay recomputes P and re-applies the persistence rule
    assert again.selection_record()["revivals"] == 1
    with pytest.raises(TreeRefusal) as e:
        t.revive(lo, LABELS)
    assert e.value.code is Code.REVIVAL_REFUSED  # once revived, it is not a dead-end any more


def test_revival_uses_every_remeasure_so_rerolling_cannot_help(st):
    t = new_tree(st)
    _, _, lo = _round_with_loser(t, loser_errors=8, loser_offset=30)
    p_before = t.p_improvement(lo, LABELS)
    t.record(lo, ok("mlp", errors=19, seed=5, offset=1), purpose="remeasure")  # a bad re-measure
    assert t.p_improvement(lo, LABELS) < min(p_before, T.REVIVE_THETA)  # min over runs: re-measures only lower P
    with pytest.raises(TreeRefusal) as e:
        t.revive(lo, LABELS)
    assert e.value.code is Code.REVIVAL_REFUSED


def test_revival_refused_without_dev_evidence_or_with_wrong_labels(st):
    t = new_tree(st)
    r = t.open_round("root", "model family")
    n = t.add_option(r, "model family", "gbm", {})
    t.abandon(n, "moved on")  # an abandoned node has no metric: no evidence, no revival (fail closed)
    with pytest.raises(TreeRefusal) as e:
        t.revive(n, LABELS)
    assert e.value.code is Code.REVIVAL_REFUSED
    t2 = new_tree(st, tree_id="t2")
    _, _, lo = _round_with_loser(t2, loser_errors=19)
    # the attack: "labels" equal to the dead-end's own predictions, under which it would look perfect
    wrong = preds_with_errors(19)
    assert wrong != LABELS and t2.p_improvement(lo, LABELS) < T.REVIVE_THETA
    with pytest.raises(TreeRefusal) as e:
        t2.revive(lo, wrong)
    assert e.value.code is Code.REVIVAL_REFUSED
    assert "dev labels" in str(e.value)


def test_forged_revival_record_does_not_replay(st):
    t = new_tree(st)
    _, _, lo = _round_with_loser(t, loser_errors=19)
    # forge a revive event that claims a high P directly into the log, bypassing the live check
    ref = st.blobs.put_json({"tree_dev_labels": LABELS})
    t._append({"op": "revive", "node": lo, "labels_ref": ref, "p_improvement": 0.99, "theta": T.REVIVE_THETA,
               "reference": "root"})
    with pytest.raises(T.TreeTampered):
        ExperimentTree.load(st, "t1")


# ------------------------------------------------------------------ (3) curriculum tune -> creative -> ablation
def _win(t: ExperimentTree, parent: str, stage: str, code: str, cfg: dict, errors: int) -> str:
    r = t.open_round(parent, f"{stage} decision", stage=stage)
    n = t.add_option(r, f"{stage} decision", code, cfg)
    t.record(n, ok(code, errors=errors, seed=0))
    t.record(n, ok(code, errors=errors, seed=1), purpose="remeasure")
    t.confirm(r, n, LABELS)
    return n


def test_curriculum_order_is_enforced(st):
    t = new_tree(st, curriculum=True)
    for stage in ("creative", "ablation"):  # the attack: skipping tune from the root
        with pytest.raises(TreeRefusal) as e:
            t.open_round("root", "x", stage=stage)
        assert e.value.code is Code.CURRICULUM_ORDER
    tuned = _win(t, "root", "tune", "baseline", {"lr": 0.1}, errors=14)
    with pytest.raises(TreeRefusal) as e:
        t.open_round(tuned, "x", stage="ablation")  # skipping creative
    assert e.value.code is Code.CURRICULUM_ORDER
    creative = _win(t, tuned, "creative", "new-arch", {"lr": 0.1}, errors=7)
    with pytest.raises(TreeRefusal) as e:
        t.open_round(creative, "x", stage="tune")  # going back
    assert e.value.code is Code.CURRICULUM_ORDER
    ablation = _win(t, creative, "ablation", "new-arch-minus-x", {"lr": 0.1}, errors=0)
    for stage in ("tune", "creative"):
        with pytest.raises(TreeRefusal) as e:
            t.open_round(ablation, "x", stage=stage)
        assert e.value.code is Code.CURRICULUM_ORDER
    codes = [x["code"] for x in refused(t)]
    assert codes == ["CURRICULUM_ORDER"] * 6
    assert [rnd.stage for rnd in ExperimentTree.load(st, "t1").rounds.values()] == ["tune", "creative", "ablation"]


def test_curriculum_tree_requires_a_declared_stage_and_known_stages(st):
    t = new_tree(st, curriculum=True)
    with pytest.raises(TreeRefusal) as e:
        t.open_round("root", "x")
    assert e.value.code is Code.ROUND_INVALID
    with pytest.raises(TreeRefusal) as e:
        t.open_round("root", "x", stage="explore")
    assert e.value.code is Code.ROUND_INVALID


def test_stage_on_a_tree_without_the_curriculum_is_refused(st):
    t = new_tree(st)  # curriculum not declared at creation: a stage label would claim an order nobody enforced
    with pytest.raises(TreeRefusal) as e:
        t.open_round("root", "x", stage="tune")
    assert e.value.code is Code.ROUND_INVALID
    assert t.open_round("root", "x")  # untyped rounds stay as before


def test_tune_round_options_keep_the_parents_code(st):
    t = new_tree(st, curriculum=True)
    r = t.open_round("root", "lr", stage="tune")
    assert t.add_option(r, "lr", "baseline", {"lr": 0.01})  # config-only change: a tune option
    with pytest.raises(TreeRefusal) as e:
        t.add_option(r, "lr", "baseline-with-new-layer", {"lr": 0.01})
    assert e.value.code is Code.CURRICULUM_ORDER
    assert refused(t)[-1]["attempted"]["code_hash"] != t.node("root").code_hash
    n = t.add_option(r, "lr", "baseline", {"lr": 0.02})
    with pytest.raises(TreeRefusal) as e:
        t.edit(n, "baseline-rewritten", {"lr": 0.02})
    assert e.value.code is Code.CURRICULUM_ORDER


def test_revived_dead_end_keeps_its_stage_in_the_curriculum(st):
    t = new_tree(st, curriculum=True)
    tuned = _win(t, "root", "tune", "baseline", {"lr": 0.1}, errors=14)
    r = t.open_round(tuned, "arch", stage="creative")
    w = t.add_option(r, "arch", "arch-a", {})
    lo = t.add_option(r, "arch", "arch-b", {})
    t.record(w, ok("arch-a", errors=0, seed=0))
    t.record(lo, ok("arch-b", errors=4, seed=0, offset=36))
    t.record(w, ok("arch-a", errors=0, seed=1), purpose="remeasure")
    t.confirm(r, w, LABELS)
    t.revive(lo, LABELS)
    with pytest.raises(TreeRefusal) as e:
        t.open_round(lo, "x", stage="tune")
    assert e.value.code is Code.CURRICULUM_ORDER
    assert t.open_round(lo, "x", stage="ablation")


def test_legacy_log_without_the_new_fields_replays_with_unchanged_hashes(st):
    """A tree logged before this change (create without curriculum/revive_theta, answers without a snapshot) replays
    with the defaults, and an answer's content hash is the pre-change formula (no `snapshot` key)."""
    old = ExperimentTree(st, "legacy")
    old._append({"op": "create", "contract": CONTRACT.to_json(), "root_code_hash": T.code_hash("baseline"),
                 "root_config": {}, "metric": "accuracy", "higher_is_better": True, "labels_sha": T._sha(LABELS),
                 "max_repairs": 2})
    t = ExperimentTree.load(st, "legacy")
    assert t.curriculum is False and t.revive_theta == T.REVIVE_THETA
    run = ok("baseline", errors=20, seed=0)
    t.record("root", run)
    ans = t.events[-1]
    assert "snapshot" not in ans or ans["snapshot"] is None
    pre_change = {"id": "root", "parent": None, "round": None, "code_hash": T.code_hash("baseline"), "config": {},
                  "answer": {"idx": 1, "seed": 0, "metric": run.metric, "preds_ref": ans["preds_ref"]}}
    assert t.node("root").content_hash == T._sha(pre_change)
    assert ExperimentTree.load(st, "legacy").node("root").content_hash == T._sha(pre_change)


def test_checkout_refuses_a_restore_that_is_not_byte_exact(st, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _write_workspace(ws, {"solution.py": b"exact"})
    snap = take_snapshot(ws, st.blobs)
    t = new_tree(st)
    r = t.open_round("root", "model family")
    n = t.add_option(r, "model family", "gbm", {})
    t.record(n, ok("gbm", errors=5, seed=0, snapshot=snap))
    real = T.restore_snapshot

    def sloppy(ref, workspace, blobs):  # the attack: a restore that leaves one stray byte behind
        real(ref, workspace, blobs)
        (workspace / "stray.txt").write_bytes(b"x")

    monkeypatch.setattr(T, "restore_snapshot", sloppy)
    with pytest.raises(TreeRefusal) as e:
        t.checkout(n, ws)
    assert e.value.code is Code.RESTORE_MISMATCH
    assert (ws / "stray.txt").exists()  # the stray file really was there when the check ran
    rec = refused(t)[-1]
    assert rec["code"] == "RESTORE_MISMATCH" and rec["attempted"]["snapshot"] == snap
    assert ExperimentTree.load(st, "t1").seq == t.seq  # the refusal record replays


def test_new_parameters_are_marked_uncalibrated():
    import inspect
    src = inspect.getsource(T)
    for name in ("REVIVE_THETA", "REVIVE_N_BOOT"):
        line = next(ln for ln in src.splitlines() if ln.startswith(f"{name} = "))
        assert "# UNCALIBRATED (owner decision, GOAL §13.8)" in line, name
