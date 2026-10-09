"""Infrastructure failure vs policy failure (GOAL L1, CR P0-1/P0-5).

Done-when (GOAL L1): a Docker outage mid-campaign changes no accept/reject decision.
"""

import json
import math
import sys

import pytest

import custom_harness.research  # noqa: F401
from custom_harness.errors import InfraError, infra_cause, is_transient
from custom_harness.evolve import Evaluator, EvolveConfig, evolve_generation, gate, race
from custom_harness.evolve.selection import paired_sample
from custom_harness.gateway import BackendError, MockBackend
from custom_harness.kernel import Kernel, KernelConfig
from custom_harness.records import RunStatus
from custom_harness.runtime import END, Condition, Edge, Engine, HarnessSpec, LLMNode, ModuleNode, register
from custom_harness.runtime.engine import RetryPolicy
from custom_harness.sandbox import LocalSandbox
from custom_harness.sandbox.docker import DockerSandbox, DockerUnavailable, docker_available
from test_evolve import PROPOSALS, declared_seed_spec, executor, seed_spec, suite  # noqa: F401

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


def _k3_replays() -> int:
    """Clean consecutive replays K3 needs under the Kernel's default reliability presets (checks.reliability)."""
    cfg = KernelConfig()
    return math.ceil(math.log(cfg.k3_beta) / math.log(1 - cfg.k3_eps_max) - 1e-12)


class FlakyDockerLike(LocalSandbox):
    """Simulates short Docker outages: in every window of PERIOD exec calls, calls 5 and 6 fail (daemon down
    for two consecutive calls) with a transient InfraError.

    PERIOD must leave a clean stretch longer than the longest run of consecutive exec calls one node needs: K3's
    zero-failure demonstration (GOAL §16.4) re-executes `_k3_replays()` times in a row inside one certify. With a
    period of 11 (the K3 count under the default presets) every 11 consecutive calls contained an outage, so no
    certify could ever finish: that is a permanent outage for certify, not a short one, and the gate correctly failed
    closed on the candidate's excluded units (found 2026-10-08). After an outage the engine's third attempt starts on
    call 7 of the window, so PERIOD >= 7 + replays - 5 is the minimum; twice the K3 count leaves margin."""

    PERIOD = 2 * _k3_replays() + 2
    count = 0

    def spawn(self, workspace):
        # like DockerSandbox.spawn: fresh boxes (dev folds, held-out runs, Kernel re-execution) share the daemon,
        # so they share its outages
        return FlakyDockerLike(workspace, i_understand_no_isolation=True)

    def exec(self, command, timeout_s=600, env=None, *, cpu_s=None):
        FlakyDockerLike.count += 1
        if FlakyDockerLike.count % FlakyDockerLike.PERIOD in (5, 6):
            raise InfraError("Cannot connect to the Docker daemon", transient=True)
        return super().exec(command, timeout_s, env, cpu_s=cpu_s)


def test_docker_outage_mid_campaign_changes_no_decision(store, tmp_path, suite, monkeypatch):  # noqa: F811
    """GOAL L1 done-when. Same campaign twice: clean environment vs outages (sandbox creation failures
    and exec failures). Decisions and the accepted harness must be identical."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")

    def campaign(sub, factory):
        from custom_harness.store import EvidenceStore
        st = EvidenceStore(tmp_path / sub)
        Kernel(st).approve_adapter("ml", approver="pytest")  # test stand-in for the human adapter approval
        llm = MockBackend(executor)
        roles = {"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}
        ev = Evaluator(roles, st, factory, suite, retry=NO_WAIT)
        cfg = EvolveConfig(n_edits=4, race_rounds=2)
        # R2-2: campaigns pre-declare their multiplicity bound (Evaluator raises MultiplicityDrift otherwise)
        res = evolve_generation(declared_seed_spec(cfg), suite, evaluator=ev,
                                proposer=MockBackend([json.dumps(PROPOSALS)]), store=st, cfg=cfg)
        st.close()
        return res, ev

    clean, _ = campaign("clean", lambda key: LocalSandbox(tmp_path / "sb1" / key, i_understand_no_isolation=True))

    created = {"n": 0}

    def outage_factory(key):
        created["n"] += 1
        if created["n"] % 4 == 0:  # daemon down when this sandbox was requested
            raise DockerUnavailable("Docker is not running")
        return FlakyDockerLike(tmp_path / "sb2" / f"{key}-{created['n']}", i_understand_no_isolation=True)

    # count certifications aborted by an outage (K3 re-execution hit the daemon being down): the outage must reach
    # the Kernel's replays too, and the retried certify must still reach the clean campaign's decision
    aborted = {"n": 0}
    real_certify = Kernel.certify

    def certify(self, *a, **kw):
        try:
            return real_certify(self, *a, **kw)
        except InfraError:
            aborted["n"] += 1
            raise

    monkeypatch.setattr(Kernel, "certify", certify)
    FlakyDockerLike.count = 0
    flaky, ev = campaign("flaky", outage_factory)
    assert ev.infra_failures > 0 and FlakyDockerLike.count >= 11  # both kinds of outage really happened
    assert aborted["n"] > 0  # ... including during the Kernel's K3 re-executions
    assert flaky.accepted == clean.accepted and flaky.decision == clean.decision
    assert flaky.new_parent.spec_hash() == clean.new_parent.spec_hash()


def test_gate_marks_an_exclusion_failure_as_infra_aborted_not_a_verdict():
    """Owner decision 2026-10-08: too many infrastructure exclusions make the comparison INCONCLUSIVE; the gate still
    never accepts, but says so (infra_aborted), so the loop does not record a policy rejection."""
    units = [("t", s) for s in range(4)]
    up = {u: 1.0 for u in units}
    down = {u: (None if u[1] < 2 else 1.0) for u in units}  # half the candidate's units lost to an outage
    healthy = paired_sample(units, up.get, lambda u: 0.5)
    outage = paired_sample(units, down.get, lambda u: 0.5)
    d = gate(outage, healthy, require_evolve_ci=False)
    assert not d.accepted and d.infra_aborted and "excluded for infrastructure" in d.reason
    worse = paired_sample(units, lambda u: 0.0, lambda u: 0.5)  # control: a real loss is a policy verdict
    d2 = gate(worse, worse, require_evolve_ci=False)
    assert not d2.accepted and not d2.infra_aborted


def test_a_persistent_outage_ends_infra_aborted_never_rejected(store, tmp_path, suite, monkeypatch):  # noqa: F811
    """Owner decision 2026-10-08 (GOAL L1, 'excluded, not scored'): when the candidate's units are lost to an outage
    past the exclusion threshold, the generation ends INCONCLUSIVE: lineage status `infra_aborted` (no
    `rejected_gate`, no rejected_on stage), not accepted, and the edit is NOT novelty-blocked for later generations."""
    from custom_harness.evolve.loop import rejected_signatures
    from custom_harness.store import EvidenceStore
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    st = EvidenceStore(tmp_path / "persistent")
    Kernel(st).approve_adapter("ml", approver="pytest")
    llm = MockBackend(executor)
    roles = {"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}
    # the pre-2026-10-08 outage pattern: exec calls 5 and 6 of every 11 fail, forever. K3 needs 11 consecutive clean
    # replays, so no certify can ever finish: a permanently degraded environment, not a mid-campaign outage
    monkeypatch.setattr(FlakyDockerLike, "PERIOD", 11)
    FlakyDockerLike.count = 0
    ev = Evaluator(roles, st, lambda key: FlakyDockerLike(tmp_path / "sb" / key, i_understand_no_isolation=True),
                   suite, retry=NO_WAIT)
    cfg = EvolveConfig(n_edits=4, race_rounds=2)
    res = evolve_generation(declared_seed_spec(cfg), suite, evaluator=ev,
                            proposer=MockBackend([json.dumps(PROPOSALS)]), store=st, cfg=cfg)
    statuses = [r["status"] for r in st.records("lineage")]
    edits = st.records("harness_edit")
    assert ev.infra_failures > 0
    assert not res.accepted and "infra_aborted" in statuses and "rejected_gate" not in statuses, statuses
    assert res.decision.startswith("inconclusive"), res.decision
    assert edits and all(e["infra_aborted"] and not e["accepted"] and e["rejected_on"] is None for e in edits)
    aborted = [r for r in st.records("lineage") if r["status"] == "infra_aborted"]
    assert aborted and not {r["signature"] for r in aborted} & set(rejected_signatures(st))  # not novelty-blocked
    assert st.records("generation_stats")[-1]["infra_aborted"] == len(aborted)
    st.close()


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


def test_an_infra_failed_smoke_run_is_excluded_not_a_rejection():
    """GOAL L1 / "an infrastructure failure means excluded, not scored": the smoke run (L6.7) rejected a candidate
    whose smoke unit hit an outage, so an outage changed an accept/reject decision (found 2026-10-08 through
    test_docker_outage_mid_campaign_changes_no_decision). An infra-failed smoke run is inconclusive: the candidate
    goes on to the race, where that unit is excluded and the gate fails closed on too many exclusions."""
    from custom_harness.evolve.evaluate import RunOutcome
    from custom_harness.evolve.loop import _smoke_failure
    from custom_harness.records import Cost

    def out(status):
        return RunOutcome("t", "r", 0, None, False, status, Cost())
    ok = out(RunStatus.COMPLETED.value)
    assert out(RunStatus.INFRA_FAILED.value).infra_failed  # the precondition: this outcome is an infra failure
    assert _smoke_failure(out(RunStatus.INFRA_FAILED.value), ok) is None
    broken = out(RunStatus.FAILED.value)  # control: a real breakage where the parent ran still rejects
    assert _smoke_failure(broken, ok) is not None
