"""`chh lit-search`, `chh cite-check`, `chh lit-freeze` (GOAL §5 L4.3). JSON on stdout.

The network is used only with an explicit --live (answers are recorded into the snapshot store under
$CHH_DATA_DIR/literature); otherwise --snapshot <id> answers offline from a frozen snapshot, as of its date T or
an earlier --as-of.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..config import data_dir
from .cite import Citation, check_citations, i3_verdict
from .client import LiteratureClient, live_client, offline_client
from .http import LiteratureError
from .snapshot import SnapshotStore


def add_parsers(sub: Any) -> None:
    ls = sub.add_parser("lit-search", help="search arXiv / Semantic Scholar / OpenAlex / Crossref (JSON)")
    ls.add_argument("query", help="';', newlines or ' OR ' split it into subqueries")
    ls.add_argument("--limit", type=int, default=10)
    ls.add_argument("--source", action="append", choices=["openalex", "crossref", "semanticscholar", "arxiv"],
                    help="sources in fallback order (default: openalex, crossref, semanticscholar, arxiv)")
    _mode_args(ls)
    cc = sub.add_parser("cite-check", help="check citations (JSON list) -> audit I3 verdict; exit 1 unless I3 passes")
    cc.add_argument("file", help='JSON list of {"key", "id", "title", "year", "authors": [...], "quotes": [...]}')
    _mode_args(cc)
    fz = sub.add_parser("lit-freeze", help="freeze every recorded literature answer as the snapshot 'as of T'")
    fz.add_argument("--as-of", required=True, type=date.fromisoformat)
    fz.add_argument("--label", default="")


def _mode_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--snapshot", help="frozen snapshot id: offline, reproducible")
    p.add_argument("--live", action="store_true", help="use the network (answers are recorded for later freezing)")
    p.add_argument("--no-cache", action="store_true", help="with --live: re-fetch even if an answer is recorded")
    p.add_argument("--as-of", type=date.fromisoformat, help="hide papers published after this date (YYYY-MM-DD)")


def _store() -> SnapshotStore:
    return SnapshotStore(data_dir() / "literature")


def _client(a: argparse.Namespace) -> LiteratureClient:
    if a.snapshot and a.live:
        raise SystemExit("choose --snapshot (offline) or --live (network), not both")
    try:
        if a.snapshot:
            return offline_client(_store(), a.snapshot, as_of=a.as_of)
        if a.live:
            return live_client(_store(), use_cache=not a.no_cache, as_of=a.as_of)
    except (LiteratureError, ValueError) as e:
        raise SystemExit(f"error: {e}") from e
    raise SystemExit("no network access without --live; pass --snapshot <id> to answer offline")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def cmd_lit_search(a: argparse.Namespace, _store_unused: Any = None) -> int:
    client = _client(a)
    try:
        res = client.search(a.query, limit=a.limit, sources=a.source)
    except ValueError as e:
        raise SystemExit(f"error: {e}") from e
    _print({**res.model_dump(), "snapshot": a.snapshot})
    return 0 if res.complete else 1


def cmd_cite_check(a: argparse.Namespace, _store_unused: Any = None) -> int:
    client = _client(a)
    try:
        raw = json.loads(Path(a.file).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("the citation file must hold a JSON list")
        cites = [Citation.model_validate(c) for c in raw]
    except (OSError, ValueError, ValidationError) as e:
        raise SystemExit(f"error: bad citation file: {e}") from e
    verdict = i3_verdict(check_citations(cites, client))
    _print({**verdict, "snapshot": a.snapshot, "as_of": client.as_of.isoformat() if client.as_of else None})
    return 0 if verdict["I3"] in (True, "n/a") else 1


def cmd_lit_freeze(a: argparse.Namespace, _store_unused: Any = None) -> int:
    print(_store().freeze(a.as_of, label=a.label))
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "lit-search": cmd_lit_search, "cite-check": cmd_cite_check, "lit-freeze": cmd_lit_freeze}
