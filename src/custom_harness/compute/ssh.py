"""SSH execution path for TRUSTED code only (GOAL §5 L0 "Compute backends", invariant 1).

A plain SSH host runs programs as a host user, with that host's files, credentials and network: it is never an
isolation boundary, so the SSH backend is TRUSTED_ONLY and admission (ComputeBackend.admit) refuses agent-written code
there with or without an approval, and admits trusted code only with a recorded human approval of exactly that
request. Admission runs before anything here is touched.

What this module adds, kept small:
- SSHTransport: the minimum remote operations (create a NEW directory, upload a tree, run a command there, fetch a
  file). Tests use a fake transport; nothing in the test suite connects anywhere.
- collect_upload: the host-side walk of the materialised revision. Regular files and directories only; anything
  link-like (symlink, junction, reparse point, unstat-able entry) or special (FIFO, device, socket) is refused, so a
  transport cannot be handed something that would make the host read outside the run directory.
- SSHSandbox: one exec = one fresh remote directory (unique name, created with a plain `mkdir`, which fails if it
  exists), the upload, then the command with cwd = that directory. A retry never inherits a previous attempt's files.
- OpenSSHTransport: the system `ssh` client via subprocess (argv lists only, never a shell on the local side; remote
  paths and arguments quoted with shlex.quote). NOT VERIFIED AGAINST A REAL SSH HOST: only the argv it builds is
  tested, with a fake runner. See docs/compute_ssh.md.
"""

from __future__ import annotations

import base64
import io
import math
import os
import posixpath
import re
import shlex
import stat
import tarfile
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..sandbox.base import (
    DEFAULT_MAX_OUTPUT_BYTES,
    BoundedRun,
    ExecResult,
    Sandbox,
    WorkspaceEscape,
    is_link_like,
    run_bounded,
)
from .backend import RemoteNotBuilt


class UploadRefused(WorkspaceEscape):
    """The local tree holds an entry that is not a plain file or directory (link-like or special)."""


class RemoteError(OSError):
    """A remote setup step (mkdir, upload, fetch) failed: infrastructure, never the program's result."""


@dataclass(frozen=True)
class UploadEntry:
    rel: str  # POSIX relative path inside the remote run directory
    data: bytes | None  # None = a directory
    mode: int


@dataclass(frozen=True)
class RemoteExec:
    returncode: int
    stdout: str
    stderr: str
    wall_s: float
    timed_out: bool
    stdout_omitted: int = 0
    stderr_omitted: int = 0


_REL_PART = re.compile(r"[^/\x00\n\r]+")


def check_rel(rel: str) -> str:
    """A plain POSIX relative path: no absolute path, no '..', no empty or control-character components."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        raise ValueError(f"not a plain relative path: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") or not _REL_PART.fullmatch(p) for p in parts):
        raise ValueError(f"not a plain relative path: {rel!r}")
    return rel


def _is_regular(st: os.stat_result) -> bool:
    return stat.S_ISREG(st.st_mode)


def collect_upload(local_dir: Path) -> list[UploadEntry]:
    """Every directory and regular file under `local_dir`, with POSIX relative paths, in a stable order. Refuses
    (UploadRefused) any link-like or special entry at any depth; never follows one."""
    root = Path(local_dir)
    if is_link_like(root) or not root.is_dir():
        raise UploadRefused(f"upload root {root} is not a plain directory")
    out: list[UploadEntry] = []
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        cur, prefix = stack.pop()
        with os.scandir(cur) as it:
            listing = sorted(it, key=lambda d: d.name)
        for e in listing:
            rel = f"{prefix}{e.name}"
            p = Path(e.path)
            if is_link_like(p):
                raise UploadRefused(f"refusing to upload a link or reparse point at {rel!r}")
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                out.append(UploadEntry(check_rel(rel), None, 0o755))
                stack.append((p, rel + "/"))
            elif _is_regular(st):
                mode = 0o755 if st.st_mode & 0o111 and os.name != "nt" else 0o644
                out.append(UploadEntry(check_rel(rel), p.read_bytes(), mode))
            else:
                raise UploadRefused(f"refusing to upload a special entry (not a file or directory) at {rel!r}")
    return out


class SSHTransport(ABC):
    """The minimum remote operations. Implementations must not reuse a directory: make_new_dir fails if it exists."""

    @abstractmethod
    def make_new_dir(self, remote_dir: str) -> None:
        """Create `remote_dir` (parents as needed); raise if the leaf already exists."""

    @abstractmethod
    def upload(self, remote_dir: str, entries: Sequence[UploadEntry]) -> None:
        """Write `entries` (from collect_upload) under `remote_dir`, keeping their relative paths."""

    @abstractmethod
    def exec(self, remote_dir: str, command: Sequence[str], env: Mapping[str, str], timeout_s: float) -> RemoteExec:
        """Run `command` (an argv) with cwd = `remote_dir`, `env` added, killed after `timeout_s`."""

    def fetch(self, remote_dir: str, rel: str) -> bytes:
        """Optional: the bytes of one file under `remote_dir`."""
        raise NotImplementedError(f"{type(self).__name__} cannot fetch files")


class SSHSandbox(Sandbox):
    """The local `workspace` holds the materialised revision; every exec uploads it into a NEW remote directory."""

    isolation = "none: a plain SSH host (host user, host files, host credentials, host network); trusted code only"

    def __init__(self, workspace: Path, *, host: str, transport: SSHTransport, remote_root: str):
        self.workspace = Path(workspace)
        self.host, self.transport, self.remote_root = host, transport, remote_root
        self.remote_dirs: list[str] = []

    def spawn(self, workspace: Path) -> SSHSandbox:
        return SSHSandbox(workspace, host=self.host, transport=self.transport, remote_root=self.remote_root)

    def environment(self) -> dict[str, str]:
        env = {"backend": "ssh", "host": self.host, "sandbox": self.isolation, "remote_root": self.remote_root}
        if self.remote_dirs:
            env["remote_dir"] = self.remote_dirs[-1]
        return env

    def exec(self, command: list[str], timeout_s: float = 600, env: dict[str, str] | None = None, *,
             cpu_s: float | None = None) -> ExecResult:
        if cpu_s is not None or self.entropy is not None:
            raise RemoteNotBuilt("CPU supervision and entropy pinning are not built for the SSH backend")
        entries = collect_upload(self.workspace)  # checked before the remote is touched
        remote_dir = posixpath.join(self.remote_root, f"{self.workspace.name}-{uuid.uuid4().hex[:12]}")
        self.transport.make_new_dir(remote_dir)
        self.remote_dirs.append(remote_dir)
        self.transport.upload(remote_dir, entries)
        r = self.transport.exec(remote_dir, list(command), dict(env or {}), timeout_s)
        return ExecResult(returncode=-1 if r.timed_out else r.returncode, stdout=r.stdout, stderr=r.stderr,
                          wall_s=r.wall_s, timed_out=r.timed_out, stdout_omitted=r.stdout_omitted,
                          stderr_omitted=r.stderr_omitted)


def check_remote_root(remote_root: str) -> str:
    """A remote root is a relative path (under the remote user's home) or an absolute one, without '..'."""
    if not isinstance(remote_root, str) or not remote_root.strip():
        raise ValueError("remote_root must be a non-empty path")
    check_rel(remote_root.lstrip("/") if remote_root.startswith("/") else remote_root)
    return remote_root


# ---------------------------------------------------------------- the real client (NOT verified against a real host)
Runner = Callable[..., BoundedRun]
_HOST = re.compile(r"(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._:\[\]-]+")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GRACE_S = 30.0  # local backstop beyond the remote timeout and the connect timeout
_KILL_AFTER_S = 5


class OpenSSHTransport(SSHTransport):
    """The system OpenSSH client, one `ssh` process per operation. NOT VERIFIED AGAINST A REAL SSH HOST.

    Options on every call: BatchMode=yes (never prompts), StrictHostKeyChecking=yes (the host key must already be in
    known_hosts; never accept-new), no agent forwarding (-a, ForwardAgent=no), no X11 (-x, ForwardX11=no), no port
    forwardings (ClearAllForwardings=yes), no tty (-T), a connect timeout. Command-line -o options take precedence
    over ~/.ssh/config. The host is validated (it can never start with '-', i.e. be read as an option) and is placed
    after '--'. The remote command is ONE string built only from shlex.quote'd pieces; locally it is an argv list run
    by sandbox.base.run_bounded (subprocess.Popen without a shell, bounded output, a timeout).

    Remote requirements (assumed, not verified): a POSIX shell, mkdir, tar, env, base64 and GNU coreutils `timeout`.
    """

    def __init__(self, host: str, *, ssh: str = "ssh", connect_timeout_s: int = 10, runner: Runner | None = None,
                 max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES):
        if not isinstance(host, str) or not _HOST.fullmatch(host) or host.startswith("-") or "@-" in host:
            raise ValueError(f"refusing SSH host {host!r} (must be [user@]hostname, never an option)")
        self.host, self.ssh, self.connect_timeout_s = host, ssh, int(connect_timeout_s)
        self._runner = runner
        self.max_output_bytes = max_output_bytes

    def argv(self, remote_command: str) -> list[str]:
        return [self.ssh, "-a", "-x", "-T",
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ForwardAgent=no",
                "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
                "-o", f"ConnectTimeout={self.connect_timeout_s}",
                "--", self.host, remote_command]

    def _run(self, remote_command: str, timeout_s: float, stdin_data: bytes | None = None) -> BoundedRun:
        runner = self._runner or run_bounded  # looked up per call (tests patch the subprocess layer under it)
        kw: dict[str, Any] = {"timeout_s": timeout_s, "max_output_bytes": self.max_output_bytes}
        if stdin_data is not None:
            kw["stdin_data"] = stdin_data
        return runner(self.argv(remote_command), **kw)

    def _setup(self, what: str, remote_command: str, stdin_data: bytes | None = None,
               timeout_s: float = 300.0) -> BoundedRun:
        r = self._run(remote_command, timeout_s + self.connect_timeout_s, stdin_data)
        if r.timed_out or r.returncode != 0:
            raise RemoteError(f"ssh {self.host}: {what} failed (rc={r.returncode}, timed_out={r.timed_out}): "
                              f"{r.stderr.strip()[-2000:]}")
        return r

    def make_new_dir(self, remote_dir: str) -> None:
        parent = posixpath.dirname(remote_dir.rstrip("/")) or "."
        q = shlex.quote
        self._setup("mkdir", f"umask 077 && mkdir -p -- {q(parent)} && mkdir -- {q(remote_dir)}")

    def upload(self, remote_dir: str, entries: Sequence[UploadEntry]) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
            for e in entries:
                info = tarfile.TarInfo(check_rel(e.rel))
                info.mode = e.mode
                if e.data is None:
                    info.type = tarfile.DIRTYPE
                    tf.addfile(info)
                else:
                    info.size = len(e.data)
                    tf.addfile(info, io.BytesIO(e.data))
        q = shlex.quote
        self._setup("upload", f"tar -x -f - --no-same-owner -C {q(remote_dir)}", stdin_data=buf.getvalue())

    def exec(self, remote_dir: str, command: Sequence[str], env: Mapping[str, str], timeout_s: float) -> RemoteExec:
        if not command:
            raise ValueError("empty command")
        bad = [k for k in env if not _ENV_NAME.fullmatch(k)]
        if bad:
            raise ValueError(f"invalid environment variable names {bad}")
        q = shlex.quote
        secs = max(1, math.ceil(timeout_s))
        parts = ["cd", "--", q(remote_dir), "&&", "exec", "env", "--",
                 *(q(f"{k}={v}") for k, v in sorted(env.items())),
                 "timeout", "-k", str(_KILL_AFTER_S), str(secs), *(q(c) for c in command)]
        remote = " ".join(parts)
        r = self._run(remote, timeout_s + self.connect_timeout_s + _GRACE_S)
        # the remote `timeout` exits 124 (137 after -k); the local backstop sets timed_out
        timed_out = r.timed_out or r.returncode in (124, 137)
        return RemoteExec(returncode=-1 if timed_out or r.returncode is None else r.returncode, stdout=r.stdout,
                          stderr=r.stderr, wall_s=r.wall_s, timed_out=timed_out, stdout_omitted=r.stdout_omitted,
                          stderr_omitted=r.stderr_omitted)

    def fetch(self, remote_dir: str, rel: str) -> bytes:
        path = posixpath.join(remote_dir, check_rel(rel))
        r = self._setup("fetch", f"base64 -- {shlex.quote(path)}")
        if r.stdout_omitted:
            raise RemoteError(f"ssh {self.host}: {rel!r} is larger than the output limit ({self.max_output_bytes})")
        return base64.b64decode(r.stdout)
