"""Experiment-tree discipline enforced in code (GOAL §5 L4.7 "Node lifecycle and tree shape"; research/tree.py).

Attack tests check BOTH that the attack really happened (the forbidden input reached the tree: different contract,
different code, an edit of a frozen node...) and that it was refused AND logged as a `refused` event."""

from __future__ import annotations

import json

import pytest

from custom_harness.research import tree as T
from custom_harness.research.tree import Code, ExperimentTree, RunContract, RunOutcome, TreeRefusal, TreeTampered
from custom_harness.store import EvidenceStore

N = 40
LABELS = ["a" if i % 2 else "b" for i in range(N)]


def preds_with_errors(k: int) -> list[str]:
    """Dev predictions with exactly k errors (the first k rows flipped)."""
    return [("b" if y == "a" else "a") if i < k else y for i, y in enumerate(LABELS)]


CONTRACT = RunContract.make(["python", "solution.py", "--train", "train.csv"], {"PYTHONHASHSEED": "0"},
                            {"train.csv": "1" * 64}, {"timeout_s": 600, "cpu_s": 600})


@pytest.fixture
def st(tmp_path):
    s = EvidenceStore(tmp_path / "store")
    yield s
    s.close()


def new_tree(st, tree_id="t1") -> ExperimentTree:
    t = ExperimentTree.create(st, tree_id, CONTRACT, root_code="baseline", root_config={}, metric="accuracy",
                              higher_is_better=True, dev_labels=LABELS)
    t.record("root", ok("baseline", errors=20, seed=0))
    return t


def ok(code: str, *, errors: int, seed: int, contract: RunContract = CONTRACT) -> RunOutcome:
    p = preds_with_errors(errors)
    acc = sum(a == b for a, b in zip(p, LABELS, strict=True)) / N
    return RunOutcome(contract, T.code_hash(code), seed, "ok", acc, p)


def fail(code: str, kind: str = "crash", seed: int = 0) -> RunOutcome:
    return RunOutcome(CONTRACT, T.code_hash(code), seed, kind)


def refused(t: ExperimentTree) -> list[dict]:
    return [e for e in t.events if e["op"] == "refused"]


def logged(st, tree_id="t1") -> list[dict]:
    return st.records_where(T.RECORD_KIND, "tree", tree_id)


# ------------------------------------------------------------------ (1) answered nodes are immutable
def test_attack_edit_of_answered_node_is_refused_and_logged(st):
    t = new_tree(st)
    r = t.open_round("root", "model family")
    n = t.add_option(r, "model family", "gbm", {"model": "gbm"})
    t.record(n, ok("gbm", errors=5, seed=0))
    frozen = t.node(n)
    assert frozen.status == "answered" and frozen.content_hash
    with pytest.raises(TreeRefusal) as e:
        t.edit(n, "gbm-tuned", {"model": "gbm", "depth": 9})
    assert e.value.code is Code.NODE_IMMUTABLE
    # the attack reached the tree (a different code hash for an answered node) and is on the record
    (rec,) = refused(t)
    assert rec["code"] == "NODE_IMMUTABLE" and rec["attempted"]["op"] == "edit"
    assert rec["attempted"]["code_hash"] == T.code_hash("gbm-tuned") != frozen.code_hash
    assert t.node(n) == frozen  # nothing changed, in memory or after a replay
    assert ExperimentTree.load(st, "t1").node(n) == frozen
    assert logged(st)[-1]["event"]["op"] == "refused"


def test_attack_second_answer_on_answered_node_is_refused(st):
    t = new_tree(st)
    with pytest.raises(TreeRefusal) as e:
        t.record("root", ok("baseline", errors=0, seed=7))  # a better "answer" for an already answered node
    assert e.value.code is Code.NODE_IMMUTABLE
    assert refused(t)[0]["attempted"]["purpose"] == "answer"
    assert t.node("root").metric == 0.5


def test_answer_freezes_a_content_hash_that_replay_recomputes(st):
    t = new_tree(st)
    h = t.node("root").content_hash
    ans = next(r["event"] for r in logged(st) if r["event"]["op"] == "attempt")
    assert ans["content_hash"] == h
    assert ExperimentTree.load(st, "t1").node("root").content_hash == h


# ------------------------------------------------------------------ (2) one run contract
@pytest.mark.parametrize("field,changed", [
    ("env", RunContract.make(CONTRACT.command, {"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "64"},
                             dict(CONTRACT.data_inputs), dict(CONTRACT.budget))),
    ("command", RunContract.make([*CONTRACT.command, "--epochs", "100"], dict(CONTRACT.env),
                                 dict(CONTRACT.data_inputs), dict(CONTRACT.budget))),
    ("data_inputs", RunContract.make(CONTRACT.command, dict(CONTRACT.env), {"train.csv": "2" * 64},
                                     dict(CONTRACT.budget))),
    ("budget", RunContract.make(CONTRACT.command, dict(CONTRACT.env), dict(CONTRACT.data_inputs),
                                {"timeout_s": 6000, "cpu_s": 600})),
])
def test_attack_sibling_run_under_a_different_contract_is_refused(st, field, changed):
    t = new_tree(st)
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "f1", {"features": 1})
    b = t.add_option(r, "features", "f2", {"features": 2})
    t.record(a, ok("f1", errors=10, seed=0))
    run = ok("f2", errors=0, seed=0, contract=changed)
    assert getattr(run.contract, field) != getattr(CONTRACT, field)  # the attack: only `field` differs
    assert run.contract.digest() != CONTRACT.digest()
    with pytest.raises(TreeRefusal) as e:
        t.record(b, run)
    assert e.value.code is Code.CONTRACT_MISMATCH
    assert refused(t)[0]["attempted"]["contract"] == changed.digest()
    assert t.node(b).status == "provisional" and t.node(b).n_attempts == 0


def test_run_hands_the_runner_the_tree_contract_only(st):
    t = new_tree(st)
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "f1", {})
    seen = []

    def runner(contract, ch, seed):
        seen.append((contract, ch, seed))
        return RunOutcome(contract, ch, seed, "ok", 1.0, list(LABELS))

    t.run(a, runner, seed=3)
    assert seen == [(CONTRACT, T.code_hash("f1"), 3)]


def test_attack_run_of_uncommitted_code_is_refused(st):
    t = new_tree(st)
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "f1", {})
    run = ok("f1 plus a quiet local change", errors=0, seed=0)
    assert run.code_hash != t.node(a).code_hash
    with pytest.raises(TreeRefusal) as e:
        t.record(a, run)
    assert e.value.code is Code.CODE_MISMATCH and refused(t)[0]["attempted"]["code_hash"] == run.code_hash
    assert t.node(a).n_attempts == 0


# ------------------------------------------------------------------ (3) ledgered, capped repairs
def test_attack_rerun_after_failure_without_ledgered_repair_is_refused(st):
    t = new_tree(st)
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "f1", {})
    t.record(a, fail("f1", "oom"))
    with pytest.raises(TreeRefusal) as e:  # just run it again until it works, no repair on the record
        t.record(a, ok("f1", errors=0, seed=1))
    assert e.value.code is Code.REPAIR_UNLEDGERED
    with pytest.raises(TreeRefusal) as e2:  # an edit that does not cite the failure it repairs
        t.edit(a, "f1-fixed", {})
    assert e2.value.code is Code.REPAIR_UNLEDGERED
    assert [x["attempted"]["op"] for x in refused(t)] == ["attempt", "edit"]
    t.edit(a, "f1-fixed", {}, repair_of=1)  # the ledgered repair
    t.record(a, ok("f1-fixed", errors=0, seed=1))
    v = t.node(a)
    assert v.status == "answered" and v.repairs == 1 and v.n_attempts == 2
    sel = t.selection_record(r)
    assert sel["attempts"] == 2 and sel["failed_attempts"] == 1 and sel["repairs"] == 1 and sel["refused"] == 2


def test_attack_edit_after_a_successful_run_without_failure_is_refused(st):
    t = new_tree(st)
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "f1", {})
    t.record(a, fail("f1", "timeout"))
    t.edit(a, "f1b", {}, repair_of=1)
    with pytest.raises(TreeRefusal) as e:  # the same failure cannot pay for a second edit
        t.edit(a, "f1c", {}, repair_of=1)
    assert e.value.code is Code.REPAIR_UNLEDGERED and t.node(a).code_hash == T.code_hash("f1b")


def test_repairs_are_capped_and_every_attempt_counts(st):
    t = new_tree(st)
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "v0", {})
    t.record(a, fail("v0", "crash"))
    for k in range(1, T.MAX_REPAIRS + 1):
        t.edit(a, f"v{k}", {}, repair_of=k)
        t.record(a, fail(f"v{k}", "missing_dep", seed=k))
    assert t.node(a).status == "abandoned" and t.node(a).repairs == T.MAX_REPAIRS
    with pytest.raises(TreeRefusal) as e:
        t.edit(a, "v-more", {}, repair_of=T.MAX_REPAIRS + 1)
    assert e.value.code is Code.NODE_ABANDONED
    sel = t.selection_record(r)
    assert sel["attempts"] == T.MAX_REPAIRS + 1 == sel["failed_attempts"]
    assert sel["abandoned"] == 1 and sel["refused"] == 1


def test_free_edits_before_any_run_are_drafting_not_repairs(st):
    t = new_tree(st)
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "draft", {})
    t.edit(a, "draft2", {})
    with pytest.raises(TreeRefusal) as e:
        t.edit(a, "draft3", {}, repair_of=1)  # cites a failure that does not exist
    assert e.value.code is Code.REPAIR_UNLEDGERED
    assert t.node(a).repairs == 0 and t.node(a).code_hash == T.code_hash("draft2")


# ------------------------------------------------------------------ (4) rounds and promotion
def _round_with_winner(t: ExperimentTree, parent: str = "root", winner_errors: int = 2, rival_errors: int = 12):
    r = t.open_round(parent, "model family")
    w = t.add_option(r, "model family", f"win-{parent}", {})
    x = t.add_option(r, "model family", f"lose-{parent}", {})
    t.record(w, ok(f"win-{parent}", errors=winner_errors, seed=0))
    t.record(x, ok(f"lose-{parent}", errors=rival_errors, seed=0))
    return r, w, x


def test_attack_promotion_on_a_single_run_is_refused(st):
    t = new_tree(st)
    r, w, _ = _round_with_winner(t)
    assert t.node(w).n_attempts == 1  # the attack: one run, however clear the win
    with pytest.raises(TreeRefusal) as e:
        t.confirm(r, w, LABELS)
    assert e.value.code is Code.PROMOTION_REFUSED
    with pytest.raises(TreeRefusal) as e2:  # and no round may descend from the unconfirmed node
        t.open_round(w, "learning rate")
    assert e2.value.code is Code.PROMOTION_REFUSED
    assert [x["attempted"]["op"] for x in refused(t)] == ["confirm", "open_round"]
    t.record(w, ok("win-root", errors=3, seed=1), purpose="remeasure")
    out = t.confirm(r, w, LABELS)
    assert out["winner"] == w and out["selection"]["remeasures"] == 1 and out["selection"]["refused"] == 1
    assert t.open_round(w, "learning rate")


def test_a_remeasure_that_loses_blocks_promotion(st):
    t = new_tree(st)
    r, w, _ = _round_with_winner(t)
    t.record(w, ok("win-root", errors=12, seed=1), purpose="remeasure")  # lucky first run, ordinary re-measure
    with pytest.raises(TreeRefusal) as e:
        t.confirm(r, w, LABELS)
    assert e.value.code is Code.PROMOTION_REFUSED and "re-measure" in str(e.value)
    t.record(w, ok("win-root", errors=0, seed=2), purpose="remeasure")  # shopping: more re-measures never help
    with pytest.raises(TreeRefusal):
        t.confirm(r, w, LABELS)


def test_remeasure_needs_a_fresh_seed_and_an_answered_node(st):
    t = new_tree(st)
    _, w, _ = _round_with_winner(t)
    with pytest.raises(TreeRefusal) as e:
        t.record(w, ok("win-root", errors=2, seed=0), purpose="remeasure")
    assert e.value.code is Code.REMEASURE_INVALID
    r2 = t.open_round("root", "features")
    p = t.add_option(r2, "features", "p", {})
    with pytest.raises(TreeRefusal) as e2:
        t.record(p, ok("p", errors=2, seed=5), purpose="remeasure")
    assert e2.value.code is Code.REMEASURE_INVALID


def test_winner_must_beat_the_parent_and_every_rival(st):
    t = new_tree(st)
    r = t.open_round("root", "model family")
    w = t.add_option(r, "model family", "w", {})
    x = t.add_option(r, "model family", "x", {})
    t.record(w, ok("w", errors=19, seed=0))  # no better than the root (20 errors)
    t.record(x, ok("x", errors=19, seed=0))
    t.record(w, ok("w", errors=19, seed=1), purpose="remeasure")
    with pytest.raises(TreeRefusal) as e:
        t.confirm(r, w, LABELS)
    assert e.value.code is Code.PROMOTION_REFUSED and "root@" in str(e.value)


def test_confirm_refuses_provisional_options_and_other_labels(st):
    t = new_tree(st)
    r, w, _ = _round_with_winner(t)
    t.add_option(r, "model family", "late", {})
    t.record(w, ok("win-root", errors=2, seed=1), purpose="remeasure")
    with pytest.raises(TreeRefusal, match="provisional"):
        t.confirm(r, w, LABELS)
    t2 = new_tree(st, "t2")
    r2, w2, _ = _round_with_winner(t2)
    t2.record(w2, ok("win-root", errors=2, seed=1), purpose="remeasure")
    with pytest.raises(TreeRefusal, match="dev labels"):
        t2.confirm(r2, w2, preds_with_errors(2))  # labels chosen to make the winner look perfect


def test_a_round_is_one_decision_with_few_options(st):
    t = new_tree(st)
    r = t.open_round("root", "model family")
    with pytest.raises(TreeRefusal) as e:
        t.add_option(r, "learning rate", "lr", {})
    assert e.value.code is Code.ROUND_INVALID
    for i in range(T.ROUND_MAX_OPTIONS):
        t.add_option(r, "model family", f"m{i}", {})
    with pytest.raises(TreeRefusal, match="options"):
        t.add_option(r, "model family", "one-more", {})
    with pytest.raises(TreeRefusal):
        t.open_round("root", "  ")


# ------------------------------------------------------------------ (5) shape diagnostics
def test_flat_fan_is_detected_and_logged(st):
    t = new_tree(st)
    for d in range(3):
        r = t.open_round("root", f"decision {d}")
        t.add_option(r, f"decision {d}", f"a{d}", {})
        t.add_option(r, f"decision {d}", f"b{d}", {})
    rep = t.report()
    assert rep["shape"]["flat_fan"] is True and rep["shape"]["noodle"] is False
    assert logged(st)[-1]["event"] == {"op": "report", "shape": rep["shape"]}
    assert "flat fan" in "\n".join(t.report_lines())


def test_noodle_is_detected_and_logged(st):
    t = new_tree(st)
    parent, errors = "root", 20
    for depth in range(T.NOODLE_MIN_CHAIN):
        errors -= 6
        r = t.open_round(parent, f"step {depth}")
        n = t.add_option(r, f"step {depth}", f"s{depth}", {})
        t.record(n, ok(f"s{depth}", errors=errors, seed=0))
        t.record(n, ok(f"s{depth}", errors=errors, seed=1), purpose="remeasure")
        t.confirm(r, n, LABELS)
        parent = n
    rep = t.report()
    assert rep["shape"]["noodle"] is True and rep["shape"]["flat_fan"] is False
    assert len(rep["shape"]["noodle_chains"][0]) == T.NOODLE_MIN_CHAIN
    assert "noodle" in "\n".join(t.report_lines())


def test_a_tree_of_rounds_is_neither_shape(st):
    t = new_tree(st)
    r, w, _ = _round_with_winner(t)
    t.record(w, ok("win-root", errors=2, seed=1), purpose="remeasure")
    t.confirm(r, w, LABELS)
    r2 = t.open_round(w, "learning rate")
    t.add_option(r2, "learning rate", "lr1", {})
    t.add_option(r2, "learning rate", "lr2", {})
    s = t.shape()
    assert not s["flat_fan"] and not s["noodle"] and s["depth"] == 2


# ------------------------------------------------------------------ the log: replay, forgery, staleness
def test_replay_reproduces_the_tree(st):
    t = new_tree(st)
    r, w, x = _round_with_winner(t)
    t.record(w, ok("win-root", errors=2, seed=1), purpose="remeasure")
    t.confirm(r, w, LABELS)
    u = ExperimentTree.load(st, "t1")
    assert u.head == t.head and [u.node(i) for i in ("root", w, x)] == [t.node(i) for i in ("root", w, x)]
    assert u.rounds[r].winner == w


def _forge(st, t: ExperimentTree, ev: dict) -> None:
    """Append a record with a valid chain hash, as a writer with store access could."""
    h = T._sha({"prev": t.head, "seq": t.seq + 1, "event": ev})
    st.append_record(T.RECORD_KIND, {"tree": t.id, "seq": t.seq + 1, "prev": t.head, "event": ev, "hash": h})


def test_attack_forged_edit_of_answered_node_fails_replay(st):
    t = new_tree(st)
    _forge(st, t, {"op": "edit", "node": "root", "code_hash": T.code_hash("other"), "config": {},
                   "repair_of": None})
    assert logged(st)[-1]["event"]["op"] == "edit"  # the forged record is in the store
    with pytest.raises(TreeTampered, match="NODE_IMMUTABLE"):
        ExperimentTree.load(st, "t1")


def test_attack_forged_confirm_without_remeasure_fails_replay(st):
    t = new_tree(st)
    r, w, _ = _round_with_winner(t)
    ref = st.blobs.put_json({"tree_dev_labels": LABELS})
    _forge(st, t, {"op": "confirm", "round": r, "winner": w, "labels_ref": ref})
    with pytest.raises(TreeTampered, match="PROMOTION_REFUSED"):
        ExperimentTree.load(st, "t1")


def test_attack_edited_or_deleted_record_fails_replay(st):
    t = new_tree(st)
    _, w, _ = _round_with_winner(t)
    rows = st._rows("SELECT rowid, body FROM records WHERE kind=?", (T.RECORD_KIND,))
    rowid, body = next((i, b) for i, b in rows if json.loads(b)["event"].get("metric") is not None
                       and json.loads(b)["event"].get("node") == w)
    d = json.loads(body)
    d["event"]["metric"] = 1.0
    st._exec("UPDATE records SET body=? WHERE rowid=?", (json.dumps(d), rowid))
    with pytest.raises(TreeTampered, match="edited"):
        ExperimentTree.load(st, "t1")
    st._exec("DELETE FROM records WHERE rowid=?", (rowid,))
    with pytest.raises(TreeTampered, match="chain"):
        ExperimentTree.load(st, "t1")


def test_a_stale_tree_object_cannot_write(st):
    t = new_tree(st)
    other = ExperimentTree.load(st, "t1")
    other.open_round("root", "features")
    with pytest.raises(TreeRefusal) as e:
        t.open_round("root", "model family")
    assert e.value.code is Code.STALE


def test_parameters_are_marked_uncalibrated():
    import inspect
    src = inspect.getsource(T)
    for name in ("MAX_REPAIRS", "ROUND_MAX_OPTIONS", "RACE_ALPHA", "RACE_N_BOOT", "FLAT_FAN_MIN_NODES",
                 "FLAT_FAN_ROOT_SHARE", "NOODLE_MIN_CHAIN"):
        line = next(ln for ln in src.splitlines() if ln.startswith(f"{name} = "))
        assert "# UNCALIBRATED (owner decision, GOAL §13.8)" in line, name


# ------------------------------------------------------------------ phase-2 wiring additions
def test_the_repair_cap_is_one_parameter_fixed_at_creation_and_replayed(st):
    """research_v1/v2 pass their run node's max_fixes as the tree's cap: the tree, not the loop, enforces it."""
    t = ExperimentTree.create(st, "cap0", CONTRACT, root_code="baseline", root_config={}, metric="accuracy",
                              higher_is_better=True, dev_labels=LABELS, max_repairs=0)
    t.record("root", ok("baseline", errors=20, seed=0))
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "v0", {})
    t.record(a, fail("v0"))
    assert t.node(a).status == "abandoned"  # cap 0: the first failure abandons the node (MAX_REPAIRS is 2)
    with pytest.raises(TreeRefusal) as e:
        t.edit(a, "v1", {}, repair_of=1)
    assert e.value.code is Code.NODE_ABANDONED
    assert ExperimentTree.load(st, "cap0").max_repairs == 0 and ExperimentTree.load(st, "cap0").node(a).status \
        == "abandoned"
    with pytest.raises(TreeRefusal):
        ExperimentTree.create(st, "capneg", CONTRACT, root_code="b", root_config={}, metric="accuracy",
                              higher_is_better=True, dev_labels=LABELS, max_repairs=-1)


def test_abandon_is_logged_and_never_withdraws_an_answered_node(st):
    t = new_tree(st)
    r = t.open_round("root", "features")
    a = t.add_option(r, "features", "a", {})
    b = t.add_option(r, "features", "b", {})
    t.record(a, ok("a", errors=2, seed=0))
    t.record(b, fail("b"))
    with pytest.raises(TreeRefusal) as e:  # the attack: withdraw an answered (losing or inconvenient) node
        t.abandon(a, "looks bad")
    assert e.value.code is Code.NODE_IMMUTABLE and refused(t)[-1]["attempted"]["op"] == "abandon"
    t.abandon(b, "no repair attempted")
    assert t.node(b).status == "abandoned"
    with pytest.raises(TreeRefusal):
        t.abandon(b, "again")
    t.record(a, ok("a", errors=2, seed=1), purpose="remeasure")
    assert t.confirm(r, a, LABELS)["winner"] == a  # no provisional option is left once b is abandoned
    assert ExperimentTree.load(st, "t1").node(b).status == "abandoned"


def test_an_exact_behavioural_twin_is_not_a_rival_but_a_near_twin_is(st):
    t = new_tree(st)
    r = t.open_round("root", "model family")
    w = t.add_option(r, "model family", "w", {})
    twin = t.add_option(r, "model family", "w-copy", {})  # different code, identical dev predictions
    t.record(w, ok("w", errors=2, seed=0))
    t.record(twin, ok("w-copy", errors=2, seed=0))
    t.record(w, ok("w", errors=2, seed=1), purpose="remeasure")
    out = t.confirm(r, w, LABELS)
    assert out["winner"] == w and out["duplicates"] == [twin]
    t2 = new_tree(st, "t2")
    r2 = t2.open_round("root", "model family")
    w2 = t2.add_option(r2, "model family", "w", {})
    near = t2.add_option(r2, "model family", "near", {})
    t2.record(w2, ok("w", errors=2, seed=0))
    t2.record(near, ok("near", errors=3, seed=0))  # differs on one row: a real rival, and w does not beat it
    t2.record(w2, ok("w", errors=2, seed=1), purpose="remeasure")
    with pytest.raises(TreeRefusal) as e:
        t2.confirm(r2, w2, LABELS)
    assert e.value.code is Code.PROMOTION_REFUSED and f"{near}@" in str(e.value)
