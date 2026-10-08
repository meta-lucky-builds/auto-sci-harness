"""SSH compute backend: TRUSTED code only, with a recorded human approval of exactly that request (GOAL §5 L0,
invariant 1). Every test here uses a FakeTransport (a local temp dir standing in for the remote host) or a fake
subprocess runner: no network, no real SSH connection is ever made."""

import base64
import io
import shlex
import sys
from pathlib import Path

import pytest

from custom_harness.compute import (
    CodeOrigin,
    IsolationClass,
    IsolationRefused,
    LocalProcessBackend,
    RemoteNotBuilt,
    Revision,
    RunRequest,
    SSHBackend,
    find_approval,
    record_non_isolated_approval,
)
from custom_harness.compute import ssh as ssh_mod
from custom_harness.compute.ssh import (
    OpenSSHTransport,
    RemoteError,
    RemoteExec,
    SSHTransport,
    UploadEntry,
    UploadRefused,
    collect_upload,
)
from custom_harness.sandbox.base import BoundedRun, safe_path


class FakeTransport(SSHTransport):
    """Stands in for a remote host: `root` is a local directory playing the remote filesystem. It records every
    operation and the files each remote dir held when exec ran. It never executes the command."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.ops: list[tuple] = []
        self.seen: list[dict[str, bytes]] = []

    def _local(self, remote_dir: str) -> Path:
        return safe_path(self.root, remote_dir)

    def make_new_dir(self, remote_dir: str) -> None:
        self.ops.append(("mkdir", remote_dir))
        d = self._local(remote_dir)
        d.parent.mkdir(parents=True, exist_ok=True)
        d.mkdir(exist_ok=False)  # never reuse

    def upload(self, remote_dir: str, entries) -> None:
        self.ops.append(("upload", remote_dir, [e.rel for e in entries]))
        base = self._local(remote_dir)
        for e in entries:
            p = safe_path(base, e.rel)
            if e.data is None:
                p.mkdir(parents=True, exist_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(e.data)

    def exec(self, remote_dir: str, command, env, timeout_s: float) -> RemoteExec:
        self.ops.append(("exec", remote_dir, list(command), dict(env), timeout_s))
        base = self._local(remote_dir)
        self.seen.append({p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()})
        return RemoteExec(returncode=0, stdout="ran " + " ".join(command), stderr="", wall_s=0.01, timed_out=False)

    def fetch(self, remote_dir: str, rel: str) -> bytes:
        self.ops.append(("fetch", remote_dir, rel))
        return safe_path(self._local(remote_dir), rel).read_bytes()


def _attack_code(marker: Path) -> bytes:
    return f"open({str(marker)!r}, 'w').write('escaped')\n".encode()


def _req(store, code: bytes, *, origin=CodeOrigin.AGENT, cmd=None, env=None, key="run") -> RunRequest:
    rev = Revision.record_file("solution.py", code, store.blobs)
    return RunRequest(rev, tuple(cmd or ("python3", "solution.py")), origin=origin, env=env or {}, key=key)


def _backend(store, tmp_path, **kw):
    fake = FakeTransport(tmp_path / "remote")
    return SSHBackend(store, tmp_path / "runs", host="gpu-box", transport=fake, **kw), fake


def _nothing_materialised(tmp_path) -> bool:
    runs = tmp_path / "runs"
    return not runs.exists() or not any(runs.iterdir())


# ---------------------------------------------------------------- admission (before the transport is touched)
def test_ssh_backend_with_a_transport_is_still_trusted_only(store, tmp_path):
    backend, _ = _backend(store, tmp_path)
    assert backend.isolation_class is IsolationClass.TRUSTED_ONLY
    iso = backend.isolation
    assert not iso.container and not iso.no_secrets and not iso.read_only_root and not iso.non_root
    assert iso.network_policy == "unknown"


def test_attack_agent_code_is_refused_without_and_with_an_approval_and_the_transport_sees_nothing(store, tmp_path):
    marker = tmp_path / "host_marker.txt"
    code = _attack_code(marker)
    # the payload is real: run as approved trusted code on the host backend, it does reach the host
    control = LocalProcessBackend(store, tmp_path / "control")
    creq = _req(store, code, origin=CodeOrigin.TRUSTED, cmd=(sys.executable, "solution.py"))
    record_non_isolated_approval(store, approver="alice", backend=control.name, request=creq)
    control.run(creq)
    assert marker.read_text() == "escaped", "the attack payload does not work: the test would prove nothing"
    marker.unlink()

    backend, fake = _backend(store, tmp_path)
    req = _req(store, code, cmd=(sys.executable, "solution.py"))
    with pytest.raises(IsolationRefused, match="invariant 1"):
        backend.run(req)
    seq = record_non_isolated_approval(store, approver="alice", backend=backend.name, request=req)
    assert find_approval(store, req.item_hash(backend.name)) == seq  # the approval really is on the ledger
    with pytest.raises(IsolationRefused, match="never runs on a non-isolated backend"):
        backend.run(req)
    assert not marker.exists()
    assert fake.ops == [] and not (tmp_path / "remote").exists()
    assert _nothing_materialised(tmp_path), "materialised before admission"


def test_trusted_code_without_a_recorded_approval_is_refused_and_the_transport_is_never_touched(store, tmp_path):
    backend, fake = _backend(store, tmp_path)
    with pytest.raises(IsolationRefused, match="recorded human approval"):
        backend.run(_req(store, b"print(1)", origin=CodeOrigin.TRUSTED))
    assert fake.ops == [] and _nothing_materialised(tmp_path)


def test_approved_trusted_code_runs_in_a_fresh_remote_dir_holding_exactly_the_recorded_revision(store, tmp_path):
    backend, fake = _backend(store, tmp_path)
    req = _req(store, b"print('hello')", origin=CodeOrigin.TRUSTED, env={"PYTHONHASHSEED": "0"}, key="task-0")
    seq = record_non_isolated_approval(store, approver="alice", backend=backend.name, request=req)
    a, b = backend.run(req), backend.run(req)  # a retry of the same request
    for res in (a, b):
        assert res.approval_seq == seq and res.isolation_class is IsolationClass.TRUSTED_ONLY
        assert res.result.returncode == 0 and res.result.stdout == "ran python3 solution.py"
        assert res.environment["backend"] == "ssh" and res.environment["host"] == "gpu-box"
    assert fake.seen == [{"solution.py": b"print('hello')"}] * 2  # exactly the recorded revision, each time
    mkdirs = [op[1] for op in fake.ops if op[0] == "mkdir"]
    assert len(mkdirs) == 2 and mkdirs[0] != mkdirs[1], "a retry must never reuse a remote dir"
    assert [res.environment["remote_dir"] for res in (a, b)] == mkdirs
    assert all(m.startswith("chh-runs/task-0-") for m in mkdirs)
    assert [op[0] for op in fake.ops] == ["mkdir", "upload", "exec"] * 2
    execs = [op for op in fake.ops if op[0] == "exec"]
    assert execs[0][3] == {"PYTHONHASHSEED": "0"} and execs[0][4] == req.timeout_s
    assert a.run_dir != b.run_dir  # the local materialisation is fresh too
    assert fake.fetch(mkdirs[0], "solution.py") == b"print('hello')"


def test_a_snapshot_revision_is_uploaded_with_its_relative_paths(store, tmp_path):
    live = tmp_path / "live"
    (live / "pkg" / "empty").mkdir(parents=True)
    (live / "solution.py").write_text("from pkg import v")
    (live / "pkg" / "__init__.py").write_text("v = 1")
    rev = Revision.record_tree(live, store.blobs)
    backend, fake = _backend(store, tmp_path)
    req = RunRequest(rev, ("python3", "solution.py"), origin=CodeOrigin.TRUSTED)
    record_non_isolated_approval(store, approver="alice", backend=backend.name, request=req)
    backend.run(req)
    assert fake.seen == [{"solution.py": b"from pkg import v", "pkg/__init__.py": b"v = 1"}]
    uploaded = next(op[2] for op in fake.ops if op[0] == "upload")
    assert sorted(uploaded) == ["pkg", "pkg/__init__.py", "pkg/empty", "solution.py"]


def test_an_approval_for_a_different_command_or_backend_does_not_admit(store, tmp_path):
    backend, fake = _backend(store, tmp_path)
    req = _req(store, b"print(1)", origin=CodeOrigin.TRUSTED)
    other_cmd = _req(store, b"print(1)", origin=CodeOrigin.TRUSTED, cmd=("python3", "solution.py", "--extra"))
    record_non_isolated_approval(store, approver="alice", backend=backend.name, request=other_cmd)
    record_non_isolated_approval(store, approver="alice", backend="local-process", request=req)
    other_env = _req(store, b"print(1)", origin=CodeOrigin.TRUSTED, env={"X": "1"})
    for r in (req, other_env):
        with pytest.raises(IsolationRefused):
            backend.run(r)
    assert fake.ops == [] and _nothing_materialised(tmp_path)


def test_without_a_transport_the_backend_still_never_connects(store, tmp_path):
    backend = SSHBackend(store, tmp_path / "runs", host="gpu-box")
    req = _req(store, b"print(1)", origin=CodeOrigin.TRUSTED)
    record_non_isolated_approval(store, approver="alice", backend=backend.name, request=req)
    with pytest.raises(RemoteNotBuilt):
        backend.run(req)


def test_cpu_supervision_and_entropy_pinning_are_refused_not_silently_ignored(store, tmp_path):
    backend, fake = _backend(store, tmp_path)
    run_dir = tmp_path / "ws"
    run_dir.mkdir()
    box = backend._sandbox(run_dir)
    with pytest.raises(RemoteNotBuilt, match="CPU"):
        box.exec(["true"], cpu_s=1.0)
    assert fake.ops == []


# ---------------------------------------------------------------- upload refuses link-like and special entries
def test_upload_refuses_a_link_like_entry(store, tmp_path, monkeypatch):
    """This Windows account cannot create symlinks, so the refusal is tested by making `is_link_like` report one
    entry of the materialised revision as a link (the same function the real check uses)."""
    backend, fake = _backend(store, tmp_path)
    req = _req(store, b"print(1)", origin=CodeOrigin.TRUSTED)
    record_non_isolated_approval(store, approver="alice", backend=backend.name, request=req)
    real = ssh_mod.is_link_like
    monkeypatch.setattr(ssh_mod, "is_link_like", lambda p: Path(p).name == "solution.py" or real(p))
    with pytest.raises(UploadRefused, match=r"solution\.py"):
        backend.run(req)
    assert fake.ops == [], "the upload is checked before the remote is touched"


def test_collect_upload_keeps_relative_paths_and_refuses_links_at_any_depth(tmp_path, monkeypatch):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f.txt").write_bytes(b"x")
    (tmp_path / "top.txt").write_bytes(b"y")
    got = {e.rel: e.data for e in collect_upload(tmp_path)}
    assert got == {"a": None, "a/b": None, "a/b/f.txt": b"x", "top.txt": b"y"}
    real = ssh_mod.is_link_like
    monkeypatch.setattr(ssh_mod, "is_link_like", lambda p: Path(p).name == "b" or real(p))  # a junction directory
    with pytest.raises(UploadRefused, match="a/b"):
        collect_upload(tmp_path)
    monkeypatch.setattr(ssh_mod, "is_link_like", lambda p: False)
    monkeypatch.setattr(ssh_mod, "_is_regular", lambda st: False)  # a FIFO / device / socket
    with pytest.raises(UploadRefused, match="special"):
        collect_upload(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="this Windows account cannot create symlinks")
def test_collect_upload_refuses_a_real_symlink(tmp_path):  # pragma: no cover - runs on POSIX only
    (tmp_path / "secret").write_text("s")
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(UploadRefused):
        collect_upload(tmp_path / "ws")


# ---------------------------------------------------------------- OpenSSHTransport: argv only, never a real host
class FakeRunner:
    def __init__(self, results=None):
        self.calls: list[dict] = []
        self.results = list(results or [])

    def __call__(self, args, *, timeout_s, max_output_bytes, stdin_data=None, **kw):
        self.calls.append({"args": args, "timeout_s": timeout_s, "stdin": stdin_data, "kw": kw})
        if self.results:
            return self.results.pop(0)
        return BoundedRun(0, "out", "", 0, 0, 0.5, False, None)


HOSTILE = "chh-runs/x'; touch /tmp/pwned; echo '$(id) `id` \"q\""


def _remote_cmd(args) -> list[str]:
    """The remote command line ssh would hand to the remote shell, split as that shell would (POSIX sh)."""
    i = args.index("--")
    assert args[i + 1] == "gpu-box"
    return shlex.split(" ".join(args[i + 2:]))


def _check_safe_options(args, connect_timeout=7):
    assert isinstance(args, list) and all(isinstance(a, str) for a in args)
    assert args[0] == "ssh"
    opts = args[: args.index("--")]
    for o in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ForwardAgent=no", "ForwardX11=no",
              "ClearAllForwardings=yes", f"ConnectTimeout={connect_timeout}"):
        assert ["-o", o] == opts[opts.index(o) - 1: opts.index(o) + 1], o
    assert "-a" in opts and "-x" in opts and "-T" in opts
    assert not any("accept-new" in a or "StrictHostKeyChecking=no" in a for a in args)
    assert "-A" not in opts and "-X" not in opts and "-Y" not in opts


def test_openssh_argv_has_the_safe_options_and_quotes_a_hostile_path_and_command():
    run = FakeRunner()
    t = OpenSSHTransport("gpu-box", connect_timeout_s=7, runner=run)
    t.make_new_dir(HOSTILE)
    t.upload(HOSTILE, [UploadEntry("a", None, 0o755), UploadEntry("a/s.py", b"print(1)", 0o644)])
    hostile_cmd = ["python3", "-c", "import os; os.system('rm -rf ~')", "$(id)", "a b;c"]
    t.exec(HOSTILE, hostile_cmd, {"SEED": "1; rm -rf /"}, 30.0)
    assert len(run.calls) == 3
    for c in run.calls:
        _check_safe_options(c["args"])
    mk, up, ex = (_remote_cmd(c["args"]) for c in run.calls)
    # every hostile string reaches the remote shell as ONE literal argument
    assert mk[-3:] == ["mkdir", "--", HOSTILE]  # the leaf is created without -p: an existing dir fails
    assert HOSTILE in up and up[0] == "tar"
    assert ex[:3] == ["cd", "--", HOSTILE]
    assert ex[-len(hostile_cmd):] == hostile_cmd
    assert "SEED=1; rm -rf /" in ex
    # the upload is one tar stream on stdin holding exactly the entries
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(run.calls[1]["stdin"])) as tf:
        members = {m.name: (m.type, m.mode) for m in tf.getmembers()}
        assert members == {"a": (tarfile.DIRTYPE, 0o755), "a/s.py": (tarfile.REGTYPE, 0o644)}
        f = tf.extractfile("a/s.py")
        assert f is not None and f.read() == b"print(1)"


def test_openssh_honours_the_timeout_locally_and_remotely():
    timed = BoundedRun(None, "", "", 0, 0, 12.0, True, None)
    run = FakeRunner([timed])
    t = OpenSSHTransport("gpu-box", connect_timeout_s=7, runner=run)
    res = t.exec("chh-runs/d", ["sleep", "100"], {}, 10.0)
    assert res.timed_out and res.returncode == -1
    call = run.calls[0]
    assert 10.0 < call["timeout_s"] <= 10.0 + 7 + 60  # local backstop: the run's timeout plus connect + grace
    remote = _remote_cmd(call["args"])
    j = remote.index("timeout")
    assert remote[j:j + 4] == ["timeout", "-k", "5", "10"]


def test_openssh_setup_failures_raise_and_bad_hosts_or_env_names_are_refused():
    run = FakeRunner([BoundedRun(255, "", "Host key verification failed.", 0, 0, 0.1, False, None)])
    t = OpenSSHTransport("gpu-box", runner=run)
    with pytest.raises(RemoteError, match="Host key verification failed"):
        t.make_new_dir("chh-runs/d")
    for bad in ("-oProxyCommand=touch /tmp/x", "", "a b", "h\nx"):
        with pytest.raises(ValueError):
            OpenSSHTransport(bad, runner=run)
    t2 = OpenSSHTransport("user@gpu-box", runner=FakeRunner())
    with pytest.raises(ValueError):
        t2.exec("chh-runs/d", ["true"], {"A=B": "1"}, 1.0)
    with pytest.raises(ValueError):
        t2.exec("chh-runs/d", [], {}, 1.0)
    for bad_rel in ("../x", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(ValueError):
            t2.fetch("chh-runs/d", bad_rel)


def test_openssh_fetch_reads_one_file_with_a_quoted_path():
    payload = bytes([0]) + b"data" + bytes([13, 10])  # binary, with a NUL and CRLF
    run = FakeRunner([BoundedRun(0, base64.b64encode(payload).decode() + "\n", "", 0, 0, 0.1, False, None)])
    t = OpenSSHTransport("gpu-box", connect_timeout_s=7, runner=run)
    assert t.fetch(HOSTILE, "out/result.json") == payload  # binary-safe
    _check_safe_options(run.calls[0]["args"])
    assert _remote_cmd(run.calls[0]["args"])[-1] == HOSTILE + "/out/result.json"


def test_openssh_default_runner_never_uses_a_shell(monkeypatch):
    """Down to Popen: the real runner (sandbox.base.run_bounded) is called with an argv list and no shell=True. The
    fake Popen means nothing is executed and no connection is attempted."""
    import custom_harness.sandbox.base as base

    seen: list[tuple] = []

    class FakePopen:
        def __init__(self, args, **kw):
            seen.append((args, kw))
            self.stdout, self.stderr, self.stdin = io.BytesIO(b"ok"), io.BytesIO(b""), io.BytesIO()
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(base.subprocess, "Popen", FakePopen)
    t = OpenSSHTransport("gpu-box")
    res = t.exec("chh-runs/d", ["echo", "hi"], {}, 5.0)
    assert res.returncode == 0 and res.stdout == "ok"
    (args, kw), = seen
    assert isinstance(args, list) and args[0] == "ssh" and not kw.get("shell", False)
    _check_safe_options(args, connect_timeout=10)
