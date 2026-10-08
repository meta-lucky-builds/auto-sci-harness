"""Sandbox interface (PLAN L0, B3). Docker/gVisor backends implement the same interface."""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .hermetic import WITNESS_CONTENDED_RATIO, EntropyPin, parse_supervisor, split_record


class WorkspaceEscape(PermissionError):
    """A host-side workspace access would leave the workspace (symlink, junction, '..', absolute path).
    Agent code controls the workspace contents, so this is treated as the agent's failure."""


def safe_path(workspace: Path, rel: str | Path) -> Path:
    """The ONLY way host code may touch a file inside a sandbox workspace.

    Agent code running in the container can replace any workspace path with a symlink. The container
    sees it as harmless, but the host would follow it OUT of the workspace (reading host files into
    evidence, or overwriting them). So every component must be a plain file or directory inside the
    workspace. Safe against races only because host code never runs while agent code is running
    (one container per exec, removed before the next host step)."""
    root = Path(workspace).resolve()
    rel_p = Path(rel)
    if rel_p.is_absolute() or rel_p.drive or any(part == ".." for part in rel_p.parts):
        raise WorkspaceEscape(f"path {str(rel)!r} is not a plain relative path")
    cur = root
    for part in rel_p.parts:
        cur = cur / part
        if is_link_like(cur):
            raise WorkspaceEscape(f"refusing to follow link or special entry at {cur.relative_to(root).as_posix()!r}")
    if not cur.resolve().is_relative_to(root):
        raise WorkspaceEscape(f"path {str(rel)!r} resolves outside the workspace")
    return cur


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (Windows symlinks, junctions, WSL links)


def is_link_like(p: Path) -> bool:
    """True for anything host code must not follow: symlinks, junctions/reparse points, and entries the
    host cannot even lstat (on Windows, a symlink created inside a Linux container shows up as a WSL
    reparse point that fails with WinError 1920). A missing path is not link-like."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


class ExecResult(BaseModel):
    returncode: int
    stdout: str
    stderr: str
    wall_s: float
    timed_out: bool = False
    #: bytes of the stream NOT kept in host memory (the middle of an over-long output; head and tail are kept)
    stdout_omitted: int = 0
    stderr_omitted: int = 0
    #: why the sandbox killed / failed the program for exceeding a resource limit (a POLICY failure of the
    #: program: returncode is LIMIT_RETURNCODE; never an infrastructure failure)
    limit_exceeded: str | None = None
    #: CPU seconds charged to the program (None = no CPU limit was set: today's wall-clock-only behaviour)
    cpu_s: float | None = None
    cpu_exceeded: bool = False
    #: contention witness verdict after a wall backstop fired below the CPU limit (None = no witness run). True means
    #: the host was contended: INFRA, the caller re-runs the WHOLE evaluation (never a single seed)
    contended: bool | None = None
    #: digest of the entropy pin the run had (None = unpinned)
    entropy_pin: str | None = None


# ---------------------------------------------------------------- resource limits (STATUS §D, M0 "agent cannot
# fake an infra failure"): every limit below is enforced as a failure OF THE PROGRAM. Exceeding one returns a normal
# ExecResult (returncode LIMIT_RETURNCODE, reason in limit_exceeded and stderr); it never raises InfraError, so an
# agent cannot turn its own failure into an excluded unit.
DEFAULT_MAX_OUTPUT_BYTES = 1 << 20  # per stream kept in host memory: first quarter + last three quarters
DEFAULT_MAX_WORKSPACE_BYTES = 1 << 30  # total size of regular files in the workspace
DEFAULT_MAX_WORKSPACE_FILES = 100_000  # entries in the workspace (also bounds the cost of measuring it)
WORKSPACE_POLL_S = 1.0  # how often the workspace is measured while the program runs
LIMIT_RETURNCODE = -2  # (-1 = timed out)


def workspace_usage(root: Path, max_bytes: int, max_files: int) -> str | None:
    """Why `root` is over its limits, or None. Walks without following links or junctions (agent code controls the
    tree); stops as soon as a limit is exceeded. Entries the host cannot stat count as files of size 0."""
    total = files = 0
    stack = [str(root)]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                files += 1
                try:
                    if is_link_like(Path(e.path)):
                        pass
                    elif e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        total += e.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
                if total > max_bytes:
                    return f"workspace exceeded its size limit ({max_bytes} bytes)"
                if files > max_files:
                    return f"workspace exceeded its limit of {max_files} files"
    return None


class _Capture:
    """Reads one pipe on a thread, keeping at most `limit` bytes in memory: the first quarter and the last three
    quarters of the stream (diagnostics live at both ends), and counting what was dropped in between."""

    def __init__(self, stream: Any, limit: int):
        self.head_cap = max(0, limit // 4)
        self.tail_cap = max(0, limit - self.head_cap)
        self.head, self.tail, self.total = bytearray(), bytearray(), 0
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: Any) -> None:
        try:
            while True:
                chunk = stream.read1(1 << 16)
                if not chunk:
                    break
                self.total += len(chunk)
                room = self.head_cap - len(self.head)
                if room > 0:
                    self.head += chunk[:room]
                    chunk = chunk[room:]
                if chunk:
                    self.tail += chunk
                    if len(self.tail) > self.tail_cap:
                        del self.tail[: len(self.tail) - self.tail_cap]
        except (OSError, ValueError):
            pass

    def result(self, join_s: float = 10.0) -> tuple[str, int]:
        self._thread.join(join_s)
        omitted = max(0, self.total - len(self.head) - len(self.tail))

        def dec(b: bytearray) -> str:  # text-mode semantics of the old subprocess.run(text=True) capture
            return bytes(b).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        if not omitted:
            return dec(self.head + self.tail), 0
        return (dec(self.head) + f"\n[sandbox] {omitted} bytes of output omitted (host keeps "
                f"{self.head_cap + self.tail_cap} bytes per stream)\n" + dec(self.tail)), omitted


@dataclass
class BoundedRun:
    returncode: int | None  # the local process's code (for Docker: the `docker start -a` client's)
    stdout: str
    stderr: str
    stdout_omitted: int
    stderr_omitted: int
    wall_s: float
    timed_out: bool
    limit_exceeded: str | None


def run_bounded(args: list[str], *, timeout_s: float, max_output_bytes: int, cwd: Path | None = None,
                env: dict[str, str] | None = None, check: Callable[[], str | None] | None = None,
                kill: Callable[[], object] | None = None, poll_s: float = WORKSPACE_POLL_S,
                stdin_data: bytes | None = None) -> BoundedRun:
    """Run a process with bounded output capture. `check` is polled while it runs (and once after it exits);
    a non-None reason kills it (`kill` first, e.g. `docker kill`, then the local process). OSError from starting
    the process propagates (the caller classifies it)."""
    t0 = time.monotonic()
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL if stdin_data is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if stdin_data is not None and proc.stdin is not None:
        with contextlib.suppress(OSError):
            proc.stdin.write(stdin_data)
            proc.stdin.close()
    out, err = _Capture(proc.stdout, max_output_bytes), _Capture(proc.stderr, max_output_bytes)
    deadline = t0 + timeout_s
    timed_out, reason = False, None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            proc.wait(timeout=min(poll_s, remaining))
            break
        except subprocess.TimeoutExpired:
            pass
        if check is not None:
            reason = check()
            if reason:
                break
    if timed_out or reason:
        if kill is not None:
            with contextlib.suppress(Exception):  # best effort; the local process is killed below either way
                kill()
        proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
    wall = time.monotonic() - t0
    if not (timed_out or reason) and check is not None:
        reason = check()  # what the program left behind counts too
    so, so_n = out.result()
    se, se_n = err.result()
    return BoundedRun(proc.returncode, so, se, so_n, se_n, wall, timed_out, reason)


# ---------------------------------------------------------------- protected paths (invariant 2): the evidence store
# (blobs hold private labels), the bundles (they contain hidden labels) and every registered task's private files
# must never be inside a sandbox mount/workspace, nor may a workspace contain them.
_PROTECTED: set[str] = set()


class ProtectedMount(PermissionError):
    """A sandbox workspace would overlap a protected host path (hidden labels would become agent-visible)."""


def _norm(p: Path | str) -> Path:
    return Path(os.path.normcase(str(Path(p).resolve())))


def protect_path(p: Path | str) -> None:
    """Register a host path that no sandbox workspace may overlap (process-wide)."""
    _PROTECTED.add(str(_norm(p)))


def protected_paths() -> list[Path]:
    from ..config import data_dir
    d = data_dir()
    return sorted({_norm(d / "store"), _norm(d / "bundles"), *(Path(p) for p in _PROTECTED)})


def check_mount(workspace: Path | str) -> None:
    """Refuse a workspace that is, is inside, or contains a protected path."""
    ws = _norm(workspace)
    for p in protected_paths():
        if ws == p or ws.is_relative_to(p) or p.is_relative_to(ws):
            raise ProtectedMount(f"sandbox workspace {workspace} overlaps the protected path {p} "
                                 "(evidence store, bundles or a task's private files must never be agent-visible)")


class Sandbox(ABC):
    #: Honest description of what this backend actually isolates.
    isolation: str
    workspace: Path
    #: Python executable to use for commands inside the sandbox.
    python: str = "python"
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_workspace_bytes: int = DEFAULT_MAX_WORKSPACE_BYTES
    max_workspace_files: int = DEFAULT_MAX_WORKSPACE_FILES

    def _workspace_check(self) -> str | None:
        return workspace_usage(self.workspace, self.max_workspace_bytes, self.max_workspace_files)

    def _result(self, r: BoundedRun, returncode: int) -> ExecResult:
        """ExecResult of a bounded run; a resource limit is the program's failure (LIMIT_RETURNCODE)."""
        stderr = r.stderr
        if r.limit_exceeded:
            stderr += f"\n[sandbox] killed: {r.limit_exceeded}"
            returncode = LIMIT_RETURNCODE
        return ExecResult(returncode=-1 if r.timed_out else returncode, stdout=r.stdout, stderr=stderr,
                          wall_s=r.wall_s, timed_out=r.timed_out, stdout_omitted=r.stdout_omitted,
                          stderr_omitted=r.stderr_omitted, limit_exceeded=r.limit_exceeded)

    #: entropy every later exec gets (None = unpinned, today's behaviour). Set with pin_entropy().
    entropy: EntropyPin | None = None

    def pin_entropy(self, pin: EntropyPin | None) -> None:
        """Pin (or unpin) the entropy of every later exec (GOAL §16.5.1; what is pinned: sandbox/hermetic.py)."""
        self.entropy = pin

    def _pinned_env(self, env: dict[str, str] | None, support: str) -> dict[str, str]:
        """The caller's env with the pin's variables on top (the pin wins: it is what the record says ran)."""
        return {**(env or {}), **(self.entropy.env(support) if self.entropy else {})}

    def _supervised_result(self, r: BoundedRun, nonce: str, cpu_s: float,
                           witness: Callable[[], float | None]) -> ExecResult:
        """ExecResult of a run under the CPU supervisor. Every failure to obtain a genuine supervisor record is the
        PROGRAM's failure (it is the only party that could have prevented the record), never infrastructure."""
        stderr, raw = split_record(r.stderr)
        sup = parse_supervisor(raw) if raw is not None and raw.get("nonce") == nonce else None
        pin = self.entropy.digest if self.entropy else None
        base = BoundedRun(r.returncode, r.stdout, stderr, r.stdout_omitted, r.stderr_omitted, r.wall_s, r.timed_out,
                          r.limit_exceeded)
        if r.timed_out or r.limit_exceeded:
            res = self._result(base, -1)
            return res.model_copy(update={"cpu_s": sup.cpu_s if sup else None, "entropy_pin": pin})
        if sup is None:
            base.limit_exceeded = "the CPU supervisor's record is missing or not genuine"
            return self._result(base, -1).model_copy(update={"entropy_pin": pin})
        if sup.cpu_exceeded:
            base.limit_exceeded = f"CPU time exceeded its limit ({cpu_s} s)"
            return self._result(base, -1).model_copy(update={"cpu_s": sup.cpu_s, "cpu_exceeded": True,
                                                             "entropy_pin": pin})
        if sup.wall_exceeded:  # the wall backstop fired below the CPU limit: slept, or the host was contended
            ratio = witness()
            contended = None if ratio is None else ratio > WITNESS_CONTENDED_RATIO
            base.timed_out = True
            verdict = {True: "contended: INFRA, re-run the whole evaluation",
                       False: "not contended: the program's own failure", None: "witness unavailable"}[contended]
            note = (f"\n[sandbox] wall backstop fired at {sup.wall_s} s with {sup.cpu_s} s CPU (limit {cpu_s} s); "
                    f"contention witness wall/CPU={ratio}: {verdict}")
            base.stderr += note
            return self._result(base, -1).model_copy(update={"cpu_s": sup.cpu_s, "contended": contended,
                                                             "entropy_pin": pin})
        return self._result(base, sup.rc if sup.rc is not None else -1).model_copy(
            update={"cpu_s": sup.cpu_s, "entropy_pin": pin})

    @abstractmethod
    def exec(self, command: list[str], timeout_s: float = 600, env: dict[str, str] | None = None, *,
             cpu_s: float | None = None) -> ExecResult:
        """Run `command`. `cpu_s` (GOAL §16.8): charge the program CPU seconds across all its processes, with a wall
        backstop of `timeout_s` (callers set it to about hermetic.CPU_WALL_BACKSTOP x cpu_s). None = wall timeout
        only (today's behaviour). Implemented by the trusted supervisor in sandbox/support/supervise.py."""

    @abstractmethod
    def spawn(self, workspace: Path) -> Sandbox:
        """A FRESH sandbox with the same isolation settings and a new, empty workspace (confirmation runs and
        Kernel re-executions never share a workspace with development runs)."""

    @abstractmethod
    def environment(self) -> dict[str, str]:
        """What a re-run must reproduce (recorded in every Kernel evaluation record)."""
