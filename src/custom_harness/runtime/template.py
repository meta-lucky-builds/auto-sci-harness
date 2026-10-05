"""Minimal, safe templating: {{dotted.path}} lookups only, no code execution."""

from __future__ import annotations

import json
import re
from typing import Any

_MISSING = object()
_PAT = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


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


def truncate(text: str, max_chars: int, head_fraction: float = 0.5) -> str:
    if len(text) <= max_chars:
        return text
    head = int(max_chars * head_fraction)
    tail = max_chars - head
    omitted = len(text) - max_chars
    return f"{text[:head]}\n...[{omitted} chars omitted]...\n{text[-tail:] if tail else ''}"


def render(template: str, state: dict[str, Any], max_chars: int = 20_000, head_fraction: float = 0.5) -> str:
    def sub(m: re.Match) -> str:
        value = lookup(state, m.group(1))
        text = value if isinstance(value, str) else json.dumps(value, default=str, indent=1)
        return truncate(text, max_chars, head_fraction)
    return _PAT.sub(sub, template)
