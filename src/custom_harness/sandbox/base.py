"""Sandbox interface (PLAN L0, B3). Docker/gVisor backends implement the same interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel


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
