"""Local subprocess sandbox — DEVELOPMENT ONLY.

It does NOT isolate network or filesystem: it only runs commands with the workspace as cwd,
a scrubbed environment (no secrets inherited) and a timeout. Agent-generated code must run in
the Docker/gVisor backend once it exists (PLAN B3); this backend refuses to start unless the
caller acknowledges that.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .base import ExecResult, Sandbox

_PASSTHROUGH = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "PATHEXT", "COMSPEC")


class LocalSandbox(Sandbox):
    isolation = "none: process + cwd + scrubbed env + timeout only (dev use)"

    def __init__(self, workspace: Path, *, i_understand_no_isolation: bool = False):
        if not i_understand_no_isolation:
            raise PermissionError("LocalSandbox provides no isolation; pass i_understand_no_isolation=True")
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.python = sys.executable

    def exec(self, command: list[str], timeout_s: float = 600, env: dict[str, str] | None = None) -> ExecResult:
        base_env = {k: os.environ[k] for k in _PASSTHROUGH if k in os.environ}
        base_env.update(env or {})
        t0 = time.time()
        try:
            p = subprocess.run(command, cwd=self.workspace, env=base_env, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
            return ExecResult(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr,
                              wall_s=time.time() - t0)
        except subprocess.TimeoutExpired as e:
            def _s(x: bytes | str | None) -> str:
                return x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
            return ExecResult(returncode=-1, stdout=_s(e.stdout), stderr=_s(e.stderr),
                              wall_s=time.time() - t0, timed_out=True)
