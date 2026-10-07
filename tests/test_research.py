"""End-to-end research loop with a scripted (mock) LLM and trusted, stdlib-only solution code."""

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.runtime.template import PrivateDataAccess, render
from custom_harness.sandbox import LocalSandbox
from custom_harness.verify.ranking import bradley_terry, rankings_to_wins
from custom_harness.verify.stats import bootstrap_diff_ci, t2_verdict

ROOT = Path(__file__).resolve().parents[1]

# A genuinely better stdlib solution for the rings task: kNN on radius (x1^2 + x2^2).
KNN_RADIUS = '''
import argparse, csv, math
p = argparse.ArgumentParser()
for k in ("--train", "--test", "--out"): p.add_argument(k)
p.add_argument("--seed", type=int)
a = p.parse_args()
def load(path):
    with open(path, newline="") as f:
        r = list(csv.reader(f))
    return r[0], r[1:]
h, rows = load(a.train); th, trows = load(a.test)
rad = lambda r, hh: math.hypot(float(r[hh.index("x1")]), float(r[hh.index("x2")]))
pts = sorted((rad(r, h), r[h.index("label")]) for r in rows)
def knn(x, k=15):
    near = sorted(pts, key=lambda t: abs(t[0] - x))[:k]
    votes = {}
    for _, y in near: votes[y] = votes.get(y, 0) + 1
    return max(sorted(votes), key=votes.get)
with open(a.out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["prediction"])
    for r in trows: w.writerow([knn(rad(r, th))])
'''


# An independently WRITTEN implementation of the same idea (what a replicator without the code would produce).
KNN_REPLICA = '''
import argparse
import csv
import math


def read(path):
    with open(path, newline="") as handle:
        data = list(csv.reader(handle))
    return data[0], data[1:]


def radius(row, cols):
    return math.sqrt(float(row[cols.index("x1")]) ** 2 + float(row[cols.index("x2")]) ** 2)


parser = argparse.ArgumentParser()
parser.add_argument("--train")
parser.add_argument("--test")
parser.add_argument("--out")
parser.add_argument("--seed", type=int)
args = parser.parse_args()
train_cols, train_rows = read(args.train)
test_cols, test_rows = read(args.test)
labelled = [(radius(r, train_cols), r[train_cols.index("label")]) for r in train_rows]
results = []
for r in test_rows:
    rr = radius(r, test_cols)
    neighbours = sorted(labelled, key=lambda item: abs(item[0] - rr))[:21]
    tally = {}
    for _, lab in neighbours:
        tally[lab] = tally.get(lab, 0) + 1
    results.append(sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0][0])
with open(args.out, "w", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["prediction"])
    for value in results:
        writer.writerow([value])
'''

BROKEN = "import sys; sys.exit('boom: ModuleNotFoundError: no module named sklearnx')"


@pytest.fixture
def rings_task(tmp_path):
    out = tmp_path / "rings"
    subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(out),
                    "--n-train", "400", "--n-test", "300"], check=True, capture_output=True)
    return out


def scripted_llm(req):
    text = req.messages[-1].content
    if "Propose 5 DISTINCT hypotheses" in text:
        return json.dumps({"hypotheses": [
            {"id": "h_radius", "idea": "kNN on radial feature", "rationale": "classes are rings"},
            {"id": "h_noise", "idea": "drop noise columns", "rationale": "n1..n4 are noise"}]})
    if "Rank ALL of these hypotheses" in text:
        return json.dumps({"ranking": ["h_noise", "h_radius"]})  # judge prefers the weaker idea
    if "Re-implement the method described below from scratch" in text:
        return json.dumps({"code": KNN_REPLICA, "notes": "replica from the description"})
    if "Ask at most 5 short questions" in text:
        return json.dumps({"questions": ["What would falsify the radius hypothesis?", "Use a random forest.",
                                         "Is the color column leaking the label?"]})
    if "This script works and is the best so far" in text:
        return json.dumps({"code": KNN_RADIUS, "notes": "no real change"})  # a non-improvement
    if "This script failed" in text:
        return json.dumps({"code": KNN_RADIUS, "notes": "rewrote"})
    if "Implement this hypothesis" in text:
        if "h_noise" in text:
            return json.dumps({"code": BROKEN, "notes": "first try"})
        return json.dumps({"code": KNN_RADIUS, "notes": "radius kNN"})
    raise AssertionError(f"unexpected prompt: {text[:200]}")


SPEC = ROOT / "examples/research_v1.yaml"


def test_research_loop_end_to_end(store, tmp_path, rings_task, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(scripted_llm)
    eng = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    # h_noise was tried first (judge ranking), failed, got fixed into a working script
    assert s["attempts"][0]["error"] and s["attempts"][0]["hypothesis_id"] == "h_noise"
    assert s["best"]["dev_score"] > s["baseline_dev"]
    # the claim was pre-registered BEFORE any held-out label was read (K2 is in the certificate)
    cert = s["certificate"]
    assert cert["label"] == "CERTIFIED-INTERNAL", cert
    assert cert["subject_mean"] > cert["comparator_mean"] and cert["effect"] > 0
    assert s["audit"]["passed"], s["audit"]
    assert s["publishable"] is True, s["publish_blockers"]
    assert "**CERTIFIED-INTERNAL** · tier T2" in s["report"] and "Reporting checklist" in s["report"]
    from custom_harness.kernel import Kernel
    from custom_harness.kernel.bundle import reproduce_bundle
    from custom_harness.kernel.execution import SandboxReplayer
    kernel = Kernel(store)
    assert kernel.status(cert["hash"]) == "active" and kernel.ledger.verify() == []
    rep = reproduce_bundle(Path(cert["bundle_path"]), lambda: SandboxReplayer(
        lambda key: LocalSandbox(tmp_path / "repro" / key, i_understand_no_isolation=True)))
    assert rep.reproduced, rep.problems
    # hidden labels never entered any prompt
    for req in llm.requests:
        for m in req.messages:
            assert "test_y" not in m.content and "_private" not in m.content


# A rule that is EXACTLY right on the relabelled training data below (label = 1 iff x1 > 0).
SIGN_RULE = '''
import argparse, csv
p = argparse.ArgumentParser()
for k in ("--train", "--test", "--out"): p.add_argument(k)
p.add_argument("--seed", type=int)
a = p.parse_args()
with open(a.test, newline="") as f:
    r = list(csv.reader(f))
h, rows = r[0], r[1:]
with open(a.out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["prediction"])
    for row in rows: w.writerow(["1" if float(row[h.index("x1")]) > 0 else "0"])
'''


def test_perfect_dev_predictions_are_not_a_leak(store, tmp_path, rings_task, monkeypatch):
    """Live run run_cd99288e65ba4a4f (wine): every candidate was perfect on dev, so its predictions blob had the same
    content hash as the private dev-labels blob; the trace scrubber redacted that "private value", and the claim
    was REJECTED by K9 and the audit failed I5. Knowing the hash of its own perfect predictions tells nothing that
    dev_score == 1.0 does not: a perfect candidate must not be flagged."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    train = rings_task / "public" / "train.csv"
    lines = train.read_text().splitlines()
    head = lines[0].split(",")
    xi, yi = head.index("x1"), head.index("label")
    rows = [line.split(",") for line in lines[1:]]
    for r in rows:
        r[yi] = "1" if float(r[xi]) > 0 else "0"
    train.write_text("\n".join([lines[0], *(",".join(r) for r in rows)]) + "\n", newline="")
    # a weak baseline (always "0"), so the perfect candidate beats it, a claim is made and K9 is actually checked
    (rings_task / "baseline.py").write_text(SIGN_RULE.replace('"1" if float(row[h.index("x1")]) > 0 else "0"', '"0"'),
                                           newline="")

    def llm_fn(req):
        text = req.messages[-1].content
        if "Implement this hypothesis" in text and "h_radius" in text:
            return json.dumps({"code": SIGN_RULE, "notes": "sign of x1"})
        return scripted_llm(req)

    llm = MockBackend(llm_fn)
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert any(a["dev_score"] == 1.0 for a in res.state["attempts"])  # the scenario really happened
    redacted = [s.index for s in store.trace(res.run_id) if s.output.get("_redacted_private_values")]
    assert redacted == [], f"perfect predictions redacted as private at steps {redacted}"
    assert res.state["audit"]["I5"] is True, res.state["audit"]
    assert "K9" not in res.state["certificate"]["failed"], res.state["certificate"]


# A dishonest solution: if the full public training file is reachable from its working directory, it copies the
# validation labels out of it (features -> label lookup); otherwise it predicts a constant.
CHEAT_DEV = '''
import argparse, csv, os
p = argparse.ArgumentParser()
for k in ("--train", "--test", "--out"): p.add_argument(k)
p.add_argument("--seed", type=int)
a = p.parse_args()
def load(path):
    with open(path, newline="") as f:
        r = list(csv.reader(f))
    return r[0], r[1:]
th, trows = load(a.test)
lookup = {}
if os.path.exists(os.path.join("data", "train.csv")):
    h, rows = load(os.path.join("data", "train.csv"))
    yi = h.index("label")
    lookup = {tuple(v for j, v in enumerate(r) if j != yi): r[yi] for r in rows}
with open(a.out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["prediction"])
    for r in trows: w.writerow([lookup.get(tuple(r), "0")])
'''


def _run_with_impl(store, tmp_path, task, code):
    def llm_fn(req):
        text = req.messages[-1].content
        if "Implement this hypothesis" in text and "h_radius" in text:
            return json.dumps({"code": code, "notes": "x"})
        return scripted_llm(req)

    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(llm_fn)
    return Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                  sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(task)})


def test_dev_scoring_cannot_read_the_validation_labels(store, tmp_path, rings_task, monkeypatch):
    """Red-team finding 2026-10-07: dev scoring ran candidates INSIDE the run workspace, next to the full public
    training file (which holds every dev label). A candidate reading it scored itself perfectly on dev (held-out
    stayed safe: confirmation uses fresh sandboxes), corrupting selection, dev racing and the claim forecast.
    Dev runs now happen per fold in fresh sandboxes holding only that fold's inputs. (With LocalSandbox a program
    could still walk the host filesystem; only Docker's mount isolation closes that, as for held-out runs.)"""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    res = _run_with_impl(store, tmp_path, rings_task, CHEAT_DEV)
    assert res.status == RunStatus.COMPLETED, res.detail
    cheat = [a for a in res.state["attempts"] if a["hypothesis_id"] == "h_radius"]
    assert cheat and cheat[0]["error"] is None  # it ran
    # noisy rings: no honest rule is perfect; ~1/3 is the constant-"0" fallback
    assert cheat[0]["dev_score"] < 0.6, cheat[0]


def test_dev_estimate_covers_every_public_training_row_once(store, tmp_path, rings_task, monkeypatch):
    """Live run run_cd99288e65ba4a4f: a 24-row dev split (20%) carried so little information that the claim fell
    back to the 0.02 floor and K7 correctly rejected it as trivially safe. The dev estimate now uses k-fold
    cross-validation: every public training row is a dev example exactly once."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    res = _run_with_impl(store, tmp_path, rings_task, KNN_RADIUS)
    assert res.status == RunStatus.COMPLETED, res.detail
    lines = (rings_task / "public" / "train.csv").read_text().splitlines()
    yi = lines[0].split(",").index("label")
    train_labels = [line.split(",")[yi] for line in lines[1:] if line]
    dev = store.blobs.get_json(res.state["_private_dev_labels_ref"])["private_dev_labels"]
    assert len(dev) == len(train_labels) == 400
    assert Counter(dev) == Counter(train_labels)
    preds = store.blobs.get_json(res.state["best"]["dev_preds_ref"])
    assert len(preds) == len(dev)  # one out-of-fold prediction per dev example


def test_tasks_sharing_data_share_held_out_ids_and_canary(store, tmp_path, rings_task):
    """Dataset ids are content-based (data files only): a repair task re-using a real task's split names the same
    held-out target (one multiplicity family, no 'twin' under another id); the baseline stays per task. A task whose
    canary differs from the one registered for its data is refused (its canary would never be scanned)."""
    import shutil

    from custom_harness.research.claims import register_task
    from custom_harness.research.task import load_task
    from custom_harness.runtime.modules import ModuleContext
    twin = tmp_path / "twin"
    shutil.copytree(rings_task, twin)
    (twin / "baseline.py").write_text("# a different (repair) baseline\n" + (twin / "baseline.py").read_text())
    ctx = ModuleContext(run_id="r", store=store, backends={}, budget=None, sandbox=None)
    a, b = register_task(ctx, load_task(rings_task), rings_task), register_task(ctx, load_task(twin), twin)
    assert a["test"] == b["test"] and a["train"] == b["train"] and a["baseline"] != b["baseline"]
    (twin / "private" / "canary.txt").write_text("CHH-CANARY-different\n")
    with pytest.raises(ValueError, match="canary"):
        register_task(ctx, load_task(twin), twin)


def test_unisolated_execution_refused_by_default(store, tmp_path, rings_task, monkeypatch):
    monkeypatch.delenv("CHH_ALLOW_UNISOLATED", raising=False)
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    eng = Engine(spec, backends={"proposer": MockBackend(["{}"])}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.FAILED and "isolation" in res.detail


def test_private_state_never_renders():
    with pytest.raises(PrivateDataAccess):
        render("{{_private_dev_labels_ref}}", {"_private_dev_labels_ref": "x"})


def test_t2_rules():
    assert t2_verdict([0.9, 0.91, 0.92], [0.7, 0.71, 0.69], higher_is_better=True).verified
    assert not t2_verdict([0.9, 0.91], [0.7, 0.71], higher_is_better=True).verified  # too few seeds
    assert not t2_verdict([0.7, 0.9, 0.5], [0.7, 0.6, 0.8], higher_is_better=True).verified
    assert t2_verdict([1.0, 1.15, 0.9], [2.0, 2.1, 1.95], higher_is_better=False).verified  # rmse-like
    # identical difference on every seed: the seed check is degenerate, never evidence (M0 review)
    v = t2_verdict([1.0, 1.1, 0.9], [2.0, 2.1, 1.9], higher_is_better=False)
    assert not v.verified and v.degenerate
    assert not t2_verdict([0.9, 0.91, 0.92], [0.7, 0.71], higher_is_better=True).verified  # unpaired
    ci = bootstrap_diff_ci([1.0] * 3, [0.0] * 3)
    assert ci.lo == ci.hi == 1.0


def test_bradley_terry_orders_consistent_winner():
    bt = bradley_terry(["a", "b", "c"], rankings_to_wins([["a", "b", "c"], ["a", "c", "b"], ["a", "b", "c"]]))
    assert max(bt, key=bt.get) == "a" and bt["b"] > bt["c"]


class _HiccupSandbox(LocalSandbox):
    """Confirmation sandboxes that fail ONCE with a transient infrastructure error (the 4th confirm run)."""

    confirm_execs = 0

    def spawn(self, workspace):
        return _HiccupSandbox(workspace, i_understand_no_isolation=True)

    def exec(self, command, timeout_s=600, env=None):
        if "-confirm" in str(self.workspace):
            _HiccupSandbox.confirm_execs += 1
            if _HiccupSandbox.confirm_execs == 4:
                from custom_harness.errors import InfraError
                raise InfraError("Cannot connect to the Docker daemon", transient=True)
        return super().exec(command, timeout_s, env)


def test_infra_retry_during_confirmation_does_not_reject_the_claim(store, tmp_path, rings_task, monkeypatch):
    """An infrastructure retry of the confirm step must not repeat held-out evaluations (K4 would count the
    repeats as unreported and reject an honest claim): confirm reuses the records it already made."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    from custom_harness.runtime.engine import RetryPolicy
    _HiccupSandbox.confirm_execs = 0
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(scripted_llm)
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=_HiccupSandbox(tmp_path / "ws", i_understand_no_isolation=True),
                 retry=RetryPolicy(max_attempts=3, backoff_s=0.0)).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    confirm_step = next(s for s in store.trace(res.run_id) if s.node_id == "confirm")
    assert confirm_step.output.get("_infra_retries"), "the hiccup did not happen: test is not testing anything"
    assert res.state["certificate"]["label"] == "CERTIFIED-INTERNAL", res.state["certificate"]
    assert len(res.state["evidence_ids"]) == 6 and res.state["audit"]["passed"]


def test_improve_operator_runs_and_dev_racing_refuses_a_non_improvement(store, tmp_path, rings_task, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(scripted_llm)
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    s = res.state
    improves = [a for a in s["attempts"] if str(a["hypothesis_id"]).startswith("improve-")]
    assert len(improves) == 2 and all(a["accepted_improvement"] is False for a in improves)
    assert not str(s["best"]["hypothesis_id"]).startswith("improve-")  # the incumbent was kept
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL" and s["audit"]["passed"]


def test_dev_racing_rule():
    from custom_harness.research.modules import _dev_better
    from custom_harness.research.task import TaskSpec
    task = TaskSpec(id="t", title="t", description="d", metric="accuracy", target_column="y")
    y = ["1"] * 100
    assert _dev_better(task, y, ["1"] * 100, ["0"] * 50 + ["1"] * 50)  # clearly better
    assert not _dev_better(task, y, ["1"] * 100, ["1"] * 100)  # identical: no evidence of improvement
    assert not _dev_better(task, y, ["0"] + ["1"] * 99, ["1"] * 100)  # worse


def test_socratic_filter_keeps_only_questions():
    from custom_harness.research.modules import socratic_filter
    raw = {"questions": ["What would falsify the radius hypothesis?", "Use a random forest instead.",
                         "Try scaling? Then it works?", "Have you checked for leakage via the color column?",
                         "Why not `import sklearn`?", "Did you use a hold-out?", "Consider stacking?", "x?"]}
    out = socratic_filter({"critique_raw": raw}, {"max_questions": 5}, None)["critique"]
    assert out["questions"] == ["What would falsify the radius hypothesis?",
                                "Have you checked for leakage via the color column?", "Did you use a hold-out?"]
    assert out["dropped"] == 5
    assert socratic_filter({}, {}, None)["critique"] == {"questions": [], "dropped": 0}


def _run_v1(store, tmp_path, task, llm):
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    return Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                  sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(task)})


def test_independent_replication_certifies_the_headline_without_seeing_the_code(store, tmp_path, rings_task,
                                                                                 monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    from custom_harness.evolve import score_state
    from custom_harness.research.claims import headline
    monkeypatch.delenv("CHH_ALLOW_SAME_FAMILY")  # two families: invariant 5 met, no override
    author, other = MockBackend(scripted_llm), MockBackend(scripted_llm)
    author.family, other.family = "family-a", "family-b"
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    res = Engine(spec, backends={"proposer": author, "executor": author, "judge": other, "replicator": other},
                 store=store, sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    s = res.state
    assert s["model_policy"]["invariant_5"] == "met"
    assert s["replica_certificate"]["label"] == "CERTIFIED-INTERNAL", s["replica_certificate"]
    assert headline(s) and s["audit"]["passed"], s["audit"]
    assert "independent re-implementation" in s["replica_claim"]["statement"]
    assert s["replica_claim"]["effect"]["min_effect"] == s["claim"]["effect"]["min_effect"]  # same bar
    replicate_prompt = next(r for r in other.requests if "Re-implement the method" in r.messages[-1].content)
    assert not any("Re-implement the method" in r.messages[-1].content for r in author.requests)
    code_lines = [ln for ln in KNN_RADIUS.splitlines() if len(ln.strip()) > 12]
    assert not any(ln in replicate_prompt.messages[-1].content for ln in code_lines)  # never saw the code
    assert "model family): yes" in s["report"] and "Loop label (GOAL §6.1): **MECHANICAL**" in s["report"]
    assert score_state(s, res.status)[0] == 1.5  # certified + replicated + capped improvement


def test_same_family_replication_is_not_a_headline_and_earns_no_replication_credit(store, tmp_path, rings_task,
                                                                                   monkeypatch):
    """Invariant 5: a replicator of the author's family shares its biases. With the (recorded) override the run
    proceeds, the replica may certify, but it is not independent: no headline, no replication credit."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    from custom_harness.evolve import score_state
    from custom_harness.research.claims import headline
    res = _run_v1(store, tmp_path, rings_task, MockBackend(scripted_llm))  # one family, CHH_ALLOW_SAME_FAMILY=1
    s = res.state
    assert s["model_policy"]["invariant_5"].startswith("NOT MET")
    assert s["replica_certificate"]["label"] == "CERTIFIED-INTERNAL" and not headline(s)
    assert "invariant 5" in s["report"] and "NOT MET" in s["report"] and "model family): no" in s["report"]
    assert score_state(s, res.status)[0] == 1.0  # certified + capped improvement, no replication credit


def test_failed_replication_means_no_headline(store, tmp_path, rings_task, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    from custom_harness.evolve import score_state
    from custom_harness.research.claims import headline

    def llm(req):
        if "Re-implement the method" in req.messages[-1].content:
            return json.dumps({"code": BROKEN, "notes": "could not reproduce"})
        return scripted_llm(req)
    res = _run_v1(store, tmp_path, rings_task, MockBackend(llm))
    s = res.state
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL" and "replica_certificate" not in s
    assert not headline(s) and s["audit"]["passed"]
    assert "no replication claim was made" in s["report"]
    assert score_state(s, res.status)[0] <= 1.0  # 1.5 when also replicated


def test_a_copied_replica_is_not_an_independent_replication(store, tmp_path, rings_task, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    from custom_harness.research.claims import code_similarity, headline

    def llm(req):
        if "Re-implement the method" in req.messages[-1].content:
            return json.dumps({"code": KNN_RADIUS, "notes": "verbatim copy"})
        return scripted_llm(req)
    s = _run_v1(store, tmp_path, rings_task, MockBackend(llm)).state
    assert "replica_certificate" not in s and not headline(s)
    assert s["replica_best"]["error"].startswith("not independent")
    assert code_similarity(KNN_RADIUS, KNN_RADIUS) == 1.0 and code_similarity(KNN_RADIUS, KNN_REPLICA) < 0.3
