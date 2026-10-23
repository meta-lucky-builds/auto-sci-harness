"""`chh court ...`: the Court side of a Lab promotion (court/minted.py, court/levels.py, court/replicate.py).
JSON on stdout.

  chh court register PROMOTION --family F --beacon-round R [--dev-pairs a,b,c,d] [--n-test N] [--anchor FILE]
  chh court mint CASE --beacon VALUE
  chh court run CASE --docker                      (--dev-sandbox: trusted test code only)
  chh court register ... --revalidation-round R2   both beacon rounds fixed at registration (RE-VALIDATED)
  chh court revalidate CASE --beacon-round R2      declare the second fresh-data test (before the first verdict)
  chh court replicate CASE --replicator KIND --beacon-round R
  chh court level CERT_HASH | CASE                 the verification badges, computed by code (never a model)
  chh court status CASE
  chh court families                               which claim families can be minted (and which cannot)

Built, NOT test-verified.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from ..store import EvidenceStore

_HEX64 = re.compile(r"[0-9a-f]{64}")


def add_parsers(sub: Any) -> None:
    c = sub.add_parser("court", help="the Court side of Lab promotions: minted route, levels, replication")
    s = c.add_subparsers(dest="court_cmd", required=True)
    r = s.add_parser("register", help="HUMAN: pre-register a frozen promotion as a minted Court case")
    r.add_argument("promotion")
    r.add_argument("--family", required=True, help="a synthetic generator family (`chh court families`)")
    r.add_argument("--beacon-round", required=True, help="the public beacon round committed NOW (before its value)")
    r.add_argument("--dev-pairs", help="public dev outcome counts both,only_method,only_baseline,neither (sizing)")
    r.add_argument("--n-test", type=int, help="held-out rows (checked by the preflight; default: sized by it)")
    r.add_argument("--n-train", type=int, help="minted training rows (default MINT_TRAIN_ROWS)")
    r.add_argument("--anchor", help="anchor file outside the store (default <data>/anchors/court.jsonl)")
    r.add_argument("--revalidation-round", help="a SECOND beacon round, fixed now, for the re-validation test")
    rv = s.add_parser("revalidate", help="declare the second fresh-data test of a case (before its first verdict)")
    rv.add_argument("case")
    rv.add_argument("--beacon-round", required=True, help="a beacon round other than the first, committed now")
    rv.add_argument("--anchor")
    m = s.add_parser("mint", help="kerneld mints the case's held-out target (after the freeze)")
    m.add_argument("case")
    m.add_argument("--beacon", required=True, help="the public value of the committed beacon round")
    ru = s.add_parser("run", help="re-execute both frozen arms offline; the Kernel certifies")
    ru.add_argument("case")
    ru.add_argument("--docker", action="store_true", help="the network-free candidate sandbox (required for real use)")
    ru.add_argument("--dev-sandbox", action="store_true", help="UNISOLATED: trusted test code only")
    rp = s.add_parser("replicate", help="independent re-implementation from the claim text (invariant 5)")
    rp.add_argument("case")
    rp.add_argument("--replicator", required=True, help="backend kind, as `chh run --role` (codex[:model] | "
                                                        "openai:<model> | ollama:<model> | mock)")
    rp.add_argument("--beacon-round", required=True)
    rp.add_argument("--dev-pairs")
    rp.add_argument("--n-test", type=int)
    lv = s.add_parser("level", help="the verification badges of a certificate or a case (code only)")
    lv.add_argument("ref", help="a certificate hash or a case id")
    st = s.add_parser("status", help="a case: registration, mint, outcome")
    st.add_argument("case")
    s.add_parser("families", help="claim families that can be minted, and those that cannot")


def _pairs(v: str | None) -> tuple[int, int, int, int] | None:
    if v is None:
        return None
    parts = [int(x) for x in v.split(",")]
    if len(parts) != 4 or any(p < 0 for p in parts):
        raise SystemExit("--dev-pairs needs four non-negative counts: both,only_method,only_baseline,neither")
    return parts[0], parts[1], parts[2], parts[3]


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=1, sort_keys=True, default=str))


def cmd_court(a: argparse.Namespace, store: EvidenceStore) -> int:
    from ..policy import PolicyError
    from . import levels as L
    from . import minted as C
    from .replicate import ReplicationRefused
    try:
        if a.court_cmd == "register":
            _print(C.register_promotion(store, a.promotion, a.family, beacon_round=a.beacon_round,
                                        dev_pairs=_pairs(a.dev_pairs), n_test=a.n_test, n_train=a.n_train,
                                        anchor_file=Path(a.anchor) if a.anchor else None,
                                        revalidation_round=a.revalidation_round))
        elif a.court_cmd == "revalidate":
            _print(C.declare_revalidation(store, a.case, a.beacon_round,
                                          anchor_file=Path(a.anchor) if a.anchor else None))
        elif a.court_cmd == "mint":
            _print(C.mint(store, a.case, a.beacon))
        elif a.court_cmd == "run":
            from ..audit.cli import _factory
            from ..config import data_dir
            out = C.run(store, a.case, _factory(a, data_dir() / "sandboxes" / f"court-{a.case}"))
            lvl = L.level_of_case(store, a.case)
            _print({**out, "level": lvl.as_dict() if isinstance(lvl, L.LevelResult) else lvl})
            return 0 if out.get("cert_hash") else 1
        elif a.court_cmd == "replicate":
            from ..cli import _backend  # local: the root CLI imports this module
            from .replicate import replicate
            _print(replicate(store, a.case, _backend(a.replicator), beacon_round=a.beacon_round,
                             dev_pairs=_pairs(a.dev_pairs), n_test=a.n_test))
        elif a.court_cmd == "level":
            res = L.level(store, a.ref) if _HEX64.fullmatch(a.ref) else L.level_of_case(store, a.ref)
            _print(res.as_dict() if isinstance(res, L.LevelResult) else res)
        elif a.court_cmd == "status":
            _print(C.summary(store, a.case))
        elif a.court_cmd == "families":
            from ..kerneld.mint import mintable_families
            from ..research.suite import FAMILIES
            _print({"mintable": {f: FAMILIES[f].metric for f in mintable_families()},
                    "not_mintable": {"public datasets (OpenML, downloads)": "retrospective Claim Audit route "
                                     "(`chh lab handoff`), capped below VALIDATED",
                                     "simulated / formal / computational / physical claims": "no generator here; "
                                     "physical claims get FUTURE data only through `chh return-data`"}})
    except (C.CourtRefused, ReplicationRefused, PolicyError) as e:
        raise SystemExit(f"refused: {e}") from None
    return 0


COMMANDS = {"court": cmd_court}
