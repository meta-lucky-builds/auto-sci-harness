"""Release emission and the verdict embargo (GOAL §16.5.3, invariant 18).

"Continuous held-out values never reach the evolver": the evolution score keeps only verdict bits (audit,
certified, replicated) plus the DEV term, and every target-derived value that reaches an agent-visible surface is a
typed release recorded through `Kernel.record_release` (kstats; until it is merged, the `release_unledgered`
fallback of kerneld/releases.py, which these tests also accept through `released()`).
"""

from __future__ import annotations

import pytest

from custom_harness.evolve.evaluate import score_state
from custom_harness.records import RunStatus

# Recorded on 2026-10-07 by running the pre-embargo `score_state` (commit f0f703f) on _state(subject_mean=m):
# the old score WAS the held-out subject mean (0.5 + 0.5 * (m - 0.5) / 0.5 = m), a continuous held-out value
# handed to the evolver every run.
OLD_F0F703F_SCORES = {0.55: 0.55, 0.6: 0.6, 0.75: 0.75, 0.9: 0.9}


def _state(subject_mean: float, *, dev: float = 0.8, label: str = "CERTIFIED-INTERNAL") -> dict:
    return {"audit": {"passed": True},
            "certificate": {"label": label, "subject_mean": subject_mean, "comparator_mean": 0.5},
            "best": {"dev_score": dev}, "baseline_dev": 0.7, "task": {"metric": "accuracy"}}


def test_old_score_exposed_the_continuous_held_out_mean():
    """The attack the embargo closes: under the old score an evolver reads the held-out subject mean off the score
    (here exactly; in general up to the known verdict bits and baseline). Shown on the recorded old values."""
    for m, old in OLD_F0F703F_SCORES.items():
        assert old == pytest.approx(m)  # held-out mean recovered from the score
    assert len(set(OLD_F0F703F_SCORES.values())) == len(OLD_F0F703F_SCORES)  # distinct held-out means -> scores


def test_evolution_score_does_not_depend_on_continuous_held_out_values():
    scores = {m: score_state(_state(m), RunStatus.COMPLETED) for m in OLD_F0F703F_SCORES}
    assert len(set(scores.values())) == 1, f"the score still varies with the held-out mean: {scores}"
    score, verified = next(iter(scores.values()))
    assert verified is True
    assert score == pytest.approx(0.5 + 0.5 * (0.8 - 0.7) / 0.7)  # verdict bit + dev term


def test_evolution_score_still_moves_with_dev_and_verdict_bits():
    """The score is not a constant: the dev term and the verdict bits still drive it (so the test above is not
    passing because the score stopped measuring anything)."""
    base = score_state(_state(0.6), RunStatus.COMPLETED)[0]
    assert score_state(_state(0.6, dev=0.9), RunStatus.COMPLETED)[0] > base
    assert score_state(_state(0.6, label="REJECTED"), RunStatus.COMPLETED)[0] == pytest.approx(base - 0.5)
    no_audit = {**_state(0.6), "audit": {"passed": False}}
    assert score_state(no_audit, RunStatus.COMPLETED) == (0.0, False)


# ---------------------------------------------------------------- emission of the score's verdict bits
from custom_harness.evolve.evaluate import SuiteTask, run_harness  # noqa: E402
from custom_harness.kerneld.releases import Releases, released  # noqa: E402
from custom_harness.runtime import HarnessSpec, register  # noqa: E402
from custom_harness.sandbox import LocalSandbox  # noqa: E402


@register("test.release_reader")
def _reader(state, params, ctx):
    from custom_harness.kernel import DatasetRole
    vault = ctx.kernel().vault
    vault.register_dataset("t-held", DatasetRole.TEST, public={"x.csv": b"x\n1\n"}, private={"y.csv": b"y\n1\n"})
    vault.read("t-held", "y.csv", purpose="leakscan", run_id=ctx.run_id)  # a private read during the run
    return {"audit": {"passed": True}, "certificate": {"label": "CERTIFIED-INTERNAL", "subject_mean": 0.9}}


SPEC = {"id": "rel", "entry": "m", "nodes": {"m": {"kind": "module", "module": "test.release_reader"}}}


def _run(store, tmp_path, split, releases=None):
    return run_harness(HarnessSpec.model_validate(SPEC), SuiteTask(f"t-{split}", tmp_path, split, family="A"), 0,
                       backends={}, store=store, releases=releases,
                       sandbox_factory=lambda k: LocalSandbox(tmp_path / k, i_understand_no_isolation=True))


def test_evolve_split_score_is_a_ledgered_verdict_release(store, tmp_path):
    out = _run(store, tmp_path, "evolve")
    assert out.verified is True  # the score really carried a held-out verdict
    rel = released(store, "t-held")
    assert [(r["channel"], r["alphabet"], r["note"]) for r in rel] == [
        ("evolution_score", "verdict_bit", "evolve.score:verdict")]


def test_gate_split_score_is_not_released_by_the_run(store, tmp_path):
    """Gate scores never reach the proposer per run (only the aggregate decision does)."""
    _run(store, tmp_path, "gate")
    assert released(store, "t-held") == []


def test_replay_pins_the_released_bits(store, tmp_path):
    rec = Releases(None, mode="record")
    assert _run(store, tmp_path, "evolve", rec).verified is True
    pinned = Releases(None, mode="replay", pins={r["key"]: [{**r["value"], "certified": False, "label": "REJECTED"}]
                                                 for r in rec.log})
    out = _run(store, tmp_path, "evolve", pinned)
    assert out.verified is False and pinned.misses == [] and pinned.unused() == []  # pinned value was used
    miss = Releases(None, mode="replay", pins={})
    _run(store, tmp_path, "evolve", miss)
    assert miss.misses  # an unrecorded release is a replay flag


# ---------------------------------------------------------------- the evidence export is embargoed
def test_export_shows_verdict_labels_only_and_records_the_release(store, tmp_path):
    from custom_harness.evolve.export import export_evidence
    from custom_harness.kernel import DatasetRole, Kernel
    from test_evolve import seed_spec
    from test_evolve_evidence import _add_run, _outcome, _task_dir
    kernel = Kernel(store)
    kernel.vault.register_dataset("e-held", DatasetRole.TEST, public={"x.csv": b"x\n1\n"}, private={"y.csv": b"y\n1\n"})
    held_mean = "0.8123457"
    cert = {"hash": "c" * 64, "label": "CERTIFIED-INTERNAL", "subject_mean": float(held_mean),
            "comparator_mean": 0.61, "failed": []}
    rid = _add_run(store, seed_spec(), "e0", [("certify", {"certificate": cert}, None),
                                              ("report", {"report": f"subject mean {held_mean}"}, None)])
    kernel.vault.read("e-held", "y.csv", purpose="leakscan", run_id=rid)
    task = SuiteTask("e0", _task_dir(tmp_path, "e0"), "evolve", family="A")
    res = export_evidence(store, tmp_path / "exp", runs=[_outcome(rid, "e0", 0.5, verified=True)], tasks=[task])
    text = "".join(p.read_text(encoding="utf-8") for p in res.path.rglob("*.json"))
    assert "CERTIFIED-INTERNAL" in text  # the verdict label is shown
    assert held_mean not in text and "0.61" not in text, "a continuous held-out value reached the export"
    assert [(r["channel"], r["note"]) for r in released(store, "e-held")] == [("export", "export.run:verdict")]


# ---------------------------------------------------------------- the gate decision bit is a pinnable release
def test_gate_decision_is_emitted_and_pinnable(store, tmp_path):
    from test_evolve_evidence import _gen
    res, _ = _gen(store, tmp_path)
    assert res.accepted  # control: this generation is accepted on its own
    from test_evolve import seed_spec
    from test_evolve_evidence import CostTable, _gain, _tasks
    pinned = Releases(None, mode="replay", pins={})
    ev = CostTable(store, _tasks(tmp_path), seed_spec().spec_hash(), _gain, lambda a, t, s: _tokens100())
    ev.releases = pinned
    _gen(store, tmp_path, evaluator=ev)
    assert [m.split("|")[-1].split("#")[0] for m in pinned.misses] == ["evolve.gate:decision"]
    key = pinned.misses[0].rsplit("#", 1)[0]
    flip = Releases(None, mode="replay", pins={key: [False]})
    ev2 = CostTable(store, _tasks(tmp_path), seed_spec().spec_hash(), _gain, lambda a, t, s: _tokens100())
    ev2.releases = flip
    res3, _ = _gen(store, tmp_path, evaluator=ev2)
    assert res3.accepted is False and flip.misses == []  # the pinned (recorded) decision bit was used


def _tokens100():
    from custom_harness.records import Cost
    return Cost(prompt_tokens=100)


# ---------------------------------------------------------------- engine: target-derived values rendered into prompts
def _prompt_run(store, prompt):
    from custom_harness.gateway import MockBackend
    from custom_harness.runtime import Engine
    spec = HarnessSpec.model_validate({
        "id": "p", "entry": "m", "nodes": {"m": {"kind": "module", "module": "test.release_reader"},
                                           "ask": {"kind": "llm", "prompt": prompt, "output_key": "o"}},
        "edges": [{"source": "m", "target": "ask"}]})
    llm = MockBackend(["ok"])
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    return res, llm


@pytest.mark.parametrize("path,alphabet", [("certificate.label", "verdict_bit"),
                                           ("certificate.subject_mean", "continuous")])
def test_a_target_derived_value_in_a_prompt_is_a_typed_release(store, path, alphabet):
    res, llm = _prompt_run(store, "Result: {{" + path + "}}")
    assert res.status.value == "completed", res.detail
    assert any(str(res.state["certificate"][path.split(".")[1]]) in m.content
               for m in llm.requests[0].messages)  # the value really reached the prompt
    prompts = [r for r in released(store, "t-held") if r["channel"] == "prompt"]
    assert [(r["alphabet"], r["note"]) for r in prompts] == [(alphabet, f"prompt:ask:{path}")]


def test_a_prompt_without_target_derived_values_releases_nothing(store):
    res, _ = _prompt_run(store, "Say hi")
    assert res.status.value == "completed", res.detail
    assert [r for r in released(store, "t-held") if r["channel"] == "prompt"] == []


@register("test.release_reader_declared")
def _reader_declared(state, params, ctx):
    """Like test.release_reader, plus a structured key built only from NON-target data whose existence is the verdict
    (the research.certify replication brief): the trusted module declares it carries only the verdict bit."""
    out = _reader(state, params, ctx)
    out["brief"] = {"claim": "pre-registered statement", "idea": "dev-time idea"}
    if params.get("declare"):
        out["_release_alphabets"] = {"brief": "verdict_bit", "not_an_output": "verdict_bit"}
    if params.get("bogus"):
        out["_release_alphabets"] = {"brief": "nonsense"}
    return out


def _declared_run(store, params):
    from custom_harness.gateway import MockBackend
    from custom_harness.runtime import Engine
    spec = HarnessSpec.model_validate({
        "id": "p", "entry": "m", "nodes": {"m": {"kind": "module", "module": "test.release_reader_declared",
                                                 "params": params},
                                           "ask": {"kind": "llm", "prompt": "Brief: {{brief.claim}}",
                                                   "output_key": "o"}},
        "edges": [{"source": "m", "target": "ask"}]})
    res = Engine(spec, backends={"executor": MockBackend(["ok"])}, store=store).run("t", {})
    assert res.status.value == "completed", res.detail
    assert "_release_alphabets" not in res.state  # the declaration never becomes state
    return [r["alphabet"] for r in released(store, "t-held") if r["channel"] == "prompt"]


@pytest.mark.parametrize("params,alphabet", [({}, "continuous"), ({"declare": True}, "verdict_bit"),
                                             ({"bogus": True}, "continuous")])
def test_a_trusted_module_may_declare_a_key_verdict_bit_only(store, params, alphabet):
    """Found 2026-10-08 (replication merge break): the replication brief (claim statement, idea, training profile)
    was released as `continuous` only because certify, which wrote it, had read the target; every replica claim then
    failed K6 as adaptively exposed. Without a declaration the fail-closed default stays continuous; an unknown
    alphabet in a declaration is ignored (stays continuous)."""
    assert _declared_run(store, params) == [alphabet]
