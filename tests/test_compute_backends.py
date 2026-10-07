"""Compute backends declare an isolation class; agent code runs only on an isolated backend (GOAL §5 L0, invariant 1),
and every run executes a recorded revision in a new directory of its own, never a live working tree."""

import sys
from pathlib import Path
from typing import ClassVar

import pytest

from custom_harness.compute import (
    CodeOrigin,
    DockerBackend,
    IsolationClass,
    IsolationRefused,
    LiveTreeRefused,
    LocalProcessBackend,
    RemoteNotBuilt,
    Revision,
    RunRequest,
    SSHBackend,
    find_approval,
    materialise,
    record_non_isolated_approval,
    unique_workspaces,
)
from custom_harness.sandbox import LocalSandbox
from custom_harness.sandbox.base import ExecResult, Sandbox
from custom_harness.sandbox.docker import CANDIDATE_IMAGE, DockerSandbox, docker_available


class FakeContainer(Sandbox):
    """Stands in for DockerSandbox (no Docker): records the workspace and the files it held when exec ran."""

    isolation = "fake container"
    made: ClassVar[list["FakeContainer"]] = []

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.seen: dict[str, bytes] = {}
        FakeContainer.made.append(self)

    def exec(self, command, timeout_s=600, env=None):
        self.seen = {p.relative_to(self.workspace).as_posix(): p.read_bytes()
                     for p in self.workspace.rglob("*") if p.is_file()}
        return ExecResult(returncode=0, stdout="ran " + " ".join(command), stderr="", wall_s=0.0)

    def spawn(self, workspace):
        return FakeContainer(workspace)

    def environment(self):
        return {"sandbox": "fake"}


@pytest.fixture(autouse=True)
def _reset():
    FakeContainer.made.clear()


def _attack_code(marker: Path) -> bytes:
    # agent-written code that acts on the HOST if it ever runs outside a container
    return f"open({str(marker)!r}, 'w').write('escaped')\n".encode()


def _req(store, code: bytes, *, origin=CodeOrigin.AGENT, cmd=None, env=None) -> RunRequest:
    rev = Revision.record_file("solution.py", code, store.blobs)
    return RunRequest(rev, tuple(cmd or (sys.executable, "solution.py")), origin=origin, env=env or {})


def test_isolation_class_is_derived_from_the_backend_settings(store, tmp_path):
    assert DockerBackend(store, tmp_path).isolation_class is IsolationClass.ISOLATED
    open_net = DockerBackend(store, tmp_path, network=True)
    assert open_net.isolation_class is IsolationClass.TRUSTED_ONLY
    assert "network_policy=open" in open_net.isolation.unmet()
    full = DockerBackend(store, tmp_path, trusted_task_building=True)
    assert full.isolation_class is IsolationClass.TRUSTED_ONLY and not full.isolation.hidden_data_free
    assert LocalProcessBackend(store, tmp_path).isolation_class is IsolationClass.TRUSTED_ONLY
    assert SSHBackend(store, tmp_path, host="gpu-box").isolation_class is IsolationClass.TRUSTED_ONLY


def test_default_origin_is_agent(store):
    assert _req(store, b"print(1)").origin is CodeOrigin.AGENT


@pytest.mark.parametrize("make", [lambda s, p: SSHBackend(s, p, host="gpu-box"),
                                  lambda s, p: LocalProcessBackend(s, p),
                                  lambda s, p: DockerBackend(s, p, network=True, sandbox_maker=FakeContainer)])
def test_attack_agent_code_submitted_to_a_non_isolated_backend_is_refused(store, tmp_path, make):
    marker = tmp_path / "host_marker.txt"
    code = _attack_code(marker)
    # the payload is real: run as trusted code on the host backend, it does reach the host
    LocalProcessBackend(store, tmp_path / "control").run(_req(store, code, origin=CodeOrigin.TRUSTED))
    assert marker.read_text() == "escaped", "the attack payload does not work: the test would prove nothing"
    marker.unlink()
    backend = make(store, tmp_path / "runs")
    with pytest.raises(IsolationRefused, match="invariant 1"):
        backend.run(_req(store, code))
    assert not marker.exists()
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir()), "materialised before admission"
    assert FakeContainer.made == []


def test_agent_code_runs_on_the_isolated_docker_backend_in_a_fresh_dir_of_the_recorded_revision(store, tmp_path):
    backend = DockerBackend(store, tmp_path / "runs", sandbox_maker=FakeContainer)
    req = _req(store, b"print('hello')", cmd=("python", "solution.py"))
    a, b = backend.run(req), backend.run(req)
    assert a.isolation_class is IsolationClass.ISOLATED and a.approval_seq is None
    assert a.run_dir != b.run_dir and a.run_dir.parent == b.run_dir.parent == tmp_path / "runs"
    assert [box.workspace for box in FakeContainer.made] == [a.run_dir, b.run_dir]
    for box in FakeContainer.made:  # each run's directory held exactly the recorded revision, nothing else
        assert box.seen == {"solution.py": b"print('hello')"}


def test_agent_code_never_receives_secret_looking_variables(store, tmp_path):
    backend = DockerBackend(store, tmp_path / "runs", sandbox_maker=FakeContainer)
    with pytest.raises(IsolationRefused, match="credentials"):
        backend.run(_req(store, b"print(1)", env={"OPENAI_API_KEY": "x"}))
    assert FakeContainer.made == []
    assert backend.run(_req(store, b"print(1)", env={"PYTHONHASHSEED": "0"})).result.returncode == 0


def test_full_image_never_runs_agent_code_even_with_an_approval(store, tmp_path):
    backend = DockerBackend(store, tmp_path / "runs", trusted_task_building=True, sandbox_maker=FakeContainer)
    req = _req(store, b"print(1)")
    record_non_isolated_approval(store, approver="alice", backend=backend.name, request=req)
    with pytest.raises(IsolationRefused, match="invariant 2"):
        backend.run(req)
    assert FakeContainer.made == []
    assert backend.run(_req(store, b"print(1)", origin=CodeOrigin.TRUSTED)).result.returncode == 0


def test_a_recorded_human_approval_binds_exactly_one_request(store, tmp_path):
    backend = LocalProcessBackend(store, tmp_path / "runs")
    req = _req(store, b"print('approved')")
    other = _req(store, b"print('approved')", cmd=(sys.executable, "solution.py", "--extra"))
    with pytest.raises(IsolationRefused):
        backend.run(req)
    seq = record_non_isolated_approval(store, approver="alice", backend=backend.name, request=req, note="one-off")
    assert find_approval(store, req.item_hash(backend.name)) == seq
    res = backend.run(req)
    assert res.approval_seq == seq and res.result.stdout.strip() == "approved"
    with pytest.raises(IsolationRefused):  # a different command is a different item
        backend.run(other)
    with pytest.raises(IsolationRefused):  # an approval for one backend is not one for another
        SSHBackend(store, tmp_path / "ssh", host="h").admit(req)
    with pytest.raises(ValueError):
        record_non_isolated_approval(store, approver="  ", backend=backend.name, request=other)


def test_ssh_backend_is_declared_but_never_connects(store, tmp_path):
    backend = SSHBackend(store, tmp_path / "runs", host="gpu-box")
    trusted = _req(store, b"print(1)", origin=CodeOrigin.TRUSTED)
    with pytest.raises(RemoteNotBuilt):
        backend.run(trusted)
    req = _req(store, b"print(1)")
    record_non_isolated_approval(store, approver="alice", backend=backend.name, request=req)
    with pytest.raises(RemoteNotBuilt):  # admitted, but there is no remote execution path
        backend.run(req)


# ---------------------------------------------------------------- recorded revisions, never live trees
def test_a_run_cannot_be_pointed_at_a_live_tree(store, tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "solution.py").write_text("print('live')")
    backend = DockerBackend(store, tmp_path / "runs", sandbox_maker=FakeContainer)
    for bad in (live, str(live)):
        with pytest.raises(LiveTreeRefused):
            backend.run(RunRequest(bad, ("python", "solution.py")))  # type: ignore[arg-type]
        with pytest.raises(LiveTreeRefused):
            materialise(bad, store.blobs, tmp_path / "runs", "k")  # type: ignore[arg-type]
        with pytest.raises(LiveTreeRefused):
            Revision("snapshot", str(bad))
    assert FakeContainer.made == []


def test_the_run_executes_the_recorded_revision_not_the_live_tree_after_it_changed(store, tmp_path):
    live = tmp_path / "live"
    (live / "pkg").mkdir(parents=True)
    (live / "solution.py").write_text("from pkg import v; print(v)")
    (live / "pkg" / "__init__.py").write_text("v = 'recorded'")
    rev = Revision.record_tree(live, store.blobs)
    (live / "pkg" / "__init__.py").write_text("v = 'edited after recording'")
    (live / "planted.txt").write_text("not part of the revision")
    res = LocalProcessBackend(store, tmp_path / "runs").run(
        RunRequest(rev, (sys.executable, "solution.py"), origin=CodeOrigin.TRUSTED))
    assert res.result.stdout.strip() == "recorded"
    assert res.run_dir.resolve() != live.resolve() and not (res.run_dir / "planted.txt").exists()


def test_a_corrupted_revision_is_refused_before_anything_runs(store, tmp_path):
    rev = Revision.record_file("solution.py", b"print(1)", store.blobs)
    p = store.blobs._path(rev.ref)
    p.write_bytes(b"print('tampered')")
    with pytest.raises(OSError, match="corrupted"):
        DockerBackend(store, tmp_path / "runs", sandbox_maker=FakeContainer).run(RunRequest(rev, ("python",)))
    assert FakeContainer.made == []


def test_unique_workspaces_never_reuses_a_directory_for_the_same_key(tmp_path):
    made = unique_workspaces(lambda ws: LocalSandbox(ws, i_understand_no_isolation=True), tmp_path / "sb")
    a, b = made("spec-task-0"), made("spec-task-0")
    assert a.workspace != b.workspace and a.workspace.name.startswith("spec-task-0-")


@pytest.mark.skipif(not docker_available(), reason="Docker daemon not running")
def test_real_docker_backend_runs_agent_code_from_the_recorded_revision(store, tmp_path):
    try:
        DockerSandbox(tmp_path / "probe")
    except Exception as e:  # candidate image missing: a human must build it
        pytest.skip(f"candidate image unavailable: {e}")
    backend = DockerBackend(store, tmp_path / "runs")
    req = _req(store, b"import os; print(os.getuid(), sorted(os.listdir('.')))", cmd=("python", "solution.py"))
    res = backend.run(req)
    assert res.isolation_class is IsolationClass.ISOLATED and res.environment["image"] == CANDIDATE_IMAGE
    assert res.result.returncode == 0 and res.result.stdout.strip() == "1000 ['solution.py']", res.result
