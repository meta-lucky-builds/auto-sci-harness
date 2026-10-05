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
from pathlib import Path

from ..records import (Claim, ClaimStatus, Cost, Evidence, Run, RunStatus, TraceStep)
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
"""


class EvidenceStore:
    def __init__(self, root: Path):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(root / "blobs")
        self._db = sqlite3.connect(root / "evidence.sqlite", check_same_thread=False)
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

    def get_evidence(self, ev_id: str) -> Evidence:
        rows = self._rows("SELECT body FROM evidence WHERE id=?", (ev_id,))
        if not rows:
            raise KeyError(ev_id)
        return Evidence.model_validate_json(rows[0][0])

    def add_claim(self, claim: Claim) -> None:
        """Claims can only be created from existing Evidence (Chain-of-Evidence by construction)."""
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

    def close(self) -> None:
        self._db.close()
