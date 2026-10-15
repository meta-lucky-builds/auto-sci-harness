"""Read-only tools over an evidence export (GOAL §5 L6.2). STDLIB ONLY: this file is copied into the proposer's
sandbox and run there as a script (`python -I export_tools.py <export_dir> <call_json>`), so it may not import
anything from the package.

Three tools, none of which writes:
    list  {"path": "<dir>"}                               entries of a directory of the export
    read  {"path": "<file>", "offset": 0, "limit": N}    a slice of a file's text
    grep  {"pattern": "<literal>", "path": "<dir|file>"}  lines containing a LITERAL substring (no regex: no
                                                          catastrophic backtracking), bounded hit count

Confinement (fail closed, every call):
  - a path is a plain relative POSIX path: no absolute path, drive, backslash, NUL, '..' or empty segment;
  - no component may be a symlink, junction or other reparse point, and the resolved path must stay inside the root;
  - only files the export's manifest.json lists (plus manifest.json itself) can be read or grepped, so a file planted
    into the export directory after the audit is invisible.
The host applies a second, content-level filter to every result (proposer_agent.py), so this file is not the only
line of defence.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

TOOLS = ("list", "read", "grep")
MAX_READ_CHARS = 20000
MAX_GREP_HITS = 50
MAX_PATTERN_CHARS = 200
_REPARSE_POINT = 0x400


class ToolRefused(Exception):
    pass


def _link_like(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _parts(rel: Any) -> list[str]:
    if not isinstance(rel, str):
        raise ToolRefused("path must be a string")
    if rel in ("", "."):
        return []
    if "\\" in rel or "\x00" in rel or ":" in rel or rel.startswith("/") or rel.startswith("~"):
        raise ToolRefused(f"path {rel!r} is not a plain relative path inside the export")
    parts = rel.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ToolRefused(f"path {rel!r} is not a plain relative path inside the export")
    return parts


_PREFIXES = (("\\\\?\\UNC\\", "\\\\"), ("\\\\.\\UNC\\", "\\\\"), ("\\\\?\\", ""), ("\\\\.\\", ""))


def _canon(p: Path) -> Path:
    """A stdlib-only copy of custom_harness.pathsafe.canon's prefix rule (this file may not import the package;
    tests/test_pathsafe.py checks the two agree): device prefixes stripped, then resolved."""
    s = os.fspath(p)
    for pre, sub in _PREFIXES:
        if s[: len(pre)].upper() == pre.upper():
            s = sub + s[len(pre):]
            break
    return Path(s).resolve()


def _within(a: Path, b: Path) -> bool:
    """`a` is `b` or under it: canonical path, then file identity of every existing ancestor (pathsafe.is_within)."""
    ca, cb = _canon(a), _canon(b)
    if ca == cb or cb in ca.parents:
        return True
    for x in (ca, *ca.parents):
        try:
            if x.exists() and cb.exists() and os.path.samefile(x, cb):
                return True
        except OSError:
            continue
    return False


def resolve(root: Path, rel: Any) -> Path:
    root = Path(root)
    if _link_like(root):
        raise ToolRefused("the export root is a link")
    cur = root
    for part in _parts(rel):
        cur = cur / part
        if _link_like(cur):
            raise ToolRefused(f"refusing to follow a link at {rel!r}")
    if not _within(cur, root):  # alias-safe: \\?\ and \\.\ prefixes, 8.3 and case spellings
        raise ToolRefused(f"path {rel!r} resolves outside the export")
    return cur


def manifest_files(root: Path) -> set[str]:
    try:
        doc = json.loads((Path(root) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ToolRefused(f"export has no readable manifest: {type(e).__name__}") from e
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ToolRefused("export manifest has no file list")
    return {*files, "manifest.json"}


def _listed(root: Path, rel: str, allowed: set[str]) -> str:
    norm = "/".join(_parts(rel))
    if norm not in allowed:
        raise ToolRefused(f"{rel!r} is not a file of this export")
    return norm


def run_tool(root: Path, call: Any) -> dict[str, Any]:
    """One tool call -> {"ok": True, "result": ...} or {"ok": False, "refused": reason}. Never raises on bad
    input (the caller is a model)."""
    try:
        return {"ok": True, "result": _run(Path(root), call)}
    except ToolRefused as e:
        return {"ok": False, "refused": str(e)}


def _run(root: Path, call: Any) -> Any:
    if not isinstance(call, dict):
        raise ToolRefused("a tool call is a JSON object")
    tool = call.get("tool")
    if tool not in TOOLS:
        raise ToolRefused(f"unknown tool {tool!r}: the only tools are {list(TOOLS)} (all read-only)")
    allowed = manifest_files(root)
    path = call.get("path", "")
    if tool == "list":
        d = resolve(root, path)
        prefix = "/".join(_parts(path))
        if not d.is_dir():
            raise ToolRefused(f"{path!r} is not a directory of this export")
        out = []
        for child in sorted(d.iterdir()):
            rel = f"{prefix}/{child.name}" if prefix else child.name
            if child.is_dir() and not _link_like(child) and any(a.startswith(rel + "/") for a in allowed):
                out.append(rel + "/")
            elif rel in allowed:
                out.append(rel)
        return out
    if tool == "read":
        rel = _listed(root, path, allowed)
        f = resolve(root, rel)
        offset, limit = call.get("offset", 0), call.get("limit", MAX_READ_CHARS)
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or limit <= 0:
            raise ToolRefused("offset/limit must be non-negative integers")
        text = f.read_text(encoding="utf-8", errors="replace")
        return {"path": rel, "size": len(text), "offset": offset,
                "text": text[offset:offset + min(limit, MAX_READ_CHARS)]}
    pattern = call.get("pattern")
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_CHARS:
        raise ToolRefused(f"pattern must be a non-empty literal string of at most {MAX_PATTERN_CHARS} chars")
    prefix = "/".join(_parts(path))
    targets = sorted(a for a in allowed if not prefix or a == prefix or a.startswith(prefix + "/"))
    hits: list[dict[str, Any]] = []
    for rel in targets:
        f = resolve(root, rel)
        if not f.is_file():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines()):
            if pattern in line:
                hits.append({"path": rel, "line": i + 1, "text": line[:400]})
                if len(hits) >= MAX_GREP_HITS:
                    return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


if __name__ == "__main__":  # inside the sandbox: python -I export_tools.py <export_dir> <call_json>
    try:
        call_obj = json.loads(sys.argv[2])
    except (IndexError, ValueError):
        call_obj = None
    sys.stdout.write(json.dumps(run_tool(Path(sys.argv[1]), call_obj)))
