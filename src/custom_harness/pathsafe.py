"""Path containment that survives Windows path aliases: the ONE helper every containment check uses.

A containment check ("is this workspace inside a protected directory?", "is this file inside an agent-readable
root?") that compares resolved paths textually is fooled by another spelling of the same place:

- device-path prefixes: `\\\\?\\C:\\x`, `\\\\.\\C:\\x`, `\\\\?\\UNC\\host\\share\\x` (Path.resolve keeps them);
- the local admin share: `\\\\localhost\\C$\\x`, `\\\\127.0.0.1\\C$\\x`, `\\\\<this host>\\C$\\x`;
- 8.3 short names (`LONGSE~1`) and letter case;
- trailing dots and spaces of a component (Win32 drops them: `suites.` is `suites`);
- any other alias of an existing directory (`\\\\?\\GLOBALROOT\\Device\\...`, junction targets): caught by file
  identity, `os.path.samefile`, of every existing ancestor.

`is_within(a, b)` is True when `a` is `b` or lies under it by ANY of these. Callers use `overlaps` for "neither may
contain the other" and `root_containing` for "which of these roots holds this path". The rule (from the red team of
s3-evolve, 2026-10-14, first fixed in evolve/reveal.py) is: compare canonical paths first, then file identity of
every existing ancestor, and never decide "outside" from text alone.

Limits (stated): a path none of whose ancestors exists yet is compared by text only after canonicalisation (there is
no file identity to compare); a hard-linked FILE (not directory) has no ancestor relation and is out of scope.
"""

from __future__ import annotations

import os
import re
import socket
import stat
from collections.abc import Iterable
from pathlib import Path

# Windows device-path prefixes, longest first: Path.resolve keeps them, so the same directory would look different
_PREFIXES = (("\\\\?\\UNC\\", "\\\\"), ("\\\\.\\UNC\\", "\\\\"), ("\\\\?\\", ""), ("\\\\.\\", ""))
_ADMIN_SHARE = re.compile(r"^\\\\([^\\]+)\\([A-Za-z])\$(\\.*)?$")


def _local_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1", ".", "?"}
    try:
        name = socket.gethostname()
    except OSError:  # pragma: no cover - no hostname: the names above still apply
        name = ""
    if name:
        hosts |= {name.lower(), name.split(".")[0].lower()}
    return hosts


def _strip_trailing(s: str) -> str:
    """Win32 path normalisation drops trailing dots and spaces of every component (except '.' and '..')."""
    if os.name != "nt":
        return s
    parts = re.split(r"([\\/])", s)
    return "".join(p if p in ("", ".", "..", "\\", "/") or p.endswith(":") else (p.rstrip(" .") or p)
                   for p in parts)


def canon(p: Path | str) -> Path:
    """`p` as one canonical spelling: device prefixes stripped, a local admin share mapped to its drive, trailing
    dots/spaces of components dropped (Windows), then resolved (symlinks, junctions and 8.3 names of the existing
    part expanded). Never raises for a path that does not exist."""
    s = os.fspath(p)
    if os.name == "nt":
        s = s.replace("/", "\\")
        for pre, sub in _PREFIXES:
            if s[: len(pre)].upper() == pre.upper():
                s = sub + s[len(pre):]
                break
        m = _ADMIN_SHARE.match(s)
        if m and m.group(1).lower() in _local_hosts():
            s = f"{m.group(2).upper()}:{m.group(3) or chr(92)}"
        s = _strip_trailing(s)
    try:
        return Path(s).resolve()
    except (OSError, RuntimeError):  # unresolvable (e.g. a symlink loop): the absolute text is the best we have
        return Path(os.path.abspath(s))


def _same(x: Path, y: Path) -> bool:
    try:
        return os.path.samefile(x, y)
    except (OSError, ValueError):
        return False


def is_within(a: Path | str, b: Path | str) -> bool:
    """`a` is `b` or lies under it: by canonical path (case-insensitive on Windows), then by file identity of every
    existing ancestor of `a` against `b` (aliases the canonical form cannot see)."""
    ca, cb = canon(a), canon(b)
    if ca == cb or cb in ca.parents:
        return True
    if not cb.exists():
        return False
    for x in (ca, *ca.parents):
        if x.exists() and _same(x, cb):
            return True
    # the raw spelling too: canonicalisation itself must never hide an alias that the OS resolves
    raw = Path(os.fspath(a))
    for x in (raw, *raw.parents):
        try:
            if x.exists() and _same(x, cb):
                return True
        except OSError:
            continue
    return False


def overlaps(a: Path | str, b: Path | str) -> bool:
    """`a` and `b` are the same place or one contains the other."""
    return is_within(a, b) or is_within(b, a)


def root_containing(p: Path | str, roots: Iterable[Path | str]) -> Path | None:
    """The first of `roots` that `p` is or lies under (alias-safe), else None."""
    for r in roots:
        if is_within(p, r):
            return Path(r)
    return None


_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT (Windows symlinks, junctions, mount points)
_FileId = tuple[int, int]


def _file_id(p: Path) -> _FileId | None:
    try:
        st = os.stat(p)
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino


def _plain_chain(p: Path, memo: dict[Path, tuple[_FileId, ...] | None]) -> tuple[_FileId, ...] | None:
    """File ids of `p` and all its ancestors, IF `p` is provably its own canonical form right now: every component
    exists and none is a link, junction or other reparse point, and its root is canonical (not, say, a subst drive).
    Then its spelled ancestors are its real ancestors. None otherwise. Read from the file system on every call, so a
    path whose identity changed since it was registered is seen; `memo` only shares ancestors within one call."""
    if p in memo:
        return memo[p]
    parent = p.parent
    up = () if parent == p else _plain_chain(parent, memo)
    chain: tuple[_FileId, ...] | None = None
    if up is not None and (parent != p or canon(p) == p):
        try:
            st = os.lstat(p)
        except (OSError, ValueError):
            st = None
        if st is not None and not stat.S_ISLNK(st.st_mode)                 and not getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
            chain = ((st.st_dev, st.st_ino), *up)
    memo[p] = chain
    return chain


def root_overlapping(p: Path | str, roots: Iterable[Path | str]) -> Path | None:
    """The first of `roots` that `p` is, lies under, or contains: the same answer as `overlaps(p, r)` for each root
    in turn, at one or two file-system calls per root instead of hundreds. `p` is measured once. A root that is
    provably canonical (`_plain_chain`) is compared by the file identity of it and its ancestors, which is what
    `overlaps` falls back to; any other root (missing, or a link anywhere on its path) takes `overlaps` itself."""
    cp = canon(p)
    raw = Path(os.fspath(p))
    around = {i for i in map(_file_id, (cp, *cp.parents, raw, *raw.parents)) if i is not None}
    own = {i for i in map(_file_id, (cp, raw)) if i is not None}
    memo: dict[Path, tuple[_FileId, ...] | None] = {}
    for r in roots:
        root = Path(r)
        chain = _plain_chain(root, memo)
        if chain is None:
            if overlaps(p, r):
                return root
        elif chain[0] in around or not own.isdisjoint(chain) or cp == root or root in cp.parents or cp in root.parents:
            return root
    return None
