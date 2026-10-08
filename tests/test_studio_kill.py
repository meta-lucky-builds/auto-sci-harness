"""Kill switch (GOAL §5 L9, §3 L1 "kill"): `chh kill RUN_ID` is a HUMAN action that stops a run safely. The run
ends FAILED (never RUNNING or COMPLETED), nothing is certified after the kill, the kill is in the Kernel ledger, and a
killed run cannot be resumed."""

import pytest

from custom_harness import cli
from custom_harness.config import data_dir
from custom_harness.kernel import KernelError, Ledger
from custom_harness.records import Run, RunStatus
from custom_harness.runtime import Engine, HarnessSpec, register
from custom_harness.store import EvidenceStore
from kernel_world import World

_HUMAN: dict = {}  # the test plays the human at the terminal: it holds the real store the CLI would open


@register("kill.human_kills_now")
def _human_kills_now(state, params, ctx):
    from custom_harness.studio.kill import kill_run
    kill_run(_HUMAN["store"], ctx.run_id, by="alice", reason_code="human_stop")
    return {"a_done": True}


@register("kill.after")
def _after(state, params, ctx):
    _HUMAN.setdefault("ran", []).append(ctx.run_id)
    return {"b_done": True}


@register("kill.kill_then_preregister")
def _kill_then_preregister(state, params, ctx):
    from custom_harness.studio.kill import kill_run
    kill_run(_HUMAN["store"], ctx.run_id, by="alice")
    w = _HUMAN["world"]
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    ctx.kernel().preregister(c)  # must be refused: the run is killed
    return {"registered": True}


@register("kill.forge_kill")
def _forge_kill(state, params, ctx):
    ctx.kernel().ledger.append("run_kill", {"run_id": ctx.run_id, "by": "module", "reason_code": "other"})
    return {}


@register("kill.forge_tier_policy")
def _forge_tier_policy(state, params, ctx):
    ctx.kernel().ledger.append("tier_policy", {"sha256": "0" * 64, "policy": {}, "by": "module"})
    return {}


def _spec(*modules: str) -> HarnessSpec:
    nodes = {f"n{i}": {"kind": "module", "module": m} for i, m in enumerate(modules)}
    edges = [{"source": f"n{i}", "target": f"n{i + 1}"} for i in range(len(modules) - 1)]
    return HarnessSpec.model_validate({"id": "killable", "entry": "n0", "nodes": nodes, "edges": edges})


@pytest.fixture
def dstore():
    s = EvidenceStore(data_dir() / "store")  # the store the CLI opens
    _HUMAN.clear()
    _HUMAN["store"] = s
    yield s
    s.close()


def _kills(store) -> list:
    return [e for e in Ledger(store).entries("run_kill")]


def test_kill_mid_run_ends_failed_runs_nothing_after_and_is_ledgered(dstore):
    eng = Engine(_spec("kill.human_kills_now", "kill.after", "kill.after"), backends={}, store=dstore)
    res = eng.run("t", {})
    assert res.status == RunStatus.FAILED  # not COMPLETED, not RUNNING
    status, detail = dstore.run_status(res.run_id)
    assert status == RunStatus.FAILED and "killed by a human (alice" in detail
    assert [s.node_id for s in dstore.trace(res.run_id)] == ["n0"]  # the step in flight finished; nothing after
    assert "ran" not in _HUMAN
    (k,) = _kills(dstore)
    assert k.body == {"run_id": res.run_id, "by": "alice", "reason_code": "human_stop", "status_before": "running"}
    with pytest.raises(ValueError, match="killed run is not resumable"):
        eng.resume(res.run_id, force=True)
    # even if a later crash wrote the resumable "engine crash" status, the kill still wins
    dstore.set_run_status(res.run_id, RunStatus.FAILED, "engine crash: OSError: x")
    with pytest.raises(ValueError, match="killed run is not resumable"):
        eng.resume(res.run_id, force=True)
    assert "ran" not in _HUMAN


def test_cli_kill_of_a_paused_run_and_refusals(dstore, capsys):
    from test_approval import spec as approval_spec
    res = Engine(approval_spec(), backends={}, store=dstore).run("t", {})
    assert res.status == RunStatus.PAUSED
    assert cli.main(["kill", res.run_id, "--by", "alice", "--reason", "wrong_config"]) == 0
    status, detail = dstore.run_status(res.run_id)
    assert status == RunStatus.FAILED and "reason wrong_config" in detail
    assert len(_kills(dstore)) == 1
    assert cli.main(["kill", res.run_id, "--by", "alice"]) == 0  # idempotent: no second entry
    assert len(_kills(dstore)) == 1
    with pytest.raises(SystemExit, match="not paused"):  # a killed run can no longer be approved
        cli.main(["approve", res.run_id, "review", "--decision", "approve", "--approver", "alice"])
    with pytest.raises(SystemExit, match="must name the human"):
        cli.main(["kill", res.run_id, "--by", "  "])
    done = Engine(_spec("kill.after"), backends={}, store=dstore).run("t", {})
    with pytest.raises(SystemExit, match="nothing to stop"):
        cli.main(["kill", done.run_id, "--by", "alice"])
    with pytest.raises(SystemExit):  # argparse: the reason is a closed code
        cli.main(["kill", done.run_id, "--by", "alice", "--reason", "because I said so"])
    assert len(_kills(dstore)) == 1


def test_module_kernel_refuses_to_preregister_after_the_kill(dstore, tmp_path):
    w = World(tmp_path / "w")
    try:
        _HUMAN["world"] = w
        _HUMAN["store"] = w.store
        res = Engine(_spec("kill.kill_then_preregister", "kill.after"), backends={}, store=w.store).run("t", {})
        assert res.status == RunStatus.FAILED
        assert not w.kernel.ledger.entries("prereg")  # the refused pre-registration left nothing
        step = w.store.trace(res.run_id)[0]
        assert "pre-registration refused" in (step.error or "")
        assert "ran" not in _HUMAN
    finally:
        w.close()


def test_module_code_cannot_write_a_kill_or_a_tier_policy(dstore):
    for module in ("kill.forge_kill", "kill.forge_tier_policy"):
        res = Engine(_spec(module, "kill.after"), backends={}, store=dstore).run("t", {})
        step = dstore.trace(res.run_id)[0]
        assert "is a human action" in (step.error or "")
    assert not _kills(dstore) and not Ledger(dstore).entries("tier_policy")


class _KillDuringReplay:
    """A replayer that lets the human kill the run while K3 is re-executing (the race the guard must close)."""

    def __init__(self, inner, store, run_id):
        self.inner, self.store, self.run_id, self.n = inner, store, run_id, 0

    def replay(self, record, vault):
        from custom_harness.studio.kill import kill_run
        self.n += 1
        if self.n == 1:
            kill_run(self.store, self.run_id, by="alice")
        return self.inner.replay(record, vault)


def _guarded(w, replayer=None):
    from custom_harness.studio.kill import RunGuardedKernel
    run = Run(harness_id="h", harness_hash="h", task_id="t")
    w.store.add_run(run)
    return run.id, RunGuardedKernel(w.store, run_id=run.id, replayer=replayer or w.replayer)


def test_nothing_certified_after_the_kill(tmp_path):
    w = World(tmp_path / "w")
    try:
        c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
        w.kernel.preregister(c)
        ids = w.evaluate_all(c)
        # 1. killed before certify: refused, no certificate entry at all
        run_id, k = _guarded(w)
        from custom_harness.studio.kill import kill_run
        kill_run(w.store, run_id, by="alice")
        with pytest.raises(KernelError, match="certification refused"):
            k.certify(c, ids)
        with pytest.raises(KernelError, match="evidence refused"):
            k.record_evaluation(None)
        assert not w.kernel.ledger.entries("certificate")
        # 2. the kill lands while the checks run: the certificate recorded after it is retracted at once
        run_id, k = _guarded(w)
        k.replayer = _KillDuringReplay(w.replayer, w.store, run_id)
        cert = k.certify(c, ids)
        assert k.replayer.n >= 1  # the attack really happened: the kill landed during re-execution
        assert cert.label.value == "CERTIFIED-INTERNAL"  # it would have stood
        kill = next(e for e in _kills(w.store) if e.body["run_id"] == run_id)
        after = [e for e in w.kernel.ledger.entries("certificate") if e.seq > kill.seq]
        assert [e.body["cert_hash"] for e in after] == [cert.cert_hash()]
        assert all(w.kernel.status(e.body["cert_hash"]) == "retracted" for e in after)
        (r,) = w.kernel.ledger.entries("retraction")
        assert r.body["reason"].startswith("run_killed:")
        # 3. an unkilled run's Kernel certifies normally
        run_id, k = _guarded(w)
        assert k.status(k.certify(c, ids).cert_hash()) == "active"
    finally:
        w.close()
