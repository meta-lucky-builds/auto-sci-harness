"""Integrity review of the research loop + evolution loop (2026-10-07, round 2).

Every test asserts the SECURE behaviour. A failing test is a verified finding; a passing test is a defence that held
(kept as a regression). Scripted mock models and LocalSandbox only (CHH_ALLOW_UNISOLATED=1, trusted test code); no
real model call, no network, no Docker.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.errors import infra_cause
from custom_harness.evolve import Evaluator, EvolveConfig, RunOutcome, SuiteTask, evolve_generation
from custom_harness.evolve.edits import Edit, EditOp, EditRejected, EditType, apply_edit
from custom_harness.evolve.evaluate import run_harness, score_state
from custom_harness.evolve.selection import paired_sample
from custom_harness.gateway import BackendError, MockBackend
from custom_harness.records import Cost, RunStatus
from custom_harness.research.claims import headline
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.verify import metrics as M
from custom_harness.verify.stats import paired_example_interval
from test_research import BROKEN, KNN_RADIUS, ROOT, SPEC, rings_task, scripted_llm  # noqa: F401

ALL_ROLES = ("proposer", "executor", "judge", "replicator")


def _seed() -> HarnessSpec:
    return HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))


def _edges(spec: HarnessSpec) -> list[dict]:
    return spec.model_dump(mode="json")["edges"]


def _fam(fn, family: str) -> MockBackend:
    m = MockBackend(fn)
    m.family = family  # declared model family (policy.model_family reads it first)
    return m


def _unisolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))


def _sandbox_factory(tmp_path: Path):
    return lambda key: LocalSandbox(tmp_path / "ws" / key, i_understand_no_isolation=True)


# ===================================================================== invariant 5 on paper vs in fact
ROLE_SWAPS = {
    # the code author becomes the replicator's backend: the "independent" replication is written by the author
    "author-is-replicator": [("implement", "replicator"), ("fix", "replicator"), ("improve", "replicator")],
    # the hypotheses are written by the judge's backend: the tournament and critic judge their own proposals
    "proposer-is-judge": [("hypothesize", "judge")],
    # the Socratic critic runs on the proposer's backend
    "critic-is-proposer": [("critic", "proposer")],
}


def _role_edit(swaps) -> Edit:
    return Edit(type=EditType.SUBAGENT_ROLE, rationale="re-bind roles",
                ops=[EditOp(op="replace", path=f"/nodes/{n}/role", value=r) for n, r in swaps])


@pytest.mark.parametrize("name", list(ROLE_SWAPS))
def test_role_rebinding_edit_cannot_keep_invariant_5_met_on_paper(store, monkeypatch, name):
    """Invariant 5 is checked on ROLE -> backend bindings, but which node runs under which role is an evolvable
    field (`LLMNode.role`, edit type subagent_role). Secure: an edit that makes the author's work run on the
    judging/replicating role's backend is rejected, or the run is no longer recorded as invariant 5 'met'."""
    monkeypatch.delenv("CHH_ALLOW_SAME_FAMILY", raising=False)
    a, b = _fam(scripted_llm, "fam_a"), _fam(scripted_llm, "fam_b")
    backends = {"proposer": a, "executor": a, "judge": b, "replicator": b}
    try:
        evolved = apply_edit(_seed(), _role_edit(ROLE_SWAPS[name]))
    except EditRejected:
        return  # defence held
    policy = Engine(evolved, backends=backends, store=store).model_policy
    assert policy["invariant_5"] != "met", (
        f"edit {ROLE_SWAPS[name]} accepted and the run is still recorded as invariant 5 'met': {policy}")


def test_author_running_as_replicator_earns_replication_credit_and_headline(store, tmp_path, rings_task,  # noqa: F811
                                                                            monkeypatch):
    """End to end: proposer/executor = family A, judge/replicator = family B (policy 'met', no override). The
    evolved harness runs implement/fix/improve under role 'replicator', so family B writes BOTH the claimed code and
    the 'independent' replica. Secure: no replication credit and no headline for such a run."""
    _unisolated(monkeypatch, tmp_path)
    monkeypatch.delenv("CHH_ALLOW_SAME_FAMILY", raising=False)
    a, b = _fam(scripted_llm, "fam_a"), _fam(scripted_llm, "fam_b")
    try:
        evolved = apply_edit(_seed(), _role_edit(ROLE_SWAPS["author-is-replicator"]))
    except EditRejected:
        return  # defence held
    res = Engine(evolved, backends={"proposer": a, "executor": a, "judge": b, "replicator": b}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    authored_by_b = [r for r in b.requests if r.messages[-1].content.startswith(("Implement this hypothesis",
                                                                                 "This script failed"))]
    replicated_by_b = [r for r in b.requests if "Re-implement the method" in r.messages[-1].content]
    assert authored_by_b and replicated_by_b and not any("Implement this hypothesis" in r.messages[-1].content
                                                         for r in a.requests)  # precondition: B wrote both
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL" and s["audit"]["passed"], (s["certificate"], s["audit"])
    score, verified = score_state(s, res.status)
    sol, base = s["certificate"]["subject_mean"], s["certificate"]["comparator_mean"]
    rel_part = 0.5 * max(-1.0, min(1.0, (sol - base) / abs(base)))
    replication_credit = score - 0.5 * verified - rel_part
    assert replication_credit == pytest.approx(0.0) and not headline(s), (
        f"replication credit {replication_credit} and headline={headline(s)} although the replicator's backend "
        f"also wrote the claimed code; model_policy={s['model_policy']}")


# ===================================================================== paired evaluation order vs Kernel multiplicity
def _bonferroni_borderline_task(task_dir: Path) -> float:
    """Set the rings task's minimum effect between the held-out lower bounds at Bonferroni family size 1 and 2 (for
    the scripted solution KNN_RADIUS vs the task baseline; both deterministic). Returns the chosen min_effect."""
    def run(prog: Path) -> list[str]:
        out = task_dir / "calib_preds.csv"
        subprocess.run([sys.executable, str(prog), "--train", str(task_dir / "public/train.csv"), "--test",
                        str(task_dir / "public/test_X.csv"), "--out", str(out), "--seed", "0"], check=True)
        preds = [r[0] for r in list(csv.reader(out.open(newline="")))[1:]]
        out.unlink()
        return preds
    sol = task_dir.parent / "calib_solution.py"
    sol.write_text(KNN_RADIUS)
    s, b = run(sol), run(task_dir / "baseline.py")
    y = [r[0] for r in list(csv.reader((task_dir / "private/test_y.csv").open(newline="")))[1:]]
    lo = {m: paired_example_interval(M.accuracy, y, [s] * 3, [b] * 3, higher_is_better=True, alpha=2 * 0.05 / m,
                                     n_boot=2000, seed=0).lo for m in (1, 2)}
    assert lo[1] > lo[2], lo
    floor = (lo[1] + lo[2]) / 2
    with (task_dir / "task.yaml").open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"min_effect: {floor!r}\n")
    return floor


def test_identical_harnesses_score_identically_in_a_paired_comparison(store, tmp_path, rings_task,  # noqa: F811
                                                                       monkeypatch):
    """The Kernel's Bonferroni family for a held-out target is every claim pre-registered on it SO FAR (K4), and
    every evolution run on a task registers claims on the SAME target in the campaign's shared store. paired_sample
    evaluates the candidate before the parent, so on a fresh unit the candidate's claim is certified at alpha/1 and
    the parent's at alpha/(>= 2). Secure: two harnesses that behave identically get identical scores (paired
    difference 0); otherwise evaluation order, not the edit, decides acceptance evidence."""
    _unisolated(monkeypatch, tmp_path)
    from custom_harness.evolve.multiplicity import MultiplicityDrift
    floor = _bonferroni_borderline_task(rings_task)
    llm = MockBackend(scripted_llm)  # responds to the user prompt only: the system-prompt change is a no-op
    task = SuiteTask("rings", rings_task, "evolve", family="rings")

    def arms(n_comparisons: int | None) -> tuple[HarnessSpec, HarnessSpec]:
        doc = _seed().model_dump(mode="json")
        if n_comparisons is not None:  # a HUMAN-set declaration on the (protected) claim nodes of the seed spec
            for nid in ("claim", "replica_claim"):
                doc["nodes"][nid]["params"]["n_comparisons"] = n_comparisons
        parent = HarnessSpec.model_validate(doc)
        noop = Edit(type=EditType.PROMPT_TEXT, rationale="cosmetic", ops=[
            EditOp(op="replace", path="/nodes/implement/system", value=parent.nodes["implement"].system + " ")])
        cand = apply_edit(parent, noop)
        assert cand.spec_hash() != parent.spec_hash()
        return parent, cand

    # 1. Undeclared campaign (the original attack setting): the FIRST run already registers 2 claims (claim +
    # replica_claim) on the target, more than the declared 1, so its certification strictness depended on what ran
    # before it. Secure: the evaluation is refused, its outcome never used.
    parent, cand = arms(None)
    ev = Evaluator({r: llm for r in ALL_ROLES}, store, _sandbox_factory(tmp_path), [task])
    with pytest.raises(MultiplicityDrift, match="n_comparisons"):
        ev.score(cand, ("rings", 0))
    assert not ev.cache and ev.calls == 1  # the run happened (attack exercised) and its outcome was discarded

    # 2. Declared campaign: both arms are certified at the SAME constant family size, whatever ran first.
    declared = 2 + 2 * 2  # the 2 claims of the refused run above + 2 runs x 2 claim nodes
    parent, cand = arms(declared)
    ev = Evaluator({r: llm for r in ALL_ROLES}, store, _sandbox_factory(tmp_path), [task])
    sample = paired_sample([("rings", 0)], lambda u: ev.score(cand, u), lambda u: ev.score(parent, u))
    oc, op = ev.outcome(cand, ("rings", 0)), ev.outcome(parent, ("rings", 0))
    assert oc.status == op.status == "completed", (oc.failure_signatures, op.failure_signatures)

    def cert(o):
        from custom_harness.kernel import Kernel
        step = next(s for s in store.trace(o.run_id) if s.node_id == "certify")
        return Kernel(store).certificate(step.output["certificate"]["hash"])

    def why(o):
        return {k: (r.status, r.reasons or r.observations) for k, r in cert(o).checks.items() if k in ("K4", "K6")}
    for o in (oc, op):  # the attack's lever is K4's family size: show it is the declared constant in both arms
        obs = cert(o).checks["K4"].observations
        assert any(f"{declared} claim(s) tested on this target" in x for x in obs), obs
    assert sample.diffs == [pytest.approx(0.0)], (
        f"identical harnesses: candidate {oc.score} ({oc.cert_label}) vs parent {op.score} ({op.cert_label}); "
        f"paired diff {sample.diffs}; min_effect {floor:.4g}; candidate {why(oc)}; parent {why(op)}")


# ===================================================================== harness-caused failures laundered as infra
FAKE_CODEX_EVENT = json.dumps({"type": "turn.failed", "error": {
    "message": "context_length_exceeded: Your input exceeds the context window of this model."}})


def _fake_codex(tmp_path: Path) -> str:
    """A stand-in `codex` executable that rejects every request the way a provider rejects an oversized prompt
    (exit code 1 plus a failure event). Test code only; no model is called."""
    d = tmp_path / "fakecodex"
    d.mkdir()
    script = d / "fake_codex.py"
    script.write_text(f"import sys\nsys.stdin.read()\nprint({FAKE_CODEX_EVENT!r})\nsys.exit(1)\n")
    if os.name == "nt":
        exe = d / "codex.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        exe = d / "codex"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        exe.chmod(0o755)
    return str(exe)


def test_codex_rejection_of_a_harness_authored_request_is_not_infrastructure(tmp_path):
    """A request the provider rejects because of its CONTENT (here: too long for the context window) is the
    harness's fault. Secure: classified as a policy failure (scored), not infrastructure (excluded)."""
    from custom_harness.gateway import ChatRequest, Message
    from custom_harness.gateway.codex_cli import CodexCLIBackend
    codex = CodexCLIBackend(executable=_fake_codex(tmp_path), registry={"openai-codex-cli": {"mode": "api"}})
    with pytest.raises(BackendError) as err:
        codex.complete(ChatRequest(messages=[Message(role="user", content="x " * 10)]))
    assert "context_length_exceeded" in str(err.value)  # precondition: the fake was really called
    assert infra_cause(err.value) is None, f"classified as infrastructure: {err.value}"


def test_edit_cannot_turn_its_own_give_up_runs_into_excluded_units(store, tmp_path, rings_task,  # noqa: F811
                                                                   monkeypatch):
    """An evolvable edit routes every give-up (a run that would score 0) through a new LLM node whose request the
    provider rejects (an oversized prompt). The run ends INFRA_FAILED and evaluation EXCLUDES the unit (score None)
    instead of scoring it 0; paired_sample drops the pair, so up to max_excluded_frac (25%) of a candidate's worst
    units vanish from the acceptance evidence. Secure: a failure the harness's own request caused is scored."""
    _unisolated(monkeypatch, tmp_path)
    from custom_harness.gateway.codex_cli import CodexCLIBackend
    codex = CodexCLIBackend(executable=_fake_codex(tmp_path), registry={"openai-codex-cli": {"mode": "api"}})

    def llm_fn(req):
        text = req.messages[-1].content
        if text.startswith("BLOWUP"):
            return codex.complete(req).text  # the provider's real classification path (fake executable)
        if text.startswith(("Implement this hypothesis", "This script failed")):
            return json.dumps({"code": BROKEN, "notes": "always broken"})  # this unit would score 0
        return scripted_llm(req)

    spec = _seed()
    edges = _edges(spec)
    give_up_from = [i for i, e in enumerate(edges) if e["source"] in ("next", "run") and e["target"] == "report"
                    and e.get("when") is None]
    ops = [EditOp(op="add", path="/nodes/blowup", value={
        "kind": "llm", "role": "executor", "prompt": "BLOWUP " + "{{profile}} " * 50, "output_key": "notes"})]
    for i in sorted(give_up_from, reverse=True):
        ops.append(EditOp(op="add", path=f"/edges/{i}", value={
            "source": edges[i]["source"], "target": "blowup",
            "when": {"key": "route", "op": "eq", "value": "give_up"}}))
    ops.append(EditOp(op="add", path="/edges/-", value={"source": "blowup", "target": "report"}))
    try:
        evolved = apply_edit(spec, Edit(type=EditType.CONTROL_FLOW, rationale="summarise before reporting", ops=ops))
    except EditRejected:
        return  # defence held
    llm = MockBackend(llm_fn)
    out = run_harness(evolved, SuiteTask("rings", rings_task, "evolve", family="rings"), 0,
                      backends={r: llm for r in ALL_ROLES}, store=store, sandbox_factory=_sandbox_factory(tmp_path))
    assert any(r.messages[-1].content.startswith("BLOWUP") for r in llm.requests)  # precondition: give-up path taken
    assert out.score is not None, (f"harness-caused failure excluded as infrastructure: status={out.status}, "
                                   f"signatures={out.failure_signatures}")


# ===================================================================== question-only critic
def test_evolved_prompt_cannot_bypass_the_socratic_filter():
    """The critic's raw output (`critique_raw`, unreserved) is filtered to questions IN CODE (socratic_filter) so a
    critic cannot smuggle answers or code into later prompts (GOAL L4.8). An evolvable prompt can render the raw
    output directly. Secure: such an edit is rejected."""
    spec = _seed()
    prompt = spec.nodes["implement"].prompt.replace("{{critique.questions}}", "{{critique_raw}}")
    assert prompt != spec.nodes["implement"].prompt
    with pytest.raises(EditRejected):
        apply_edit(spec, Edit(type=EditType.PROMPT_TEXT, rationale="show the full critique",
                              ops=[EditOp(op="replace", path="/nodes/implement/prompt", value=prompt)]))


# ===================================================================== loop label of a generation
class _LabelEvaluator(Evaluator):
    """Scores and certificate labels from a table (no harness runs): candidate +1 everywhere, parent 0; gate units
    carry CERTIFIED-VALIDATED certificates, evolve units only CERTIFIED-INTERNAL ones."""

    def __init__(self, store, tasks, parent_hash):
        super().__init__({}, store, lambda k: None, tasks)
        self.parent_hash = parent_hash

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            self.calls += 1
            cand = spec.spec_hash() != self.parent_hash
            score = (1.0 + 0.001 * (int(unit[0][1:]) + 3 * (unit[1] % 1000))) if cand else 0.0
            label = "CERTIFIED-VALIDATED" if self.tasks[unit[0]].split == "gate" else "CERTIFIED-INTERNAL"
            # equal non-zero cost in both arms: the B3 cost term fails closed on missing (zero) cost data
            self.cache[key] = RunOutcome(unit[0], "", unit[1], score, True, "completed", Cost(prompt_tokens=100), [],
                                         cert_label=label)
        return self.cache[key]


def test_generation_label_validated_only_if_all_acceptance_evidence_is_validated(store, tmp_path):
    """GOAL §6.1 / loop.py: a generation is VALIDATED only if the decision rested ENTIRELY on CERTIFIED-VALIDATED
    evidence. The acceptance decision requires the fresh EVOLVE re-measurement (CI lo > 0) as well as the gate.
    Secure: internal-only evolve evidence behind an acceptance makes the generation MECHANICAL."""
    from test_evolve import seed_spec
    from test_lofo import ONE_EDIT
    tasks = [SuiteTask(f"e{i}", tmp_path, "evolve", family="A") for i in range(3)]
    tasks += [SuiteTask(f"g{i}", tmp_path, "gate", family="A") for i in range(2)]
    tasks += [SuiteTask(f"h{i}", tmp_path, "gate", family="H") for i in range(2)]
    parent = seed_spec()
    res = evolve_generation(parent, tasks, evaluator=_LabelEvaluator(store, tasks, parent.spec_hash()),
                            proposer=MockBackend([ONE_EDIT]), store=store,
                            cfg=EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1))
    assert res.accepted, res.decision  # precondition: the decision used the (internal) evolve evidence
    assert res.loop_label == "MECHANICAL", (
        f"generation labelled {res.loop_label} although its accepted evolve evidence is CERTIFIED-INTERNAL only")


# ===================================================================== defences that held (regressions)
def test_held_unbound_role_failure_is_scored_zero_not_excluded(store, tmp_path):
    """A node bound to a role with no backend is the harness's fault: FAILED, scored 0 (never excluded)."""
    spec = HarnessSpec.model_validate({"id": "t", "entry": "a", "nodes": {
        "a": {"kind": "llm", "role": "ghost", "prompt": "hi", "output_key": "o"}}})
    out = run_harness(spec, SuiteTask("t", tmp_path, "evolve", family="A"), 0,
                      backends={"executor": MockBackend(["{}"])}, store=store,
                      sandbox_factory=_sandbox_factory(tmp_path))
    assert out.status == RunStatus.FAILED.value and out.score == 0.0


@pytest.mark.parametrize("status", [RunStatus.PAUSED, RunStatus.STEP_LIMIT, RunStatus.BUDGET_EXCEEDED,
                                    RunStatus.FAILED])
def test_held_non_completed_runs_earn_nothing(status):
    state = {"audit": {"passed": True}, "certificate": {"label": "CERTIFIED-INTERNAL", "subject_mean": 0.9,
                                                        "comparator_mean": 0.4},
             "replica_certificate": {"label": "CERTIFIED-INTERNAL"}, "model_policy": {"invariant_5": "met"},
             "task": {"metric": "accuracy"}}
    assert score_state(state, status) == (0.0, False)


@pytest.mark.parametrize("path,value", [("/nodes/replicate/role", "executor"),
                                        ("/nodes/claim/params/delta_fraction", 0.0),
                                        ("/nodes/claim/params/n_comparisons", 0),
                                        ("/nodes/setup/params/dev_folds", 2)])
def test_held_protected_parameters_and_roles(path, value):
    with pytest.raises(EditRejected):
        apply_edit(_seed(), Edit(type=EditType.PARAMS, rationale="x", ops=[EditOp(op="replace", path=path,
                                                                                    value=value)]))


@pytest.mark.parametrize("key", ["best", "baseline_dev", "baseline_dev_preds_ref", "certificate", "audit",
                                 "model_policy", "replica_certificate", "task", "dev_folds"])
def test_held_scoring_inputs_are_reserved(key):
    """Every state key score_state, claim/forecast or the audit reads is reserved against evolvable writers."""
    spec = _seed()
    ops = [EditOp(op="add", path="/nodes/sneak", value={"kind": "llm", "role": "proposer", "prompt": "p",
                                                        "output_key": key})]
    i = next(n for n, e in enumerate(_edges(spec)) if e["source"] == "socratic")
    ops += [EditOp(op="replace", path=f"/edges/{i}/target", value="sneak"),
            EditOp(op="add", path="/edges/-", value={"source": "sneak", "target": "next"})]
    with pytest.raises(EditRejected, match="reserved state key"):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=ops))


def test_held_remeasure_seeds_must_be_disjoint_from_race_seeds(store, tmp_path):
    from test_evolve import seed_spec
    tasks = [SuiteTask("e0", tmp_path, "evolve", family="A"), SuiteTask("g0", tmp_path, "gate", family="H")]
    with pytest.raises(ValueError, match="disjoint"):
        evolve_generation(seed_spec(), tasks, evaluator=Evaluator({}, store, lambda k: None, tasks),
                          proposer=MockBackend(["{}"]), store=store,
                          cfg=EvolveConfig(seeds=(0, 1), remeasure_seeds=(1, 2)))
