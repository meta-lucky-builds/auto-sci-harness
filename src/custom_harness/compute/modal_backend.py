"""Modal Sandboxes: "isolation UNVERIFIED" until the owner records evidence (GOAL §5 L0, §17; PORTMAP §4 modal row).

What we configure: no secrets at all (OR passes the synced env as a Modal Secret), `block_network=True`, the
owner's digest-pinned image, a timeout. What we cannot know from reading the SDK: whether the sandbox root
filesystem is read-only and whether the program runs as non-root (PORTMAP: [U]); nor has anyone shown that
`block_network` really blocks egress. So each isolation property is DERIVED from owner-recorded evidence (the hash of
a planted test's result, per property). With no evidence the backend is TRUSTED_ONLY with "isolation UNVERIFIED" and
admission refuses agent code (invariant 1). It never becomes ISOLATED by assertion.

Credential custody: the Modal token (id + secret) is read from owner secret files and given ONLY to the host-side
launcher process's environment (MODAL_TOKEN_ID / MODAL_TOKEN_SECRET); the sandbox gets no secret. The launcher runs
under an owner-provided Python with the `modal` SDK already installed; we never pip-install anything (OR
auto-provisions a venv on first launch).

Ported from OpenResearch (MIT, Copyright (c) 2026 alphaXiv; src/jobs/modal.rs at fe36f74): the launcher pattern
(`<python> -c LAUNCHER <subcommand>`, spec JSON on stdin, `{"sandboxId": ...}` back, exit 3 = SDK missing), reattach
by `Sandbox.from_id`, `poll()` -> exit code, `terminate()` for cancel, and the "gated" script that waits for the
uploaded source archive's `.ready` marker before extracting it (src/compute.rs `gated_script`).
Changed: block_network, no secrets, no uv injection into the image, status/failure classes, logs only after the
sandbox finished (live log streaming is not built). Built, NOT test-verified; never run against Modal.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from .backend import IsolationProperties, RunRequest
from .credentials import OwnerConfig, check_keys, read_secret_file, tool_env
from .jobs import (
    AsyncComputeBackend,
    JobHandle,
    JobStage,
    JobStatus,
    LaunchUncertain,
    LogBatch,
    RemoteError,
    Runner,
    policy_exit,
    subprocess_runner,
)
from .remote_docker import DIGEST_IMAGE, tar_entries
from .ssh import collect_upload

_HEX64 = re.compile(r"[0-9a-f]{64}")
_SANDBOX_ID = re.compile(r"sb-[A-Za-z0-9]{1,64}")
SOURCE_PATH = "/tmp/chh-source.tar"
SECTION_KEYS = frozenset({"python", "token_id_file", "token_secret_file", "image", "app", "gpu", "cpu", "memory",
                          "isolation_evidence"})
EVIDENCE_KEYS = ("egress_test_sha256", "rootfs_test_sha256", "user_test_sha256")

LAUNCHER = r'''
import base64, json, sys

def _modal():
    try:
        import modal
        return modal
    except Exception:
        sys.stderr.write("modal-not-installed")
        sys.exit(3)

def submit():
    modal = _modal()
    spec = json.load(sys.stdin)
    app = modal.App.lookup(spec["app"], create_if_missing=True)
    kwargs = {"app": app, "image": modal.Image.from_registry(spec["image"]), "timeout": int(spec["timeoutSeconds"]),
              "block_network": True}
    for k in ("gpu", "cpu", "memory"):
        if spec.get(k):
            kwargs[k] = spec[k]
    sb = modal.Sandbox.create("sh", "-c", spec["script"], *spec["argv"], **kwargs)
    try:
        sb.filesystem.copy_from_local(spec["sourceArchive"], spec["sourcePath"])
        sb.exec("touch", spec["sourcePath"] + ".ready").wait()
    except Exception:
        sb.terminate()
        raise
    print(json.dumps({"sandboxId": sb.object_id}))

def status(sid):
    modal = _modal()
    code = modal.Sandbox.from_id(sid).poll()
    print(json.dumps({"returncode": code}))

def logs(sid):
    modal = _modal()
    sb = modal.Sandbox.from_id(sid)
    if sb.poll() is None:
        print(json.dumps({"running": True}))
        return
    print(json.dumps({"running": False, "data": base64.b64encode(sb.stdout.read().encode()).decode()}))

def cancel(sid):
    _modal().Sandbox.from_id(sid).terminate()

cmd = sys.argv[1] if len(sys.argv) > 1 else ""
{"submit": lambda: submit(), "status": lambda: status(sys.argv[2]), "logs": lambda: logs(sys.argv[2]),
 "cancel": lambda: cancel(sys.argv[2])}.get(cmd, lambda: sys.exit(2))()
'''


def gated_script(archive_path: str) -> str:
    """Wait for the uploaded archive's .ready marker, extract into /work, then exec the argv given as "$@"
    (port of OR src/compute.rs `gated_script`, with the command passed as argv instead of a shell string)."""
    a = shlex.quote(archive_path)
    r = shlex.quote(archive_path + ".ready")
    return (f"set -e; while [ ! -f {r} ]; do sleep 0.1; done; mkdir -p /work; tar -xf {a} -C /work; cd /work; "
            'exec "$@"')


def isolation_from_evidence(evidence: Mapping[str, Any] | None) -> IsolationProperties:
    ev = evidence or {}
    has = {k: bool(_HEX64.fullmatch(str(ev.get(k, "")))) for k in EVIDENCE_KEYS}
    recorded = bool(str(ev.get("recorded_by", "")).strip())
    return IsolationProperties(container=True, no_secrets=True,
                               network_policy="off" if has["egress_test_sha256"] and recorded else "unknown",
                               read_only_root=has["rootfs_test_sha256"] and recorded,
                               non_root=has["user_test_sha256"] and recorded,
                               verified=all(has.values()) and recorded)


class ModalBackend(AsyncComputeBackend):
    name = "modal"

    def __init__(self, store: EvidenceStore, runs_root: Path, *, python: str, token_id_file: str,
                 token_secret_file: str, image: str, app: str = "custom-harness-lab", gpu: str | None = None,
                 cpu: float | None = None, memory: int | None = None,
                 isolation_evidence: Mapping[str, Any] | None = None, runner: Runner | None = None, **kw: Any):
        super().__init__(store, runs_root, **kw)
        if not DIGEST_IMAGE.fullmatch(image or ""):
            raise ValueError("the Modal image must be pinned by digest")
        self.python, self.image, self.app = python, image, app
        self.gpu, self.cpu, self.memory = gpu, cpu, memory
        self._token_files = (token_id_file, token_secret_file)
        self._runner = runner or subprocess_runner
        self.isolation = isolation_from_evidence(isolation_evidence)

    @classmethod
    def from_config(cls, store: EvidenceStore, runs_root: Path, config: OwnerConfig, name: str,
                    **kw: Any) -> ModalBackend:
        sec = config.section("modal", name)
        check_keys(sec, SECTION_KEYS, frozenset({"python", "token_id_file", "token_secret_file", "image"}),
                   f"modal/{name}")
        return cls(store, runs_root, python=str(sec["python"]), token_id_file=str(sec["token_id_file"]),
                   token_secret_file=str(sec["token_secret_file"]), image=str(sec["image"]),
                   app=str(sec.get("app", "custom-harness-lab")), gpu=sec.get("gpu"), cpu=sec.get("cpu"),
                   memory=sec.get("memory"), isolation_evidence=sec.get("isolation_evidence"), **kw)

    def _launch(self, *args: str, stdin: bytes | None = None, timeout_s: float = 300.0) -> Any:
        token_id, token_secret = (read_secret_file(p) for p in self._token_files)
        env = tool_env({"MODAL_TOKEN_ID": token_id.reveal(), "MODAL_TOKEN_SECRET": token_secret.reveal()})
        r = self._runner([self.python, "-I", "-c", LAUNCHER, *args], stdin=stdin, timeout_s=timeout_s, env=env)
        if r.returncode == 3:
            raise RemoteError("the modal SDK is not installed in the owner's launcher Python", transient=False)
        return r

    def spec(self, request: RunRequest, archive: Path) -> dict[str, Any]:
        return {"app": self.app, "image": self.image, "timeoutSeconds": int(request.timeout_s),
                "script": gated_script(SOURCE_PATH), "argv": ["chh", *request.command], "gpu": self.gpu,
                "cpu": self.cpu, "memory": self.memory, "sourceArchive": str(archive), "sourcePath": SOURCE_PATH}

    def _submit(self, job_key: str, request: RunRequest, run_dir: Path) -> dict[str, str]:
        if request.env:
            raise RemoteError("the Modal backend passes no environment to the sandbox (not built)", transient=False)
        archive = run_dir.parent / f"{run_dir.name}.tar"
        archive.write_bytes(tar_entries(collect_upload(run_dir)))
        r = self._launch("submit", stdin=json.dumps(self.spec(request, archive)).encode(), timeout_s=900)
        try:
            sid = json.loads(r.stdout.strip().splitlines()[-1])["sandboxId"]
        except (ValueError, KeyError, IndexError) as e:
            raise LaunchUncertain(f"modal submit not acknowledged (rc={r.returncode}): {r.stderr.strip()[-300:]}",
                                  transient=False) from e
        if not isinstance(sid, str) or not _SANDBOX_ID.fullmatch(sid):
            raise LaunchUncertain(f"modal returned an unexpected sandbox id {sid!r}", transient=False)
        return {"sandbox_id": sid, "app": self.app, "image": self.image}

    def _status(self, handle: JobHandle) -> JobStatus:
        r = self._launch("status", handle.remote["sandbox_id"])
        try:
            code = json.loads(r.stdout.strip().splitlines()[-1])["returncode"]
        except (ValueError, KeyError, IndexError):
            return JobStatus(JobStage.UNKNOWN, message=f"modal status failed: {r.stderr.strip()[-300:]}")
        if code is None:
            return JobStatus(JobStage.RUNNING)
        if any(e["kind"] == "cancel" and e["job_key"] == handle.job_key for e in self.journal.entries()):
            return JobStatus(JobStage.CANCELED)
        return policy_exit(int(code))  # a Modal timeout's exit code is [U]: counted as the program's failure

    def _logs(self, handle: JobHandle, cursor: int, max_bytes: int) -> LogBatch:
        import base64
        r = self._launch("logs", handle.remote["sandbox_id"], timeout_s=600)
        try:
            doc = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return LogBatch(b"", cursor, False)
        if doc.get("running"):
            return LogBatch(b"", cursor, False)
        data = base64.b64decode(doc.get("data", ""))[cursor:cursor + max_bytes]
        return LogBatch(data, cursor + len(data), len(data) < max_bytes)

    def _cancel(self, handle: JobHandle) -> None:
        r = self._launch("cancel", handle.remote["sandbox_id"])
        if r.returncode != 0:
            raise RemoteError(f"modal cancel failed: {r.stderr.strip()[-300:]}", transient=True)
