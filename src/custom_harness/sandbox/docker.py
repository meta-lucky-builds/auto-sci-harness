"""Docker sandbox (PLAN B3): the backend for running agent-written code.

One fresh container per exec; the workspace is the only mount, so state lives in files
(matching state-exact snapshots). Defaults: no network, capped memory/CPU/pids, non-root user,
read-only root filesystem with a tmpfs /tmp. gVisor (`runsc`) is used when installed.

Disk and output limits (all POLICY failures of the program, see sandbox.base): Docker cannot put a quota on a bind
mount, and `--storage-opt size=` is accepted but NOT enforced by Docker Desktop's overlayfs/containerd snapshotter
(verified 2026-10-07: a 100 MB write into a container created with `--storage-opt size=50m` succeeded) and would only
cover the container's own (read-only) layer anyway. So: RLIMIT_FSIZE (`--ulimit fsize`) caps every single file the
program writes (EFBIG); the host measures the workspace while the program runs and kills the container when the
total size or file count exceeds the limit (overshoot bounded by what can be written in one poll interval, and by
the per-file cap); output is read through bounded buffers; container logging is off (`--log-driver none`) so the
daemon does not keep a second, unbounded copy of the output.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

from ..errors import InfraError
from .base import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_WORKSPACE_BYTES,
    DEFAULT_MAX_WORKSPACE_FILES,
    ExecResult,
    Sandbox,
    check_mount,
    run_bounded,
)

# Two images (STATUS ledger BUNDLED-DATA). The full image ships scikit-learn's bundled datasets, from which the real
# suite tasks are built: a program running in it could load every labelled row, held-out rows included. So agent code
# ALWAYS runs in the candidate image (derived from the full one with every bundled dataset file removed, see
# docker/sandbox-candidate.Dockerfile); only trusted task building (`chh build-suite --docker`) uses the full image,
# and it must say so (`trusted_task_building=True`). A missing candidate image is a refusal, never a fallback.
FULL_IMAGE = "custom-harness-sandbox:py312"
CANDIDATE_IMAGE = "custom-harness-sandbox:py312-candidate"
DEFAULT_IMAGE = CANDIDATE_IMAGE
CANDIDATE_LABEL = ("org.custom-harness.bundled-data", "removed")
CANDIDATE_BUILD = "docker build -f docker/sandbox-candidate.Dockerfile -t {image} docker"
DEFAULT_MAX_FILE_BYTES = 256 << 20  # RLIMIT_FSIZE: the largest single file a program may write


class DockerUnavailable(InfraError):
    pass


class CandidateImageMissing(InfraError):
    """The candidate image is not built (or the tag is not the candidate build): a human must build it. Not transient:
    retrying cannot help, and falling back to the full image would expose the bundled datasets."""


def select_image(image: str | None, *, trusted_task_building: bool = False) -> str:
    """The image policy: None = the candidate image; the full image only for trusted task building."""
    if trusted_task_building:
        if image != FULL_IMAGE:
            raise PermissionError(f"trusted_task_building is only for the full image {FULL_IMAGE}, not {image}")
        return FULL_IMAGE
    if image is None:
        return CANDIDATE_IMAGE
    if image == FULL_IMAGE:
        raise PermissionError(f"{FULL_IMAGE} ships bundled datasets (the real tasks' labelled rows): agent code runs "
                              f"in {CANDIDATE_IMAGE}; only trusted task building may pass trusted_task_building=True")
    return image


def docker_available() -> bool:
    exe = shutil.which("docker")
    if exe is None:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=20, check=False).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _has_runtime(exe: str, name: str) -> bool:
    p = subprocess.run([exe, "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True, check=False)
    return p.returncode == 0 and f'"{name}"' in p.stdout


class DockerSandbox(Sandbox):
    def __init__(self, workspace: Path, *, image: str | None = None, trusted_task_building: bool = False,
                 network: bool = False,
                 memory: str = "4g", cpus: float = 2.0, pids: int = 256, gpus: str | None = None,
                 prefer_gvisor: bool = True, create_attempts: int = 3, create_backoff_s: float = 1.0,
                 max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
                 max_workspace_bytes: int = DEFAULT_MAX_WORKSPACE_BYTES,
                 max_workspace_files: int = DEFAULT_MAX_WORKSPACE_FILES,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES):
        check_mount(workspace)  # before anything else: a protected path is never mounted, Docker or not
        image = select_image(image, trusted_task_building=trusted_task_building)  # also before Docker is contacted
        self.create_attempts, self.create_backoff_s = max(1, create_attempts), create_backoff_s
        self.max_output_bytes, self.max_workspace_bytes, self.max_workspace_files, self.max_file_bytes = (
            max_output_bytes, max_workspace_bytes, max_workspace_files, max_file_bytes)
        exe = shutil.which("docker")
        if exe is None or not docker_available():
            raise DockerUnavailable("Docker is not installed or the daemon is not running")
        self.exe = exe
        if image == CANDIDATE_IMAGE:
            self._require_candidate(exe, image)
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.image, self.network, self.memory, self.cpus, self.pids, self.gpus = (
            image, network, memory, cpus, pids, gpus)
        self.runtime = "runsc" if prefer_gvisor and _has_runtime(exe, "runsc") else None
        self.isolation = (f"docker{'+gvisor' if self.runtime else ''}; network={'on' if network else 'off'}; "
                          f"mem={memory}; cpus={cpus}; non-root; read-only rootfs; max file {max_file_bytes} B; "
                          f"workspace <= {max_workspace_bytes} B / {max_workspace_files} files")
        self._settings = {"image": image, "trusted_task_building": trusted_task_building, "network": network,
                          "memory": memory, "cpus": cpus, "pids": pids,
                          "gpus": gpus, "prefer_gvisor": prefer_gvisor, "create_attempts": create_attempts,
                          "create_backoff_s": create_backoff_s, "max_output_bytes": max_output_bytes,
                          "max_workspace_bytes": max_workspace_bytes, "max_workspace_files": max_workspace_files,
                          "max_file_bytes": max_file_bytes}
        self._image_id: str | None = None

    @classmethod
    def _require_candidate(cls, exe: str, image: str) -> None:
        """Fail closed unless `image` exists locally AND carries the candidate build's label (a full image merely
        tagged with the candidate name does not)."""
        key, want = CANDIDATE_LABEL
        p = cls._docker([exe, "image", "inspect", "--format", f'{{{{index .Config.Labels "{key}"}}}}', image],
                        timeout_s=60)
        build = CANDIDATE_BUILD.format(image=image)
        if p.returncode != 0:
            raise CandidateImageMissing(f"the candidate sandbox image {image} is not built; agent code never runs in "
                                        f"the full image {FULL_IMAGE} (it ships bundled datasets). Build it once "
                                        f"(no downloads): {build}")
        if p.stdout.strip() != want:
            raise CandidateImageMissing(f"{image} lacks the label {key}={want}, so it is not the candidate build "
                                        f"(bundled datasets may be present). Rebuild it: {build}")

    def spawn(self, workspace: Path) -> DockerSandbox:
        return DockerSandbox(workspace, **self._settings)  # type: ignore[arg-type]

    def environment(self) -> dict[str, str]:
        """Image content id (not just the tag): a re-run on a different image is a different environment."""
        if self._image_id is None:
            p = self._docker([self.exe, "image", "inspect", "--format", "{{.Id}}", self.image], timeout_s=60)
            if p.returncode != 0:
                raise InfraError(f"cannot inspect image {self.image}: {p.stderr[-300:]}", transient=True)
            self._image_id = p.stdout.strip()
        return {"sandbox": self.isolation, "image": self.image, "image_id": self._image_id}

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
                "--ulimit", f"fsize={self.max_file_bytes}", "--log-driver", "none",
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
        try:
            try:
                p = run_bounded([self.exe, "start", "-a", name], timeout_s=timeout_s,
                                max_output_bytes=self.max_output_bytes, check=self._workspace_check,
                                kill=lambda: self._docker([self.exe, "kill", name], timeout_s=60))
            except OSError as e:
                raise InfraError(f"could not invoke docker: {e}", transient=True) from e
            if p.timed_out or p.limit_exceeded:  # we killed it: the program's failure (timeout / resource limit)
                return self._result(p, -1)
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
            res = self._result(p, code)
            if oom.strip() == "true":  # the agent's program exceeded its memory cap: its own failure
                res = res.model_copy(update={"stderr": res.stderr + f"\n[sandbox] killed: out of memory "
                                                                    f"(cap {self.memory})"})
            return res
        finally:
            self._docker([self.exe, "rm", "-f", name], timeout_s=60)

    @staticmethod
    def _docker(args: list[str], timeout_s: float) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(args, capture_output=True, text=True, check=False, encoding="utf-8", errors="replace",
                                  timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise InfraError(f"docker CLI call failed: {type(e).__name__}: {e}", transient=True) from e
