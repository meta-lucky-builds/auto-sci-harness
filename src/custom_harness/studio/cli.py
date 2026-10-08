"""`chh` commands of the L9 studio (CLI only): explain, graph, limitations, queue, kill, tier-policy."""

from __future__ import annotations

import argparse
import json
from typing import Any

from ..store import EvidenceStore


def cmd_kill(a: argparse.Namespace, store: EvidenceStore) -> int:
    """HUMAN action: stop a run safely (ledgered; the run ends FAILED; nothing is certified after it)."""
    from .kill import KillRefused, kill_run, killed_detail
    try:
        entry = kill_run(store, a.run_id, by=a.by, reason_code=a.reason)
    except KillRefused as e:
        raise SystemExit(f"refused: {e}") from e
    print(json.dumps({"killed": a.run_id, "ledger_seq": entry.seq, "detail": killed_detail(entry),
                      "note": "a step already in flight finishes; no further step runs and the run is not resumable"}))
    return 0


def add_parsers(sub: Any) -> None:
    from .kill import KILL_REASONS
    k = sub.add_parser("kill", help="HUMAN: stop a run safely (ends FAILED, ledgered, never resumed)")
    k.add_argument("run_id")
    k.add_argument("--by", required=True, help="the human stopping the run (recorded in the ledger)")
    k.add_argument("--reason", default="human_stop", choices=KILL_REASONS)


COMMANDS = {"kill": cmd_kill}
