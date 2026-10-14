"""Append-only Evidence store (PLAN L2).

SQLite in this dev mode; Postgres backend before scorecard campaigns (PLAN B5).
Records are never updated in place: run status and claim status changes are appended
as new rows and the latest row wins.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..records import Claim, ClaimStatus, Cost, Evidence, Run, RunStatus, TraceStep
from ..tiers import Tier, chain_tier
from .blobs import BlobStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_status (run_id TEXT, status TEXT, detail TEXT, at REAL);
CREATE TABLE IF NOT EXISTS steps (run_id TEXT, idx INTEGER, body TEXT NOT NULL,
                                  PRIMARY KEY (run_id, idx));
CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, run_id TEXT, tier INTEGER, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim_status (claim_id TEXT, status TEXT, detail TEXT, at REAL);
CREATE TABLE IF NOT EXISTS harness_versions (hash TEXT PRIMARY KEY, harness_id TEXT,
                                             parent_hash TEXT, spec_ref TEXT, at REAL);
CREATE TABLE IF NOT EXISTS records (kind TEXT, body TEXT NOT NULL, at REAL);
CREATE INDEX IF NOT EXISTS records_kind ON records(kind);
CREATE TABLE IF NOT EXISTS ledger (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
                                   prev TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, at REAL NOT NULL);
"""

LedgerRow = tuple[int, str, str, str, str, float]  # seq, kind, body (canonical JSON), prev, hash, at

# In-process observers of ledger appends, called as observe(seq, kind) in the appending thread and context right
# after the entry is committed. The engine uses one to see which private reads a step made ITSELF (runtime/engine.py,
# taint attribution), whatever run id the read names. An observer that raises fails the append's caller (fail closed);
# an entry appended by ANOTHER process (kerneld) is not observed here.
_LEDGER_OBSERVERS: list[Callable[[int, str], None]] = []


def add_ledger_observer(observe: Callable[[int, str], None]) -> None:
    if observe not in _LEDGER_OBSERVERS:
        _LEDGER_OBSERVERS.append(observe)


class EvidenceStore:
    def __init__(self, root: Path):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.root = root  # kerneld is started on the same store (GOAL §16.5.2)
        from ..sandbox.base import protect_path
        protect_path(root)  # blobs hold private labels: no sandbox workspace may overlap the store (invariant 2)
        self.blobs = BlobStore(root / "blobs")
        # timeout: a second process (kerneld) may hold the write lock briefly; wait instead of failing
        self._db = sqlite3.connect(root / "evidence.sqlite", check_same_thread=False, timeout=30)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._lock, self._db:
            self._db.execute(sql, args)

    def _rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # --- runs & traces -------------------------------------------------
    def add_run(self, run: Run) -> None:
        self._exec("INSERT INTO runs VALUES (?, ?)", (run.id, run.model_dump_json()))
        self.set_run_status(run.id, RunStatus.RUNNING)

    def set_run_status(self, run_id: str, status: RunStatus, detail: str = "") -> None:
        self._exec("INSERT INTO run_status VALUES (?, ?, ?, ?)", (run_id, status, detail, time.time()))

    def run_status(self, run_id: str) -> tuple[RunStatus, str]:
        rows = self._rows("SELECT status, detail FROM run_status WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
                          (run_id,))
        if not rows:
            raise KeyError(run_id)
        return RunStatus(rows[0][0]), rows[0][1]

    def get_run(self, run_id: str) -> Run:
        rows = self._rows("SELECT body FROM runs WHERE id=?", (run_id,))
        if not rows:
            raise KeyError(run_id)
        return Run.model_validate_json(rows[0][0])

    def list_runs(self, limit: int = 20) -> list[Run]:
        rows = self._rows("SELECT body FROM runs ORDER BY rowid DESC LIMIT ?", (limit,))
        return [Run.model_validate_json(r[0]) for r in rows]

    def add_step(self, step: TraceStep) -> None:
        self._exec("INSERT INTO steps VALUES (?, ?, ?)", (step.run_id, step.index, step.model_dump_json()))

    def trace(self, run_id: str) -> list[TraceStep]:
        rows = self._rows("SELECT body FROM steps WHERE run_id=? ORDER BY idx", (run_id,))
        return [TraceStep.model_validate_json(r[0]) for r in rows]

    def run_cost(self, run_id: str) -> Cost:
        total = Cost()
        for s in self.trace(run_id):
            total = total + s.cost
        return total

    # --- evidence & claims ---------------------------------------------
    def add_evidence(self, ev: Evidence) -> None:
        if not self.blobs.has(ev.payload_ref):
            raise ValueError(f"evidence {ev.id} points to missing blob {ev.payload_ref}")
        self._exec("INSERT INTO evidence VALUES (?, ?, ?, ?)",
                   (ev.id, ev.produced_by_run, int(ev.tier), ev.model_dump_json()))

    def evidence_for_run(self, run_id: str) -> list[Evidence]:
        rows = self._rows("SELECT body FROM evidence WHERE run_id=? ORDER BY rowid", (run_id,))
        return [Evidence.model_validate_json(r[0]) for r in rows]

    def get_evidence(self, ev_id: str) -> Evidence:
        rows = self._rows("SELECT body FROM evidence WHERE id=?", (ev_id,))
        if not rows:
            raise KeyError(ev_id)
        return Evidence.model_validate_json(rows[0][0])

    def add_claim(self, claim: Claim) -> None:
        """Claims can only be created from existing Evidence (Chain-of-Evidence by construction): at least one id,
        and every id must resolve (fail closed: an empty list is refused, not vacuously accepted)."""
        if not claim.evidence_ids:
            raise ValueError(f"claim {claim.id} has no evidence: claims come only from evidence (GOAL L2)")
        for ev_id in claim.evidence_ids:
            self.get_evidence(ev_id)
        self._exec("INSERT INTO claims VALUES (?, ?)", (claim.id, claim.model_dump_json()))
        self.set_claim_status(claim.id, ClaimStatus.DRAFT)

    def get_claim(self, claim_id: str) -> Claim:
        rows = self._rows("SELECT body FROM claims WHERE id=?", (claim_id,))
        if not rows:
            raise KeyError(claim_id)
        return Claim.model_validate_json(rows[0][0])

    def claim_tier(self, claim_id: str) -> Tier:
        claim = self.get_claim(claim_id)
        return chain_tier(self.get_evidence(e).tier for e in claim.evidence_ids)

    def set_claim_status(self, claim_id: str, status: ClaimStatus, detail: str = "") -> None:
        self.get_claim(claim_id)  # KeyError for an unknown claim: no status without a claim (and its evidence)
        self._exec("INSERT INTO claim_status VALUES (?, ?, ?, ?)", (claim_id, status, detail, time.time()))

    def claim_status(self, claim_id: str) -> ClaimStatus:
        rows = self._rows("SELECT status FROM claim_status WHERE claim_id=? ORDER BY rowid DESC LIMIT 1",
                          (claim_id,))
        return ClaimStatus(rows[0][0])

    # --- harness versions ----------------------------------------------
    def add_harness_version(self, harness_id: str, spec_json: dict, parent_hash: str | None) -> str:
        spec_ref = self.blobs.put_json(spec_json)
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO harness_versions VALUES (?, ?, ?, ?, ?)",
                             (spec_ref, harness_id, parent_hash, spec_ref, time.time()))
        return spec_ref

    def harness_spec(self, spec_hash: str) -> dict:
        return json.loads(self.blobs.get(spec_hash))

    # --- generic append-only records (lineage, ledger, calibration, memory) -----
    def append_record(self, kind: str, body: dict) -> None:
        self._exec("INSERT INTO records VALUES (?, ?, ?)", (kind, json.dumps(body, default=str), time.time()))

    def records_where(self, kind: str, key: str, value: str) -> list[dict]:
        """Records of `kind` whose top-level JSON field `key` equals `value` (in insertion order)."""
        if not key.isidentifier():
            raise ValueError(f"invalid record key {key!r}")
        rows = self._rows(f"SELECT body FROM records WHERE kind=? AND json_extract(body, '$.{key}')=? ORDER BY rowid",
                          (kind, value))
        return [json.loads(r[0]) for r in rows]

    def records(self, kind: str, limit: int | None = None) -> list[dict]:
        sql = "SELECT body FROM records WHERE kind=? ORDER BY rowid"
        rows = self._rows(sql, (kind,))
        out = [json.loads(r[0]) for r in rows]
        return out[-limit:] if limit else out

    # --- Evidence-Kernel ledger (hash chain; the chain rule lives in kernel/ledger.py) -------------
    def ledger_append(self, kind: str, body: str, entry_hash: Callable[[int, str, str, str, float], str]
                      ) -> LedgerRow:
        """Append atomically: seq, prev and the timestamp are fixed under one lock and one transaction. The
        transaction takes SQLite's write lock BEFORE reading the head (BEGIN IMMEDIATE): another PROCESS on the same
        store (kerneld, §16.5.2) cannot read the same head in between, which would fork or collide the chain."""
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            last = self._db.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            seq, prev = (last[0] + 1, last[1]) if last else (1, "genesis")
            at = time.time()
            h = entry_hash(seq, kind, body, prev, at)
            self._db.execute("INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?)", (seq, kind, body, prev, h, at))
        for observe in tuple(_LEDGER_OBSERVERS):  # in the appender's thread and context, after the commit
            observe(seq, kind)
        return seq, kind, body, prev, h, at

    def ledger_import(self, row: LedgerRow) -> None:
        """Raw insert of an existing entry (used only to rebuild a ledger excerpt from a bundle)."""
        self._exec("INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?)", row)

    def ledger_rows(self, kind: str | None = None) -> list[LedgerRow]:
        if kind is None:
            rows = self._rows("SELECT seq, kind, body, prev, hash, at FROM ledger ORDER BY seq")
        else:
            rows = self._rows("SELECT seq, kind, body, prev, hash, at FROM ledger WHERE kind=? ORDER BY seq", (kind,))
        return [tuple(r) for r in rows]

    def close(self) -> None:
        self._db.close()
