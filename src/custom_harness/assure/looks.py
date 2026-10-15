"""The look ledger of the verifier's sealed validation halves (GOAL §16.6, invariant 20).

Every look at a sealed half is appended to a hash-chained JSONL file BEFORE the items are used (who, why, which
items, the git HEAD), and its result after. Each line carries the hash of the previous line, so an edited or deleted
look breaks the chain (`check_chain`). Code that reads a sealed half takes a `Look` receipt and calls `require` on it,
which fails closed unless the receipt's line is in an intact ledger and names the items being read.

Honest limit (owner decision 2026-10-08: sealed halves are ACCOUNTING ONLY): the ledger records looks made through
this code. It cannot stop an agent from reading the sealed files directly; they are ordinary files on this machine.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SALT = "chh-validation-split-2026-10-07"


class LookError(RuntimeError):
    """A sealed half was about to be read without an intact, matching ledger record."""


def half_of(set_name: str, item: str) -> str:
    """'dev' or 'sealed': a pure function of the set name, the item id and a fixed salt (never changes)."""
    return "sealed" if hashlib.sha256(f"{SALT}|{set_name}|{item}".encode()).digest()[0] & 1 else "dev"


def line_hash(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def check_chain(path: Path, label: str | None = None) -> list[str]:
    """Problems with the ledger (empty = intact). A missing file is an empty, intact ledger."""
    name = label or path.name
    problems, prev = [], "genesis"
    for i, line in enumerate(_lines(path), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{name}:{i}: not JSON")
            break
        if not isinstance(rec, dict):
            problems.append(f"{name}:{i}: not a JSON object")
            break
        if rec.get("prev") != prev:
            problems.append(f"{name}:{i}: chain broken (an earlier look was edited or deleted)")
        if rec.get("kind") == "look" and not (str(rec.get("by", "")).strip() and str(rec.get("reason", "")).strip()):
            problems.append(f"{name}:{i}: a look must name who looked and why")
        prev = line_hash(line)
    return problems


def append(path: Path, rec: dict[str, Any]) -> str:
    """Append one record (chained to the last line); returns the new line's hash."""
    lines = _lines(path)
    rec = {**rec, "prev": line_hash(lines[-1]) if lines else "genesis"}
    line = json.dumps(rec, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(line + "\n")
    return line_hash(line)


@dataclass(frozen=True)
class Look:
    """Receipt of one ledgered look: the ledger file, the set, the items and the hash of the look's line."""

    path: Path
    set_name: str
    items: tuple[str, ...]
    line: str  # hash of the look line


def open_look(path: Path, set_name: str, items: list[str] | tuple[str, ...], by: str, reason: str,
              head: str = "", extra: dict[str, Any] | None = None) -> Look:
    """Record a look BEFORE the sealed items are used. Refuses an anonymous look or a broken ledger."""
    if not by.strip() or not reason.strip():
        raise LookError("a look at a sealed half must name who looks (--by) and why (--reason)")
    problems = check_chain(path)
    if problems:
        raise LookError(f"refusing to look: the look ledger is not intact: {problems[:3]}")
    rec = {"kind": "look", "set": set_name, "items": sorted(items), "by": by, "reason": reason, "head": head,
           "at": dt.datetime.now(dt.UTC).isoformat(), **(extra or {})}
    return Look(path, set_name, tuple(sorted(items)), append(path, rec))


def require(look: Look, items: list[str] | tuple[str, ...] | None = None) -> None:
    """Fail closed unless `look` is a recorded look in an intact ledger covering `items` (default: its own)."""
    if not isinstance(look, Look):
        raise LookError("no look receipt: every read of a sealed half goes through the look ledger")
    problems = check_chain(look.path)
    if problems:
        raise LookError(f"the look ledger is not intact: {problems[:3]}")
    for line in _lines(look.path):
        if line_hash(line) == look.line:
            rec = json.loads(line)
            wanted = set(items if items is not None else look.items)
            if rec.get("kind") != "look" or rec.get("set") != look.set_name or not wanted <= set(rec.get("items", [])):
                raise LookError("the look record does not cover the items being read")
            return
    raise LookError("the look is not in the ledger (unledgered read of a sealed half)")


def record_result(look: Look, **result: Any) -> str:
    require(look)
    return append(look.path, {"kind": "result", "set": look.set_name, "look": look.line, **result})
