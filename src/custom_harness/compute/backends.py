"""Concrete compute backends (GOAL §5 L0). Each one's isolation properties are DERIVED from its settings, never
asserted by the caller.

- DockerBackend: the local Docker sandbox (sandbox/docker.py, wrapped, not changed). ISOLATED with the defaults
  (container, no network, read-only root, non-root, only explicitly passed variables, candidate image). Full
  network makes it TRUSTED_ONLY; the full image (bundled datasets, trusted task building) refuses agent code
  outright (invariant 2).
- LocalProcessBackend: the test-only LocalSandbox. Always TRUSTED_ONLY.
- SSHBackend: a DECLARED stub for a plain SSH host. Always TRUSTED_ONLY; its execution path is not built and it
  never opens a connection (RemoteNotBuilt), so even an approved trusted request does not run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..sandbox.base import Sandbox
from ..store import EvidenceStore
from .backend import ComputeBackend, IsolationProperties, RemoteNotBuilt

SandboxMaker = Callable[[Path], Sandbox]


class DockerBackend(ComputeBackend):
    name = "local-docker"

    def __init__(self, store: EvidenceStore, runs_root: Path, *, network: bool = False,
                 trusted_task_building: bool = False, sandbox_maker: SandboxMaker | None = None, **settings: Any):
        super().__init__(store, runs_root)
        self.settings = {"network": network, "trusted_task_building": trusted_task_building, **settings}
        if trusted_task_building and "image" not in settings:
            from ..sandbox.docker import FULL_IMAGE
            self.settings["image"] = FULL_IMAGE
        # the full image ships bundled datasets (invariant 2): never for agent code, approved or not
        self.isolation = IsolationProperties(container=True, no_secrets=True,
                                             network_policy="open" if network else "off", read_only_root=True,
                                             non_root=True, hidden_data_free=not trusted_task_building)
        self._maker = sandbox_maker  # tests inject a fake; None = the real DockerSandbox

    def _sandbox(self, run_dir: Path) -> Sandbox:
        if self._maker is not None:
            return self._maker(run_dir)
        from ..sandbox.docker import DockerSandbox
        return DockerSandbox(run_dir, **self.settings)


class LocalProcessBackend(ComputeBackend):
    name = "local-process"
    isolation = IsolationProperties(container=False, no_secrets=False, network_policy="open", read_only_root=False,
                                    non_root=False)

    def _sandbox(self, run_dir: Path) -> Sandbox:
        from ..sandbox.local import LocalSandbox
        return LocalSandbox(run_dir, i_understand_no_isolation=True)


class SSHBackend(ComputeBackend):
    """Declared, not built: a plain SSH host runs programs as a host user with that host's files, credentials and
    network. No connection is ever opened."""

    name = "ssh"
    isolation = IsolationProperties(container=False, no_secrets=False, network_policy="unknown",
                                    read_only_root=False, non_root=False)

    def __init__(self, store: EvidenceStore, runs_root: Path, *, host: str):
        super().__init__(store, runs_root)
        self.host = host

    def _sandbox(self, run_dir: Path) -> Sandbox:
        raise RemoteNotBuilt(f"the SSH backend ({self.host}) is declared but not built: no remote execution")
