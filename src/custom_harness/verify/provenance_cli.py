"""`chh feature-screen` and `chh rule-feature` (GOAL §16.5 item 6). JSON on stdout.

`chh rule-feature` is a HUMAN action, like `chh approve-role`: models and harness code never run it. It refuses
unless stdin is an interactive terminal and the human re-types the feature name; the ruling entry names the
approver. It is the only supported way to record a feature-legitimacy ruling.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import provenance as prov


def add_parsers(sub: Any) -> None:
    fs = sub.add_parser("feature-screen", help="feature-provenance screen of a task dir (JSON report + gate codes)")
    fs.add_argument("task_dir")
    rf = sub.add_parser("rule-feature", help="HUMAN: rule a routed feature legitimate or a leak (interactive)")
    rf.add_argument("task_dir")
    rf.add_argument("--feature", required=True)
    rf.add_argument("--ruling", required=True, choices=sorted(prov.RULINGS))
    rf.add_argument("--approver", required=True, help="the human making the ruling (recorded)")
    rf.add_argument("--note", default="")


def _screen(task_dir: str) -> dict[str, Any]:
    try:
        return prov.screen(Path(task_dir))
    except (prov.ProvenanceError, FileNotFoundError) as e:
        raise SystemExit(f"screen failed: {e}") from e


def cmd_feature_screen(a: argparse.Namespace, _store_unused: Any = None) -> int:
    report = _screen(a.task_dir)
    codes = prov.gate(report, prov.load_rulings())
    print(json.dumps({"report": report, "gate": codes}, indent=2, sort_keys=True))
    return 0 if not codes else 1


def cmd_rule_feature(a: argparse.Namespace, _store_unused: Any = None) -> int:
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
        entry = prov.record_ruling(Path(a.task_dir), a.feature, a.ruling, a.approver, a.note)
    except prov.ProvenanceError as e:
        raise SystemExit(f"NOT recorded: {e}") from e
    print(json.dumps({"recorded": entry["ruling"], "feature": a.feature, "approver": entry["approver"],
                      "hash": entry["hash"]}))
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "feature-screen": cmd_feature_screen, "rule-feature": cmd_rule_feature}
