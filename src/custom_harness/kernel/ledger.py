"""The Kernel ledger: an append-only, hash-chained log of everything whose ORDER matters (GOAL §4.3 K2).

Entry kinds: register_dataset, register_artifact, prereg, data_access, certificate, bundle, retraction,
adapter_approval, approval_request, approval.
Each entry's hash covers (seq, kind, body, prev, at), and `prev` is the previous entry's hash, so editing,
deleting or reordering any entry breaks every later hash. This makes tampering EVIDENT, not impossible:
whoever controls the database can rewrite the whole chain. Publishing the head hash (e.g. in a bundle or
a git commit) pins it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..store.evidence import EvidenceStore, LedgerRow
from .claim import canonical_json

KINDS = frozenset({"register_dataset", "register_artifact", "prereg", "data_access", "certificate", "bundle",
                   "retraction", "adapter_approval",
                   # approval node (runtime/engine.py): the engine records each pause; only `chh approve` records
                   # a human decision
                   "approval_request", "approval"})


def entry_hash(seq: int, kind: str, body: str, prev: str, at: float) -> str:
    return hashlib.sha256(canonical_json([seq, kind, body, prev, repr(at)])).hexdigest()


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: str
    body: dict[str, Any]
    prev: str
    hash: str
    at: float

    @staticmethod
    def from_row(row: LedgerRow) -> Entry:
        seq, kind, body, prev, h, at = row
        return Entry(seq, kind, json.loads(body), prev, h, at)

    def row(self) -> LedgerRow:
        return self.seq, self.kind, canonical_json(self.body).decode(), self.prev, self.hash, self.at

    def intact(self) -> bool:
        return entry_hash(self.seq, self.kind, canonical_json(self.body).decode(), self.prev, self.at) == self.hash


class Ledger:
    def __init__(self, store: EvidenceStore):
        self.store = store

    def append(self, kind: str, body: dict[str, Any]) -> Entry:
        if kind not in KINDS:
            raise ValueError(f"unknown ledger entry kind {kind!r}")
        return Entry.from_row(self.store.ledger_append(kind, canonical_json(body).decode(), entry_hash))

    def entries(self, kind: str | None = None) -> list[Entry]:
        return [Entry.from_row(r) for r in self.store.ledger_rows(kind)]

    def head(self) -> str:
        rows = self.store.ledger_rows()
        return rows[-1][4] if rows else "genesis"

    def verify(self) -> list[str]:
        """Problems with the full chain (empty list = intact)."""
        return verify_chain(self.entries(), full=True)


def verify_chain(entries: list[Entry], *, full: bool) -> list[str]:
    """Every entry must hash correctly. With full=True the entries must also form one unbroken chain
    from genesis; an excerpt (full=False) is checked per entry and for increasing seq only."""
    problems: list[str] = []
    prev_hash, prev_seq = "genesis", 0
    for e in entries:
        if not e.intact():
            problems.append(f"ledger entry {e.seq} ({e.kind}) does not match its hash")
        if e.seq <= prev_seq:
            problems.append(f"ledger entry {e.seq} out of order")
        if full and (e.seq != prev_seq + 1 or e.prev != prev_hash):
            problems.append(f"ledger chain broken at entry {e.seq}")
        prev_hash, prev_seq = e.hash, e.seq
    return problems
