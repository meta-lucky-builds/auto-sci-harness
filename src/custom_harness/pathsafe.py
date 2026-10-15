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


def root_overlapping(p: Path | str, roots: Iterable[Path | str]) -> Path | None:
    """The first of `roots` that `p` is, lies under, or contains (alias-safe), else None."""
    for r in roots:
        if overlaps(p, r):
            return Path(r)
    return None
