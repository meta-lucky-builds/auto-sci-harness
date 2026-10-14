"""`chh feature-screen` and `chh rule-feature` (GOAL §16.5 item 6). JSON on stdout.

`chh rule-feature` is a HUMAN action, like `chh approve-role`: models and harness code never run it. It refuses
unless stdin is an interactive terminal and the human re-types the feature name; the ruling is a Kernel ledger entry
(kind `feature_ruling`) that names the approver. It is the only supported way to record a feature-legitimacy ruling.
`chh feature-rulings-migrate` (HUMAN, interactive) imports the earlier interim JSONL rulings once
(docs/feature_rulings_ledger.md). `chh feature-screen --anchor FILE` also checks the ledger against anchors written
by `chh ledger-anchor`, which detects removed trailing rulings.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..store.evidence import EvidenceStore
from . import provenance as prov


def add_parsers(sub: Any) -> None:
    fs = sub.add_parser("feature-screen", help="feature-provenance screen of a task dir (JSON report + gate codes)")
    fs.add_argument("task_dir")
    fs.add_argument("--anchor", action="append", default=[],
                    help="anchor file written by ledger-anchor (repeatable); a mismatch fails the gate")
    rf = sub.add_parser("rule-feature", help="HUMAN: rule a routed feature legitimate or a leak (interactive)")
    rf.add_argument("task_dir")
    rf.add_argument("--feature", required=True)
    rf.add_argument("--ruling", required=True, choices=sorted(prov.RULINGS))
    rf.add_argument("--approver", required=True, help="the human making the ruling (recorded)")
    rf.add_argument("--note", default="")
    rf.add_argument("--anchor", action="append", default=[], help="anchor file the ledger must match (repeatable)")
    mg = sub.add_parser("feature-rulings-migrate",
                        help="HUMAN: import the interim rulings.jsonl into the Kernel ledger (interactive, once)")
    mg.add_argument("--by", required=True, help="the human running the migration (recorded)")
    mg.add_argument("--file", default=None, help="interim file (default: data_dir()/provenance/rulings.jsonl)")


def _screen(task_dir: str) -> dict[str, Any]:
    try:
        return prov.screen(Path(task_dir))
    except (prov.ProvenanceError, FileNotFoundError) as e:
        raise SystemExit(f"screen failed: {e}") from e


def cmd_feature_screen(a: argparse.Namespace, store: EvidenceStore) -> int:
    report = _screen(a.task_dir)
    codes = prov.gate(report, prov.load_rulings(store, anchors=[Path(p) for p in a.anchor]))
    print(json.dumps({"report": report, "gate": codes}, indent=2, sort_keys=True))
    return 0 if not codes else 1


def cmd_rule_feature(a: argparse.Namespace, store: EvidenceStore) -> int:
    if not sys.stdin.isatty():
        raise SystemExit("NOT recorded: rule-feature is a HUMAN action and needs an interactive terminal")
    if not a.approver.strip():
        raise SystemExit("NOT recorded: --approver must name the human making the ruling")
    report = _screen(a.task_dir)
    if a.feature not in report["routed"]:
        raise SystemExit("NOT recorded: that feature is not routed to a human by the screen")
    detail = report["features"].get(a.feature, {"duplicate_fraction": report["duplicate_fraction"]})
    print(json.dumps({"feature": a.feature, "routed_for": report["routed"][a.feature], "screen": detail,
                      "ruling": a.ruling}, indent=2, sort_keys=True))
    typed = input(f"Type the feature name to confirm the ruling '{a.ruling}': ")
    if typed.strip() != a.feature:
        raise SystemExit("NOT recorded: confirmation did not match the feature name")
    try:
        entry = prov.record_ruling(Path(a.task_dir), a.feature, a.ruling, a.approver, a.note, store=store,
                                   anchors=[Path(p) for p in a.anchor])
    except prov.ProvenanceError as e:
        raise SystemExit(f"NOT recorded: {e}") from e
    print(json.dumps({"recorded": entry["ruling"], "feature": a.feature, "approver": entry["approver"],
                      "seq": entry["seq"], "hash": entry["hash"]}))
    return 0


def cmd_feature_rulings_migrate(a: argparse.Namespace, store: EvidenceStore) -> int:
    if not sys.stdin.isatty():
        raise SystemExit("NOT migrated: feature-rulings-migrate is a HUMAN action and needs an interactive terminal")
    path = Path(a.file) if a.file else prov.rulings_path()
    interim = prov.load_interim_rulings(path)
    print(json.dumps({"file": str(path), "exists": path.exists(), "lines": len(interim.rulings),
                      "chain_ok": interim.chain_ok}, sort_keys=True))
    typed = input("Type the number of lines to confirm importing them into the Kernel ledger: ")
    if typed.strip() != str(len(interim.rulings)):
        raise SystemExit("NOT migrated: confirmation did not match the number of lines")
    try:
        out = prov.migrate_interim_rulings(store, a.by, path)
    except prov.ProvenanceError as e:
        raise SystemExit(f"NOT migrated: {e}") from e
    print(json.dumps(out, sort_keys=True))
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "feature-screen": cmd_feature_screen, "rule-feature": cmd_rule_feature,
    "feature-rulings-migrate": cmd_feature_rulings_migrate}
