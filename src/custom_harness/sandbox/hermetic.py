"""Hermetic execution support (GOAL §16.5.1 entropy pin; §16.8 CPU-time limits with a contention witness).

Two trusted in-sandbox helpers live in `sandbox/support/` and are mounted READ-ONLY into a Docker sandbox at
/opt/chh (LocalSandbox, tests only, runs them from a host copy):
  - `pin/sitecustomize.py`: with CHH_ENTROPY_SEED set, Python's entropy sources draw from one deterministic stream
    (see that file for exactly what is and is not pinned);
  - `supervise.py`: the CPU-time supervisor (container PID 1) and the contention witness.

What the entropy pin buys, measured (docs/hermetic_sandbox.md): honest programs that use unseeded randomness become
byte-deterministic, so they stay in the exact non-interference class (verify/noninterference.py) instead of falling
back to the McNemar test. What it does not buy: the clock and sub-Python entropy (getrandom via ctypes, ASLR) are
not pinned. No compiler exists in the sandbox images (checked 2026-10-07: no gcc/cc/tcc/clang, no libfaketime),
so an LD_PRELOAD shim cannot be built without a download. It is therefore NOT a boundary against a program that
wants to be nondeterministic; that program falls to the McNemar fallback, whose stealth margin is printed.

Every parameter the owner has not set is in the block below.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------------------------------------------
# Parameters (GOAL §13.8). One place.
# BLAS/OpenMP threads under the pin (1 = a fixed floating-point reduction order)
PIN_THREADS = 1  # UNCALIBRATED (owner decision, GOAL §13.8)
CPU_WALL_BACKSTOP = 3.0  # UNCALIBRATED (owner decision, GOAL §13.8): wall backstop = this x the CPU limit
WITNESS_CPU_S = 2.0  # UNCALIBRATED (owner decision, GOAL §13.8): CPU seconds of the contention witness loop
WITNESS_CONTENDED_RATIO = 1.5  # UNCALIBRATED (owner decision, GOAL §13.8): witness wall/CPU above this = contended
SUPERVISOR_GRACE_S = 60.0  # host-side margin over the supervisor's own wall backstop (container start, teardown)
# ---------------------------------------------------------------------------------------------------------------

SUPPORT_SRC = Path(__file__).resolve().parent / "support"
SUPPORT_FILES = ("supervise.py", "pin/sitecustomize.py")
CONTAINER_SUPPORT = "/opt/chh"
SUPERVISOR_MARK = "CHH-SUPERVISOR "
PIN_VERSION = "chh-entropy-pin/1"


def support_hash() -> str:
    """Content hash of the trusted in-sandbox helpers (recorded with every pinned or CPU-limited run)."""
    h = hashlib.sha256()
    for rel in SUPPORT_FILES:
        h.update(rel.encode() + b"\0" + (SUPPORT_SRC / rel).read_bytes() + b"\0")
    return h.hexdigest()


def support_dir() -> Path:
    """A host directory holding exactly the helper files (content-addressed, outside every workspace). Only these
    files are mounted, never the package directory."""
    digest = support_hash()
    d = Path(tempfile.gettempdir()) / f"chh-support-{digest[:16]}"
    if not all((d / rel).is_file() and (d / rel).read_bytes() == (SUPPORT_SRC / rel).read_bytes()
               for rel in SUPPORT_FILES):
        tmp = Path(tempfile.mkdtemp(prefix="chh-support-tmp-"))
        for rel in SUPPORT_FILES:
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SUPPORT_SRC / rel, tmp / rel)
        shutil.rmtree(d, ignore_errors=True)
        try:
            tmp.rename(d)
        except OSError:  # another process won the race: its copy has the same content
            shutil.rmtree(tmp, ignore_errors=True)
    return d


@dataclass(frozen=True)
class EntropyPin:
    """The entropy a run is given. Same seed => same entropy stream, hash seed and thread layout."""

    seed: bytes

    @classmethod
    def for_run(cls, *parts: str | int) -> EntropyPin:
        """Derived from the run's identity (e.g. claim hash, arm, seed), so a replay of a stored run gets exactly the
        production run's entropy (the replay must reproduce production, not just itself)."""
        msg = "\x1f".join(str(p) for p in parts).encode()
        return cls(hashlib.sha256(PIN_VERSION.encode() + b"\0" + msg).digest())

    @property
    def digest(self) -> str:
        return hashlib.sha256(b"digest\0" + self.seed).hexdigest()[:16]

    def env(self, support: str) -> dict[str, str]:
        """Environment of a pinned run; `support` is where the helper directory is visible to the program."""
        hashseed = int.from_bytes(hashlib.sha256(b"hashseed\0" + self.seed).digest()[:4], "big")
        threads = str(PIN_THREADS)
        return {"CHH_ENTROPY_SEED": self.seed.hex(), "PYTHONPATH": f"{support}/pin",
                "PYTHONHASHSEED": str(hashseed), "TZ": "UTC", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
                "OMP_NUM_THREADS": threads, "OPENBLAS_NUM_THREADS": threads, "MKL_NUM_THREADS": threads,
                "NUMEXPR_NUM_THREADS": threads, "VECLIB_MAXIMUM_THREADS": threads, "SOURCE_DATE_EPOCH": "0",
                "CUBLAS_WORKSPACE_CONFIG": ":4096:8"}


def supervisor_argv(python: str, support: str, cpu_s: float, wall_s: float, command: list[str]) -> list[str]:
    return [python, "-I", "-S", f"{support}/supervise.py", "--cpu-s", repr(float(cpu_s)), "--wall-s",
            repr(float(wall_s)), "--", *command]


def witness_argv(python: str, support: str) -> list[str]:
    return [python, "-I", "-S", f"{support}/supervise.py", "--witness", repr(WITNESS_CPU_S)]


@dataclass(frozen=True)
class SupervisorRecord:
    rc: int | None
    cpu_s: float
    cpu_exceeded: bool
    wall_exceeded: bool
    wall_s: float
    mode: str


def split_record(stderr: str) -> tuple[str, dict[str, object] | None]:
    """(stderr without the supervisor's record lines, the LAST record or None). Only the last line counts: the
    supervisor kills every other process before writing it, so anything the program printed comes earlier."""
    lines = stderr.split("\n")
    rec: dict[str, object] | None = None
    idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(SUPERVISOR_MARK):
            idx = i
            break
    if idx is not None:
        trailing = [ln for ln in lines[idx + 1:] if ln.strip()]
        if not trailing:  # a record followed by more output is not the supervisor's last word: not trusted
            try:
                obj = json.loads(lines[idx][len(SUPERVISOR_MARK):])
            except ValueError:
                obj = None
            rec = obj if isinstance(obj, dict) else None
    kept = [ln for ln in lines if not ln.startswith(SUPERVISOR_MARK)]
    return "\n".join(kept).rstrip("\n") + ("\n" if kept and stderr.endswith("\n") else ""), rec


def parse_supervisor(rec: dict[str, object] | None) -> SupervisorRecord | None:
    if rec is None:
        return None
    try:
        rc = rec["rc"]
        cpu = rec["cpu_s"]
        wall = rec["wall_s"]
        if not (rc is None or (isinstance(rc, int) and not isinstance(rc, bool))):
            return None
        if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in (cpu, wall)):
            return None
        cpu_ex, wall_ex = rec["cpu_exceeded"], rec["wall_exceeded"]
        if not (isinstance(cpu_ex, bool) and isinstance(wall_ex, bool)):
            return None
        assert isinstance(cpu, int | float) and isinstance(wall, int | float)
        return SupervisorRecord(rc=rc, cpu_s=float(cpu), cpu_exceeded=cpu_ex,
                                wall_exceeded=wall_ex, wall_s=float(wall), mode=str(rec.get("mode", "")))
    except KeyError:
        return None


def parse_witness(stderr: str) -> float | None:
    """The witness's wall/CPU ratio, or None when its record is missing (the caller treats None as unknown)."""
    _, rec = split_record(stderr)
    if rec is None:
        return None
    ratio = rec.get("ratio")
    return float(ratio) if isinstance(ratio, int | float) and not isinstance(ratio, bool) else None
