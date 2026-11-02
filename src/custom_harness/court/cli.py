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
    rs = s.add_parser("register-sealed", help="HUMAN: pre-register a frozen promotion on a sealed part of its project")
    rs.add_argument("promotion")
    rs.add_argument("--project", required=True)
    rs.add_argument("--revalidate", action="store_true", help="fix part 2 as the second test now")
    rs.add_argument("--anchor")
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
    rt = s.add_parser("retire", help="HUMAN: retire a target; its autopsy opens and it takes no further claim")
    rt.add_argument("target")
    rt.add_argument("--by", required=True)
    re_ = s.add_parser("rehearse", help="a mock trial on Lab data: shows everything, spends no fresh truth")
    re_.add_argument("promotion")
    re_.add_argument("--data", required=True, help="Lab data (CSV), with labels")
    re_.add_argument("--label-column", required=True)
    re_.add_argument("--rows", type=int, required=True, help="rows held back as the mock test set")
    re_.add_argument("--docker", action="store_true")
    re_.add_argument("--dev-sandbox", action="store_true", help="UNISOLATED: trusted test code only")
    rd = s.add_parser("readiness", help="the power check on public inputs: UNCALIBRATED, it can only deny or warn")
    rd.add_argument("promotion")
    rd.add_argument("--dev-pairs", required=True)
    he = s.add_parser("health", help="run the real Court on planted claims; needed before any new case")
    he.add_argument("--docker", action="store_true")
    he.add_argument("--dev-sandbox", action="store_true", help="UNISOLATED: trusted test code only")
    s.add_parser("families", help="claim families that can be minted, and those that cannot")


def _pairs(v: str | None) -> tuple[int, int, int, int] | None:
    if v is None:
        return None
    parts = [int(x) for x in v.split(",")]
    if len(parts) != 4 or any(p < 0 for p in parts):
        raise SystemExit("--dev-pairs needs four non-negative counts: both,only_method,only_baseline,neither")
    return parts[0], parts[1], parts[2], parts[3]


def _viewer() -> str:
    import getpass
    return getpass.getuser()


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=1, sort_keys=True, default=str))


def cmd_court(a: argparse.Namespace, store: EvidenceStore) -> int:
    from ..policy import PolicyError
    from . import display as D
    from . import levels as L
    from . import minted as C
    from .rehearsal import RehearsalRefused
    from .replicate import ReplicationRefused
    from .sealed import SealRefused
    try:
        if a.court_cmd == "register":
            _print(C.register_promotion(store, a.promotion, a.family, beacon_round=a.beacon_round,
                                        dev_pairs=_pairs(a.dev_pairs), n_test=a.n_test, n_train=a.n_train,
                                        anchor_file=Path(a.anchor) if a.anchor else None,
                                        revalidation_round=a.revalidation_round))
        elif a.court_cmd == "register-sealed":
            _print(C.register_sealed(store, a.promotion, a.project, revalidate=a.revalidate,
                                     anchor_file=Path(a.anchor) if a.anchor else None))
        elif a.court_cmd == "revalidate":
            _print(C.declare_revalidation(store, a.case, a.beacon_round,
                                          anchor_file=Path(a.anchor) if a.anchor else None))
        elif a.court_cmd == "mint":
            _print(C.mint(store, a.case, a.beacon))
        elif a.court_cmd == "run":
            from ..audit.cli import _factory
            from ..config import data_dir
            out = C.run(store, a.case, _factory(a, data_dir() / "sandboxes" / f"court-{a.case}"))
            _print(D.open_case(store, a.case, _viewer()))
            return 0 if out.get("cert_hash") else 1
        elif a.court_cmd == "replicate":
            from ..cli import _backend  # local: the root CLI imports this module
            from .replicate import replicate
            _print(replicate(store, a.case, _backend(a.replicator), beacon_round=a.beacon_round,
                             dev_pairs=_pairs(a.dev_pairs), n_test=a.n_test))
        elif a.court_cmd == "level":
            ref = a.ref
            if _HEX64.fullmatch(ref):
                res = L.level(store, ref)
                own = C.case_of_claim(store, str(res.claim_hash))
                if own is None:  # not a Court case: no fresh target is involved
                    _print(res.as_dict())
                    return 0
                ref = str(own["case_id"])
            _print(D.open_case(store, ref, _viewer()))
        elif a.court_cmd == "status":
            _print(D.open_case(store, a.case, _viewer()))
        elif a.court_cmd == "retire":
            _print(D.retire(store, a.target, a.by))
        elif a.court_cmd == "rehearse":
            from ..audit.cli import _factory
            from ..config import data_dir
            from .rehearsal import rehearse
            _print(rehearse(store, a.promotion, Path(a.data), label_column=a.label_column, rows=a.rows,
                            sandbox_factory=lambda root: _factory(a, Path(root) / "sandboxes")))
        elif a.court_cmd == "readiness":
            from ..lab.promote import PromoteRequest, _promotion
            from .rehearsal import readiness
            req = PromoteRequest.model_validate(_promotion(store, a.promotion)[0]["request"])
            _print(readiness(req.claim, _pairs(a.dev_pairs)))
        elif a.court_cmd == "health":
            from ..audit.cli import _factory
            from .health import run_health
            body = run_health(store, lambda root: _factory(a, Path(root) / "sandboxes"))
            _print(body)
            return 0 if body["passed"] else 1
        elif a.court_cmd == "families":
            from ..kerneld.mint import mintable_families
            from ..research.suite import FAMILIES
            _print({"mintable": {f: FAMILIES[f].metric for f in mintable_families()},
                    "not_mintable": {"public datasets (OpenML, downloads)": "retrospective Claim Audit route "
                                     "(`chh lab handoff`), capped below VALIDATED",
                                     "simulated / formal / computational / physical claims": "no generator here; "
                                     "physical claims get FUTURE data only through `chh return-data`"}})
    except (C.CourtRefused, ReplicationRefused, PolicyError, D.DisplayRefused, RehearsalRefused, SealRefused) as e:
        raise SystemExit(f"refused: {e}") from None
    return 0


COMMANDS = {"court": cmd_court}
