"""Automated controllers act on studio runs ONLY through the downgrade-only gate (GOAL §5 L1, §1.1;
control/downgrade.py, studio/controller.py, studio/kill.py).

End to end on the real engine: an LLM controller (MockBackend) consulted while a run is RUNNING proposes grant,
enlarge, a disguised enlargement, admit, revive, approve and one kill with code-checkable `stalled` evidence. Only the
kill is applied: the engine stops the run before its next step, the run is not resumable or forkable, and the kill is
ledgered as `controller_action`, never as the human-only `run_kill`. Every other action is rejected and ledgered with
no effect. A human `chh kill` works unchanged.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from custom_harness import cli
from custom_harness.budget import Budget
from custom_harness.config import data_dir
from custom_harness.control.downgrade import ACTION_KIND, AdmissionRegister, AdmissionState
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Ledger
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec, register
from custom_harness.store import EvidenceStore

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
_W: dict = {}


def _proposal(run_id: str, last_step_index: int) -> str:
    return json.dumps({"actions": [
        {"type": "grant", "target": "camp", "unit": "tokens", "value": 1_000_000},
        {"type": "enlarge", "target": "camp", "unit": "tokens", "value": 1000},
        {"type": "shrink", "target": "camp", "unit": "tokens", "value": 1000},  # an enlargement named "shrink"
        {"type": "admit", "target": "req1"},
        {"type": "revive", "target": run_id},
        {"type": "approve", "target": run_id},
        {"type": "kill", "target": run_id, "evidence": {"kind": "stalled", "last_step_index": last_step_index}},
        {"type": "revive", "target": run_id},
    ]})


@register("ctl.noop")
def _noop(state, params, ctx):
    _W.setdefault("ran", []).append(ctx.run_id)
    return {}


@register("ctl.supervise_now")
def _supervise_now(state, params, ctx):
    """The controller is consulted while this run is RUNNING (its last finished step is index 0)."""
    from custom_harness.studio.controller import supervise
    obs = {}

    def controller(req):
        obs.update(json.loads(req.messages[-1].content))
        return _proposal(ctx.run_id, 0)

    _W["result"] = supervise(_W["store"], MockBackend(controller), controller_id="ctl-test",
                             budgets={"camp": _W["budget"]}, admissions=_W["admissions"],
                             clock=lambda: time.time() + 10**6)
    _W["observation"] = obs
    return {"supervised": True}


def _spec(*modules: str) -> HarnessSpec:
    nodes = {f"n{i}": {"kind": "module", "module": m} for i, m in enumerate(modules)}
    edges = [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(len(modules) - 1)]
    return HarnessSpec.model_validate({"id": "supervised", "entry": "n0", "nodes": nodes, "edges": edges})


@pytest.fixture
def dstore():
    s = EvidenceStore(data_dir() / "store")  # the store the CLI opens
    _W.clear()
    _W["store"] = s
    _W["budget"] = Budget(limits={"tokens": 100.0})
    reg = AdmissionRegister()
    reg.submit("req1")
    _W["admissions"] = reg
    yield s
    s.close()


def _actions(store) -> list[dict]:
    return store.records(ACTION_KIND)


def test_an_llm_controller_can_only_downgrade_a_running_run(dstore):
    eng = Engine(_spec("ctl.noop", "ctl.supervise_now", "ctl.noop"), backends={}, store=dstore)
    res = eng.run("t", {})
    # the observation was built by code: the running run, its last step, no free text
    (seen,) = [r for r in _W["observation"]["runs"] if r["run_id"] == res.run_id]
    assert seen["status"] == "running" and seen["last_step_index"] == 0
    # only the evidence-backed kill was applied; everything else was rejected, with no effect
    outcomes = [(a.action_type, a.outcome.value, a.reason.value) for a in _W["result"].actions]
    assert outcomes == [("invalid", "rejected", "unknown_action"), ("invalid", "rejected", "unknown_action"),
                        ("shrink", "rejected", "not_a_shrink"), ("invalid", "rejected", "unknown_action"),
                        ("invalid", "rejected", "unknown_action"), ("invalid", "rejected", "unknown_action"),
                        ("kill", "applied", "ok"), ("invalid", "rejected", "unknown_action")]
    assert _W["budget"].limits == {"tokens": 100.0}  # no grant, no enlargement
    assert _W["admissions"].state("req1") == AdmissionState.PENDING  # nothing admitted
    # the engine stopped the run before its next step; it ends FAILED, stopped by the controller
    assert res.status == RunStatus.FAILED
    status, detail = dstore.run_status(res.run_id)
    assert status == RunStatus.FAILED and "stopped by an automated controller (ctl-test" in detail
    assert [s.node_id for s in dstore.trace(res.run_id)] == ["n0", "n1"] and _W["ran"] == [res.run_id]
    # ledgered as controller_action, NEVER as the human-only run_kill
    assert Ledger(dstore).entries("run_kill") == []
    applied = [a for a in _actions(dstore) if a["outcome"] == "applied"]
    assert applied == [{"controller_id": "ctl-test", "proposal_sha256": _W["result"].proposal_sha256, "index": 6,
                        "action_type": "kill", "target_id": res.run_id, "outcome": "applied", "reason": "ok",
                        "evidence_kind": "stalled"}]
    assert len(_actions(dstore)) == 8 and all("grant" not in json.dumps(a) for a in _actions(dstore))
    # no revival: not resumable, not forkable
    with pytest.raises(ValueError, match="not resumable"):
        eng.resume(res.run_id, force=True)
    with pytest.raises(ValueError, match="not forked"):
        eng.fork(res.run_id, 0)
    # the module Kernel of a controller-stopped run refuses claims like a human-killed one
    from custom_harness.kernel import KernelError
    from custom_harness.studio.kill import RunGuardedKernel
    with pytest.raises(KernelError, match="stopped by an automated controller"):
        RunGuardedKernel(dstore, run_id=res.run_id)._refuse_if_killed("pre-registration")


def test_a_paused_run_is_never_stalled_and_a_human_kill_works_unchanged(dstore, capsys):
    from custom_harness.studio.controller import supervise
    from test_approval import spec as approval_spec
    res = Engine(approval_spec(), backends={}, store=dstore).run("t", {})
    assert res.status == RunStatus.PAUSED
    last = dstore.trace(res.run_id)[-1].index
    kill = json.dumps({"actions": [{"type": "kill", "target": res.run_id,
                                    "evidence": {"kind": "stalled", "last_step_index": last}}]})
    out = supervise(dstore, MockBackend([kill]), controller_id="ctl", clock=lambda: time.time() + 10**6)
    assert [(a.outcome.value, a.reason.value) for a in out.actions] == [("rejected", "evidence_not_met")]
    assert dstore.run_status(res.run_id)[0] == RunStatus.PAUSED  # waits for its human
    # the human kill: unchanged command, entry and effect
    assert cli.main(["kill", res.run_id, "--by", "alice", "--reason", "wrong_config"]) == 0
    (k,) = Ledger(dstore).entries("run_kill")
    assert k.body == {"run_id": res.run_id, "by": "alice", "reason_code": "wrong_config", "status_before": "paused"}
    status, detail = dstore.run_status(res.run_id)
    assert status == RunStatus.FAILED and "killed by a human (alice, reason wrong_config" in detail
    assert cli.main(["kill", res.run_id, "--by", "alice"]) == 0 and len(Ledger(dstore).entries("run_kill")) == 1
    # a controller cannot act on it any more
    out = supervise(dstore, MockBackend([kill]), controller_id="ctl", clock=lambda: time.time() + 10**6)
    assert [(a.outcome.value, a.reason.value) for a in out.actions] == [("rejected", "run_terminal")]


def test_cli_supervise_routes_the_controller_through_the_gate(dstore, capsys):
    """`chh supervise --controller mock`: the mock replies non-JSON text, so the whole proposal is rejected and
    ledgered; nothing else happens."""
    assert cli.main(["supervise", "--controller", "mock", "--controller-id", "cli-ctl"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["envelope"] == "not_json" and [a["outcome"] for a in doc["actions"]] == ["rejected"]
    assert [r["controller_id"] for r in dstore.records("controller_proposal")] == ["cli-ctl"]


def _calls(name: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in SRC.rglob("*.py"):
        n = sum(1 for node in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == name)
        if n:
            out[p.relative_to(SRC).as_posix()] = n
    return out


def test_the_gate_is_the_only_automated_path_and_run_kill_stays_human_only():
    """Ratchet: the human kill (`kill_run`, the only writer of `run_kill`) is called only by the human CLI command;
    the downgrade gate is constructed only by the studio controller; the controller's kill effect is used only there."""
    assert _calls("kill_run") == {"studio/cli.py": 1}
    assert _calls("DowngradeGate") == {"studio/controller.py": 1}
    assert _calls("controller_kill_effect") == {"studio/controller.py": 1}
    src = (SRC / "studio" / "controller.py").read_text(encoding="utf-8")
    assert "KILL_KIND" not in src and "run_kill" not in src.replace("`run_kill`", "")
