"""GOAL §16.8 for Kernel-driven replays: K3 re-executions (and held-out confirm runs, research/claims.py) are charged
CPU time with a contention witness, so machine load is INFRA (InfraError: no certificate, retry), never a K3 policy
verdict; a program that burns CPU or sleeps on an idle host is still the program's failure."""

from __future__ import annotations

import sys
from typing import ClassVar

import pytest

from custom_harness.errors import InfraError
from custom_harness.kernel import Kernel, KernelConfig, Label, SandboxReplayer
from custom_harness.kernel.execution import run_program
from custom_harness.sandbox import LocalSandbox
from custom_harness.sandbox.base import ExecResult
from custom_harness.sandbox.hermetic import CPU_WALL_BACKSTOP
from kernel_world import World

FAST = KernelConfig(k3_eps_max=0.5, k3_beta=0.25)


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")


class Loaded(LocalSandbox):
    """The wall backstop fires below the CPU limit; `contended` is the witness's verdict (None = unavailable)."""

    contended: bool | None = True
    calls: ClassVar[list[tuple[float, float | None]]] = []

    def exec(self, command, timeout_s=600, env=None, *, cpu_s=None):
        Loaded.calls.append((timeout_s, cpu_s))
        if cpu_s is None:
            return super().exec(command, timeout_s, env)
        return ExecResult(returncode=-1, stdout="", stderr="", wall_s=timeout_s, timed_out=True, cpu_s=0.1,
                          contended=Loaded.contended)


def _world_claim(tmp_path):
    w = World(tmp_path / "w", seed=11)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2, eps_max=0.5, beta=0.25)
    w.kernel.preregister(c)
    return w, c, w.evaluate_all(c)


@pytest.mark.parametrize("contended", [True, None])
def test_a_contended_or_unwitnessed_replay_is_infra_not_a_k3_verdict(tmp_path, contended):
    w, c, ids = _world_claim(tmp_path)
    try:
        Loaded.contended, Loaded.calls = contended, []
        k = Kernel(w.store, config=FAST, replayer=SandboxReplayer(
            lambda key: Loaded(tmp_path / "rp" / key, i_understand_no_isolation=True)))
        with pytest.raises(InfraError, match="host load"):
            k.certify(c, ids)
        assert not k.ledger.entries("certificate")  # no verdict was recorded
        # the replay ran under the CPU limit = the recorded wall limit, with the wall backstop on top
        timeout_s, cpu_s = Loaded.calls[0]
        assert cpu_s == 120.0 and timeout_s == CPU_WALL_BACKSTOP * cpu_s
    finally:
        w.close()


def test_an_idle_host_backstop_is_the_programs_failure(tmp_path):
    w, c, ids = _world_claim(tmp_path)
    try:
        Loaded.contended, Loaded.calls = False, []
        k = Kernel(w.store, config=FAST, replayer=SandboxReplayer(
            lambda key: Loaded(tmp_path / "rp" / key, i_understand_no_isolation=True)))
        cert = k.certify(c, ids)
        assert cert.label == Label.REJECTED and "K3_REPLAY_NO_OUTPUT" in cert.checks["K3"].codes
    finally:
        w.close()


def test_the_world_replay_still_certifies_under_the_cpu_limit(tmp_path):
    w, c, ids = _world_claim(tmp_path)
    try:
        assert w.kernel.certify(c, ids).label == Label.CERTIFIED_INTERNAL  # LocalSandbox + real CPU supervisor
    finally:
        w.close()


def test_run_program_charges_cpu_and_a_cpu_burner_fails_as_the_program(tmp_path):
    burn = b"import sys\nwhile True:\n    pass\n"
    box = LocalSandbox(tmp_path / "b", i_understand_no_isolation=True)
    preds, diag = run_program(box, code=burn, entrypoint="solution.py", argv=(sys.executable, "{entrypoint}"),
                              inputs={}, seed=0, timeout_s=5, cpu_s=1.0)
    assert preds is None and "CPU time exceeded" in diag
