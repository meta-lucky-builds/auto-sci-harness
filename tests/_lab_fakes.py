"""Test doubles for the Lab tests: a fake docker client (no Docker is used by any Lab test) and helpers.

Written 2026-10-23 by lab-agents; NOT run yet (owner gate busy). Not a test module (helper, imported as `_lab_fakes`).
"""

from __future__ import annotations

from typing import Any

from custom_harness.lab.box import DockerCLI, Result


class FakeDocker:
    """Records every docker call (argv, env handed to the docker CLI, stdin) and simulates one box's state."""

    def __init__(self, run_rc: int = 0, setup_rc: int = 0):
        self.calls: list[tuple[list[str], dict[str, str] | None, bytes | None]] = []
        self.state = "absent"
        self.run_rc = run_rc
        self.setup_rc = setup_rc
        self.files: dict[str, str] = {}
        self.internal = "true"  # what `docker network inspect` says
        self.log = ""  # the gateway's stdout
        self.log_rc = 0

    def __call__(self, argv: list[str], env: dict[str, str] | None, stdin: bytes | None,
                 timeout: float | None) -> Result:
        self.calls.append((list(argv), dict(env) if env is not None else None, stdin))
        sub = argv[1]
        if sub == "network" and argv[2] == "inspect":
            return Result(0, self.internal + "\n", "")
        if sub == "logs":
            return Result(self.log_rc, self.log, "")
        if sub == "run" and "chh-lab-gw-" in " ".join(argv[:8]):
            return Result(0, "gw0123\n", "")
        if sub == "run":
            if self.run_rc:
                return Result(self.run_rc, "", "Unable to find image")
            self.state = "running"
            return Result(0, "cid0123456789\n", "")
        if sub == "inspect":
            return Result(0, self.state + "\n", "") if self.state != "absent" else Result(1, "", "No such object")
        if sub == "pause":
            self.state = "paused"
            return Result(0, "", "")
        if sub == "unpause":
            self.state = "running"
            return Result(0, "", "")
        if sub in ("stop", "rm"):
            self.state = "absent"
            return Result(0, "", "")
        if sub == "image":
            return Result(0, "sha256:" + "ab" * 32 + "\n", "")
        if sub == "exec":
            if "cat > \"$1\"" in " ".join(argv):
                self.files[argv[-1]] = (stdin or b"").decode("utf-8")
                return Result(0, "", "")
            if "pip" in argv:
                return Result(0, "numpy==2.1.0\n", "")
            return Result(self.setup_rc, "agent output\n", "")
        return Result(0, "", "")

    def subcommands(self) -> list[str]:
        return [c[0][1] for c in self.calls]

    def run_call(self) -> tuple[list[str], dict[str, str] | None, bytes | None]:
        return next(c for c in self.calls if c[0][1] == "run" and "chh-lab-gw-" not in " ".join(c[0][:8]))


def docker(fake: FakeDocker) -> DockerCLI:
    return DockerCLI(runner=fake)


def no_value_anywhere(obj: Any, value: str) -> bool:
    return value not in repr(obj)
