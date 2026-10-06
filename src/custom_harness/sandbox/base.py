"""Sandbox interface (PLAN L0, B3). Docker/gVisor backends implement the same interface."""

from __future__ import annotations

import os
import stat
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel


class WorkspaceEscape(PermissionError):
    """A host-side workspace access would leave the workspace (symlink, junction, '..', absolute path).
    Agent code controls the workspace contents, so this is treated as the agent's failure."""


def safe_path(workspace: Path, rel: str | Path) -> Path:
    """The ONLY way host code may touch a file inside a sandbox workspace.

    Agent code running in the container can replace any workspace path with a symlink. The container
    sees it as harmless, but the host would follow it OUT of the workspace (reading host files into
    evidence, or overwriting them). So every component must be a plain file or directory inside the
    workspace. Safe against races only because host code never runs while agent code is running
    (one container per exec, removed before the next host step)."""
    root = Path(workspace).resolve()
    rel_p = Path(rel)
    if rel_p.is_absolute() or rel_p.drive or any(part == ".." for part in rel_p.parts):
        raise WorkspaceEscape(f"path {str(rel)!r} is not a plain relative path")
    cur = root
    for part in rel_p.parts:
        cur = cur / part
        if is_link_like(cur):
            raise WorkspaceEscape(f"refusing to follow link or special entry at {cur.relative_to(root).as_posix()!r}")
    if not cur.resolve().is_relative_to(root):
        raise WorkspaceEscape(f"path {str(rel)!r} resolves outside the workspace")
    return cur


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (Windows symlinks, junctions, WSL links)


def is_link_like(p: Path) -> bool:
    """True for anything host code must not follow: symlinks, junctions/reparse points, and entries the
    host cannot even lstat (on Windows, a symlink created inside a Linux container shows up as a WSL
    reparse point that fails with WinError 1920). A missing path is not link-like."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


class ExecResult(BaseModel):
    returncode: int
    stdout: str
    stderr: str
    wall_s: float
    timed_out: bool = False


class Sandbox(ABC):
    #: Honest description of what this backend actually isolates.
    isolation: str
    workspace: Path
    #: Python executable to use for commands inside the sandbox.
    python: str = "python"

    @abstractmethod
    def exec(self, command: list[str], timeout_s: float = 600, env: dict[str, str] | None = None) -> ExecResult:
        ...

    @abstractmethod
    def spawn(self, workspace: Path) -> Sandbox:
        """A FRESH sandbox with the same isolation settings and a new, empty workspace (confirmation runs and
        Kernel re-executions never share a workspace with development runs)."""

    @abstractmethod
    def environment(self) -> dict[str, str]:
        """What a re-run must reproduce (recorded in every Kernel evaluation record)."""
