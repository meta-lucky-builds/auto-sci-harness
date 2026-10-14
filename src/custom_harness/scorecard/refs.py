"""Artifact references: every number on the scorecard names the artifacts it was computed from (invariant 8).

A ref is a string that pins an artifact by content, so a resolved ref proves the artifact still exists AND is the one
the number was computed from:

    ledger:<seq>:<entry hash>             a Kernel ledger entry
    blob:<sha256>                         a content-addressed blob of the Evidence store
    record:<kind>:<sha256 of body>        a row of the store's records table (canonical JSON of its body)
    run:<run id>:<sha256 of trace>        a run and its full trace (canonical JSON of every step)
    claim:<id>:<sha256 of body>           a row of the store's claims table
    input:<sha256>:<name>                 an input file given to `build` (museum log, salts audit, findings registry);
                                          a copy travels in the scorecard bundle under inputs/<sha256>

`resolve` is fail closed: anything it cannot find or whose content hash differs is a problem.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..store.evidence import EvidenceStore


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def sha(obj: Any) -> str:
    return hashlib.sha256(obj if isinstance(obj, bytes) else canonical(obj)).hexdigest()


def trace_doc(store: EvidenceStore, run_id: str) -> list[dict[str, Any]]:
    return [s.model_dump(mode="json") for s in store.trace(run_id)]


def run_ref(store: EvidenceStore, run_id: str) -> str:
    return f"run:{run_id}:{sha(trace_doc(store, run_id))}"


def record_ref(kind: str, body: dict[str, Any]) -> str:
    return f"record:{kind}:{sha(body)}"


def all_claim_rows(store: EvidenceStore) -> list[tuple[str, str]]:
    """(id, body JSON) of every row of the claims table. EvidenceStore has no listing method for claims; this is a
    read-only query through its own locked accessor (the scorecard never writes the claims table)."""
    return [(str(r[0]), str(r[1])) for r in store._rows("SELECT id, body FROM claims ORDER BY rowid")]


def resolve(ref: str, store: EvidenceStore | None, inputs_dir: Path | None) -> str | None:
    """None if `ref` resolves to the artifact it pins, else the problem (fail closed)."""
    kind, _, rest = ref.partition(":")
    try:
        if kind == "input":
            digest, _, _name = rest.partition(":")
            if inputs_dir is None:
                return f"{ref}: no inputs directory to resolve it in"
            p = inputs_dir / digest
            if not p.is_file():
                return f"{ref}: input copy missing"
            return None if hashlib.sha256(p.read_bytes()).hexdigest() == digest else f"{ref}: input copy corrupted"
        if store is None:
            return f"{ref}: no store to resolve it in"
        if kind == "ledger":
            seq_s, _, h = rest.partition(":")
            rows = [r for r in store.ledger_rows() if r[0] == int(seq_s)]
            return None if rows and rows[0][4] == h else f"{ref}: no ledger entry {seq_s} with that hash"
        if kind == "blob":
            if not store.blobs.has(rest):
                return f"{ref}: blob missing"
            store.blobs.get(rest)  # raises OSError if the bytes do not hash to the name
            return None
        if kind == "record":
            rkind, _, h = rest.partition(":")
            return None if any(sha(b) == h for b in store.records(rkind)) else f"{ref}: no such {rkind} record"
        if kind == "run":
            rid, _, h = rest.partition(":")
            store.get_run(rid)
            return None if sha(trace_doc(store, rid)) == h else f"{ref}: trace of {rid} differs"
        if kind == "claim":
            cid, _, h = rest.partition(":")
            bodies = [b for i, b in all_claim_rows(store) if i == cid]
            return None if bodies and sha(json.loads(bodies[0])) == h else f"{ref}: no such claim body"
    except (KeyError, ValueError, OSError) as e:
        return f"{ref}: {type(e).__name__}: {e}"
    return f"{ref}: unknown ref kind {kind!r}"
