"""Ray Jobs (REST): TRUSTED_ONLY (GOAL §5 L0, §17; invariant 1). Human-approved trusted code only; agent code never.

A Ray job runs as the cluster's worker user with the cluster's network and whatever the nodes hold; a Ray dashboard
is usually unauthenticated (PORTMAP §4: "usually none (open dashboard) [I]"), so anyone who can reach it can run
code there. Nothing here makes it an isolation boundary.

Ported from OpenResearch (MIT, Copyright (c) 2026 alphaXiv; src/jobs/ray.rs at fe36f74):
- POST {address}/api/jobs/ with a client-chosen submission_id, entrypoint, runtime_env {env_vars, working_dir},
  entrypoint_num_cpus/gpus; GET {address}/api/jobs/{id}; POST .../stop; GET .../logs (JSON {"logs"} or text);
- the working_dir package: content-addressed `_ray_pkg_<digest>.zip` uploaded with PUT /api/packages/gcs/<name>
  after a GET existence check (what Ray's own SDK does), referenced as gcs://<name>;
- the status map PENDING->SCHEDULING, RUNNING, SUCCEEDED->COMPLETED, FAILED->ERROR, STOPPED->CANCELED, and a 404 as
  "the cluster no longer knows the job" (UNKNOWN here, never terminal).
Changed: the entrypoint is our argv joined with shlex.quote (OR passes the run's shell command); FAILED is the
program's failure (POLICY) because Ray's job API does not separate node loss from a failing driver (stated limit);
no secrets in env_vars (jobs.refuse_secret_env). Built, NOT test-verified; never run against a Ray cluster.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
)
from .rest import HttpCall, check, httpx_call
from .ssh import collect_upload

SECTION_KEYS = frozenset({"address", "num_cpus", "num_gpus"})
_STATUS = {"PENDING": JobStage.SCHEDULING, "RUNNING": JobStage.RUNNING, "SUCCEEDED": JobStage.COMPLETED,
           "COMPLETED": JobStage.COMPLETED, "FAILED": JobStage.ERROR, "ERROR": JobStage.ERROR,
           "STOPPED": JobStage.CANCELED, "STOPPING": JobStage.CANCELED, "CANCELLED": JobStage.CANCELED,
           "CANCELED": JobStage.CANCELED}
_SUBMISSION = re.compile(r"chh-[0-9a-f]{32}")


def zip_package(run_dir: Path) -> bytes:
    """A deterministic zip of the materialised revision (sorted entries, fixed timestamps)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for e in sorted(collect_upload(run_dir), key=lambda e: e.rel):
            if e.data is None:
                continue
            info = zipfile.ZipInfo(e.rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (e.mode & 0o777) << 16
            zf.writestr(info, e.data)
    return buf.getvalue()


def map_ray_status(raw: str, message: str = "") -> JobStatus:
    stage = _STATUS.get(raw.upper(), JobStage.UNKNOWN)
    if stage is JobStage.ERROR:
        return JobStatus(JobStage.ERROR, FailureClass.POLICY, message or "ray reported FAILED")
    if stage is JobStage.COMPLETED:
        return JobStatus(JobStage.COMPLETED, exit_code=0)
    return JobStatus(stage, message=message if stage is not JobStage.UNKNOWN else f"unknown ray status {raw!r}")


class RayBackend(AsyncComputeBackend):
    name = "ray"
    isolation = IsolationProperties(container=False, no_secrets=False, network_policy="unknown",
                                    read_only_root=False, non_root=False)

    def __init__(self, store: EvidenceStore, runs_root: Path, *, address: str, num_cpus: float = 0.0,
                 num_gpus: float = 0.0, http: HttpCall | None = None, **kw: Any):
        super().__init__(store, runs_root, **kw)
        u = urlsplit(address)
        if u.scheme not in ("http", "https") or not u.netloc or u.path not in ("", "/") or u.query:
            raise ValueError(f"bad Ray address {address!r} (http[s]://host:port)")
        self.address = address.rstrip("/")
        self.num_cpus, self.num_gpus = float(num_cpus), float(num_gpus)
        self._http = http or httpx_call

    @classmethod
    def from_config(cls, store: EvidenceStore, runs_root: Path, config: OwnerConfig, name: str,
                    **kw: Any) -> RayBackend:
        sec = config.section("ray", name)
        check_keys(sec, SECTION_KEYS, frozenset({"address"}), f"ray/{name}")
        return cls(store, runs_root, address=str(sec["address"]), num_cpus=float(sec.get("num_cpus", 0)),
                   num_gpus=float(sec.get("num_gpus", 0)), **kw)

    def _call(self, method: str, path: str, body: bytes | None = None, ctype: str = "application/json") -> Any:
        headers = {"Content-Type": ctype} if body is not None else {}
        return self._http(method, f"{self.address}{path}", headers, body, 120.0)

    def stage_working_dir(self, package: bytes) -> str:
        name = f"_ray_pkg_{hashlib.sha256(package).hexdigest()[:32]}.zip"
        got = self._call("GET", f"/api/packages/gcs/{name}")
        if got.status != 200:
            if got.status != 404:
                check(got, "ray package lookup")
            check(self._call("PUT", f"/api/packages/gcs/{name}", package, "application/octet-stream"),
                  "ray package upload")
        return f"gcs://{name}"

    def body(self, submission_id: str, request: RunRequest, working_dir: str) -> dict[str, Any]:
        env = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", **request.env}
        return {"entrypoint": shlex.join(request.command), "submission_id": submission_id,
                "runtime_env": {"env_vars": env, "working_dir": working_dir}, "metadata": {"chh": "lab"},
                "entrypoint_num_cpus": self.num_cpus, "entrypoint_num_gpus": self.num_gpus}

    def _submit(self, job_key: str, request: RunRequest, run_dir: Path) -> dict[str, str]:
        if not request.command:
            raise ValueError("empty command")
        working_dir = self.stage_working_dir(zip_package(run_dir))
        sid = "chh-" + job_key.split(":", 1)[1]
        try:
            check(self._call("POST", "/api/jobs/", json.dumps(self.body(sid, request, working_dir)).encode()),
                  "ray job submit")
        except RemoteError as e:
            raise LaunchUncertain(f"ray submit not acknowledged: {e}", transient=False) from e
        return {"address": self.address, "submission_id": sid}

    def _sid(self, handle: JobHandle) -> str:
        sid = handle.remote["submission_id"]
        if not _SUBMISSION.fullmatch(sid):
            raise ValueError(f"bad submission id {sid!r}")
        return sid

    def _status(self, handle: JobHandle) -> JobStatus:
        r = self._call("GET", f"/api/jobs/{self._sid(handle)}")
        if r.status == 404:
            return JobStatus(JobStage.UNKNOWN, message="the cluster no longer knows the job (GONE)")
        try:
            doc = json.loads(check(r, "ray job inspect").text())
        except (RemoteError, ValueError) as e:
            return JobStatus(JobStage.UNKNOWN, message=str(e)[:300])
        return map_ray_status(str(doc.get("status") or "PENDING"), str(doc.get("message") or ""))

    def _logs(self, handle: JobHandle, cursor: int, max_bytes: int) -> LogBatch:
        r = check(self._call("GET", f"/api/jobs/{self._sid(handle)}/logs"), "ray job logs")
        text = r.text()
        if "json" in r.headers.get("content-type", ""):
            try:
                text = str(json.loads(text).get("logs") or "")
            except ValueError:
                text = ""
        data = text.encode()[cursor:cursor + max_bytes]
        return LogBatch(data, cursor + len(data), len(data) < max_bytes and self._status(handle).terminal)

    def _cancel(self, handle: JobHandle) -> None:
        r = self._call("POST", f"/api/jobs/{self._sid(handle)}/stop")
        if r.status not in (200, 404):
            check(r, "ray job stop")
