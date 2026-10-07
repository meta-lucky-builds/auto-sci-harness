"""Minimal, safe templating: {{dotted.path}} and {{dotted.path|log}} lookups only, no code execution.

Context management (SoL-Pi ObservationPack, GOAL L4 "context management"):
- a value longer than its budget keeps head + tail; the FULL value is archived (blob) and the marker carries
  its handle, so nothing is silently lost and the trace can resolve it;
- `|log` renders a log through a verified-quote reducer: error-bearing lines and the tail are kept, every kept
  line is a verbatim line of the original (checked), with a raw head/tail fallback.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

_MISSING = object()
_PAT = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*(\|\s*log\s*)?\}\}")
_ERRORISH = re.compile(r"error|exception|traceback|failed|fatal|warning|assert|nan|inf\b|killed|timed out|"
                       r"File \"", re.IGNORECASE)

Archive = Callable[[str], str]  # full text -> handle (e.g. a blob hash)


def references(template: str) -> list[str]:
    """The dotted state paths a template renders (exactly the lookups `render` performs), in order."""
    return [m.group(1) for m in _PAT.finditer(template)]


def lookup(state: dict[str, Any], path: str, default: Any = _MISSING) -> Any:
    cur: Any = state
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            if default is _MISSING:
                raise KeyError(path)
            return default
    return cur


def truncate(text: str, max_chars: int, head_fraction: float = 0.5, archive: Archive | None = None) -> str:
    if len(text) <= max_chars:
        return text
    head = int(max_chars * head_fraction)
    tail = max_chars - head
    omitted = len(text) - max_chars
    where = f"; full value archived as {archive(text)[:16]}" if archive else ""
    return f"{text[:head]}\n...[{omitted} chars omitted{where}]...\n{text[-tail:] if tail else ''}"


def reduce_log(text: str, max_chars: int, archive: Archive | None = None, tail_lines: int = 15) -> str:
    """Verified-quote reducer: keep error-bearing lines (in order) plus the last `tail_lines` lines, each as a
    verbatim original line. Falls back to head/tail truncation if the reduction is still too long or empty."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    keep = sorted({i for i, ln in enumerate(lines) if _ERRORISH.search(ln)}
                  | set(range(max(0, len(lines) - tail_lines), len(lines))))
    out: list[str] = []
    prev = -1
    for i in keep:
        if i != prev + 1:
            out.append(f"...[{i - prev - 1} lines omitted]...")
        out.append(lines[i])
        prev = i
    reduced = "\n".join(out)
    original = set(lines)
    verified = all(ln in original for ln in out if not (ln.startswith("...[") and ln.endswith("omitted]...")))
    if not verified or not keep or len(reduced) > max_chars:
        return truncate(text, max_chars, 0.3, archive)
    where = f" (full log archived as {archive(text)[:16]})" if archive else ""
    return f"[log reduced to {len(out)} of {len(lines)} lines, verbatim{where}]\n{reduced}"


class PrivateDataAccess(KeyError):
    pass


def render(template: str, state: dict[str, Any], max_chars: int = 20_000, head_fraction: float = 0.5,
           archive: Archive | None = None) -> str:
    """Render {{path}} references. Paths starting with `_private` can never reach a prompt:
    hidden labels / held-out data must not leak through an evolved prompt (PLAN P2, L6 leakage)."""
    def sub(m: re.Match) -> str:
        if m.group(1).split(".")[0].startswith("_private"):
            raise PrivateDataAccess(f"template referenced private state '{m.group(1)}'")
        value = lookup(state, m.group(1))
        text = value if isinstance(value, str) else json.dumps(value, default=str, indent=1)
        if m.group(2):
            return reduce_log(text, max_chars, archive)
        return truncate(text, max_chars, head_fraction, archive)
    return _PAT.sub(sub, template)
