"""`chh scorecard build` and `chh scorecard check` (GOAL §2.4: one command reproduces the scorecard). JSON on stdout."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..store.evidence import EvidenceStore
from .bundle import build_scorecard, check_scorecard
from .registry import DEFAULT_BENCHMARKS, DEFAULT_CAPABILITIES


def add_parsers(sub: Any) -> None:
    sc = sub.add_parser("scorecard", help="build / check the GOAL §2 scorecard bundle of this store's campaign")
    s = sc.add_subparsers(dest="scorecard_cmd", required=True)
    b = s.add_parser("build", help="compute the scorecard from the store and write the bundle directory")
    b.add_argument("--out", required=True, help="bundle directory to write")
    b.add_argument("--museum-log", type=Path, help="stdout of `python scripts/museum.py` (axis H)")
    b.add_argument("--findings", type=Path, help="red-team findings registry (redteam/findings.yaml, axis H)")
    b.add_argument("--salts", type=Path, help="JSON of a salt campaign audit (dataclasses.asdict, axis H)")
    b.add_argument("--timesplit", type=Path, help="comparison JSON from `chh timesplit compare` (axis E)")
    b.add_argument("--registry", type=Path, default=DEFAULT_BENCHMARKS, help="pinned-benchmark registry")
    b.add_argument("--capabilities", type=Path, default=DEFAULT_CAPABILITIES, help="claimed-capability registry")
    c = s.add_parser("check", help="reproduce a scorecard bundle against this store (exit 1 unless reproduced)")
    c.add_argument("bundle")


def cmd_scorecard(a: Any, store: EvidenceStore) -> int:
    if a.scorecard_cmd == "build":
        card = build_scorecard(store, Path(a.out), museum_log=a.museum_log, findings=a.findings,
                               salts_audit=a.salts, timesplit=a.timesplit, registry=a.registry,
                               capabilities=a.capabilities)
        print(json.dumps({"out": a.out, "overall": card["overall"], "losses": card["losses"],
                          "not_shown": card["not_shown"]}, indent=2))
        return 0
    rep = check_scorecard(Path(a.bundle), store)
    print(json.dumps(rep, indent=2))
    return 0 if rep["reproduced"] else 1


COMMANDS = {"scorecard": cmd_scorecard}
