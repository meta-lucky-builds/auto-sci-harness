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

from .base import ExecResult, Sandbox

DEFAULT_IMAGE = "custom-harness-sandbox:py312"


class DockerUnavailable(RuntimeError):
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
                 prefer_gvisor: bool = True):
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
        name = f"chh-{uuid.uuid4().hex[:12]}"
        args = [self.exe, "run", "--rm", "--name", name,
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
        t0 = time.time()
        try:
            p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout_s)
            return ExecResult(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr, wall_s=time.time() - t0)
        except subprocess.TimeoutExpired as e:
            subprocess.run([self.exe, "kill", name], capture_output=True)
            def _s(x):
                return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
            return ExecResult(returncode=-1, stdout=_s(e.stdout), stderr=_s(e.stderr),
                              wall_s=time.time() - t0, timed_out=True)
