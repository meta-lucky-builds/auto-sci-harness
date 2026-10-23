"""HUMAN feature-provenance rulings (GOAL §16.5 item 6; verify/provenance.py owns the screen, the ruling body and
the gate).

A screened task routes suspicious features to a human, who rules each `legitimate` or `leak`. The ruling is a Kernel
ledger entry of kind `feature_ruling` (module code cannot write that kind: studio/kill.py `_ModuleLedger`).

What this layer adds (orchestrator decision 2026-10-14):
- **An anchor is required by default.** The ledger chain alone cannot show that trailing entries were removed; an
  external anchor file (`chh ledger-anchor FILE`) can. `rule_feature` refuses without at least one anchor unless
  the caller passes `allow_unanchored=True` (the CLI flag `--no-anchor`); the ruling records how many anchors were
  checked (`anchors_checked`).
- **The ruling binds the report the human saw.** The caller names the screen report hash it reviewed; the ruling is
  refused if the data, the tags or the screen changed since (`expect_report_hash` in `record_ruling`, checked
  between the screen and the append).
- **Named human, not from inside a run** (guard.require_human).

The queue: `chh feature-screen` notes each screen it runs (records kind `feature_screen`: the task directory and
the report's hashes and routed items, never data values); `pending_rulings` lists every routed item of the latest
screen of each task directory that has neither a current `legitimate` ruling nor a `leak` ruling.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..store import EvidenceStore
from ..verify import provenance as prov
from .guard import HumanActionRefused, require_human

SCREEN_RECORD = "feature_screen"


def rule_feature(store: EvidenceStore, task_dir: Path | str, feature: str, ruling: str, *, approver: str,
                 report_hash: str, anchors: Sequence[Path | str] = (), allow_unanchored: bool = False,
                 note: str = "") -> dict[str, Any]:
    """HUMAN action: rule one routed feature of `task_dir`, bound to the screen report hash the human reviewed."""
    name = require_human(approver, what="the approver")
    anchor_paths = [Path(p) for p in anchors]
    if not anchor_paths and not allow_unanchored:
        raise HumanActionRefused("a ruling needs at least one ledger anchor (write one with `chh ledger-anchor "
                                 "FILE`, then pass --anchor FILE); --no-anchor records it without one")
    if not isinstance(report_hash, str) or len(report_hash) != 64:
        raise HumanActionRefused("name the full sha256 of the screen report you reviewed (report_hash)")
    try:
        return prov.record_ruling(Path(task_dir), feature, ruling, name, note, store=store, anchors=anchor_paths,
                                  expect_report_hash=report_hash)
    except prov.ProvenanceError as e:
        raise HumanActionRefused(str(e)) from None


def note_screen(store: EvidenceStore, task_dir: Path | str, report: Mapping[str, Any]) -> None:
    """Remember a screen for the queue: hashes and routed item names only (no data values, no MI numbers)."""
    store.append_record(SCREEN_RECORD, {
        "task_dir": str(Path(task_dir).resolve()), "task_id": str(report.get("task_id", "")),
        "data_digest": report.get("data_digest"), "tags_hash": report.get("tags_hash"),
        "report_hash": report.get("report_hash"),
        "routed": {str(k): list(v) for k, v in dict(report.get("routed") or {}).items()},
        "refused": sorted(dict(report.get("refused") or {})), "at": time.time()})


def pending_rulings(store: EvidenceStore) -> list[dict[str, Any]]:
    """Routed items of the latest noted screen per task directory still waiting for a human ruling (read-only).
    The screen is not re-run here: `chh rule-feature` re-screens and refuses if the report changed."""
    latest: dict[str, dict[str, Any]] = {}
    for rec in store.records(SCREEN_RECORD):
        if isinstance(rec, dict) and isinstance(rec.get("task_dir"), str):
            latest[rec["task_dir"]] = rec
    rulings = prov.load_rulings(store).rulings
    out: list[dict[str, Any]] = []
    for task_dir, rec in sorted(latest.items()):
        for feat in sorted(rec.get("routed") or {}):
            same = [r for r in rulings if r.get("feature") == feat and r.get("data_digest") == rec["data_digest"]]
            if any(r.get("ruling") == "leak" for r in same):
                continue  # ruled a leak: the gate refuses; no human action is pending
            if any(r.get("ruling") == "legitimate" and r.get("tags_hash") == rec["tags_hash"]
                   and r.get("report_hash") == rec["report_hash"] and prov._has_human_approver(r) for r in same):
                continue
            out.append({"kind": "feature_ruling", "task_dir": task_dir, "task_id": rec.get("task_id"),
                        "feature": feat, "routed_for": rec["routed"][feat],
                        "binds": {"data_digest": rec["data_digest"], "tags_hash": rec["tags_hash"],
                                  "report_hash": rec["report_hash"]},
                        "decide": f"chh rule-feature {task_dir} --feature {feat} --ruling legitimate|leak "
                                  f"--approver NAME --report-hash {rec['report_hash']} --anchor FILE"})
    return out
