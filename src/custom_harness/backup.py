"""Backup, restore and safe upgrades of the evidence store (docs/user_guide.md).

A backup holds everything the store holds, the sealed answers included: without them a restored store could not run a
trial. So each step here is a person's, refused for an agent's name or from inside a run, and a backup may not be
written where a Lab can read it. A backup is a copy: this module never changes a ledger row.

The store's format number is SQLite's `user_version`. An upgrade makes a verified backup first, then runs each step in
order, one transaction each; a store newer than this code is refused, never touched.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .human.guard import HumanActionRefused, require_human
from .kernel.ledger import Entry, verify_chain
from .lab.walls import lab_root, overlaps
from .store import EvidenceStore

FORMAT = "chh-backup/1"
MANIFEST = "backup-manifest.json"
DB = "evidence.sqlite"
SKIP = frozenset({DB, f"{DB}-wal", f"{DB}-shm", f"{DB}-journal"})
STORE_FORMAT = 1


class BackupRefused(RuntimeError):
    pass


def _by(name: str) -> str:
    try:
        return require_human(name, what="the person taking this step")
    except HumanActionRefused as e:
        raise BackupRefused(str(e)) from None


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != MANIFEST}


def _safe_place(path: Path, store_root: Path | None = None) -> None:
    if overlaps(path, lab_root()):
        raise BackupRefused("A backup holds the sealed answers: it cannot go where a Lab can read it. Pick a folder "
                            "outside the Lab folder.")
    if store_root is not None and overlaps(path, store_root):
        raise BackupRefused("A backup cannot go inside the store it copies. Pick another folder.")


def _copy_db(src: Path, dst: Path) -> None:
    """SQLite's own backup: a consistent copy, even while another process writes."""
    a, b = _readonly(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def _readonly(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)


def _ledger_problems(db: Path) -> tuple[list[str], int]:
    """The whole chain, from the rows themselves, read-only: opening a store here would be a second store route."""
    con = _readonly(db)
    try:
        rows = con.execute("SELECT seq, kind, body, prev, hash, at FROM ledger ORDER BY seq").fetchall()
    finally:
        con.close()
    try:
        entries = [Entry.from_row(r) for r in rows]
    except ValueError as e:
        return [f"unreadable ledger row: {type(e).__name__}"], len(rows)
    return verify_chain(entries, full=True), len(entries)


def backup(store: EvidenceStore, out_dir: Path, by: str) -> dict[str, Any]:
    """HUMAN: a verified copy of the whole store in a new folder under `out_dir`."""
    name = _by(by)
    root = Path(store.root)
    _safe_place(Path(out_dir), root)
    dest = Path(out_dir) / f"chh-backup-{time.strftime('%Y%m%dT%H%M%S')}"
    if dest.exists():
        raise BackupRefused(f"{dest} already exists")
    dest.mkdir(parents=True)
    _copy_db(root / DB, dest / DB)  # first: blobs only grow, so everything the copied ledger names is copied after it
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and rel.as_posix() not in SKIP:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest / rel)
    files = _files(dest)
    manifest: dict[str, Any] = {"format": FORMAT, "by": name, "at": time.time(), "store_format": store_format(root),
                                "files": files}
    problems, n = _ledger_problems(dest / DB)
    if problems:
        raise BackupRefused(f"The ledger failed its check while backing up, so this is no backup: {problems[:3]}")
    manifest["ledger_entries"] = n
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    return {"path": str(dest), "files": len(files), "ledger_entries": n,
            "note": "This backup holds the sealed answers. Keep it where no Lab and no agent can read it."}


def verify(path: Path) -> dict[str, Any]:
    """Every file's hash and the ledger's chain. Reads only."""
    path = Path(path)
    if not (path / MANIFEST).is_file():
        raise BackupRefused(f"{path} is not a backup: it has no {MANIFEST}")
    doc = json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    if doc.get("format") != FORMAT:
        raise BackupRefused(f"{path}: unknown backup format {doc.get('format')!r}")
    now, then = _files(path), doc["files"]
    bad = sorted(k for k in set(now) | set(then) if now.get(k) != then.get(k))
    if bad:
        raise BackupRefused(f"{len(bad)} file(s) changed or are missing, e.g. {bad[:3]}: do not restore this backup")
    problems, n = _ledger_problems(path / DB)
    if problems or n != doc["ledger_entries"]:
        raise BackupRefused(f"The ledger in this backup fails its check: {problems[:3]}")
    return {"path": str(path), "files": len(now), "ledger_entries": n, "ok": True}


def restore(path: Path, to: Path, by: str) -> dict[str, Any]:
    """HUMAN: copy a verified backup into an empty or new folder. It never writes over a store."""
    name = _by(by)
    checked = verify(path)
    to = Path(to)
    _safe_place(to)
    if to.exists() and any(to.iterdir()):
        raise BackupRefused(f"{to} is not empty. A restore never writes over a store.")
    shutil.copytree(path, to, dirs_exist_ok=True, ignore=shutil.ignore_patterns(MANIFEST))
    return {**checked, "restored_to": str(to), "by": name,
            "note": f"Set CHH_DATA_DIR so that {to} is the store folder, then run `chh exp doctor`."}


# ---------------------------------------------------------------- safe upgrades of stored data
def store_format(root: Path) -> int:
    con = _readonly(Path(root) / DB)
    try:
        return int(con.execute("PRAGMA user_version").fetchone()[0])
    finally:
        con.close()


def _mark_format(db: sqlite3.Connection) -> None:
    """Format 1: the format the store has had so far, now written down. No row changes."""


# format number -> what moves a store to it. Steps may add tables or indexes; they never rewrite a ledger row.
STEPS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _mark_format}


def upgrade_status(root: Path) -> str:
    """`current`, `older` or `newer`."""
    have = store_format(root)
    return "current" if have == STORE_FORMAT else "older" if have < STORE_FORMAT else "newer"


def upgrade(store: EvidenceStore, backup_to: Path, by: str) -> dict[str, Any]:
    """HUMAN: back up, verify, then step the store up to this code's format. Safe to run twice."""
    name = _by(by)
    root = Path(store.root)
    have = store_format(root)
    if have > STORE_FORMAT:
        raise BackupRefused(f"This store is format {have}, newer than this code ({STORE_FORMAT}). Update the code; "
                            "nothing was changed.")
    if have == STORE_FORMAT:
        return {"from": have, "to": have, "backup": None, "note": "Already current."}
    saved = backup(store, backup_to, name)
    verify(Path(saved["path"]))
    db = sqlite3.connect(root / DB, timeout=30)
    try:
        for step in range(have + 1, STORE_FORMAT + 1):
            with db:
                STEPS[step](db)
                db.execute(f"PRAGMA user_version = {int(step)}")
    finally:
        db.close()
    return {"from": have, "to": STORE_FORMAT, "backup": saved["path"], "note": "Upgraded. The backup was made first."}
