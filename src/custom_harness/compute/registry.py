"""Every compute backend the Lab knows, with its isolation class, and the ones deliberately DROPPED (GOAL §17;
PORTMAP §4). The class column is what the backend DERIVES from its properties with no owner evidence recorded; the
code (ComputeBackend.isolation_class) is the authority, this table is documentation that a test compares with it.

`build(kind, name, ...)` constructs a configured backend from the human-owned config (credentials.py); a dropped
kind is refused with its reason. Built, NOT test-verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from .credentials import OwnerConfig


@dataclass(frozen=True)
class BackendInfo:
    kind: str
    module: str  # "<module>:<class>"
    default_class: str  # "isolated" | "trusted_only" (derived with no owner evidence)
    becomes_isolated_with: str  # what it would take, or "never"
    note: str


BACKENDS: tuple[BackendInfo, ...] = (
    BackendInfo("local-docker", "custom_harness.compute.backends:DockerBackend", "isolated", "n/a",
                "local Docker sandbox, network off, candidate image"),
    BackendInfo("remote-docker", "custom_harness.compute.remote_docker:RemoteDockerBackend", "isolated", "n/a",
                "Docker on a rented GPU host over SSH; host key pinned; digest-pinned candidate image"),
    BackendInfo("k8s", "custom_harness.compute.k8s:KubernetesBackend", "trusted_only",
                "owner-recorded planted-egress test on that cluster (NetworkPolicy enforced)",
                "Job from a human-approved hashed template; agent never writes manifests"),
    BackendInfo("modal", "custom_harness.compute.modal_backend:ModalBackend", "trusted_only",
                "owner-recorded egress, rootfs and user tests (isolation UNVERIFIED until then)",
                "block_network, no secrets"),
    BackendInfo("ssh", "custom_harness.compute.backends:SSHBackend", "trusted_only", "never",
                "plain SSH host"),
    BackendInfo("slurm", "custom_harness.compute.slurm:SlurmBackend", "trusted_only", "never (Apptainer variant "
                "not built)", "sbatch over pinned SSH"),
    BackendInfo("ray", "custom_harness.compute.ray_jobs:RayBackend", "trusted_only", "never",
                "Ray Jobs REST; dashboards are often unauthenticated"),
    BackendInfo("hf-jobs", "custom_harness.compute.hf_jobs:HFJobsBackend", "trusted_only", "never (network policy "
                "unknown)", "HF Jobs REST + huggingface_hub launcher"),
    BackendInfo("local-process", "custom_harness.compute.backends:LocalProcessBackend", "trusted_only", "never",
                "tests only"),
)

DROPPED: dict[str, str] = {
    "tinker": "training is out of scope (GOAL §5 L8)",
    "openresearch-managed": "third-party billing and accepts ANY host key (OR HostKeyPolicy::Ephemeral)",
    "openresearch-rental": "third-party billing and accepts ANY host key",
    "ssh-container": "docker exec into a user's running container: we do not control its flags",
    "local-host-process": "runs on the owner's host with the owner's environment (replaced by local Docker)",
}

CONFIGURABLE = {"remote-docker", "slurm", "k8s", "modal", "ray", "hf-jobs"}


class BackendDropped(ValueError):
    pass


def info(kind: str) -> BackendInfo:
    if kind in DROPPED:
        raise BackendDropped(f"backend {kind!r} is dropped: {DROPPED[kind]}")
    for b in BACKENDS:
        if b.kind == kind:
            return b
    raise KeyError(f"unknown backend {kind!r}")


def build(kind: str, name: str, store: EvidenceStore, runs_root: Path, config: OwnerConfig | None = None,
          **kw: Any) -> Any:
    """A configured backend from the owner config. Remote backends with no config section are refused."""
    b = info(kind)
    if kind not in CONFIGURABLE:
        raise ValueError(f"{kind} is constructed directly, not from the owner config")
    import importlib
    mod, cls = b.module.split(":")
    backend_cls = getattr(importlib.import_module(mod), cls)
    return backend_cls.from_config(store, runs_root, config or OwnerConfig.load(), name, **kw)
