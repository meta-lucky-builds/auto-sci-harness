"""Retrieval provenance and the retrieval loop (GOAL §5 L4.3).

RetrievalLog: every search result set of a session (one per answered subquery and source) is stored as a
content-addressed entry {session, source, query, window, envelope, ids}; `retrievals/<session>.jsonl` lists the
entry digests. Reading the log RE-DERIVES each entry's ids by re-parsing its stored envelope with the named source
(and the entry's window), so an edited entry, an entry copied from another session, or a lookup response passed off
as a search is an integrity error. Only searches are logged: looking up an id someone supplied is not retrieval.

RetrievalLoop: ONE publication window fixed at construction (ending at T under a time split; never widened, not
even by a follow-up), a hard cap on rounds (a failed or incomplete round still counts), and dedup across rounds by
source id -> DOI / arXiv id / PMID -> normalised title.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from .client import Deduper, LiteratureClient, SearchResult
from .http import LiteratureError
from .records import PaperRecord, in_window
from .snapshot import SnapshotStore
from .sources import ALL_SOURCES

_SESSION = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class RoundCapExceeded(LiteratureError):
    def __init__(self, msg: str):
        super().__init__(msg, transient=False, infra=False)


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


class RetrievalLog:
    def __init__(self, store: SnapshotStore, session: str):
        if not _SESSION.match(session or "") or session in (".", ".."):
            raise ValueError(f"bad session id {session!r} (ASCII letters, digits, '_', '.', '-'; at most 64)")
        self.store, self.session = store, session
        self.path = store.root / "retrievals" / f"{session}.jsonl"

    def record_search(self, result: SearchResult) -> list[str]:
        """Log every answered subquery of `result`. Results whose response was not stored (no snapshot store
        behind the client) cannot be provenance: refused."""
        digests = []
        for r in result.retrievals:
            if not r.get("envelope"):
                raise LiteratureError("retrieval result has no stored response: use a recording or offline client",
                                      infra=False)
        for r in result.retrievals:
            ids = self._derive(r["source"], r["envelope"], result.window)
            entry = {"v": 1, "session": self.session, "kind": "search", "source": r["source"],
                     "query": r["subquery"], "envelope": r["envelope"], "window": list(result.window), "ids": ids}
            digests.append(self.store.blobs.put_json(entry))
        if digests:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                for d in digests:
                    f.write(json.dumps({"digest": d}) + "\n")
        return digests

    def _derive(self, source: str, envelope: str, window: Sequence[str | None]) -> list[str]:
        try:
            env = self.store.blobs.get_json(envelope)
            resp = self.store.load(envelope, str(env.get("key")))
            src = ALL_SOURCES[source]
            recs = src.parse_search(resp)
        except (OSError, ValueError, KeyError, AttributeError, LiteratureError) as e:
            raise LiteratureError(f"retrieval log integrity: envelope {envelope[:12]} is not a {source!r} search "
                                  f"result: {e}") from e
        start, end = _d(window[0]), _d(window[1])
        return sorted({i for rec in recs if in_window(rec, start, end) for i in rec.ids()})

    def entries(self) -> list[dict[str, Any]]:
        """All entries, each verified (content hash, session, re-derived ids). Raises on any inconsistency."""
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                digest = json.loads(line)["digest"]
                e = self.store.blobs.get_json(digest)
                window = tuple(e["window"])
                ok = (e.get("v") == 1 and e.get("kind") == "search" and e.get("session") == self.session
                      and len(window) == 2 and e.get("ids") == self._derive(e["source"], e["envelope"], window))
            except (OSError, ValueError, KeyError, TypeError) as err:
                raise LiteratureError(f"retrieval log integrity: unreadable entry in {self.path.name}: {err}") from err
            if not ok:
                raise LiteratureError(f"retrieval log integrity: entry {digest[:12]} does not match its stored "
                                      f"response or belongs to another session")
            out.append({**e, "window": window, "digest": digest})
        return out

    def retrieved_ids(self) -> frozenset[str]:
        return frozenset(i for e in self.entries() for i in e["ids"])


@dataclass(frozen=True)
class Window:
    start: date | None
    end: date | None


class RetrievalLoop:
    def __init__(self, client: LiteratureClient, log: RetrievalLog | None, *, max_rounds: int, limit: int = 10,
                 sources: Sequence[str] | None = None, published_from: date | None = None,
                 published_to: date | None = None, full_text: bool = False):
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        start, end = client.window(published_from, published_to)  # raises if it would widen past T
        self._window = Window(start, end)
        self._client, self._log = client, log
        self._max_rounds, self._limit = max_rounds, limit
        self._sources = tuple(sources) if sources else None
        self._full_text = full_text  # rounds use full-text keyword search (records carry text + snippets)
        self._dedup = Deduper()
        self.rounds = 0
        self.complete = True
        self.records: list[PaperRecord] = []
        self.results: list[SearchResult] = []

    @property
    def window(self) -> Window:
        return self._window

    @property
    def max_rounds(self) -> int:
        return self._max_rounds

    def search(self, query: str) -> list[PaperRecord]:
        """One round: search inside the loop's window; returns the records not seen in earlier rounds."""
        if self.rounds >= self._max_rounds:
            raise RoundCapExceeded(f"retrieval loop round cap reached ({self._max_rounds})")
        self.rounds += 1  # counted before the call: a failing round still uses up the budget
        run = self._client.full_text_search if self._full_text else self._client.search
        res = run(query, self._limit, self._sources, published_from=self._window.start,
                  published_to=self._window.end)
        self.results.append(res)
        self.complete = self.complete and res.complete
        if self._log is not None:
            self._log.record_search(res)
        new = [r for r in res.records if self._dedup.add(r)]
        self.records.extend(new)
        return new

    follow_up = search
