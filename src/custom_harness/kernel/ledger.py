"""The Kernel ledger: an append-only, hash-chained log of everything whose ORDER matters (GOAL §4.3 K2).

Entry kinds: register_dataset, register_artifact, prereg, data_access, certificate, bundle, retraction,
adapter_approval, role_approval, approval_request, approval.
Each entry's hash covers (seq, kind, body, prev, at), and `prev` is the previous entry's hash, so editing,
deleting or reordering any entry breaks every later hash. This makes tampering EVIDENT, not impossible:
whoever controls the database can rewrite the whole chain. External anchoring narrows that: `write_anchor`
appends (length, head hash) to an anchor file kept OUTSIDE the store (another disk, a git repo, a printout, a
message to a third party), and `check_anchors` fails unless the current chain still contains every anchored head at
its position. A rewrite of anything before an anchor is then detected, as long as the attacker cannot also rewrite
the anchor copy that is checked. It does NOT stop someone who controls both the store and every anchor copy, nor
a rewrite of entries made after the latest anchor (docs/kernel.md).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..store.evidence import EvidenceStore, LedgerRow
from .claim import canonical_json

KINDS = frozenset({"register_dataset", "register_artifact", "prereg", "data_access", "certificate", "bundle",
                   "retraction", "adapter_approval",
                   # a HUMAN's approval that a dataset really is external/future data (`chh approve-role`)
                   "role_approval",
                   "statement_approval",  # a HUMAN approved a Lean statement (`chh approve-statement`)
                   # approval node (runtime/engine.py): the engine records each pause; only `chh approve` records
                   # a human decision
                   "approval_request", "approval",
                   # §16.5.3 release ledger; §16.4 per-target alpha-accounts (open: human; reserve: preregister;
                   # retire: slots exhausted or a human)
                   "release", "alpha_open", "alpha_reserve", "alpha_retire",
                   # §16.2 minted targets (Kernel.record_mint); bodies owned by other workstreams: human feature-
                   # provenance rulings (§16.5.6) and second-family role-exam admissions (§16.8)
                   "mint", "feature_ruling", "role_exam"})


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


class AsOfLedger(Ledger):
    """The ledger as it was up to and including entry `last_seq` (§16.4: `verify` re-derives a certificate as of
    its own ledger prefix). Reads see only that prefix; appends (the logged label reads a re-derivation makes) still
    go to the real ledger, because a read is a real event that must be logged."""

    def __init__(self, store: EvidenceStore, last_seq: int):
        super().__init__(store)
        self.last_seq = last_seq

    def entries(self, kind: str | None = None) -> list[Entry]:
        return [e for e in super().entries(kind) if e.seq <= self.last_seq]


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


# ------------------------------------------------------------------ external anchoring
ANCHOR_FORMAT = "chh-anchor/1"


class AnchorError(ValueError):
    pass


def write_anchor(ledger: Ledger, path: Path, store_root: Path) -> dict[str, Any]:
    """Append the current (length, head) of an INTACT chain to an anchor file outside the store; returns the anchor."""
    path, root = Path(path).resolve(), Path(store_root).resolve()
    if path == root or path.is_relative_to(root):
        raise AnchorError(f"the anchor file must live outside the store ({root}): whoever can rewrite the store "
                          f"could rewrite an anchor kept inside it")
    entries = ledger.entries()
    problems = verify_chain(entries, full=True)
    if problems:
        raise AnchorError(f"refusing to anchor a broken chain: {problems[:3]}")
    anchor = {"format": ANCHOR_FORMAT, "length": len(entries), "head": entries[-1].hash if entries else "genesis",
              "at": time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(anchor, sort_keys=True) + "\n")
    return anchor


def check_anchors(entries: list[Entry], path: Path) -> list[str]:
    """Problems (empty = consistent): the chain must contain each anchored head at its anchored length."""
    problems: list[str] = []
    lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return [f"anchor file {path} holds no anchor"]
    for i, line in enumerate(lines, 1):
        try:
            a = json.loads(line)
            n, head = int(a["length"]), str(a["head"])
            if a.get("format") != ANCHOR_FORMAT or n < 0:
                raise ValueError("not a chh-anchor/1 record")
        except (ValueError, KeyError, TypeError) as e:
            problems.append(f"anchor line {i} unreadable ({type(e).__name__}): fail closed")
            continue
        if n > len(entries):
            problems.append(f"anchor line {i}: the ledger has {len(entries)} entries, fewer than the {n} anchored "
                            f"(entries were deleted, or this is another ledger)")
        elif (entries[n - 1].hash if n else "genesis") != head or (n and entries[n - 1].seq != n):
            problems.append(f"anchor line {i}: entry {n} no longer has the anchored head hash {head[:16]}: the "
                            f"ledger was rewritten at or before entry {n}")
    return problems
