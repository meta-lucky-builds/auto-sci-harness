"""Dated, content-addressed literature snapshots (GOAL §5 L4.3, §6.4).

Every final response (2xx, 404/410) is stored as a write-once blob *envelope* {key, request, retrieved_at, status,
headers, body}; `index.jsonl` maps request keys to envelopes (append-only, newest wins). `freeze(as_of=T)` writes
a content-addressed manifest {as_of: T, entries: key -> envelope}; its digest is the snapshot id. An
`OfflineTransport` answers only from one frozen manifest, never from the network, so a query replays exactly.

Integrity (fail closed): an envelope is served only if its blob hash verifies AND its stored key equals the
request's key AND its stored request hashes to that key. Anything else is a LiteratureError (never a silent
re-fetch, never another request's answer). Requests missing from a frozen snapshot are lookup failures.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ..store.blobs import BlobStore
from .http import ANSWER_STATUSES, HttpResponse, LiteratureError, Transport

MANIFEST_KIND = "literature-snapshot"


def _request(url: str, params: dict[str, str]) -> dict[str, Any]:
    return {"method": "GET", "url": url, "params": sorted([str(k), str(v)] for k, v in params.items())}


def request_key(url: str, params: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(_request(url, params), sort_keys=True).encode()).hexdigest()


class SnapshotStore:
    def __init__(self, root: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(self.root / "blobs")
        self.index_path = self.root / "index.jsonl"
        self.clock = clock

    # -- writing
    def record(self, url: str, params: dict[str, str], resp: HttpResponse) -> HttpResponse:
        key = request_key(url, params)
        when = self.clock().isoformat()
        env = {"v": 1, "key": key, "request": _request(url, params), "retrieved_at": when,
               "status": resp.status, "headers": {k: v for k, v in resp.headers.items() if k == "content-type"},
               "body": resp.text}
        digest = self.blobs.put_json(env)
        with open(self.index_path, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps({"key": key, "digest": digest, "retrieved_at": when}, sort_keys=True) + "\n")
        return HttpResponse(status=resp.status, text=resp.text, headers=resp.headers, retrieved_at=when,
                            envelope=digest)

    # -- reading
    def _index(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.index_path.exists():
            for line in self.index_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    out[row["key"]] = row["digest"]
        return out

    def latest(self, key: str) -> str | None:
        return self._index().get(key)

    def load(self, digest: str, key: str) -> HttpResponse:
        try:
            env = self.blobs.get_json(digest)
        except (OSError, ValueError) as e:
            raise LiteratureError(f"snapshot integrity: envelope {digest[:12]} unreadable or corrupted: {e}") from e
        req = env.get("request") if isinstance(env, dict) else None
        if (not isinstance(req, dict) or env.get("key") != key
                or request_key(str(req.get("url")), dict(req.get("params") or [])) != key):
            raise LiteratureError(f"snapshot integrity: envelope {digest[:12]} does not belong to this request "
                                  f"(cache poisoning or a corrupted index)")
        return HttpResponse(status=int(env["status"]), text=str(env["body"]), headers=dict(env.get("headers") or {}),
                            retrieved_at=env.get("retrieved_at"), envelope=digest)

    # -- freezing
    def freeze(self, as_of: date, label: str = "") -> str:
        """Freeze every recorded request (latest envelope each) as the snapshot 'as of T'; returns its id.
        `frozen_at` is the date part only, so freezing the same content twice on one day gives the same id."""
        manifest = {"v": 1, "kind": MANIFEST_KIND, "as_of": as_of.isoformat(), "label": label,
                    "frozen_at": self.clock().date().isoformat(), "entries": self._index()}
        return self.blobs.put_json(manifest)

    def manifest(self, snapshot_id: str) -> dict[str, Any]:
        try:
            m = self.blobs.get_json(snapshot_id)
        except (OSError, ValueError) as e:
            raise LiteratureError(f"snapshot {snapshot_id[:12]} not found or corrupted: {e}") from e
        if not isinstance(m, dict) or m.get("kind") != MANIFEST_KIND or not isinstance(m.get("entries"), dict):
            raise LiteratureError(f"{snapshot_id[:12]} is not a literature snapshot manifest")
        return m


class RecordingTransport:
    """Live transport with a cache: serve the latest verified envelope if present, else fetch and record."""

    offline = False

    def __init__(self, inner: Transport, store: SnapshotStore, use_cache: bool = True):
        self.inner, self.store, self.use_cache = inner, store, use_cache

    def get(self, url: str, params: dict[str, str]) -> HttpResponse:
        key = request_key(url, params)
        if self.use_cache:
            digest = self.store.latest(key)
            if digest is not None:
                return self.store.load(digest, key)
        resp = self.inner.get(url, params)
        if 200 <= resp.status < 300 or resp.status in ANSWER_STATUSES:
            return self.store.record(url, params, resp)
        return resp


class OfflineTransport:
    """Answers only from one frozen snapshot; no network. `as_of` is the snapshot's date T."""

    offline = True

    def __init__(self, store: SnapshotStore, snapshot_id: str):
        self.store, self.snapshot_id = store, snapshot_id
        m = store.manifest(snapshot_id)
        self.entries: dict[str, str] = m["entries"]
        self.as_of = date.fromisoformat(m["as_of"])

    def get(self, url: str, params: dict[str, str]) -> HttpResponse:
        key = request_key(url, params)
        digest = self.entries.get(key)
        if digest is None:
            raise LiteratureError(f"request not in the frozen snapshot {self.snapshot_id[:12]}: GET {url}")
        return self.store.load(digest, key)
