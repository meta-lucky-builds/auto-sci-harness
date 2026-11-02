"""Cassettes: record model calls of a live run, replay them as a zero-call regression run (GOAL §16.8, review P2).

Format (agreed with flowk for campaign-scope replay, mailbox yield-to-flowk-01): one JSONL file per cassette, one
record per successful model call, UTF-8 with LF line ends:

    {"v": 1, "key": "<request_key>", "role": "<role or null>", "seq": <int>,
     "request": <ChatRequest.model_dump(mode="json")>,
     "response": {"text": ..., "cost": {...}, "provenance": {...}}}

`seq` numbers the records of one file in write order. A `ReplayBackend` serves the recorded response for a
request's key; identical requests are served in recorded order, and a request with no (remaining) record raises
`CassetteMiss`. It never calls a model and never falls back to live: a miss means the harness saw different input.

Invariant 21: replayed output is never evidence. Every replayed response carries `replayed=True` on the
ChatResponse and on its Provenance (which the engine stores on the trace step / module call record); the Kernel
refuses evidence derived from marked output (workstream kstats). Cassettes test the code around the model, not the
model.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import threading
from collections import deque
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..records import AccessMode, Cost, Provenance
from .base import Backend, BackendError, ChatRequest, ChatResponse

CASSETTE_VERSION = 1


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def request_key(req: ChatRequest) -> str:
    """sha256 hex of the canonical JSON of the full request (messages, model, max_tokens, temperature,
    json_schema): a request naming another model, or asking for another schema, is another request."""
    return hashlib.sha256(_canonical(req.model_dump(mode="json")).encode("utf-8")).hexdigest()


def load_cassette(path: str | Path) -> list[dict[str, Any]]:
    """All records of a cassette file, in file order (a missing file is an empty cassette)."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8", newline="") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            doc = json.loads(line)
            if doc.get("v") != CASSETTE_VERSION:
                raise ValueError(f"{p}:{n}: unsupported cassette version {doc.get('v')!r} "
                                 f"(this code reads version {CASSETTE_VERSION})")
            out.append(doc)
    return out


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path.resolve()), threading.Lock())


def _count_records(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, encoding="utf-8", newline="") as f:
        return sum(1 for line in f if line.strip())


class RecordingBackend(Backend):
    """Transparent proxy that appends one cassette record after each successful call of `inner`. Failed calls are
    not recorded. Identity (name, provider, mode, training_on_outputs, and via delegation model/family) is the
    inner backend's, so the family policy sees exactly what it would see without recording."""

    def __init__(self, inner: Backend, path: str | Path, role: str | None = None):
        self._inner = inner
        self.path = Path(path)
        self.role = role
        self.name, self.provider, self.mode = inner.name, inner.provider, inner.mode
        self.training_on_outputs = inner.training_on_outputs

    def complete(self, req: ChatRequest) -> ChatResponse:
        resp = self._inner.complete(req)
        response = {"text": resp.text, "cost": resp.cost.model_dump(mode="json"),
                    "provenance": resp.provenance.model_dump(mode="json")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(self.path):
            doc = {"v": CASSETTE_VERSION, "key": request_key(req), "role": self.role,
                   "seq": _count_records(self.path), "request": req.model_dump(mode="json"), "response": response}
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(doc, sort_keys=True) + "\n")
        return resp

    def __getattr__(self, item: str) -> Any:  # model, family, MockBackend.requests ...
        if item == "_inner":
            raise AttributeError(item)
        return getattr(self._inner, item)


class CassetteMiss(BackendError):
    """The replayed run made a request that was not recorded (or was recorded fewer times). The harness's own
    input differs from the recording: never infrastructure, never transient, never served live."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(f"cassette miss: no recorded response for request {key}" + (f"\n{detail}" if detail else ""),
                         transient=False, infra=False)
        self.key = key


def _last_text(request_doc: Mapping[str, Any]) -> str:
    msgs = request_doc.get("messages") or []
    return str(msgs[-1].get("content", "")) if msgs else ""


class ReplayBackend(Backend):
    """Serves recorded responses by request key. `role`: only that role's records (None: every record).
    strict=True (default): a key whose recorded responses are used up misses. strict=False: such a key re-serves
    its last recorded response. An unrecorded key ALWAYS misses: there is no live fallback in either mode.

    Identity (provider, model, mode, training_on_outputs) comes from the first served record set's provenance, so
    `policy.model_family` gives the recorded family for vendor providers and model-named hosts. A family the
    recorded backend only DECLARED (a `family` attribute) is not in the cassette format: pass `family=`."""

    name = "replay"

    def __init__(self, path: str | Path | None = None, strict: bool = True, *, role: str | None = None,
                 family: str | None = None, records: Iterable[Mapping[str, Any]] | None = None):
        docs = list(records) if records is not None else load_cassette(path) if path is not None else []
        if role is not None:
            docs = [d for d in docs if d.get("role") == role]
        self.path = Path(path) if path is not None else None
        self.role = role
        self.strict = strict
        self._queues: dict[str, deque[Mapping[str, Any]]] = {}
        self._last: dict[str, Mapping[str, Any]] = {}
        for d in sorted(docs, key=lambda d: d.get("seq", 0)):
            self._queues.setdefault(d["key"], deque()).append(d)
        self.served = 0
        self.misses: list[str] = []
        prov = docs[0]["response"]["provenance"] if docs else {}
        self.provider = str(prov.get("provider", "replay"))
        self.model = prov.get("model")
        self.mode = AccessMode(prov.get("mode", AccessMode.MOCK.value))
        self.training_on_outputs = str(prov.get("training_on_outputs", "unknown"))
        if family is not None:
            self.family = family
        self._docs = docs

    def complete(self, req: ChatRequest) -> ChatResponse:
        key = request_key(req)
        q = self._queues.get(key)
        if q:
            doc = q.popleft()
            self._last[key] = doc
        elif not self.strict and key in self._last:
            doc = self._last[key]
        else:
            self.misses.append(key)
            raise CassetteMiss(key, self._nearest_diff(req))
        self.served += 1
        r = doc["response"]
        prov = Provenance.model_validate({**r["provenance"], "replayed": True})
        return ChatResponse(text=r["text"], cost=Cost.model_validate(r["cost"]), provenance=prov, replayed=True)

    def _nearest_diff(self, req: ChatRequest, limit: int = 2000) -> str:
        """A short diff of the request's last message against the most similar recorded one (debugging aid)."""
        if not self._docs or not req.messages:
            return ""
        mine = req.messages[-1].content
        best = max(self._docs, key=lambda d: difflib.SequenceMatcher(None, _last_text(d["request"]), mine,
                                                                     autojunk=False).quick_ratio())
        diff = "\n".join(difflib.unified_diff(_last_text(best["request"]).splitlines(), mine.splitlines(),
                                              f"recorded seq {best.get('seq')}", "requested", lineterm="", n=1))
        return diff[:limit]


def replay_backends(path: str | Path, roles: Iterable[str] | None = None,
                    families: Mapping[str, str] | None = None, strict: bool = True) -> dict[str, ReplayBackend]:
    """One ReplayBackend per role, each serving only its role's records. `roles`: the roles to bind (default: every
    role recorded in the file; a role with no records gets an empty replay that misses on every call)."""
    docs = load_cassette(path)
    names = list(roles) if roles is not None else sorted({d["role"] for d in docs if d.get("role") is not None})
    fam = families or {}
    return {r: ReplayBackend(path, strict, role=r, family=fam.get(r),
                             records=[d for d in docs if d.get("role") == r]) for r in names}
