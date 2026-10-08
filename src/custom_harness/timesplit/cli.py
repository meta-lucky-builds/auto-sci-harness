"""`chh timesplit compare SEED.json EVOLVED.json --out F` (GOAL §6.4). JSON in, JSON out, offline.

SEED.json / EVOLVED.json are `Scorecard.model_dump_json()` outputs of `timesplit.score` for the seed and the evolved
harness on the same benchmark, resolutions, audit and date. The comparison written to --out is what
`chh scorecard build --timesplit F` reads for axis E. Building a benchmark and resolving it needs post-cutoff data and
a model budget (needs owner), so there is no CLI for those steps yet: they are library calls (docs/timesplit.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..store.evidence import EvidenceStore
from .benchmark import TimesplitError
from .scoring import Scorecard, compare


def add_parsers(sub: Any) -> None:
    ts = sub.add_parser("timesplit", help="time-split prediction benchmark (GOAL 6.4)")
    s = ts.add_subparsers(dest="timesplit_cmd", required=True)
    c = s.add_parser("compare", help="seed vs evolved scorecards -> comparison JSON (exit 1 unless 'evolved better')")
    c.add_argument("seed", type=Path)
    c.add_argument("evolved", type=Path)
    c.add_argument("--out", type=Path, required=True)


def cmd_timesplit(a: Any, store: EvidenceStore) -> int:
    try:
        seed = Scorecard.model_validate_json(Path(a.seed).read_text(encoding="utf-8"))
        evolved = Scorecard.model_validate_json(Path(a.evolved).read_text(encoding="utf-8"))
        comp = compare(seed, evolved)
    except (OSError, ValidationError, TimesplitError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 2
    text = comp.model_dump_json(indent=2)
    Path(a.out).write_text(text, encoding="utf-8", newline="")
    print(json.dumps({"out": str(a.out), "verdict": comp.verdict, "reasons": list(comp.reasons)}, indent=2))
    return 0 if comp.verdict == "evolved better" else 1


COMMANDS = {"timesplit": cmd_timesplit}
