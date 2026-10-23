"""Docker on a remote (GPU) host over SSH: the realistic ISOLATED GPU path (GOAL §17, §5 L0; PORTMAP §4, §8 item 2).

The container gets exactly the local Docker sandbox's isolation flags (sandbox/docker.py): `--network none`,
`--read-only` with a private /tmp tmpfs, `--security-opt no-new-privileges`, `--cap-drop ALL`, a NON-ROOT user,
memory/CPU/pid caps, `--ulimit fsize`, plus `--gpus` when configured. Admission (invariant 1) runs before anything is
uploaded; the image is the owner-configured candidate image pinned BY DIGEST, and the submit script refuses it unless
it carries the candidate label (bundled datasets removed, invariant 2). Nothing the agent writes chooses the image,
the flags or the host.

Differences from local Docker, chosen here (docs/lab_compute.md "design choices"):
- user: the container runs as the SSH user's own uid:gid (so the 0700 run directory is readable), and the submit
  script refuses uid 0 (the container would be root);
- logs: `--log-driver json-file` with a size cap (64 MiB, one file), because an asynchronous job is read back with
  `docker logs` (local Docker uses `--log-driver none` and a live pipe);
- cancellation writes a `canceled` marker next to the run so a later status says CANCELED, not "exit 137".

Lifecycle commands (each one `ssh` call through PinnedSSH, host key pinned):
  submit: mkdir a NEW 0700 run dir -> untar the materialised revision into <dir>/work -> image label check ->
          docker create ... && docker start (an unacknowledged start is LaunchUncertain)
  status: canceled marker, else `docker inspect` State.{Status,ExitCode,OOMKilled,Error}
  logs:   `docker logs` bytes from the cursor, base64 on the wire
  cancel: marker + `docker stop -t 5` (idempotent)
  fetch:  one regular file under <dir>/work, resolved with realpath and refused if it leaves <dir>/work or is a link
  cleanup:`docker rm -f` + `rm -rf` of the run dir
Ported from OR (MIT, Copyright (c) 2026 alphaXiv, fe36f74): the run-directory layout with an owner-only umask, the
stdin-carried upload, the "exit code first, then the runtime" status order and the TERM-then-KILL cancel window
(src/jobs/ssh.rs `run_job`, `host_job_script`, `cancel_job`; src/jobs/ssh/container.rs). The docker flags are ours.
Built, NOT test-verified; never run against a real host.
"""

from __future__ import annotations

import base64
import io
import posixpath
import re
import shlex
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..sandbox.docker import CANDIDATE_LABEL, DEFAULT_MAX_FILE_BYTES
from ..store import EvidenceStore
from .backend import IsolationProperties, RunRequest
from .credentials import OwnerConfig, check_keys
from .jobs import (
    AsyncComputeBackend,
    FailureClass,
    JobHandle,
    JobStage,
    JobStatus,
    LaunchUncertain,
    LogBatch,
    RemoteError,
    infra_error,
    policy_exit,
)
from .pinned_ssh import PinnedSSH, fetch_file_script
from .ssh import UploadEntry, check_rel, check_remote_root, collect_upload

DIGEST_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SIZE = re.compile(r"[1-9][0-9]*[kmgKMG]?")
_GPUS = re.compile(r"all|[1-9][0-9]?|device=[0-9,]+")
FETCH_MAX_BYTES = 64 << 20
LOG_MAX_SIZE = "64m"
SECTION_KEYS = frozenset({"host", "port", "host_key", "identity_file", "image", "gpus", "remote_root", "memory",
                          "cpus", "pids"})


def tar_entries(entries: Sequence[UploadEntry]) -> bytes:
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
    return buf.getvalue()


class RemoteDockerBackend(AsyncComputeBackend):
    name = "remote-docker"
    isolation = IsolationProperties(container=True, no_secrets=True, network_policy="off", read_only_root=True,
                                    non_root=True, hidden_data_free=True, verified=True)

    def __init__(self, store: EvidenceStore, runs_root: Path, *, ssh: PinnedSSH, image: str,
                 remote_root: str = "chh-runs", gpus: str | None = None, memory: str = "16g", cpus: float = 4.0,
                 pids: int = 1024, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES, **kw: Any):
        super().__init__(store, runs_root, **kw)
        if not DIGEST_IMAGE.fullmatch(image or ""):
            raise ValueError("the remote image must be pinned by digest (repo@sha256:<64 hex>), never a mutable tag")
        if gpus is not None and not _GPUS.fullmatch(gpus):
            raise ValueError(f"bad --gpus value {gpus!r}")
        if not _SIZE.fullmatch(memory) or not 0 < float(cpus) <= 512 or not 0 < int(pids) <= 1 << 20:
            raise ValueError("bad memory/cpus/pids limits")
        self.ssh, self.image, self.gpus = ssh, image, gpus
        self.remote_root = check_remote_root(remote_root)
        self.memory, self.cpus, self.pids, self.max_file_bytes = memory, float(cpus), int(pids), int(max_file_bytes)

    @classmethod
    def from_config(cls, store: EvidenceStore, runs_root: Path, config: OwnerConfig, name: str,
                    **kw: Any) -> RemoteDockerBackend:
        sec = config.section("remote-docker", name)
        check_keys(sec, SECTION_KEYS, frozenset({"host", "host_key", "image"}), f"remote-docker/{name}")
        ssh = PinnedSSH(str(sec["host"]), host_key=str(sec["host_key"]), port=int(sec.get("port", 22)),
                        identity_file=sec.get("identity_file"),
                        known_hosts_dir=Path(store.root) / "compute" / "known_hosts")
        return cls(store, runs_root, ssh=ssh, image=str(sec["image"]), remote_root=str(sec.get("remote_root",
                                                                                                "chh-runs")),
                   gpus=sec.get("gpus"), memory=str(sec.get("memory", "16g")), cpus=float(sec.get("cpus", 4.0)),
                   pids=int(sec.get("pids", 1024)), **kw)

    # ------------------------------------------------------------ command builders (pure: unit-testable)
    @staticmethod
    def container_name(job_key: str) -> str:
        return "chh-" + job_key.split(":", 1)[1][:24]

    def run_dir(self, job_key: str) -> str:
        return posixpath.join(self.remote_root, job_key.split(":", 1)[1])

    def create_args(self, job_key: str, run_dir: str, command: Sequence[str], env: Mapping[str, str]) -> list[str]:
        """The `docker create` argv (quoted later). `$CHH_UID:$CHH_GID` is expanded by the remote shell."""
        bad = [k for k in env if not _ENV_NAME.fullmatch(k)]
        if bad or not command:
            raise ValueError(f"bad environment names {bad} or empty command")
        a = ["docker", "create", "--name", self.container_name(job_key), "--label", f"chh.job={job_key}",
             "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,size=1g",
             "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
             "--memory", self.memory, "--cpus", str(self.cpus), "--pids-limit", str(self.pids),
             "--ulimit", f"fsize={self.max_file_bytes}",
             "--log-driver", "json-file", "--log-opt", f"max-size={LOG_MAX_SIZE}", "--log-opt", "max-file=1",
             "-v", f"{posixpath.join(run_dir, 'work')}:/work", "-w", "/work"]
        if self.gpus:
            a += ["--gpus", self.gpus]
        for k, v in sorted(env.items()):
            a += ["-e", f"{k}={v}"]
        return [*a, self.image, *command]

    def submit_script(self, job_key: str, run_dir: str, command: Sequence[str], env: Mapping[str, str]) -> str:
        q = shlex.quote
        key, want = CANDIDATE_LABEL
        create = " ".join(q(x) for x in self.create_args(job_key, run_dir, command, env))
        # --user is inserted after `create` with the remote uid:gid (double-quoted so the shell expands it)
        create = create.replace("docker create ", 'docker create --user "$CHH_UID:$CHH_GID" ', 1)
        label_fmt = '{{index .Config.Labels "' + key + '"}}'
        return (
            "set -e; "
            "CHH_UID=$(id -u); CHH_GID=$(id -g); "
            'if [ "$CHH_UID" = 0 ]; then echo "refusing: the SSH user is root (the container would run as root)" >&2;'
            " exit 3; fi; "
            f"lbl=$(docker image inspect --format {q(label_fmt)} {q(self.image)}); "
            f'if [ "$lbl" != {q(want)} ]; then echo "refusing: {self.image} is not the candidate image" >&2; exit 4; '
            "fi; "
            f"{create} >/dev/null; echo CHH_CREATED; "
            f"docker start {q(self.container_name(job_key))} >/dev/null; echo CHH_STARTED"
        )

    def status_script(self, job_key: str) -> str:
        q, d = shlex.quote, self.run_dir(job_key)
        fmt = "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}"
        return (f"if [ -f {q(posixpath.join(d, 'canceled'))} ]; then echo CANCELED; "
                f"elif st=$(docker inspect --format {q(fmt)} {q(self.container_name(job_key))} 2>/dev/null); "
                f'then echo "ST $st"; else echo MISSING; fi')

    @staticmethod
    def parse_status(out: str) -> JobStatus:
        """Pure mapping of the status probe onto our stages and failure classes."""
        out = out.strip()
        if out == "CANCELED":
            return JobStatus(JobStage.CANCELED)
        if out == "MISSING":
            return JobStatus(JobStage.DELETED, FailureClass.INFRA, "the container no longer exists")
        if not out.startswith("ST "):
            return JobStatus(JobStage.UNKNOWN, message=f"unexpected status output: {out[:200]!r}")
        parts = (out[3:].split("|", 3) + ["", "", ""])[:4]
        state, code_s, oom, err = (p.strip() for p in parts)
        if state == "created":
            return JobStatus(JobStage.SCHEDULING)
        if state in ("running", "restarting", "paused", "removing"):
            return JobStatus(JobStage.RUNNING)
        if state in ("exited", "dead"):
            if err:
                return infra_error(f"container runtime error: {err[:300]}")
            try:
                code = int(code_s)
            except ValueError:
                return JobStatus(JobStage.UNKNOWN, message=f"unreadable exit code {code_s!r}")
            if oom == "true":
                return JobStatus(JobStage.ERROR, FailureClass.POLICY, "killed: out of memory", exit_code=code,
                                 oom=True)
            return policy_exit(code)
        return JobStatus(JobStage.UNKNOWN, message=f"unknown container state {state!r}")

    # ------------------------------------------------------------ lifecycle
    def _submit(self, job_key: str, request: RunRequest, run_dir: Path) -> dict[str, str]:
        entries = collect_upload(run_dir)  # refuses links and special files before the remote is touched
        q, d = shlex.quote, self.run_dir(job_key)
        self.ssh.checked("mkdir", f"umask 077 && mkdir -p -- {q(self.remote_root)} && mkdir -- {q(d)} && "
                                  f"mkdir -- {q(posixpath.join(d, 'work'))}")
        self.ssh.checked("upload", f"tar -x -f - --no-same-owner -C {q(posixpath.join(d, 'work'))}",
                         stdin=tar_entries(entries), timeout_s=600)
        script = self.submit_script(job_key, d, request.command, request.env)
        try:
            r = self.ssh.run(f"sh -c {q(script)}", timeout_s=300)
        except RemoteError as e:
            raise LaunchUncertain(f"remote-docker start not acknowledged: {e}", transient=False) from e
        if r.returncode in (3, 4):
            raise RemoteError(f"remote-docker refused: {r.stderr.strip()[-500:]}", transient=False)
        if "CHH_CREATED" not in r.stdout:
            raise RemoteError(f"remote-docker could not create the container (rc={r.returncode}): "
                              f"{r.stderr.strip()[-500:]}", transient=True)
        if r.returncode != 0 or "CHH_STARTED" not in r.stdout:
            raise LaunchUncertain(f"remote-docker start not acknowledged (rc={r.returncode}): "
                                  f"{r.stderr.strip()[-500:]}", transient=False)
        return {"host": self.ssh.host, "port": str(self.ssh.port), "run_dir": d,
                "container": self.container_name(job_key), "image": self.image}

    def _status(self, handle: JobHandle) -> JobStatus:
        r = self.ssh.run(f"sh -c {shlex.quote(self.status_script(handle.job_key))}")
        if r.returncode != 0:
            return JobStatus(JobStage.UNKNOWN, message=f"status probe failed: {r.stderr.strip()[-300:]}")
        return self.parse_status(r.stdout)

    def _logs(self, handle: JobHandle, cursor: int, max_bytes: int) -> LogBatch:
        q = shlex.quote
        cmd = (f"docker logs {q(self.container_name(handle.job_key))} 2>&1 | tail -c +{int(cursor) + 1} | "
               f"head -c {int(max_bytes)} | base64")
        r = self.ssh.checked("logs", f"sh -c {q(cmd)}", max_output_bytes=max_bytes * 14 // 10 + 4096)
        data = base64.b64decode(r.stdout)
        eof = len(data) < max_bytes and self._status(handle).terminal
        return LogBatch(data, cursor + len(data), eof)

    def _cancel(self, handle: JobHandle) -> None:
        q, d = shlex.quote, self.run_dir(handle.job_key)
        cmd = (f"touch -- {q(posixpath.join(d, 'canceled'))} 2>/dev/null; "
               f"docker stop -t 5 {q(self.container_name(handle.job_key))} >/dev/null 2>&1; exit 0")
        self.ssh.checked("cancel", f"sh -c {q(cmd)}", timeout_s=60)

    def fetch_script(self, job_key: str, rel: str) -> str:
        return fetch_file_script(posixpath.join(self.run_dir(job_key), "work"), rel, FETCH_MAX_BYTES)

    def _fetch(self, handle: JobHandle, rel: str) -> bytes:
        r = self.ssh.checked("fetch", f"sh -c {shlex.quote(self.fetch_script(handle.job_key, rel))}",
                             timeout_s=600, max_output_bytes=FETCH_MAX_BYTES * 14 // 10 + 4096)
        if r.stdout_omitted:
            raise RemoteError(f"{rel!r} exceeds the transfer limit", transient=False)
        return base64.b64decode(r.stdout)

    def _cleanup(self, handle: JobHandle) -> None:
        q, d = shlex.quote, self.run_dir(handle.job_key)
        cmd = (f"docker rm -f {q(self.container_name(handle.job_key))} >/dev/null 2>&1; "
               f"rm -rf -- {q(d)}")
        self.ssh.checked("cleanup", f"sh -c {q(cmd)}", timeout_s=300)
