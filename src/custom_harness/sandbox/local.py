"""Local subprocess sandbox — TESTS ONLY.

It is NOT an isolation boundary and cannot be made one: the program runs as the host user, so it can read (and
write) anything that user can, including the evidence store and task private files by absolute path, and it has
the network. What it does: workspace as cwd, a minimal environment (no credentials; HOME/USERPROFILE/TEMP point to
a fresh per-sandbox directory, not the user's), bounded output, a workspace size limit and a timeout.

Two locks keep it out of production: the constructor needs `i_understand_no_isolation=True`, and every exec
refuses unless the process is in test mode (CHH_ALLOW_UNISOLATED=1). The CLI refuses `--dev-sandbox` without it.
Agent-written code must run in DockerSandbox.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from .base import ExecResult, Sandbox, check_mount, run_bounded

# what Python and the OS need to start a process; never credentials, never the user's home or temp directory
_PASSTHROUGH = ("PATH", "SYSTEMROOT", "PATHEXT", "COMSPEC", "LANG")
TEST_MODE_ENV = "CHH_ALLOW_UNISOLATED"


def require_test_mode(what: str = "LocalSandbox") -> None:
    if os.environ.get(TEST_MODE_ENV) != "1":
        raise PermissionError(f"{what} runs programs on the host without isolation; it is for trusted test code "
                              f"only and refuses unless {TEST_MODE_ENV}=1 (use DockerSandbox)")


class LocalSandbox(Sandbox):
    isolation = "none: process + cwd + minimal env + output/workspace limits + timeout only (tests only)"

    def __init__(self, workspace: Path, *, i_understand_no_isolation: bool = False):
        if not i_understand_no_isolation:
            raise PermissionError("LocalSandbox provides no isolation; pass i_understand_no_isolation=True")
        check_mount(workspace)
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.python = sys.executable
        self._home: str | None = None

    def spawn(self, workspace: Path) -> LocalSandbox:
        box = LocalSandbox(workspace, i_understand_no_isolation=True)
        box.max_output_bytes, box.max_workspace_bytes, box.max_workspace_files = (
            self.max_output_bytes, self.max_workspace_bytes, self.max_workspace_files)
        return box

    def environment(self) -> dict[str, str]:
        return {"sandbox": self.isolation, "python": sys.version.split()[0]}

    def _env(self, extra: dict[str, str] | None) -> dict[str, str]:
        if self._home is None:  # a fresh private directory, outside the workspace (snapshots stay unchanged)
            self._home = tempfile.mkdtemp(prefix="chh-local-home-")
        env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
        env.update(HOME=self._home, USERPROFILE=self._home, TEMP=self._home, TMP=self._home, TMPDIR=self._home,
                   PYTHONNOUSERSITE="1")
        env.update(extra or {})
        return env

    def exec(self, command: list[str], timeout_s: float = 600, env: dict[str, str] | None = None) -> ExecResult:
        require_test_mode()
        r = run_bounded(command, timeout_s=timeout_s, max_output_bytes=self.max_output_bytes, cwd=self.workspace,
                        env=self._env(env), check=self._workspace_check)
        return self._result(r, r.returncode if r.returncode is not None else -1)
