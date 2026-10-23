"""Slurm over SSH: TRUSTED_ONLY (GOAL §5 L0, §17; invariant 1). Human-approved trusted code only; agent code never.

A Slurm job runs as the cluster user with that user's files, network and credentials: there is no container
boundary we control, so the backend is TRUSTED_ONLY whatever the cluster. (An Apptainer `--containall --no-home
--net` variant could become ISOLATED where the cluster allows it; it is NOT built: PORTMAP §4.)

Ported from OpenResearch (MIT, Copyright (c) 2026 alphaXiv; src/jobs/slurm.rs at fe36f74; THIRD_PARTY_NOTICES.md):
- `render_sbatch`: the fixed directives (job name, `--output=log`, `--error=log`, `--open-mode=append`, `--ntasks=1`),
  optional time/partition/account/gres/cpus/mem directives, the payload in a SUBSHELL so its own `exit` still reaches
  the `echo $? > exit_code` line, and the script exiting with the payload's code so sacct mirrors it;
- `parse_mem` (strict: the value is interpolated into a directive), `resolve_gres`, `slurm_time`, `parse_job_id`;
- the combined inspect probe (exit_code file first, then squeue, then `sacct -P`) and its token mapping, including
  "squeue COMPLETED / NODE_FAIL / PREEMPTED are transient" and "GONE never proves termination";
- cancel = `scancel`, succeeding when the controller no longer knows the job.
Changed from OR, and why:
- the source is the recorded revision uploaded as a tar (OR clones a git branch on the login node);
- no secrets in the batch script (OR exports tokens there and keeps the file 0600): every async backend refuses
  secret-named variables (jobs.refuse_secret_env), so the exports are plain run settings only;
- every partition/account/gres value is validated against a strict pattern before it reaches a directive;
- the token mapping assigns OUR failure classes: TIMEOUT/OUT_OF_MEMORY/FAILED are the program's (POLICY);
  NODE_FAIL/BOOT_FAIL/PREEMPTED from sacct are INFRA; an unrecognised state is UNKNOWN (OR: RUNNING).
Built, NOT test-verified; never run against a real cluster.
"""

from __future__ import annotations

import base64
import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    policy_exit,
)
from .pinned_ssh import PinnedSSH, fetch_file_script, log_slice_script
from .remote_docker import tar_entries
from .ssh import check_remote_root, collect_upload

_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_GRES = re.compile(r"gpu(?::[A-Za-z0-9_.-]{1,32})?(?::[1-9][0-9]{0,2})?")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FETCH_MAX_BYTES = 64 << 20
SECTION_KEYS = frozenset({"host", "port", "host_key", "identity_file", "remote_root", "partition", "account",
                          "cpus_per_task", "mem", "gres"})


def parse_mem(mem: str) -> str:
    """A positive integer with an optional K/M/G/T suffix (bare = megabytes). Port of OR `parse_mem`."""
    m = mem.strip()
    i = next((n for n, c in enumerate(m) if not c.isdigit()), len(m))
    digits, suffix = m[:i], m[i:]
    if not digits or not digits.lstrip("0") or suffix not in ("", "K", "M", "G", "T"):
        raise ValueError(f"unreadable memory request {mem!r}: use 64G, 4000M or a bare number of megabytes")
    return digits + suffix


def resolve_gres(flavor: str) -> str | None:
    """'gpu' -> gpu:1; 'gpu:...' verbatim; 'h100:2' -> gpu:h100:2 (port of OR `resolve_gres`), then validated."""
    f = flavor.strip()
    if not f:
        return None
    g = "gpu:1" if f == "gpu" else (f if f.startswith("gpu:") else f"gpu:{f}")
    if not _GRES.fullmatch(g):
        raise ValueError(f"refusing GRES request {g!r}")
    return g


def slurm_time(secs: int) -> str:
    """Seconds -> `D-HH:MM:SS` / `HH:MM:SS` (port of OR `slurm_time`)."""
    days, rem = divmod(int(secs), 86_400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{days}-{h:02}:{m:02}:{s:02}" if days else f"{h:02}:{m:02}:{s:02}"


def parse_job_id(out: str) -> str:
    """`sbatch --parsable` prints `<jobid>` or `<jobid>;<cluster>` (port of OR `parse_job_id`)."""
    jid = out.strip().split(";")[0].strip()
    if not jid or not jid.isdigit():
        raise RemoteError(f"unexpected sbatch output: {out.strip()[:200]!r}", transient=False)
    return jid


@dataclass(frozen=True)
class SlurmJobSpec:
    job_key: str
    command: Sequence[str]
    env: Mapping[str, str]
    time_limit_s: int | None = None
    partition: str | None = None
    account: str | None = None
    gres: str | None = None
    cpus_per_task: int | None = None
    mem: str | None = None


def render_sbatch(spec: SlurmJobSpec) -> str:
    q = shlex.quote
    directives = [f"#SBATCH --job-name=chh-{spec.job_key.split(':', 1)[1][:8]}", "#SBATCH --output=log",
                  "#SBATCH --error=log", "#SBATCH --open-mode=append", "#SBATCH --ntasks=1"]
    if spec.time_limit_s:
        directives.append(f"#SBATCH --time={slurm_time(spec.time_limit_s)}")
    for flag, value in (("partition", spec.partition), ("account", spec.account)):
        if value:
            if not _NAME.fullmatch(value):
                raise ValueError(f"refusing {flag} {value!r}")
            directives.append(f"#SBATCH --{flag}={value}")
    if spec.gres:
        if not _GRES.fullmatch(spec.gres):
            raise ValueError(f"refusing GRES {spec.gres!r}")
        directives.append(f"#SBATCH --gres={spec.gres}")
    if spec.cpus_per_task:
        directives.append(f"#SBATCH --cpus-per-task={int(spec.cpus_per_task)}")
    if spec.mem:
        directives.append(f"#SBATCH --mem={parse_mem(spec.mem)}")
    bad = [k for k in spec.env if not _ENV_NAME.fullmatch(k)]
    if bad or not spec.command:
        raise ValueError(f"bad environment names {bad} or empty command")
    env = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **spec.env}  # OR jobs::default_python_env
    exports = "\n".join(f"export {k}={q(v)}" for k, v in sorted(env.items()))
    command = " ".join(q(c) for c in spec.command)
    return (f"#!/usr/bin/env bash\n{chr(10).join(directives)}\n{exports}\n(\ncd repo || exit 97\n{command}\n)\n"
            'code=$?\necho "$code" > exit_code\nexit "$code"\n')


def queue_probe(job_id: str) -> str:
    """Port of OR `queue_probe`: a job-specific squeue, falling back to the user's whole queue."""
    j = shlex.quote(job_id)
    return ("if st=$(squeue --all --states=all -h --jobs=" + j + " -o %T 2>/dev/null); then :; "
            "elif all=$(squeue --all --states=all --user=\"$(id -un)\" -h -o '%i %T' 2>/dev/null); then "
            "st=$(printf '%s\\n' \"$all\" | while read -r id state; do "
            "if [ \"$id\" = " + j + " ]; then printf '%s\\n' \"$state\"; fi; done); "
            "else exit 1; fi; "
            "if [ -n \"$st\" ]; then echo \"SQ $st\"; else echo GONE; fi")


def inspect_script(run_dir: str, job_id: str) -> str:
    """Port of OR `inspect_job`'s probe: exit_code first, then squeue, then sacct."""
    q = shlex.quote
    return (f"d={q(run_dir)}; "
            'if [ -f "$d/exit_code" ]; then echo "EXIT $(cat "$d/exit_code")"; '
            f"else queue=$({queue_probe(job_id)}); "
            'case "$queue" in SQ*) echo "$queue";; '
            f'*) if st=$(sacct -nPX -j {q(job_id)} -o State 2>/dev/null) && [ -n "$st" ]; then echo "SA $st"; '
            'elif [ "$queue" = GONE ]; then echo GONE; else echo UNAVAILABLE; fi;; esac; fi')


def map_inspect_token(out: str) -> JobStatus:
    """Pure token -> status mapping (OR `map_inspect_token` with our failure classes)."""
    out = out.strip()
    if out.startswith("EXIT "):
        code_s = out[5:].strip()
        if not code_s:  # between open(O_TRUNC) and the write: not a verdict
            return JobStatus(JobStage.RUNNING)
        try:
            return policy_exit(int(code_s))
        except ValueError:
            return JobStatus(JobStage.UNKNOWN, message=f"unreadable exit code {code_s!r}")
    if out in ("GONE", "UNAVAILABLE"):
        return JobStatus(JobStage.UNKNOWN, message=f"scheduler monitoring: {out}")
    if out.startswith("SQ "):
        source, raw = "squeue", out[3:]
    elif out.startswith("SA "):
        source, raw = "sacct", out[3:]
    else:
        return JobStatus(JobStage.UNKNOWN, message=f"unexpected inspect output: {out[:200]!r}")
    word = (raw.split() or [""])[0].rstrip("+")
    if word in ("PENDING", "CONFIGURING", "REQUEUED", "RESV_DEL_HOLD"):
        return JobStatus(JobStage.SCHEDULING)
    if word in ("RUNNING", "COMPLETING", "SUSPENDED", "STAGE_OUT", "SIGNALING"):
        return JobStatus(JobStage.RUNNING)
    if word == "COMPLETED":
        return JobStatus(JobStage.COMPLETED, exit_code=0) if source == "sacct" else JobStatus(JobStage.RUNNING)
    if word in ("NODE_FAIL", "PREEMPTED") and source == "squeue":
        return JobStatus(JobStage.RUNNING)  # default JobRequeue re-queues under the same id
    if word in ("CANCELLED", "REVOKED"):
        return JobStatus(JobStage.CANCELED)
    if word in ("TIMEOUT", "DEADLINE"):
        return JobStatus(JobStage.ERROR, FailureClass.POLICY, "job hit its time limit")
    if word == "OUT_OF_MEMORY":
        return JobStatus(JobStage.ERROR, FailureClass.POLICY, "slurm reported out_of_memory", oom=True)
    if word == "FAILED":
        return JobStatus(JobStage.ERROR, FailureClass.POLICY, "slurm reported failed")
    if word in ("NODE_FAIL", "BOOT_FAIL", "PREEMPTED"):
        return JobStatus(JobStage.ERROR, FailureClass.INFRA, f"slurm reported {word.lower()}")
    return JobStatus(JobStage.UNKNOWN, message=f"unrecognized slurm state: {word}")


class SlurmBackend(AsyncComputeBackend):
    name = "slurm"
    isolation = IsolationProperties(container=False, no_secrets=False, network_policy="unknown",
                                    read_only_root=False, non_root=False)

    def __init__(self, store: EvidenceStore, runs_root: Path, *, ssh: PinnedSSH, remote_root: str = "chh-runs",
                 partition: str | None = None, account: str | None = None, gres: str | None = None,
                 cpus_per_task: int | None = None, mem: str | None = None, **kw: Any):
        super().__init__(store, runs_root, **kw)
        self.ssh, self.remote_root = ssh, check_remote_root(remote_root)
        self.partition, self.account, self.gres = partition, account, gres
        self.cpus_per_task, self.mem = cpus_per_task, (parse_mem(mem) if mem else None)

    @classmethod
    def from_config(cls, store: EvidenceStore, runs_root: Path, config: OwnerConfig, name: str,
                    **kw: Any) -> SlurmBackend:
        sec = config.section("slurm", name)
        check_keys(sec, SECTION_KEYS, frozenset({"host", "host_key"}), f"slurm/{name}")
        ssh = PinnedSSH(str(sec["host"]), host_key=str(sec["host_key"]), port=int(sec.get("port", 22)),
                        identity_file=sec.get("identity_file"),
                        known_hosts_dir=Path(store.root) / "compute" / "known_hosts")
        return cls(store, runs_root, ssh=ssh, remote_root=str(sec.get("remote_root", "chh-runs")),
                   partition=sec.get("partition"), account=sec.get("account"), gres=sec.get("gres"),
                   cpus_per_task=sec.get("cpus_per_task"), mem=sec.get("mem"), **kw)

    def run_dir(self, job_key: str) -> str:
        return posixpath.join(self.remote_root, job_key.split(":", 1)[1])

    def spec(self, job_key: str, request: RunRequest) -> SlurmJobSpec:
        return SlurmJobSpec(job_key, tuple(request.command), dict(request.env), int(request.timeout_s) or None,
                            self.partition, self.account, self.gres, self.cpus_per_task, self.mem)

    def _submit(self, job_key: str, request: RunRequest, run_dir: Path) -> dict[str, str]:
        entries = collect_upload(run_dir)
        q, d = shlex.quote, self.run_dir(job_key)
        repo = posixpath.join(d, "repo")
        self.ssh.checked("mkdir", f"umask 077 && mkdir -p -- {q(self.remote_root)} && mkdir -- {q(d)} && "
                                  f"mkdir -- {q(repo)}")
        self.ssh.checked("upload", f"tar -x -f - --no-same-owner -C {q(repo)}", stdin=tar_entries(entries),
                         timeout_s=600)
        script = render_sbatch(self.spec(job_key, request))
        self.ssh.checked("write job.sbatch", f"umask 077 && cat > {q(posixpath.join(d, 'job.sbatch'))}",
                         stdin=script.encode())
        try:
            r = self.ssh.checked("sbatch", f"cd -- {q(d)} && sbatch --parsable job.sbatch")
        except RemoteError as e:
            raise LaunchUncertain(f"sbatch not acknowledged: {e}", transient=False) from e
        return {"host": self.ssh.host, "port": str(self.ssh.port), "run_dir": d, "job_id": parse_job_id(r.stdout)}

    def _status(self, handle: JobHandle) -> JobStatus:
        r = self.ssh.run(inspect_script(handle.remote["run_dir"], handle.remote["job_id"]))
        if r.returncode != 0:
            return JobStatus(JobStage.UNKNOWN, message=f"inspect failed: {r.stderr.strip()[-300:]}")
        return map_inspect_token(r.stdout)

    def _logs(self, handle: JobHandle, cursor: int, max_bytes: int) -> LogBatch:
        path = posixpath.join(handle.remote["run_dir"], "log")
        r = self.ssh.checked("logs", log_slice_script(path, cursor, max_bytes),
                             max_output_bytes=max_bytes * 14 // 10 + 4096)
        data = base64.b64decode(r.stdout)
        return LogBatch(data, cursor + len(data), len(data) < max_bytes and self._status(handle).terminal)

    def _cancel(self, handle: JobHandle) -> None:
        j = shlex.quote(handle.remote["job_id"])
        self.ssh.checked("scancel", f"if error=$(scancel {j} 2>&1); then exit 0; "
                                    f"elif [ \"$({queue_probe(handle.remote['job_id'])})\" = GONE ]; then exit 0; "
                                    "else printf '%s\\n' \"$error\" >&2; exit 1; fi")

    def _fetch(self, handle: JobHandle, rel: str) -> bytes:
        base = posixpath.join(handle.remote["run_dir"], "repo")
        r = self.ssh.checked("fetch", fetch_file_script(base, rel, FETCH_MAX_BYTES), timeout_s=600,
                             max_output_bytes=FETCH_MAX_BYTES * 14 // 10 + 4096)
        return base64.b64decode(r.stdout)

    def _cleanup(self, handle: JobHandle) -> None:
        self.ssh.checked("cleanup", f"rm -rf -- {shlex.quote(handle.remote['run_dir'])}", timeout_s=300)
