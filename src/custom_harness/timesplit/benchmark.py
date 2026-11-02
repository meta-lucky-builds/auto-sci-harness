"""Time-split benchmark: freeze at T, register predictions on frozen information, resolve on later data (GOAL §6.4).

Guards (each enforced in exactly one function, reused wherever the invariant matters):
- G1 `_check_data_file`: a data file dated after T is never frozen.
- G2 `check_registration`: a prediction registered after T, or with q < p0, is refused (at registration and again
  at scoring, so a hand-built Registration cannot slip past).
- G3 `check_observation_date`: an observation dated on or before T, or outside the task's window (start, end], never
  resolves a task (at resolution and again at scoring).
- G4 `resolve` is a pure function of (benchmark, task, observation): no clock, no randomness. The Resolution's
  record hash equals its BlobStore.put_json digest.

The task's `answer` is used only by the contamination probes; it must never be shown to a predictor.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..literature.snapshot import SnapshotStore
from ..store.blobs import BlobStore
from .rules import ThresholdRule, evaluate, rule_hash

BENCHMARK_KIND = "timesplit-benchmark"


class TimesplitError(ValueError):
    """A refused time-split operation."""


def _norm(s: str) -> str:
    return " ".join(s.casefold().split())


def _digest(body: dict[str, Any]) -> str:
    # Same encoding as BlobStore.put_json, so the hash is the blob digest.
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Window(_Frozen):
    """Half-open (start, end]."""

    start: date
    end: date

    def contains(self, d: date) -> bool:
        return self.start < d <= self.end


class Task(_Frozen):
    id: str
    question: str
    rule: ThresholdRule
    window: int
    key_phrases: tuple[str, ...]
    answer: str  # the resolving fact; probes only, never shown to the predictor

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task id must be non-empty")
        return v

    @field_validator("key_phrases")
    @classmethod
    def _phrases(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v or any(not _norm(p) for p in v):
            raise ValueError("a task needs at least one non-empty key phrase (no phrases would mean no prior art)")
        return v

    @field_validator("answer")
    @classmethod
    def _answer(cls, v: str) -> str:
        if not _norm(v):
            raise ValueError("task answer must be non-empty (the answer-leak probe needs it)")
        return v


class DataFile(_Frozen):
    """Input to freeze_benchmark: a data file known at its `dated` day."""

    name: str
    content: bytes
    dated: date


class DataEntry(_Frozen):
    sha256: str
    dated: date


class Benchmark(_Frozen):
    id: str
    as_of: date
    label: str
    literature_snapshot: str
    data: dict[str, DataEntry]
    windows: tuple[Window, ...]
    tasks: tuple[Task, ...]

    def body(self) -> dict[str, Any]:
        d = self.model_dump(mode="json", exclude={"id"})
        return {"v": 1, "kind": BENCHMARK_KIND, **d}

    def task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise TimesplitError(f"task {task_id!r} is not in benchmark {self.id[:12]}")

    def window_of(self, task: Task) -> Window:
        return self.windows[task.window]


def _check_windows(as_of: date, windows: Sequence[Window]) -> None:
    if not windows:
        raise TimesplitError("a benchmark needs at least one window")
    if windows[0].start != as_of:
        raise TimesplitError(f"the first window must start at T={as_of}, not {windows[0].start}")
    for i, w in enumerate(windows):
        if not w.start < w.end:
            raise TimesplitError(f"window {i} ({w.start}, {w.end}] is empty or reversed")
        if i and windows[i - 1].end != w.start:
            raise TimesplitError(f"windows must be contiguous: window {i} starts {w.start}, "
                                 f"previous ends {windows[i - 1].end}")


def _check_data_file(as_of: date, f: DataFile) -> None:
    """G1."""
    if f.dated > as_of:
        raise TimesplitError(f"data file {f.name!r} is dated {f.dated}, after T={as_of}: only data known at T "
                             f"may be frozen")


def _check_tasks(as_of: date, windows: Sequence[Window], tasks: Sequence[Task]) -> None:
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            raise TimesplitError(f"duplicate task id {t.id!r}")
        seen.add(t.id)
        if not 0 <= t.window < len(windows):
            raise TimesplitError(f"task {t.id!r}: window index {t.window} is not one of the declared windows")
        if windows[t.window].start < as_of:
            raise TimesplitError(f"task {t.id!r}: its resolution window starts before T={as_of}")


def freeze_benchmark(*, snapshots: SnapshotStore, blobs: BlobStore, as_of: date,
                     windows: Sequence[Window | tuple[date, date]], tasks: Iterable[Task | dict[str, Any]],
                     data_files: Iterable[DataFile] = (), label: str = "") -> Benchmark:
    """Freeze literature (via SnapshotStore.freeze(as_of=T)) and data at T; returns the content-addressed Benchmark
    (its id is the BlobStore digest of its manifest). Nothing is stored before every check has passed."""
    ws = [w if isinstance(w, Window) else Window(start=w[0], end=w[1]) for w in windows]
    ts = [t if isinstance(t, Task) else Task.model_validate(t) for t in tasks]
    files = list(data_files)
    _check_windows(as_of, ws)
    _check_tasks(as_of, ws, ts)
    names: set[str] = set()
    for f in files:
        if f.name in names:
            raise TimesplitError(f"duplicate data file name {f.name!r}")
        names.add(f.name)
        _check_data_file(as_of, f)
    data = {f.name: DataEntry(sha256=blobs.put(f.content), dated=f.dated) for f in files}
    snapshot_id = snapshots.freeze(as_of=as_of, label=label)
    draft = Benchmark(id="", as_of=as_of, label=label, literature_snapshot=snapshot_id, data=data,
                      windows=tuple(ws), tasks=tuple(ts))
    bid = blobs.put_json(draft.body())
    return draft.model_copy(update={"id": bid})


def load_benchmark(blobs: BlobStore, benchmark_id: str) -> Benchmark:
    body = blobs.get_json(benchmark_id)  # verifies the blob hash
    if not isinstance(body, dict) or body.get("kind") != BENCHMARK_KIND:
        raise TimesplitError(f"{benchmark_id[:12]} is not a time-split benchmark manifest")
    fields = {k: v for k, v in body.items() if k not in ("v", "kind")}
    b = Benchmark.model_validate({"id": benchmark_id, **fields})
    if _digest(b.body()) != benchmark_id:
        raise TimesplitError(f"benchmark {benchmark_id[:12]} does not round-trip to its id")
    return b


# -- predictions

class Registration(_Frozen):
    benchmark_id: str
    task_id: str
    harness_id: str
    q: float
    p0: float
    registered_on: date

    @model_validator(mode="after")
    def _probabilities(self) -> Registration:
        if not (0.0 < self.q < 1.0 and 0.0 < self.p0 < 1.0):
            raise ValueError("q and p0 must lie strictly between 0 and 1")
        return self

    @property
    def hash(self) -> str:
        return _digest(self.model_dump(mode="json"))


def check_registration(benchmark: Benchmark, reg: Registration) -> None:
    """G2."""
    if reg.benchmark_id != benchmark.id:
        raise TimesplitError(f"registration is for benchmark {reg.benchmark_id[:12]}, not {benchmark.id[:12]}")
    benchmark.task(reg.task_id)
    if reg.registered_on > benchmark.as_of:
        raise TimesplitError(f"prediction registered on {reg.registered_on}, after T={benchmark.as_of}: "
                             f"predictions are made on frozen information")
    if reg.q < reg.p0:
        raise TimesplitError(f"q={reg.q} < p0={reg.p0} refused (a prediction below its baseline is not risky)")


def register_prediction(benchmark: Benchmark, task_id: str, harness_id: str, q: float, p0: float,
                        registered_on: date) -> Registration:
    reg = Registration(benchmark_id=benchmark.id, task_id=task_id, harness_id=harness_id, q=q, p0=p0,
                       registered_on=registered_on)
    check_registration(benchmark, reg)
    return reg


# -- resolution

class Observation(_Frozen):
    content: bytes
    dated: date
    source: str


class Resolution(_Frozen):
    benchmark_id: str
    task_id: str
    outcome: bool
    data_sha256: str
    dated: date
    source: str
    rule_hash: str

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def record_hash(self) -> str:
        return _digest(self.body())


def check_observation_date(benchmark: Benchmark, task: Task, dated: date) -> None:
    """G3."""
    if dated <= benchmark.as_of:
        raise TimesplitError(f"observation dated {dated} is on or before T={benchmark.as_of}: it was knowable at "
                             f"the freeze and cannot resolve a time-split task")
    w = benchmark.window_of(task)
    if not w.contains(dated):
        raise TimesplitError(f"observation dated {dated} is outside task {task.id!r}'s window ({w.start}, {w.end}]")


def resolve(benchmark: Benchmark, task_id: str, observation: Observation) -> Resolution:
    """Pure: the same inputs give the same Resolution (and record hash). Raises RuleUndetermined when the
    observation does not determine the outcome."""
    task = benchmark.task(task_id)
    check_observation_date(benchmark, task, observation.dated)
    outcome = evaluate(task.rule, observation.content)
    return Resolution(benchmark_id=benchmark.id, task_id=task.id, outcome=outcome,
                      data_sha256=hashlib.sha256(observation.content).hexdigest(), dated=observation.dated,
                      source=observation.source, rule_hash=rule_hash(task.rule))
