"""Asynchronous compute lifecycle: submit -> status -> logs(cursor) -> cancel -> collect, and reattach after a crash
(GOAL §5 L0 "Compute backends", §17 Lab; PORTMAP §4 "gaps in our interface that OR shows").

Ported from OpenResearch (MIT, Copyright (c) 2026 alphaXiv; commit fe36f74; THIRD_PARTY_NOTICES.md):
- the stage vocabulary SCHEDULING / RUNNING / COMPLETED / ERROR / CANCELED / DELETED and `is_terminal_stage`
  (src/jobs/mod.rs `stage_to_run_status`, `is_terminal_stage`);
- the async trait shape submit/status/logs(cursor)/cancel/cleanup with a byte cursor and an `eof` flag
  (src/compute.rs `ComputeBackend`, `LogCursor`, `LogBatch`);
- `log_lines`: an unterminated last line only counts once the writer has exited (src/jobs/mod.rs).
What is OURS (not in OR) and why:
- admission (ComputeBackend.admit, invariant 1) runs before anything is materialised or submitted; every async
  backend additionally refuses secret-named variables for ANY code origin (credentials never fan out to runs, §17);
- UNKNOWN stage: OR's GONE/UNAVAILABLE ("missing scheduler evidence never proves the job has terminated") made a stage
  of its own, never terminal;
- every terminal status carries a FailureClass, mapping provider states onto our infra-vs-policy split (errors.py):
  the program's own failure (non-zero exit, OOM, its time limit) is POLICY and is scored; node failure, preemption, a
  provider error or a vanished run directory is INFRA and excludes the unit;
- handles are APPENDED to a hash-chained journal (never mutated in place), so `reattach(job_key)` after a crash reads
  the submit entry back and a rewritten journal line breaks every later hash;
- results come back only by `collect`, which pulls named files into the content-addressed store; a remote status
  string is never trusted as a result (invariant 8);
- every lifecycle step is also emitted to the Lab discovery trace (discovery_trace.py): submitted, started, log
  pointers, finished/failed/cancelled with duration and cost, collected outputs as content hashes. Events carry
  environment variable NAMES only, never values, and no credentials (TraceRefused otherwise).
Built, NOT test-verified. No backend in this package has been run against a real host or cluster.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..discovery_trace import JsonlTraceSink, TraceSink, safe_emit, sha256_text
from ..errors import InfraError
from ..sandbox.base import DEFAULT_MAX_OUTPUT_BYTES, LIMIT_RETURNCODE, ExecResult, Sandbox, run_bounded
from ..store import EvidenceStore
from .backend import (
    _SECRET_NAME,
    CodeOrigin,
    ComputeBackend,
    IsolationRefused,
    RemoteNotBuilt,
    RunRequest,
    RunResult,
    materialise,
)
from .ssh import check_rel

LOG_CHUNK_BYTES = 256 * 1024  # OR src/compute.rs reads the log in 256 KiB chunks
_JOB_KEY = re.compile(r"[a-z0-9-]{1,32}:[0-9a-f]{32}")


class JobStage(StrEnum):
    SCHEDULING = "SCHEDULING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"
    CANCELED = "CANCELED"
    DELETED = "DELETED"
    UNKNOWN = "UNKNOWN"  # monitoring unavailable: never terminal, never a verdict


TERMINAL = frozenset({JobStage.COMPLETED, JobStage.ERROR, JobStage.CANCELED, JobStage.DELETED})


class FailureClass(StrEnum):
    NONE = "none"
    POLICY = "policy"  # the program's own failure: scored
    INFRA = "infra"  # the environment failed: the unit is excluded, never scored


@dataclass(frozen=True)
class JobStatus:
    stage: JobStage
    failure: FailureClass = FailureClass.NONE
    message: str = ""
    exit_code: int | None = None
    oom: bool = False

    @property
    def terminal(self) -> bool:
        return self.stage in TERMINAL

    def __post_init__(self) -> None:
        if self.stage is JobStage.COMPLETED and (self.failure is not FailureClass.NONE or self.exit_code not in (0,
                                                                                                                 None)):
            raise ValueError("COMPLETED means exit code 0 and no failure")
        if self.stage is JobStage.ERROR and self.failure is FailureClass.NONE:
            raise ValueError("an ERROR status must say whether it is a policy or an infra failure")


def policy_exit(code: int, message: str = "") -> JobStatus:
    """A finished program: exit 0 is COMPLETED, anything else is the program's own (POLICY) failure."""
    if code == 0:
        return JobStatus(JobStage.COMPLETED, exit_code=0)
    return JobStatus(JobStage.ERROR, FailureClass.POLICY, message or f"exited with code {code}", exit_code=code)


def infra_error(message: str) -> JobStatus:
    return JobStatus(JobStage.ERROR, FailureClass.INFRA, message)


@dataclass(frozen=True)
class LogBatch:
    data: bytes
    next_cursor: int
    eof: bool


def log_lines(text: str, drain: bool) -> Iterator[str]:
    """Lines of a run log; an unterminated last line may still be mid-flush and only counts when `drain` is set
    (port of OR src/jobs/mod.rs `log_lines`)."""
    for line in text.splitlines(keepends=True):
        if line.endswith("\n"):
            yield line[:-1].removesuffix("\r")
        elif drain:
            yield line


@dataclass(frozen=True)
class JobHandle:
    """What reattaches to a job: our key, the backend, and the backend's own remote coordinates (strings only)."""

    backend: str
    job_key: str
    remote: Mapping[str, str]
    item_hash: str
    origin: str
    approval_seq: int | None = None

    def __post_init__(self) -> None:
        if not _JOB_KEY.fullmatch(self.job_key):
            raise ValueError(f"bad job key {self.job_key!r}")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in self.remote.items()):
            raise ValueError("remote coordinates are strings")
        object.__setattr__(self, "remote", MappingProxyType(dict(self.remote)))

    def to_json(self) -> dict[str, Any]:
        return {"backend": self.backend, "job_key": self.job_key, "remote": dict(sorted(self.remote.items())),
                "item_hash": self.item_hash, "origin": self.origin, "approval_seq": self.approval_seq}

    @staticmethod
    def from_json(d: Mapping[str, Any]) -> JobHandle:
        return JobHandle(str(d["backend"]), str(d["job_key"]), dict(d["remote"]), str(d["item_hash"]),
                         str(d["origin"]), d.get("approval_seq"))


# ---------------------------------------------------------------- the journal (append-only, hash-chained)
class JournalError(ValueError):
    """The job journal does not verify (an edited, deleted or reordered line)."""


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class JobJournal:
    """`<store>/compute/jobs.jsonl`: one entry per lifecycle event. Each entry's hash covers (seq, kind, job_key,
    body, prev); `prev` is the previous entry's hash. Reading verifies the whole chain (tamper-EVIDENT, like the
    Kernel ledger; whoever controls the file can rewrite all of it)."""

    KINDS = frozenset({"submit", "submit_uncertain", "status", "cancel", "collect", "cleanup"})

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @classmethod
    def for_store(cls, store: EvidenceStore) -> JobJournal:
        return cls(Path(store.root) / "compute" / "jobs.jsonl")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        prev = ""
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                core = {k: e[k] for k in ("seq", "kind", "job_key", "body", "prev")}
            except (ValueError, KeyError, TypeError) as err:
                raise JournalError(f"job journal line {n + 1} is unreadable") from err
            h = hashlib.sha256(_canon(core).encode()).hexdigest()
            if e.get("hash") != h or e["prev"] != prev or e["seq"] != len(out):
                raise JournalError(f"job journal entry {n + 1} does not verify (edited, deleted or reordered)")
            out.append(e)
            prev = h
        return out

    def append(self, kind: str, job_key: str, body: Mapping[str, Any]) -> dict[str, Any]:
        if kind not in self.KINDS:
            raise ValueError(f"unknown journal kind {kind!r}")
        with self._lock:
            existing = self.entries()
            prev = existing[-1]["hash"] if existing else ""
            core = {"seq": len(existing), "kind": kind, "job_key": job_key, "body": dict(body), "prev": prev}
            entry = {**core, "at": time.time(), "hash": hashlib.sha256(_canon(core).encode()).hexdigest()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                f.write(_canon(entry) + "\n")
            return entry

    def handle(self, job_key: str) -> JobHandle:
        for e in self.entries():
            if e["kind"] == "submit" and e["job_key"] == job_key:
                return JobHandle.from_json(e["body"]["handle"])
        raise KeyError(f"no submitted job {job_key!r} in the journal")

    def last_stage(self, job_key: str) -> str | None:
        stage = None
        for e in self.entries():
            if e["job_key"] == job_key and e["kind"] == "status":
                stage = e["body"].get("stage")
        return stage


# ---------------------------------------------------------------- host-side command execution
@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    stdout_omitted: int = 0


Runner = Callable[..., CommandResult]  # (argv, *, stdin: bytes | None, timeout_s: float, env: dict | None)


def subprocess_runner(argv: Sequence[str], *, stdin: bytes | None = None, timeout_s: float = 120.0,
                      env: dict[str, str] | None = None,
                      max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> CommandResult:
    """argv list only (never a local shell), bounded output, a timeout. OSError -> InfraError."""
    try:
        r = run_bounded(list(argv), timeout_s=timeout_s, max_output_bytes=max_output_bytes, env=env,
                        stdin_data=stdin)
    except OSError as e:
        raise InfraError(f"could not start {argv[0]!r}: {e}", transient=False) from e
    return CommandResult(r.returncode, r.stdout, r.stderr, r.timed_out, r.stdout_omitted)


class RemoteError(InfraError):
    """A host-side tool or a remote setup step failed: infrastructure, never the program's result."""


class LaunchUncertain(RemoteError):
    """The launch request may or may not have reached the provider (OR src/jobs/ssh.rs `LaunchUncertain`)."""


def refuse_secret_env(env: Mapping[str, str]) -> None:
    """No credential-looking variable reaches ANY async run, trusted or not (§17: credentials stay with the owner)."""
    bad = sorted(k for k in env if _SECRET_NAME.search(k))
    if bad:
        raise IsolationRefused(f"credentials never reach a run: refusing variables {bad} (any code origin)")


# ---------------------------------------------------------------- the async backend base
class AsyncComputeBackend(ComputeBackend):
    """A backend whose runs are remote jobs. Subclasses implement `_submit/_status/_logs/_cancel/_fetch/_cleanup`;
    this class enforces admission, the journal and result collection. `run()` (the synchronous interface) is
    submit + poll + drain logs, so every existing caller keeps working on an async backend."""

    poll_interval_s: float = 5.0
    submit_grace_s: float = 600.0  # scheduling time allowed on top of the request's timeout before run() gives up

    def __init__(self, store: EvidenceStore, runs_root: Path, *, journal: JobJournal | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 trace: TraceSink | None = None):
        super().__init__(store, runs_root)
        self.journal = journal or JobJournal.for_store(store)
        self.trace: TraceSink = trace if trace is not None else JsonlTraceSink.for_store(store)
        self._sleep, self._clock = sleep, clock
        self._run_dirs: dict[str, Path] = {}  # job key -> the local materialised revision (this process only)

    # --- subclass hooks
    @abstractmethod
    def _submit(self, job_key: str, request: RunRequest, run_dir: Path) -> dict[str, str]:
        """Launch the job; return its remote coordinates (strings). Raise LaunchUncertain if the launch may have
        happened but was not acknowledged."""

    @abstractmethod
    def _status(self, handle: JobHandle) -> JobStatus: ...

    @abstractmethod
    def _logs(self, handle: JobHandle, cursor: int, max_bytes: int) -> LogBatch: ...

    @abstractmethod
    def _cancel(self, handle: JobHandle) -> None:
        """Idempotent: cancelling a finished or vanished job succeeds."""

    def _fetch(self, handle: JobHandle, rel: str) -> bytes:
        raise RemoteNotBuilt(f"{self.name}: fetching run files is not built")

    def _cleanup(self, handle: JobHandle) -> None:
        """Remove remote state of a TERMINAL job (default: nothing)."""

    def _sandbox(self, run_dir: Path) -> Sandbox:
        raise RemoteNotBuilt(f"{self.name} is an asynchronous backend: use submit()/status()/logs() or run()")

    # --- public lifecycle
    def submit(self, request: RunRequest) -> JobHandle:
        seq = self.admit(request)  # invariant 1 first: nothing is materialised or sent before this
        refuse_secret_env(request.env)
        run_dir = materialise(request.revision, self.store.blobs, self.runs_root, request.key)
        job_key = f"{self.name}:{uuid.uuid4().hex}"
        item = request.item(self.name)
        try:
            remote = self._submit(job_key, request, run_dir)
        except LaunchUncertain as e:
            self.journal.append("submit_uncertain", job_key, {"item": item, "error": str(e)[:500]})
            raise
        handle = JobHandle(self.name, job_key, remote, request.item_hash(self.name), request.origin.value, seq)
        self._run_dirs[job_key] = run_dir
        self.journal.append("submit", job_key, {"handle": handle.to_json(), "item": item,
                                                "isolation_class": self.isolation_class.value})
        safe_emit(self.trace, "compute.submitted", job_key, {
            "backend": self.name, "job_key": job_key, "isolation_class": self.isolation_class.value,
            "origin": request.origin.value, "item_hash": handle.item_hash,
            "revision": {"kind": request.revision.kind, "ref": request.revision.ref},
            "command_sha256": sha256_text(json.dumps(list(request.command))), "env_names": sorted(request.env)})
        return handle

    def _submitted_at(self, job_key: str) -> float | None:
        for e in self.journal.entries():
            if e["kind"] == "submit" and e["job_key"] == job_key:
                return float(e["at"])
        return None

    def _trace_status(self, handle: JobHandle, previous: str | None, st: JobStatus) -> None:
        if st.stage is JobStage.RUNNING and previous != JobStage.RUNNING.value:
            safe_emit(self.trace, "compute.started", handle.job_key, {"backend": self.name,
                                                                      "job_key": handle.job_key})
        if st.terminal and previous not in {s.value for s in TERMINAL}:
            t0 = self._submitted_at(handle.job_key)
            duration = None if t0 is None else max(0.0, time.time() - t0)
            kind = {JobStage.COMPLETED: "compute.finished", JobStage.CANCELED: "compute.cancelled"}.get(
                st.stage, "compute.failed")
            safe_emit(self.trace, kind, handle.job_key, {
                "backend": self.name, "job_key": handle.job_key, "isolation_class": self.isolation_class.value,
                "stage": st.stage.value, "failure": st.failure.value, "exit_code": st.exit_code, "oom": st.oom,
                "message": st.message[:500], "duration_s": duration,
                # wall time is all we measure; provider billing is not read (usd null = unknown, never 0)
                "cost": {"wall_s": duration, "usd": None}})

    def reattach(self, job_key: str) -> JobHandle:
        h = self.journal.handle(job_key)
        if h.backend != self.name:
            raise ValueError(f"job {job_key} belongs to backend {h.backend!r}, not {self.name!r}")
        return h

    def status(self, handle: JobHandle) -> JobStatus:
        self._own(handle)
        st = self._status(handle)
        previous = self.journal.last_stage(handle.job_key)
        self._trace_status(handle, previous, st)
        if previous != st.stage.value or st.terminal:
            self.journal.append("status", handle.job_key, {"stage": st.stage.value, "failure": st.failure.value,
                                                           "message": st.message[:500], "exit_code": st.exit_code,
                                                           "oom": st.oom})
        return st

    def logs(self, handle: JobHandle, cursor: int = 0, max_bytes: int = LOG_CHUNK_BYTES) -> LogBatch:
        self._own(handle)
        if cursor < 0 or max_bytes <= 0:
            raise ValueError("cursor must be >= 0 and max_bytes > 0")
        batch = self._logs(handle, cursor, max_bytes)
        safe_emit(self.trace, "compute.log", handle.job_key, {"backend": self.name, "job_key": handle.job_key,
                                                              "cursor": cursor, "next_cursor": batch.next_cursor,
                                                              "eof": batch.eof})
        return batch

    def cancel(self, handle: JobHandle, reason: str) -> None:
        self._own(handle)
        self._cancel(handle)
        self.journal.append("cancel", handle.job_key, {"reason": reason[:500]})

    def collect(self, handle: JobHandle, rels: Sequence[str]) -> dict[str, str]:
        """Pull named run files into the content-addressed store (rel -> blob hash). Only for a terminal job."""
        self._own(handle)
        st = self._status(handle)
        if not st.terminal:
            raise RemoteError(f"job {handle.job_key} is {st.stage}: collect only after it has finished")
        out = {rel: self.store.blobs.put(self._fetch(handle, check_rel(rel))) for rel in rels}
        self.journal.append("collect", handle.job_key, {"files": dict(sorted(out.items()))})
        safe_emit(self.trace, "compute.collected", handle.job_key, {"backend": self.name, "job_key": handle.job_key,
                                                                    "outputs": dict(sorted(out.items()))})
        return out

    def cleanup(self, handle: JobHandle) -> None:
        self._own(handle)
        if not self._status(handle).terminal:
            raise RemoteError(f"job {handle.job_key} is not finished: cancel it first")
        self._cleanup(handle)
        self.journal.append("cleanup", handle.job_key, {})

    def _own(self, handle: JobHandle) -> None:
        if not isinstance(handle, JobHandle) or handle.backend != self.name:
            raise ValueError(f"not a {self.name} job handle")

    # --- the synchronous interface on top
    def run(self, request: RunRequest) -> RunResult:
        t0 = self._clock()
        handle = self.submit(request)
        deadline = t0 + request.timeout_s + self.submit_grace_s
        st = self.status(handle)
        timed_out = False
        while not st.terminal:
            if self._clock() >= deadline:
                timed_out = True
                self.cancel(handle, "timeout in run()")
                self.status(handle)  # journal + trace whatever the provider reports right after the cancel
                break
            self._sleep(self.poll_interval_s)
            st = self.status(handle)
        if st.stage is JobStage.UNKNOWN or (st.terminal and st.failure is FailureClass.INFRA):
            raise InfraError(f"{self.name} job {handle.job_key}: {st.stage} {st.message}", transient=True)
        if st.stage in (JobStage.CANCELED, JobStage.DELETED) and not timed_out:
            raise InfraError(f"{self.name} job {handle.job_key} was {st.stage} by someone else", transient=False)
        out, cursor = bytearray(), 0
        while len(out) < DEFAULT_MAX_OUTPUT_BYTES:
            batch = self._logs(handle, cursor, LOG_CHUNK_BYTES)
            out += batch.data
            if not batch.data or batch.eof:
                break
            cursor = batch.next_cursor
        if timed_out:
            code = -1
        elif st.oom:
            code = LIMIT_RETURNCODE
        else:
            code = st.exit_code if st.exit_code is not None else (0 if st.stage is JobStage.COMPLETED else 1)
        res = ExecResult(returncode=code, stdout=bytes(out).decode("utf-8", errors="replace"),
                         stderr=st.message, wall_s=self._clock() - t0, timed_out=timed_out,
                         limit_exceeded="out of memory" if st.oom else None)
        env = {"backend": self.name, "job_key": handle.job_key, **{f"remote.{k}": v for k, v in handle.remote.items()}}
        run_dir = self._run_dirs.get(handle.job_key, self.runs_root)
        return RunResult(res, run_dir, request.revision, self.name, self.isolation_class, CodeOrigin(handle.origin),
                         handle.approval_seq, env)


@dataclass
class FakeClock:
    """Deterministic clock/sleep pair for tests (no real waiting)."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s
