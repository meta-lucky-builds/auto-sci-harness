"""GOAL §5 L0: every run executes a snapshot of a RECORDED revision in its own directory, never a live working tree.

What the code does today (verified by reading, proved here end to end): agent programs execute in three places,
dev folds (research/modules._dev_predict), held-out confirmation (research/claims.confirm) and Kernel replay
(kernel/execution.SandboxReplayer). Each spawns a NEW sandbox directory and writes the program from recorded bytes
(a stored blob) into it. The witness sandbox below records, for every program execution, where it ran, whether that
directory existed before its sandbox was spawned, and the bytes of the program it ran. Pointing any of the three at
the run's own (live) workspace makes the test fail.
"""

import hashlib
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.errors import InfraError
from custom_harness.gateway import MockBackend
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.runtime.engine import RetryPolicy
from custom_harness.sandbox import LocalSandbox
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401  (fixture re-used)


class Witness(LocalSandbox):
    log: ClassVar[list[dict]] = []
    fail_confirm_exec: int | None = None
    confirm_execs = 0

    def __init__(self, workspace, *, fresh=False, root=False):
        super().__init__(workspace, i_understand_no_isolation=True)
        self.fresh, self.root = fresh, root

    def spawn(self, workspace):
        return Witness(workspace, fresh=not Path(workspace).exists())

    def exec(self, command, timeout_s=600, env=None):
        progs = [c for c in command if c.endswith(".py") and (self.workspace / c).is_file()]
        code = (self.workspace / progs[0]).read_bytes() if progs else b""
        Witness.log.append({"ws": Path(self.workspace).resolve(), "fresh": self.fresh, "root": self.root,
                            "sha": hashlib.sha256(code).hexdigest(), "has_program": bool(progs)})
        if "-confirm" in str(self.workspace) and Witness.fail_confirm_exec is not None:
            Witness.confirm_execs += 1
            if Witness.confirm_execs == Witness.fail_confirm_exec:
                (self.workspace / "left_by_failed_attempt.txt").write_text("state from the attempt that failed")
                raise InfraError("Cannot connect to the Docker daemon", transient=True)
        return super().exec(command, timeout_s, env)


@pytest.fixture(autouse=True)
def _reset():
    Witness.log, Witness.fail_confirm_exec, Witness.confirm_execs = [], None, 0


def _run(store, tmp_path, rings_task, **kw):  # noqa: F811
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(scripted_llm)
    root = Witness(tmp_path / "ws", root=True)
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=root, **kw).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    return res, root


def _check(store, root_ws: Path, entries):
    root_ws = root_ws.resolve()
    for e in entries:
        assert not e["root"] and e["ws"] != root_ws and not e["ws"].is_relative_to(root_ws), \
            f"a program ran in the live run workspace: {e}"
        assert e["fresh"], f"a program ran in a directory that existed before its sandbox: {e}"
        assert store.blobs.has(e["sha"]), f"the program run is not a recorded revision: {e}"


def test_every_program_runs_a_recorded_revision_in_its_own_new_directory(store, tmp_path, rings_task):  # noqa: F811
    res, root = _run(store, tmp_path, rings_task)
    progs = [e for e in Witness.log if e["has_program"]]
    kinds = {k for e in progs for k in ("-dev", "-confirm") if k in str(e["ws"])}
    _check(store, root.workspace, Witness.log)
    assert kinds == {"-dev", "-confirm"} and len(progs) >= 10, "the witness saw no dev or confirm runs"
    cert = res.state["certificate"]
    confirm_shas = {e["sha"] for e in progs if "-confirm" in str(e["ws"])}
    claims = [v for k, v in res.state.items() if k.endswith("claim") and isinstance(v, dict) and "subject" in v]
    assert len(claims) >= 2, "expected the headline claim and its replica"
    assert confirm_shas == {c[arm]["hash"] for c in claims for arm in ("subject", "comparator")}
    # Kernel replay (K3 / reproduce) runs the recorded bytes in new directories too
    from custom_harness.kernel.bundle import reproduce_bundle
    from custom_harness.kernel.execution import SandboxReplayer
    Witness.log = []
    rep = reproduce_bundle(Path(cert["bundle_path"]), lambda: SandboxReplayer(
        lambda key: Witness(tmp_path / "repro" / key, fresh=not (tmp_path / "repro" / key).exists())))
    assert rep.reproduced, rep.problems
    assert Witness.log, "the replay ran nothing"
    _check(store, root.workspace, Witness.log)


def test_a_retried_confirmation_never_reuses_the_failed_attempts_directory(store, tmp_path, rings_task):  # noqa: F811
    Witness.fail_confirm_exec = 4
    _run(store, tmp_path, rings_task, retry=RetryPolicy(max_attempts=3, backoff_s=0.0))
    assert Witness.confirm_execs > 4, "the infrastructure failure did not happen: the test is not testing anything"
    _check(store, tmp_path / "ws", [e for e in Witness.log if e["has_program"]])


@pytest.mark.parametrize("which", ["chh", "chh-audit"])
def test_cli_sandbox_factories_never_reuse_a_directory_for_the_same_key(tmp_path, which):
    """Same class as the confirm-retry gap: the evolve Evaluator retries an infra-failed unit under the SAME key
    (spec, task, seed), and the CLI factories used to map a key to `root / key`, handing the retry the failed
    attempt's directory (orcompute-to-flowk-01). Every call now gets a new, not yet existing directory."""
    import argparse

    from custom_harness import cli
    from custom_harness.audit import cli as audit_cli
    make = cli._sandbox_factory if which == "chh" else audit_cli._factory
    factory = make(argparse.Namespace(docker=False, dev_sandbox=True), tmp_path / "sb")
    a = factory("spec-task-0")
    (a.workspace / "left_by_failed_attempt.txt").write_text("x")
    b = factory("spec-task-0")
    assert a.workspace != b.workspace and a.workspace.parent == b.workspace.parent == tmp_path / "sb"
    assert not (b.workspace / "left_by_failed_attempt.txt").exists()
