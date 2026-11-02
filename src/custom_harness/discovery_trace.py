"""Lab discovery-trace events from compute, literature and output builds (GOAL §17 Lab; orchestrator requirement
2026-10-23: every backend emits its lifecycle into the Lab discovery trace).

ONE trace (int18, stage-a): lab-agents owns the schema (`custom_harness/lab/trace.py`, `chh-lab-trace/1`, record
kind `lab_trace`, one hash chain per session, docs/lab.md §9). Producers keep calling
`safe_emit(sink, kind, subject, fields)` with the vocabulary below; the default sink `LabTraceSink` maps each event
onto chh-lab-trace/1 (`lab_payload`) and writes it with `lab.trace.record` (source `backend`), so the lab trace's
schema and guards (no held-out truth, no verdict details, no secrets, private-blob hash check) apply on top of the
field guard here. lab-compute's separate hash-chained JSONL file (`JsonlTraceSink`) is removed: no duplicate chains.
Mapping (only new OPTIONAL fields were added to chh-lab-trace/1, a minor change by its versioning rule):
  compute.*            -> `run` (run_id = job key, op = the lifecycle step; command by sha256, env by names only,
                          log cursors as pointers, collected outputs as {path, sha256}, wall_s, usd None = unknown)
  literature.retrieval -> `literature` (source, query, window, as_of, envelopes, ids, n_records, complete, errors)
  output.*             -> `action` (tool court_paper / latex:<driver> / figure, outcome, output content hashes). The
                          Court paper's certificate list is NOT copied into the Lab trace (no verdict details there).
Events of producers without a Lab session go to the session id `UNATTACHED` (its own chain, same schema).

Producer event kinds (each with the fields listed; every event also carries `kind`, `subject`, `at`):
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


UNATTACHED = "unattached"  # the session id of Lab trace events that belong to no Lab session
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _sha(v: Any) -> str | None:
    return v if isinstance(v, str) and _HEX64.fullmatch(v) else None


def lab_payload(event: TraceEvent) -> tuple[str, dict[str, Any]]:
    """(chh-lab-trace/1 kind, payload) for a producer event (module docstring). Fields not mapped are dropped."""
    f = dict(event.fields)
    k = event.kind
    if k.startswith("compute."):
        cost = f.get("cost") if isinstance(f.get("cost"), Mapping) else {}
        p: dict[str, Any] = {"run_id": str(f.get("job_key") or event.subject)[:80], "command": "",
                             "backend": str(f.get("backend") or "")[:80], "op": k.split(".", 1)[1],
                             "job_key": f.get("job_key")}
        for key in ("isolation_class", "origin", "item_hash", "stage", "failure", "oom", "exit_code", "duration_s"):
            if f.get(key) is not None:
                p[key] = f[key]
        if f.get("message"):
            p["message"] = str(f["message"])
        rev = f.get("revision")
        if isinstance(rev, Mapping):
            p["revision"] = {"kind": str(rev.get("kind", "")), "ref": str(rev.get("ref", ""))}
        if _sha(f.get("command_sha256")):
            p["command_sha256"] = f["command_sha256"]
        if f.get("env_names"):
            p["env_names"] = [str(n) for n in f["env_names"]]
        for src, dst in (("cursor", "log_cursor"), ("next_cursor", "log_next_cursor"), ("eof", "log_eof")):
            if f.get(src) is not None:
                p[dst] = f[src]
        if cost:
            p["wall_s"], p["usd"] = cost.get("wall_s"), cost.get("usd")
        if isinstance(f.get("outputs"), Mapping):
            p["outputs"] = [{"path": str(rel), "sha256": h} for rel, h in sorted(f["outputs"].items())]
        return "run", {key: v for key, v in p.items() if v is not None}
    if k == "literature.retrieval":
        lit = {"retrieval_session": f"lab-search:{f.get('source') or event.subject}"[:64],
               "source": f.get("source"), "query": str(f.get("query") or ""),
               "window": [str(x) for x in f.get("window") or [] if x is not None],
               "as_of": None if f.get("as_of") is None else str(f["as_of"]),
               "envelopes": [str(x) for x in f.get("envelopes") or [] if x],
               "ids": [str(x) for x in f.get("record_ids") or []], "n_records": f.get("n_records"),
               "complete": f.get("complete"), "errors": [str(x) for x in f.get("errors") or [] if x]}
        return "literature", {key: v for key, v in lit.items() if v is not None}
    if k == "output.paper_built":
        outs = [{"path": n, "sha256": h} for n, h in (("paper.tex", f.get("tex_sha256")),
                                                       ("manifest.json", f.get("manifest_sha256"))) if _sha(h)]
        return "action", {"tool": "court_paper", "target": event.subject, "outcome": "ok", "outputs": outs}
    if k == "output.compiled":
        pdf = _sha(f.get("pdf_sha256"))
        return "action", {"tool": f"latex:{f.get('driver')}", "target": event.subject,
                          "outcome": "ok" if f.get("ok") else "error",
                          "outputs": [{"path": f"{event.subject}.pdf", "sha256": pdf}] if pdf else [],
                          "problems": [str(x) for x in f.get("problems") or []]}
    if k == "output.figure_built":
        outs = [{"path": n, "sha256": f[n]} for n in ("script_sha256", "svg_sha256", "renderer_sha256")
                if _sha(f.get(n))]
        return "action", {"tool": "figure", "target": str(f.get("figure") or event.subject), "outcome": "ok",
                          "outputs": outs}
    raise TraceRefused(f"no chh-lab-trace/1 mapping for {k!r}")


class LabTraceSink:
    """The default sink: each event becomes one chh-lab-trace/1 event (lab/trace.record, source `backend`) of
    `session_id` (UNATTACHED when the producer has no Lab session). The lab trace's guards apply; a refusal there
    raises lab.trace.TraceRefused (a producer bug, like TraceRefused here). One writer per session at a time (the
    lab trace's rule); the lock serialises this sink's own threads."""

    def __init__(self, store: Any, session_id: str = UNATTACHED, *, actor_id: str = "lab-compute"):
        self.store, self.session_id, self.actor_id = store, session_id, actor_id
        self._lock = threading.Lock()

    @classmethod
    def for_store(cls, store: Any, session_id: str = UNATTACHED) -> LabTraceSink:
        return cls(store, session_id)

    def emit(self, event: TraceEvent) -> None:
        from .lab import trace as T
        kind, payload = lab_payload(event)
        actor = T.Actor(type="backend", id=(str(payload.get("backend") or "") or self.actor_id)[:120])
        with self._lock:
            T.record(self.store, self.session_id, kind, payload, actor=actor, source="backend", at=event.at)


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
