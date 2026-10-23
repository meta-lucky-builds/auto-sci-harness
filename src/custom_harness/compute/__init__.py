"""Compute backends with declared isolation classes; runs execute recorded revisions (GOAL §5 L0).

Synchronous `run()` on every backend; the asynchronous lifecycle (submit/status/logs/cancel/reattach/collect) on the
remote ones (jobs.py). Remote backends are imported lazily from their modules (remote_docker, slurm, k8s,
modal_backend, ray_jobs, hf_jobs) and listed in registry.py.
"""

from .backend import (
    CodeOrigin,
    ComputeBackend,
    IsolationClass,
    IsolationProperties,
    IsolationRefused,
    LiveTreeRefused,
    RemoteNotBuilt,
    Revision,
    RunRequest,
    RunResult,
    find_approval,
    fresh_dir,
    materialise,
    record_non_isolated_approval,
    unique_workspaces,
)
from .backends import DockerBackend, LocalProcessBackend, SSHBackend
from .jobs import AsyncComputeBackend, FailureClass, JobHandle, JobJournal, JobStage, JobStatus, LogBatch
from .ssh import OpenSSHTransport, SSHTransport

__all__ = ["AsyncComputeBackend", "CodeOrigin", "ComputeBackend", "DockerBackend", "FailureClass", "IsolationClass",
           "IsolationProperties", "IsolationRefused", "JobHandle", "JobJournal", "JobStage", "JobStatus",
           "LiveTreeRefused", "LocalProcessBackend", "LogBatch", "OpenSSHTransport", "RemoteNotBuilt", "Revision",
           "RunRequest", "RunResult", "SSHBackend", "SSHTransport", "find_approval", "fresh_dir", "materialise",
           "record_non_isolated_approval", "unique_workspaces"]
