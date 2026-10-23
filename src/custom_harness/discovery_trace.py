"""Lab discovery-trace events from compute, literature and output builds (GOAL §17 Lab; orchestrator requirement
2026-10-23: every backend emits its lifecycle into the Lab discovery trace).

lab-agents owns the trace SCHEMA (docs/lab.md on origin/lab-agents). It was not pushed when this was built, so the
events are defined here behind a small adapter interface: producers call `TraceSink.emit(TraceEvent)`; the default
sink appends to an append-only, hash-chained JSONL file in the evidence store; `CallableSink` re-points the same
events at lab-agents' writer later without touching the producers.

Event kinds (each with the fields listed; every event also carries `kind`, `subject`, `at`):
  compute.submitted   backend, job_key, isolation_class, origin, item_hash, revision {kind, ref}, command_sha256,
                      env_names (names only, never values)
  compute.started     backend, job_key
  compute.log         backend, job_key, cursor, next_cursor, eof   (a POINTER into the job's log, not its content)
  compute.finished / compute.failed / compute.cancelled
                      backend, job_key, isolation_class, stage, failure, exit_code, oom, duration_s, cost
                      {wall_s, usd: null = unknown}
  compute.collected   backend, job_key, outputs {rel: blob sha256}
  literature.retrieval source, query (capped), window, envelope (stored response digest), n_records, record_ids
  output.paper_built  tex_sha256, certificates, manifest_sha256
  output.compiled     driver, ok, pdf_sha256, problems
  output.figure_built figure_dir name, script/svg sha256 (from figure.json)
Guard (fail closed, `TraceRefused`): no field NAME that looks like a secret or like held-out truth (key, token,
secret, password, credential, held_out, sealed, truth, label, verdict, private, hidden), no environment VALUES, and
only JSON scalars/lists/dicts with strings capped. Held-out truth never exists in the Lab (§17), and a trace event is
agent-visible Lab data. Built, NOT test-verified.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

KINDS = frozenset({"compute.submitted", "compute.started", "compute.log", "compute.finished", "compute.failed",
                   "compute.cancelled", "compute.collected", "literature.retrieval", "output.paper_built",
                   "output.compiled", "output.figure_built"})
_FORBIDDEN_KEY = re.compile(r"(?i)(key|token|secret|passw|credential|cookie|session_id|auth|held.?out|sealed|"
                            r"truth|label|verdict|private|hidden)")
# names we use that contain a forbidden fragment but carry no secret or truth (a content hash, a job identifier)
_ALLOWED_KEYS = frozenset({"job_key"})
MAX_STR = 2000


class TraceRefused(ValueError):
    """An event would carry a secret, held-out truth or an unserialisable value."""


def _check(value: Any, path: str) -> Any:
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TraceRefused(f"{path}: non-string key")
            if k not in _ALLOWED_KEYS and _FORBIDDEN_KEY.search(k):
                raise TraceRefused(f"{path}.{k}: a trace event never carries secrets or held-out truth")
            out[k] = _check(v, f"{path}.{k}")
        return out
    if isinstance(value, list | tuple):
        return [_check(v, f"{path}[]") for v in value]
    if isinstance(value, str):
        return value[:MAX_STR]
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceRefused(f"{path}: non-finite number")
        return value
    raise TraceRefused(f"{path}: {type(value).__name__} is not a JSON value")


@dataclass(frozen=True)
class TraceEvent:
    kind: str
    subject: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise TraceRefused(f"unknown trace event kind {self.kind!r}")
        object.__setattr__(self, "fields", _check(dict(self.fields), self.kind))

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject[:200], "at": self.at, "fields": dict(self.fields)}


class TraceSink(Protocol):
    def emit(self, event: TraceEvent) -> None: ...


class NullSink:
    def emit(self, event: TraceEvent) -> None:
        return None


class CallableSink:
    """Adapter: hand each event (as JSON) to another writer, e.g. lab-agents' discovery-trace API."""

    def __init__(self, fn: Callable[[dict[str, Any]], None]):
        self._fn = fn

    def emit(self, event: TraceEvent) -> None:
        self._fn(event.to_json())


class MemorySink:
    """Collects events (tests)."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> None:
        self.events.append(event)


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class JsonlTraceSink:
    """Append-only, hash-chained JSONL (each line's hash covers seq, event and the previous hash)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    @classmethod
    def for_store(cls, store: Any) -> JsonlTraceSink:
        return cls(Path(store.root) / "lab_trace" / "events.jsonl")

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, ""
        lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return 0, ""
        last = json.loads(lines[-1])
        return int(last["seq"]) + 1, str(last["hash"])

    def emit(self, event: TraceEvent) -> None:
        with self._lock:
            seq, prev = self._tail()
            core = {"seq": seq, "event": event.to_json(), "prev": prev}
            line = {**core, "hash": hashlib.sha256(_canon(core).encode()).hexdigest()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                f.write(_canon(line) + "\n")

    def verify(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        prev = ""
        if not self.path.exists():
            return out
        for n, ln in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not ln.strip():
                continue
            e = json.loads(ln)
            core = {"seq": e.get("seq"), "event": e.get("event"), "prev": e.get("prev")}
            if e.get("hash") != hashlib.sha256(_canon(core).encode()).hexdigest() or e["prev"] != prev or \
                    e["seq"] != len(out):
                raise ValueError(f"trace line {n + 1} does not verify")
            out.append(e)
            prev = e["hash"]
        return out


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_emit(sink: TraceSink | None, kind: str, subject: str, fields: Mapping[str, Any]) -> None:
    """Emit, never letting a trace failure change the producer's result. A REFUSED event (secret or truth in it) is
    a bug and is raised; an I/O failure of the sink is swallowed (the trace is observability, not evidence)."""
    if sink is None:
        return
    event = TraceEvent(kind, subject, fields)  # TraceRefused propagates
    try:
        sink.emit(event)
    except OSError:
        return


def figure_built(sink: TraceSink | None, fig_dir: Path) -> None:
    """Emit output.figure_built for a figure directory (hashes from its figure.json)."""
    meta = json.loads((Path(fig_dir) / "figure.json").read_text(encoding="utf-8"))
    safe_emit(sink, "output.figure_built", Path(fig_dir).name,
              {"figure": Path(fig_dir).name, "script_sha256": meta.get("script_sha256"),
               "svg_sha256": meta.get("svg_sha256"), "renderer_sha256": meta.get("renderer_sha256")})
