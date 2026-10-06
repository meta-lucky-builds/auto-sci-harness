"""Infrastructure failure vs policy failure (GOAL L1, CR P0-1/P0-5).

Done-when (GOAL L1): a Docker outage mid-campaign changes no accept/reject decision.
"""

import json
import sys

import pytest

import custom_harness.research  # noqa: F401
from custom_harness.errors import InfraError, infra_cause, is_transient
from custom_harness.evolve import EvolveConfig, Evaluator, evolve_generation, gate, race
from custom_harness.evolve.selection import paired_sample
from custom_harness.gateway import BackendError, MockBackend
from custom_harness.records import RunStatus
from custom_harness.runtime import END, Condition, Edge, Engine, HarnessSpec, LLMNode, ModuleNode, register
from custom_harness.runtime.engine import RetryPolicy
from custom_harness.sandbox import LocalSandbox
from custom_harness.sandbox.docker import DockerSandbox, DockerUnavailable, docker_available
from test_evolve import PROPOSALS, executor, seed_spec, suite  # noqa: F401

NO_WAIT = RetryPolicy(max_attempts=3, backoff_s=0.0)

_calls = {"n": 0}


@register("test.flaky_infra")
def _flaky(state, params, ctx):
    _calls["n"] += 1
    if _calls["n"] <= params["fail_times"]:
        raise InfraError("docker daemon hiccup", transient=params.get("transient", True))
    return {"done": True}


def flaky_spec(fail_times, transient=True):
    return HarnessSpec(id="flaky", entry="a",
                       nodes={"a": ModuleNode(module="test.flaky_infra",
                                              params={"fail_times": fail_times, "transient": transient}),
                              "recover": LLMNode(prompt="recover", output_key="r")},
                       edges=[Edge(source="a", target="recover", when=Condition(key="_last_error", op="truthy")),
                              Edge(source="a", target=END)])


def test_classification_walks_cause_chain():
    try:
        try:
            raise InfraError("x", transient=True)
        except InfraError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as outer:
        assert infra_cause(outer) is not None and is_transient(outer)
    assert infra_cause(BackendError("HTTP 400", infra=False)) is None  # malformed request = harness's fault
    assert infra_cause(BackendError("HTTP 503", transient=True)) is not None
    assert infra_cause(ValueError("agent bug")) is None


def test_transient_infra_failure_is_retried_and_run_completes(store):
    _calls["n"] = 0
    res = Engine(flaky_spec(2), backends={"executor": MockBackend(["x"])}, store=store, retry=NO_WAIT).run("t", {})
    assert res.status == RunStatus.COMPLETED and res.state["done"] is True
    step = store.trace(res.run_id)[0]
    assert step.error is None and len(step.output["_infra_retries"]) == 2


def test_persistent_infra_failure_is_infra_failed_and_never_routed_through_error_edges(store):
    _calls["n"] = 0
    backend = MockBackend(["should never be called"])
    res = Engine(flaky_spec(99), backends={"executor": backend}, store=store, retry=NO_WAIT).run("t", {})
    assert res.status == RunStatus.INFRA_FAILED
    assert backend.requests == []  # the harness's 'recover' error edge was NOT taken
    assert _calls["n"] == 3  # max_attempts
    step = store.trace(res.run_id)[0]
    cp = store.blobs.get_json(step.checkpoint_ref)
    assert cp["next"] == "a"  # a resume re-tries the same node


def test_non_transient_infra_failure_is_not_retried(store):
    _calls["n"] = 0
    res = Engine(flaky_spec(99, transient=False), backends={}, store=store, retry=NO_WAIT).run("t", {})
    assert res.status == RunStatus.INFRA_FAILED and _calls["n"] == 1


def test_policy_backend_error_still_uses_harness_error_edges(store):
    class Rejecting(MockBackend):
        def complete(self, req):
            if "first" in req.messages[-1].content:
                raise BackendError("HTTP 400: invalid schema", infra=False)
            return super().complete(req)
    spec = HarnessSpec(id="p", entry="a", nodes={"a": LLMNode(prompt="first", output_key="x"),
                                                 "b": LLMNode(prompt="second", output_key="y")},
                       edges=[Edge(source="a", target="b", when=Condition(key="_last_error", op="truthy"))])
    res = Engine(spec, backends={"executor": Rejecting(["ok"])}, store=store, retry=NO_WAIT).run("t", {})
    assert res.status == RunStatus.COMPLETED and res.state["y"] == "ok"


def test_paired_sample_drops_excluded_pairs_and_checks_alignment():
    units = [("a", 0), ("b", 0), ("c", 0)]
    cand = {("a", 0): 1.0, ("b", 0): None, ("c", 0): 0.5}
    par = {("a", 0): 0.0, ("b", 0): 0.2, ("c", 0): None}
    s = paired_sample(units, cand.get, par.get)
    assert s.diffs == [1.0] and s.excluded == [("b", 0), ("c", 0)]
    with pytest.raises(ValueError, match="duplicate"):
        paired_sample(units + [("a", 0)], cand.get, par.get)


def test_gate_fails_closed_when_too_many_units_excluded():
    good = paired_sample([("e", i) for i in range(4)], lambda u: 1.0, lambda u: 0.0)
    gate_ok = paired_sample([("g", i) for i in range(4)], lambda u: 0.6, lambda u: 0.5)
    assert gate(good, gate_ok).accepted
    broken_env = paired_sample([("g", i) for i in range(4)], lambda u: 0.6 if u[1] < 2 else None,
                               lambda u: 0.5)
    d = gate(good, broken_env)
    assert not d.accepted and "infrastructure" in d.reason


def test_race_never_scores_infra_failures_as_zero():
    units = [(f"t{i}", 0) for i in range(6)]
    parent = {u: 0.5 for u in units}
    # 'good' is clearly better but half its units hit infra failures; 'zero' would win if None counted as 0
    scores = {"good": lambda u: None if int(u[0][1:]) % 2 else 0.9, "flat": lambda u: 0.5}
    rr = race(list(scores), units, lambda c, u: scores[c](u), parent, rounds=2, keep=1)
    assert rr.survivors == ["good"]
    assert rr.stats["good"]["excluded"] == 3


class FlakyDockerLike(LocalSandbox):
    """Simulates short Docker outages: in every window of 11 exec calls, calls 5 and 6 fail (daemon down
    for two consecutive calls) with a transient InfraError."""

    count = 0

    def exec(self, command, timeout_s=600, env=None):
        FlakyDockerLike.count += 1
        if FlakyDockerLike.count % 11 in (5, 6):
            raise InfraError("Cannot connect to the Docker daemon", transient=True)
        return super().exec(command, timeout_s, env)


def test_docker_outage_mid_campaign_changes_no_decision(store, tmp_path, suite, monkeypatch):  # noqa: F811
    """GOAL L1 done-when. Same campaign twice: clean environment vs outages (sandbox creation failures
    and exec failures). Decisions and the accepted harness must be identical."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")

    def campaign(sub, factory):
        from custom_harness.store import EvidenceStore
        st = EvidenceStore(tmp_path / sub)
        llm = MockBackend(executor)
        ev = Evaluator({"proposer": llm, "executor": llm, "judge": llm}, st, factory, suite, retry=NO_WAIT)
        res = evolve_generation(seed_spec(), suite, evaluator=ev, proposer=MockBackend([json.dumps(PROPOSALS)]),
                                store=st, cfg=EvolveConfig(n_edits=4, race_rounds=2))
        st.close()
        return res, ev

    clean, _ = campaign("clean", lambda key: LocalSandbox(tmp_path / "sb1" / key, i_understand_no_isolation=True))

    created = {"n": 0}

    def outage_factory(key):
        created["n"] += 1
        if created["n"] % 4 == 0:  # daemon down when this sandbox was requested
            raise DockerUnavailable("Docker is not running")
        return FlakyDockerLike(tmp_path / "sb2" / f"{key}-{created['n']}", i_understand_no_isolation=True)

    FlakyDockerLike.count = 0
    flaky, ev = campaign("flaky", outage_factory)
    assert ev.infra_failures > 0 and FlakyDockerLike.count >= 11  # both kinds of outage really happened
    assert flaky.accepted == clean.accepted and flaky.decision == clean.decision
    assert flaky.new_parent.spec_hash() == clean.new_parent.spec_hash()


@pytest.mark.skipif(not docker_available(), reason="Docker daemon not running")
def test_docker_agent_exit_code_125_is_policy_not_infra(tmp_path):
    """Agent code must not be able to fake an infrastructure failure by exiting with Docker's codes."""
    sb = DockerSandbox(tmp_path / "ws")
    for code in (125, 126, 127):
        res = sb.exec(["python", "-c", f"import sys; print('docker: fake'); sys.exit({code})"], timeout_s=120)
        assert res.returncode == code
    ok = sb.exec(["python", "-c", "print('hi')"], timeout_s=120)
    assert ok.returncode == 0 and ok.stdout.strip() == "hi"


@pytest.mark.skipif(not docker_available(), reason="Docker daemon not running")
def test_docker_missing_image_is_infra(tmp_path):
    sb = DockerSandbox(tmp_path / "ws", image="custom-harness-does-not-exist:none")
    with pytest.raises(InfraError):
        sb.exec([sys.executable, "-c", "pass"], timeout_s=60)
