"""Docker sandbox (PLAN B3): the backend for running agent-written code.

One fresh container per exec; the workspace is the only mount, so state lives in files
(matching state-exact snapshots). Defaults: no network, capped memory/CPU/pids, non-root user,
read-only root filesystem with a tmpfs /tmp. gVisor (`runsc`) is used when installed.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

from ..errors import InfraError
from .base import ExecResult, Sandbox

DEFAULT_IMAGE = "custom-harness-sandbox:py312"


class DockerUnavailable(InfraError):
    pass


def docker_available() -> bool:
    exe = shutil.which("docker")
    if exe is None:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=20).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _has_runtime(exe: str, name: str) -> bool:
    p = subprocess.run([exe, "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True)
    return p.returncode == 0 and f'"{name}"' in p.stdout


class DockerSandbox(Sandbox):
    def __init__(self, workspace: Path, *, image: str = DEFAULT_IMAGE, network: bool = False,
                 memory: str = "4g", cpus: float = 2.0, pids: int = 256, gpus: str | None = None,
                 prefer_gvisor: bool = True, create_attempts: int = 3, create_backoff_s: float = 1.0):
        self.create_attempts, self.create_backoff_s = max(1, create_attempts), create_backoff_s
        exe = shutil.which("docker")
        if exe is None or not docker_available():
            raise DockerUnavailable("Docker is not installed or the daemon is not running")
        self.exe = exe
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.image, self.network, self.memory, self.cpus, self.pids, self.gpus = (
            image, network, memory, cpus, pids, gpus)
        self.runtime = "runsc" if prefer_gvisor and _has_runtime(exe, "runsc") else None
        self.isolation = (f"docker{'+gvisor' if self.runtime else ''}; network={'on' if network else 'off'}; "
                          f"mem={memory}; cpus={cpus}; non-root; read-only rootfs")

    def exec(self, command: list[str], timeout_s: float = 600, env: dict[str, str] | None = None) -> ExecResult:
        """create -> start -a -> inspect -> rm. Splitting the steps is what lets us tell an
        infrastructure failure (create/inspect failed, runtime State.Error) from the agent process's
        own exit code: agent code can exit with any code, including Docker's 125, so exit codes alone
        must never decide that a failure was "infra" (that would let agent code hide its failures)."""
        name = f"chh-{uuid.uuid4().hex[:12]}"
        args = [self.exe, "create", "--name", name,
                "--network", "bridge" if self.network else "none",
                "--memory", self.memory, "--cpus", str(self.cpus), "--pids-limit", str(self.pids),
                "--read-only", "--tmpfs", "/tmp:rw,size=1g", "--security-opt", "no-new-privileges",
                "--cap-drop", "ALL", "--user", "1000:1000",
                "-v", f"{self.workspace}:/work", "-w", "/work"]
        if self.runtime:
            args += ["--runtime", self.runtime]
        if self.gpus:
            args += ["--gpus", self.gpus]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args += [self.image, *command]
        # Creating the container runs no agent code, so retrying it here is always safe and keeps a brief
        # daemon hiccup from failing a whole multi-exec step (the engine-level retry re-runs entire nodes).
        for attempt in range(self.create_attempts):
            try:
                c = self._docker(args, timeout_s=120)
            except InfraError:
                if attempt == self.create_attempts - 1:
                    raise
            else:
                if c.returncode == 0:
                    break
                if attempt == self.create_attempts - 1:
                    raise InfraError(f"docker create failed (rc={c.returncode}): {c.stderr[-1000:]}",
                                     transient=True)
            try:  # a half-created container would block the name on the next attempt
                self._docker([self.exe, "rm", "-f", name], timeout_s=60)
            except InfraError:
                pass
            time.sleep(self.create_backoff_s * 2 ** attempt)
        t0 = time.time()
        try:
            try:
                p = subprocess.run([self.exe, "start", "-a", name], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout_s)
            except subprocess.TimeoutExpired as e:
                self._docker([self.exe, "kill", name], timeout_s=60)
                def _s(x):
                    return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
                return ExecResult(returncode=-1, stdout=_s(e.stdout), stderr=_s(e.stderr),
                                  wall_s=time.time() - t0, timed_out=True)
            except OSError as e:
                raise InfraError(f"could not invoke docker: {e}", transient=True) from e
            wall = time.time() - t0
            st = self._docker([self.exe, "inspect", "--format",
                               "{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}", name], timeout_s=60)
            if st.returncode != 0:
                raise InfraError(f"docker inspect failed (daemon lost?): {st.stderr[-500:]}", transient=True)
            code_s, oom, err = (st.stdout.strip().split("|", 2) + ["", ""])[:3]
            if err.strip():
                raise InfraError(f"container runtime error: {err.strip()[:500]}", transient=True)
            try:
                code = int(code_s)
            except ValueError as e:
                raise InfraError(f"unreadable container state: {st.stdout[:200]!r}") from e
            stderr = p.stderr
            if oom.strip() == "true":  # the agent's program exceeded its memory cap: its own failure
                stderr += f"\n[sandbox] killed: out of memory (cap {self.memory})"
            return ExecResult(returncode=code, stdout=p.stdout, stderr=stderr, wall_s=wall)
        finally:
            self._docker([self.exe, "rm", "-f", name], timeout_s=60)

    @staticmethod
    def _docker(args: list[str], timeout_s: float) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise InfraError(f"docker CLI call failed: {type(e).__name__}: {e}", transient=True) from e
