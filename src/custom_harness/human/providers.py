"""HUMAN sign-off of provider registry entries (GOAL invariant 11; gateway/registry.py, gateway/costs.py).

`chh verify-registry --release` fails until every usable entry has a recent human verification. A `verified_by`
string in providers.yaml (or the override file) is text anyone with the repository or the data directory can write,
so it is not counted by the release check any more. A sign-off is a Kernel ledger entry:

- kind `approval`, node id `provider_signoff`, run id `provider:<key>` (no engine run carries that prefix, so the
  engine can never read it as a run's approval; the same scheme as audit/gate.py and evolve/finalize.py);
- bound to `entry_hash`: the sha256 of the canonical JSON of the registry entry WITHOUT `verified_by` and
  `verified_on`. Any later change to the entry (provider, mode, auth kind, BYOS status, licence flag, terms URL,
  notes) gives a new hash, and the sign-off no longer counts (invariant 15);
- naming the human (`approver`) and the date (`verified_on`, the sign-off day).

`with_ledgered_signoffs(reg, store)` is what the release check reads: each entry's `verified_by` / `verified_on` are
REPLACED by the latest intact ledgered sign-off for its current hash, or cleared when there is none. The age rule
(`costs.RELEASE_MAX_AGE_DAYS`) then applies to the sign-off date.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..kernel.claim import canonical_json
from ..kernel.ledger import Entry, Ledger
from ..store import EvidenceStore
from .catalog import PROVIDER_SIGNOFF_NODE
from .guard import HumanActionRefused, require_human

SIGNOFF_FIELDS = ("verified_by", "verified_on")
RUN_PREFIX = "provider:"


def _jsonable(v: Any) -> Any:
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def entry_hash(entry: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON of a registry entry without its sign-off fields (YAML dates as ISO strings)."""
    body = {k: _jsonable(v) for k, v in entry.items() if k not in SIGNOFF_FIELDS}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _registry(override: Path | None) -> dict[str, dict[str, Any]]:
    from ..gateway.registry import load_registry
    return load_registry(override)


def signoffs(store: EvidenceStore) -> list[dict[str, Any]]:
    """Every intact ledgered provider sign-off, oldest first."""
    out = []
    for e in Ledger(store).entries("approval"):
        b = e.body
        if not (e.intact() and b.get("node_id") == PROVIDER_SIGNOFF_NODE and b.get("decision") == "approved"):
            continue
        p = b.get("provider")
        if isinstance(p, dict) and isinstance(b.get("approver"), str) and b["approver"].strip():
            out.append({**p, "approver": b["approver"], "ledger_seq": e.seq})
    return out


def current_signoff(store: EvidenceStore, key: str, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    h = entry_hash(entry)
    mine = [s for s in signoffs(store) if s.get("key") == key and s.get("entry_hash") == h]
    return mine[-1] if mine else None


def with_ledgered_signoffs(reg: Mapping[str, Any], store: EvidenceStore) -> dict[str, Any]:
    """The registry the release check reads: sign-off fields come from the ledger only (see module docstring)."""
    out: dict[str, Any] = {}
    for key, e in reg.items():
        if not isinstance(e, Mapping):
            out[key] = e
            continue
        s = current_signoff(store, str(key), e)
        out[key] = {**{k: v for k, v in e.items() if k not in SIGNOFF_FIELDS},
                    "verified_by": s["approver"] if s else None, "verified_on": s["verified_on"] if s else None}
    return out


def sign_off_provider(store: EvidenceStore, key: str, *, by: str, entry_hash_reviewed: str, note: str = "",
                      today: dt.date | None = None, override: Path | None = None) -> Entry:
    """HUMAN action: I checked this provider's current terms and this exact registry entry is accurate."""
    name = require_human(by, what="the human signing off")
    reg = _registry(override)
    if key not in reg or not isinstance(reg[key], Mapping):
        raise HumanActionRefused(f"no provider registry entry {key!r}; known: {sorted(reg)}")
    entry = reg[key]
    h = entry_hash(entry)
    if entry_hash_reviewed != h:
        raise HumanActionRefused(f"registry entry {key} hashes to {h}, not the {entry_hash_reviewed} you reviewed")
    from ..gateway.registry import policy_problems
    problems = [p for p in policy_problems({key: entry}, owner_training={}) if p.startswith(f"{key}:")]
    if problems:
        raise HumanActionRefused(f"entry {key} fails the registry policy; fix it before signing off: {problems}")
    day = (today or dt.date.today()).isoformat()
    item = {"key": key, "entry_hash": h, "verified_on": day}
    from ..runtime.engine import approval_item_hash
    return Ledger(store).append("approval", {
        "run_id": f"{RUN_PREFIX}{key}", "node_id": PROVIDER_SIGNOFF_NODE, "item_hash": approval_item_hash(item),
        "decision": "approved", "approver": name, "note": note, "provider": item})


def provider_view(key: str, store: EvidenceStore, override: Path | None = None) -> dict[str, Any]:
    reg = _registry(override)
    if key not in reg:
        raise HumanActionRefused(f"no provider registry entry {key!r}; known: {sorted(reg)}")
    entry = reg[key]
    return {"key": key, "entry": _jsonable(entry), "entry_hash": entry_hash(entry),
            "current_signoff": current_signoff(store, key, entry),
            "decide": f"chh sign-off-provider {key} --by NAME --entry-hash {entry_hash(entry)}"}


def pending_signoffs(store: EvidenceStore, *, today: dt.date | None = None,
                     override: Path | None = None) -> list[dict[str, Any]]:
    """Usable registry entries without a ledgered sign-off of their current hash within the release age."""
    from ..gateway.costs import RELEASE_MAX_AGE_DAYS
    day = today or dt.date.today()
    out: list[dict[str, Any]] = []
    for key, e in sorted(_registry(override).items()):
        if not isinstance(e, Mapping):
            continue
        usable = e.get("mode") in ("api", "ow") or e.get("byos_status") == "allowed"
        if not usable:
            continue
        s = current_signoff(store, key, e)
        fresh = False
        if s is not None:
            try:
                age = (day - dt.date.fromisoformat(str(s["verified_on"]))).days
                fresh = 0 <= age <= RELEASE_MAX_AGE_DAYS
            except ValueError:
                fresh = False
        if fresh:
            continue
        h = entry_hash(e)
        out.append({"kind": "provider_signoff", "key": key, "provider": e.get("provider"), "mode": e.get("mode"),
                    "last_signoff": s, "binds": {"entry_hash": h},
                    "decide": f"chh sign-off-provider {key} --by NAME --entry-hash {h}"})
    return out
