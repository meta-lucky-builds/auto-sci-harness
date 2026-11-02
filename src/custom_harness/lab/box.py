"""The lab box: one long-lived Docker container per Lab session (GOAL §17).

Full power inside: internet through the session's recording gateway, with the LAN and the host out of reach (GOAL
18.6, lab/egress.py), a writable root filesystem (pip / npm / uv / conda
installs work; apt needs the `--root` opt-in), the persistent workspace bind-mounted at /workspace, optional GPUs,
no time limit. The container runs `sleep infinity` under `--init`; agents and shells are started in it with
`docker exec`, so the credential set at `docker run` reaches every process in the box and nothing is written to disk.

Kept tight, because none of it limits what an agent can do in the box and all of it limits what the box can do to
the host: exactly one bind mount (the workspace), no Docker socket, no privileged mode, no host namespaces, no added
capabilities or devices (GPUs only through `--gpus`), `no-new-privileges`, all capabilities dropped and uid 1000
unless the owner opts into `--root` for a session (then Docker's default capability set; recorded). The final argv is
checked by `walls.docker_argv_problems` before it runs (fail closed).

The image (`docker/lab.Dockerfile`) is built by the owner; nothing here builds, pulls or installs anything. A missing
image makes `docker run` fail, and the session is recorded as `start_failed`.

Built, NOT test-verified.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .agents import BOX_HOME, Plan
from .egress import GATEWAY_IMAGE, HOST_NAME, Network, gateway_sha256, network_name, proxy_env
from .walls import CONTAINER_WORKSPACE, LabWallError, docker_argv_problems

LAB_IMAGE = "custom-harness-lab:py312"  # built by the owner from docker/lab.Dockerfile
LAB_LABEL = "org.custom-harness.lab"


@dataclass(frozen=True)
class BoxSpec:
    image: str = LAB_IMAGE
    gpus: str | None = None  # e.g. "all" or "device=0": passed to `--gpus` (NVIDIA container toolkit on the host)
    memory: str | None = "16g"  # UNCALIBRATED (owner decision, GOAL §13.8)
    cpus: float | None = None
    pids: int = 8192  # UNCALIBRATED (owner decision, GOAL §13.8): a fork bomb stays inside the box
    shm_size: str = "2g"  # PyTorch data loaders need more than Docker's 64 MB default
    root: bool = False  # opt-in: root in the box with Docker's default capabilities (system package installs)
    gateway_image: str = GATEWAY_IMAGE

    def record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], dict[str, str] | None, bytes | None, float | None], Result]


def subprocess_runner(argv: list[str], env: dict[str, str] | None, stdin: bytes | None,
                      timeout: float | None) -> Result:
    p = subprocess.run(argv, env=env, input=stdin, capture_output=True, timeout=timeout, check=False)
    return Result(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))


class DockerCLI:
    """The docker client calls a lab box needs. `runner` is injectable (tests use a fake; nothing here is run by the
    build). `exe` is the docker executable."""

    def __init__(self, exe: str = "docker", runner: Runner = subprocess_runner):
        self.exe = exe
        self.runner = runner

    def call(self, args: Sequence[str], *, env: dict[str, str] | None = None, stdin: bytes | None = None,
             timeout: float | None = 120) -> Result:
        return self.runner([self.exe, *args], env, stdin, timeout)


def container_name(session_id: str) -> str:
    return f"chh-lab-{session_id}"


def run_argv(exe: str, session_id: str, workspace: Path, spec: BoxSpec, plan: Plan) -> list[str]:
    """The `docker run` argv of a lab box (checked by walls.docker_argv_problems before use)."""
    a = [exe, "run", "-d", "--init", "--name", container_name(session_id), "--hostname", "lab",
         "--label", f"{LAB_LABEL}=EXPLORATORY", "--label", f"{LAB_LABEL}.session={session_id}",
         "--network", network_name(session_id), "--security-opt", "no-new-privileges", "--pids-limit", str(spec.pids),
         "--shm-size", spec.shm_size]
    if spec.memory:
        a += ["--memory", spec.memory]
    if spec.cpus:
        a += ["--cpus", str(spec.cpus)]
    if not spec.root:
        a += ["--cap-drop", "ALL", "--user", "1000:1000"]
    else:
        a += ["--user", "0:0"]
    if spec.gpus:
        a += ["--gpus", spec.gpus]
    a += ["-v", f"{workspace}:{CONTAINER_WORKSPACE}", "-w", CONTAINER_WORKSPACE]
    for k, v in sorted({**plan.env, **proxy_env(session_id)}.items()):
        a += ["-e", f"{k}={v}"]
    if plan.box_env:
        a += ["-e", plan.box_env]  # name only: the value is in the docker CLI's environment, never in argv
    a += [spec.image, "sleep", "infinity"]
    return a


def host_allow(plan: Plan) -> tuple[str, ...]:
    """The one host service a box may reach: the local model server the researcher named in the agent's options."""
    return (f"{HOST_NAME}:{plan.host_port}",) if plan.host_gateway else ()


def network_record(session_id: str, spec: BoxSpec, plan: Plan) -> dict[str, Any]:
    """What the session record says about the network rule."""
    return {"rule": "internet through the gateway; LAN and host blocked", "network": network_name(session_id),
            "gateway_image": spec.gateway_image, "gateway_sha256": gateway_sha256(),
            "host_allow": list(host_allow(plan))}


def check_argv(argv: list[str], workspace: Path, plan: Plan, session_id: str) -> None:
    problems = docker_argv_problems(argv, workspace=workspace,
                                    credential_env=[plan.box_env] if plan.box_env else [],
                                    allowed_env={**plan.env, **proxy_env(session_id)},
                                    network=network_name(session_id))
    if problems:
        raise LabWallError("lab box argv refused: " + "; ".join(problems))


def _docker_env(credential_env: dict[str, str] | None) -> dict[str, str]:
    """The docker CLI's own environment: this process's (the docker client needs PATH, DOCKER_HOST, its config)
    plus the session credential under its in-box name. `-e NAME` forwards only NAME into the box."""
    env = dict(os.environ)
    env.update(credential_env or {})
    return env


class LabBox:
    def __init__(self, session_id: str, docker: DockerCLI | None = None):
        self.session_id = session_id
        self.name = container_name(session_id)
        self.docker = docker or DockerCLI()

    def start(self, workspace: Path, spec: BoxSpec, plan: Plan, credential_env: dict[str, str] | None) -> str:
        """`docker run -d` the box; returns the container id. Raises LabWallError (argv refused) or RuntimeError."""
        argv = run_argv(self.docker.exe, self.session_id, workspace, spec, plan)
        check_argv(argv, workspace, plan, self.session_id)
        if plan.box_env and set(credential_env or {}) != {plan.box_env}:
            raise LabWallError("the credential handed to docker must be exactly the plan's in-box variable")
        Network(self.session_id, self.docker).up(spec.gateway_image, host_allow(plan))  # before the box exists
        r = self.docker.runner(argv, _docker_env(credential_env), None, 300)
        if r.returncode != 0:
            raise RuntimeError(f"docker run failed (rc={r.returncode}): {r.stderr[-800:]}")
        return r.stdout.strip()

    def write_file(self, path: str, content: str) -> None:
        """Write a config file into the box's HOME (never the workspace), content on stdin."""
        if not path.startswith(BOX_HOME + "/") or ".." in path.split("/"):
            raise LabWallError(f"lab config files go under {BOX_HOME} only, not {path!r}")
        r = self.docker.call(["exec", "-i", self.name, "sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"',
                              "sh", path], stdin=content.encode("utf-8"), timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f"writing {path} into the box failed: {r.stderr[-500:]}")

    def run_setup(self, argv: list[str], timeout: float = 300) -> Result:
        return self.docker.call(["exec", "-w", CONTAINER_WORKSPACE, self.name, *argv], timeout=timeout)

    def exec_argv(self, argv: list[str], *, interactive: bool, tty: bool) -> list[str]:
        flags = (["-i"] if interactive else []) + (["-t"] if tty else [])
        return [self.docker.exe, "exec", *flags, "-w", CONTAINER_WORKSPACE, self.name, *argv]

    def state(self) -> str:
        """Docker's state of the box ("running", "paused", "exited", ...), or "absent"."""
        r = self.docker.call(["inspect", "--format", "{{.State.Status}}", self.name], timeout=60)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "absent"

    def image_id(self, image: str) -> str | None:
        r = self.docker.call(["image", "inspect", "--format", "{{.Id}}", image], timeout=60)
        return r.stdout.strip() or None if r.returncode == 0 else None

    def pause(self) -> bool:
        return self.docker.call(["pause", self.name], timeout=60).returncode == 0

    def unpause(self) -> bool:
        return self.docker.call(["unpause", self.name], timeout=60).returncode == 0

    def stop(self) -> bool:
        """Stop and remove the box (the workspace on the host is kept; the box's HOME, with the CLI logins, goes)."""
        self.docker.call(["stop", "-t", "30", self.name], timeout=120)
        removed = self.docker.call(["rm", "-f", self.name], timeout=120).returncode == 0
        Network(self.session_id, self.docker).down()  # box first: the network cannot go while a member is on it
        return removed
