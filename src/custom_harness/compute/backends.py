"""Concrete compute backends (GOAL §5 L0). Each one's isolation properties are DERIVED from its settings, never
asserted by the caller.

- DockerBackend: the local Docker sandbox (sandbox/docker.py, wrapped, not changed). ISOLATED with the defaults
  (container, no network, read-only root, non-root, only explicitly passed variables, candidate image). Full
  network makes it TRUSTED_ONLY; the full image (bundled datasets, trusted task building) refuses agent code
  outright (invariant 2).
- LocalProcessBackend: the test-only LocalSandbox. Always TRUSTED_ONLY.
- SSHBackend: a plain SSH host. Always TRUSTED_ONLY (trusted code with a recorded human approval only; agent code
  never). Without a transport it never opens a connection (RemoteNotBuilt). With one (compute/ssh.py) an approved
  trusted request runs in a fresh remote directory per run. The OpenSSH transport is NOT verified against a real host.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..sandbox.base import Sandbox
from ..store import EvidenceStore
from .backend import ComputeBackend, IsolationProperties, RemoteNotBuilt
from .ssh import SSHSandbox, SSHTransport, check_remote_root

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
    """A plain SSH host runs programs as a host user with that host's files, credentials and network: TRUSTED_ONLY,
    whatever the transport. With `transport=None` (the default) no connection is ever opened: an admitted run raises
    RemoteNotBuilt. With a transport, each run uploads the materialised revision into a NEW remote directory under
    `remote_root` (never reused, also not by a retry of the same request) and runs the command there."""

    name = "ssh"
    isolation = IsolationProperties(container=False, no_secrets=False, network_policy="unknown",
                                    read_only_root=False, non_root=False)

    def __init__(self, store: EvidenceStore, runs_root: Path, *, host: str, transport: SSHTransport | None = None,
                 remote_root: str = "chh-runs"):
        super().__init__(store, runs_root)
        self.host, self.transport, self.remote_root = host, transport, check_remote_root(remote_root)

    def _sandbox(self, run_dir: Path) -> Sandbox:
        if self.transport is None:
            raise RemoteNotBuilt(f"the SSH backend ({self.host}) has no transport: no remote execution")
        return SSHSandbox(run_dir, host=self.host, transport=self.transport, remote_root=self.remote_root)
