"""Hugging Face Jobs: TRUSTED_ONLY (GOAL §5 L0, §17; invariant 1). Human-approved trusted code only; agent code never.

HF Jobs' network policy is unknown (PORTMAP §4: [U]) and the job runs in HF's infrastructure with the owner's
namespace, so it is not an isolation boundary we can show.

Credential custody: the HF token is read from an owner secret file at the moment of a call. It goes (a) into the
Authorization header of our own REST calls and (b) to the host-side source-staging launcher on STDIN (as OR does),
never into an argv, the job's `environment` or its `secrets` (always {}: the job itself gets no credential).

Ported from OpenResearch (MIT, Copyright (c) 2026 alphaXiv; src/jobs/huggingface.rs at fe36f74):
- REST surface: GET {endpoint}/api/jobs/{ns}/{id}, POST .../cancel, GET .../logs (SSE `data: {"data": ...}` lines,
  skipping the "===== Job started" banner), Bearer auth on every call;
- source staging through `huggingface_hub` (`HfApi.sync_job_volume(read_only=True)` + `run_job`) in a launcher
  process whose stdout carries only the job JSON;
- the stage vocabulary (SCHEDULING, RUNNING/UPDATING, COMPLETED, ERROR, CANCELED, DELETED);
- `parse_timeout`.
Changed: no auto-provisioned venv (the owner provides the launcher Python), no secrets, ERROR is the program's
failure (POLICY: HF's job status does not separate infrastructure errors; stated limit), redirects never followed.
Built, NOT test-verified; never run against Hugging Face.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from .backend import IsolationProperties, RunRequest
from .credentials import OwnerConfig, check_keys, read_secret_file, tool_env
from .jobs import (
    AsyncComputeBackend,
    FailureClass,
    JobHandle,
    JobStage,
    JobStatus,
    LaunchUncertain,
    LogBatch,
    RemoteError,
    Runner,
    subprocess_runner,
)
from .remote_docker import DIGEST_IMAGE
from .rest import HttpCall, check, httpx_call

ENDPOINT = "https://huggingface.co"
SECTION_KEYS = frozenset({"namespace", "token_file", "python", "flavor", "image"})
_NS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_FLAVOR = re.compile(r"[a-z0-9-]{1,32}")
_STAGES = {"SCHEDULING": JobStage.SCHEDULING, "RUNNING": JobStage.RUNNING, "UPDATING": JobStage.RUNNING,
           "COMPLETED": JobStage.COMPLETED, "ERROR": JobStage.ERROR, "CANCELED": JobStage.CANCELED,
           "DELETED": JobStage.DELETED}

SOURCE_LAUNCHER = r'''
import contextlib, json, sys
from huggingface_hub import HfApi
spec = json.load(sys.stdin)
api = HfApi(endpoint=spec["endpoint"], token=spec["token"])
with contextlib.redirect_stdout(sys.stderr):
    volume = api.sync_job_volume(spec["sourceDir"], "/chh-source", namespace=spec["namespace"],
                                 remote_name="chh-" + spec["digest"], read_only=True)
    job = api.run_job(image=spec["image"], command=spec["command"], env=spec["environment"], secrets={},
                      flavor=spec["flavor"], timeout=spec["timeoutSeconds"], labels=spec["labels"],
                      volumes=[volume], namespace=spec["namespace"])
print(json.dumps({"id": job.id}))
'''


def parse_timeout(value: str) -> int:
    """'90s', '30m', '4h', '1d' or bare seconds -> positive seconds (port of OR `parse_timeout`)."""
    v = value.strip()
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86_400}.get(v[-1:], 0)
    num = v[:-1] if factor else v
    n = int(num) * (factor or 1) if num.isdigit() else 0
    if n <= 0:
        raise ValueError(f"bad timeout {value!r}: use e.g. 30m, 4h, 1d")
    return n


def map_hf_stage(stage: str, message: str = "") -> JobStatus:
    s = _STAGES.get(stage.upper(), JobStage.UNKNOWN)
    if s is JobStage.ERROR:
        return JobStatus(JobStage.ERROR, FailureClass.POLICY, message or "hf job reported ERROR")
    if s is JobStage.COMPLETED:
        return JobStatus(JobStage.COMPLETED, exit_code=0)
    return JobStatus(s, message=message if s is not JobStage.UNKNOWN else f"unknown HF stage {stage!r}")


def sse_lines(text: str) -> list[str]:
    """`data: {"data": ...}` events of an HF log stream, banner skipped (port of OR `stream_logs`' parser)."""
    out = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line.startswith("data: {"):
            continue
        try:
            data = json.loads(line[len("data: "):]).get("data")
        except (ValueError, AttributeError):
            continue
        if isinstance(data, str) and not data.startswith("===== Job started"):
            out.append(data)
    return out


class HFJobsBackend(AsyncComputeBackend):
    name = "hf-jobs"
    isolation = IsolationProperties(container=True, no_secrets=True, network_policy="unknown",
                                    read_only_root=False, non_root=False, verified=False)

    def __init__(self, store: EvidenceStore, runs_root: Path, *, namespace: str, token_file: str, python: str,
                 image: str, flavor: str = "cpu-basic", http: HttpCall | None = None, runner: Runner | None = None,
                 **kw: Any):
        super().__init__(store, runs_root, **kw)
        if not _NS.fullmatch(namespace) or not _FLAVOR.fullmatch(flavor) or not DIGEST_IMAGE.fullmatch(image):
            raise ValueError("bad namespace, flavor or (digest-pinned) image")
        self.namespace, self.token_file, self.python, self.image, self.flavor = (namespace, token_file, python,
                                                                                  image, flavor)
        self._http = http or httpx_call
        self._runner = runner or subprocess_runner

    @classmethod
    def from_config(cls, store: EvidenceStore, runs_root: Path, config: OwnerConfig, name: str,
                    **kw: Any) -> HFJobsBackend:
        sec = config.section("hf-jobs", name)
        check_keys(sec, SECTION_KEYS, frozenset({"namespace", "token_file", "python", "image"}), f"hf-jobs/{name}")
        return cls(store, runs_root, namespace=str(sec["namespace"]), token_file=str(sec["token_file"]),
                   python=str(sec["python"]), image=str(sec["image"]), flavor=str(sec.get("flavor", "cpu-basic")),
                   **kw)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {read_secret_file(self.token_file).reveal()}"}

    def _url(self, handle: JobHandle, suffix: str = "") -> str:
        jid = handle.remote["job_id"]
        if not _JOB_ID.fullmatch(jid):
            raise ValueError(f"bad HF job id {jid!r}")
        return f"{ENDPOINT}/api/jobs/{self.namespace}/{jid}{suffix}"

    def launcher_spec(self, request: RunRequest, run_dir: Path, digest: str) -> dict[str, Any]:
        return {"endpoint": ENDPOINT, "namespace": self.namespace, "sourceDir": str(run_dir), "digest": digest,
                "image": self.image, "command": ["sh", "-c", 'cd /chh-source && exec "$@"', "chh", *request.command],
                "environment": {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **request.env},
                "flavor": self.flavor, "timeoutSeconds": int(request.timeout_s), "labels": {"chh": "lab"}}

    def _submit(self, job_key: str, request: RunRequest, run_dir: Path) -> dict[str, str]:
        spec = self.launcher_spec(request, run_dir, job_key.split(":", 1)[1])
        spec["token"] = read_secret_file(self.token_file).reveal()  # stdin only, never argv or the job
        r = self._runner([self.python, "-I", "-c", SOURCE_LAUNCHER], stdin=json.dumps(spec).encode(),
                         timeout_s=1800, env=tool_env())
        try:
            jid = json.loads(r.stdout.strip().splitlines()[-1])["id"]
        except (ValueError, KeyError, IndexError) as e:
            raise LaunchUncertain(f"HF job launch not acknowledged (rc={r.returncode})", transient=False) from e
        if not isinstance(jid, str) or not _JOB_ID.fullmatch(jid):
            raise LaunchUncertain(f"HF returned an unexpected job id {jid!r}", transient=False)
        return {"namespace": self.namespace, "job_id": jid}

    def _status(self, handle: JobHandle) -> JobStatus:
        r = self._http("GET", self._url(handle), self._auth(), None, 60.0)
        if r.status == 404:
            return JobStatus(JobStage.DELETED, FailureClass.INFRA, "HF no longer knows the job")
        try:
            st = (json.loads(check(r, "hf job inspect").text()).get("status") or {})
        except (RemoteError, ValueError, AttributeError) as e:
            return JobStatus(JobStage.UNKNOWN, message=str(e)[:300])
        return map_hf_stage(str(st.get("stage") or ""), str(st.get("message") or ""))

    def _logs(self, handle: JobHandle, cursor: int, max_bytes: int) -> LogBatch:
        r = check(self._http("GET", self._url(handle, "/logs"), self._auth(), None, 120.0), "hf job logs")
        data = "".join(line + "\n" for line in sse_lines(r.text())).encode()[cursor:cursor + max_bytes]
        return LogBatch(data, cursor + len(data), len(data) < max_bytes and self._status(handle).terminal)

    def _cancel(self, handle: JobHandle) -> None:
        r = self._http("POST", self._url(handle, "/cancel"), self._auth(), None, 60.0)
        if r.status not in (200, 204, 404):
            check(r, "hf job cancel")
