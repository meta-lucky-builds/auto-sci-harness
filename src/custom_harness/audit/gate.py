"""HUMAN approvals of a Claim Audit and the publication gate (GOAL §16.10).

Three human steps, each a hash-chained ledger entry naming the human (kind "approval", the human-decision kind, as
`chh finalize` does; node ids `audit_fidelity`, `audit_reply`, `audit_publication`; run ids `audit:<audit id>`,
which no engine run carries, so the engine can never read them as a run's approval):

1. fidelity (`chh audit approve-fidelity`): the typed claim is a faithful reading of the paper's sentence. Bound to
   the spec hash (YAML content + both arms' code) and the claim hash. The audit does not run without it.
2. right of reply (`chh audit reply`): the authors were notified with the exact report and their reply (or that none
   came within MIN_REPLY_DAYS) is recorded. Bound to the report hash.
3. publication (`chh audit approve-publication`): a human approves that this exact report may leave the machine.
   Bound to the report hash; only valid after the reply record.

`publish` (`chh audit publish`) is the ONLY code path that writes an export package, and it refuses unless 2 and 3
exist for the CURRENT report hash and the certificate is not retracted, and (since 2026-10-09) unless the VG
reporting checklist (audit/checklist.py) is complete: it is shipped as checklist.md next to the report.
Trust boundary (stated, as for every approval here): trusted host code could append these entries, and nothing
stops a person copying local files; the ledger names who approved what (accountability, not enforcement).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..kernel import Ledger
from ..kerneld.scope import ScopedKernel
from ..runtime.engine import approval_item_hash
from ..store import EvidenceStore
from . import config

FIDELITY, REPLY, PUBLICATION = "audit_fidelity", "audit_reply", "audit_publication"


class AuditRefused(RuntimeError):
    pass


def _run_id(audit_id: str) -> str:
    return f"audit:{audit_id}"


def _human(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise AuditRefused("this is a human action: name the human (--approver / --recorded-by)")
    return name


def _entries(store: EvidenceStore, audit_id: str, node: str) -> list[Any]:
    return [e for e in Ledger(store).entries("approval") if e.intact() and e.body.get("run_id") == _run_id(audit_id)
            and e.body.get("node_id") == node and e.body.get("decision") == "approved"]


# ------------------------------------------------------------------ 1. statement fidelity
def fidelity_item(spec_hash: str, claim_hash: str, sentence: str) -> dict[str, str]:
    return {"spec_hash": spec_hash, "claim_hash": claim_hash, "paper_sentence": sentence}


def approve_fidelity(store: EvidenceStore, *, audit_id: str, spec_hash: str, claim_hash: str, sentence: str,
                     approver: str, reviewed_hash: str, note: str = "") -> int:
    """HUMAN action. `reviewed_hash` is the spec hash the human reviewed: refused if the spec changed since."""
    approver = _human(approver)
    if reviewed_hash != spec_hash:
        raise AuditRefused(f"the spec hash you reviewed ({reviewed_hash[:16]}) is not the current spec "
                           f"({spec_hash[:16]}): review the current spec")
    item = fidelity_item(spec_hash, claim_hash, sentence)
    e = Ledger(store).append("approval", {"run_id": _run_id(audit_id), "node_id": FIDELITY,
                                          "item_hash": approval_item_hash(item), "decision": "approved",
                                          "approver": approver, "note": note, "audit": item})
    return e.seq


def fidelity_approval(store: EvidenceStore, audit_id: str, spec_hash: str, claim_hash: str,
                      sentence: str) -> dict[str, Any] | None:
    want = approval_item_hash(fidelity_item(spec_hash, claim_hash, sentence))
    for e in reversed(_entries(store, audit_id, FIDELITY)):
        if e.body.get("item_hash") == want:
            return {"approver": e.body["approver"], "ledger_seq": e.seq}
    return None


# ------------------------------------------------------------------ 2. right of reply
def record_reply(store: EvidenceStore, *, audit_id: str, report_hash: str, recorded_by: str, notified: str,
                 notified_on: date, reply_text: str | None, clock: Callable[[], float] = time.time) -> int:
    """HUMAN action: the authors were sent the report with this hash on `notified_on` (who, how: `notified`).
    Either their reply text is recorded, or (reply_text None) that none came: allowed only MIN_REPLY_DAYS after."""
    recorded_by = _human(recorded_by)
    if not notified.strip():
        raise AuditRefused("say who was notified and how (e.g. 'corresponding author, email')")
    today = datetime.fromtimestamp(clock()).date()  # the human's local calendar date
    if notified_on > today:
        raise AuditRefused("the notification date is in the future")
    if reply_text is None and (today - notified_on).days < config.MIN_REPLY_DAYS:
        raise AuditRefused(f"no reply can be recorded before {config.MIN_REPLY_DAYS} days have passed since the "
                           f"notification ({notified_on.isoformat()})")
    item = {"report_hash": report_hash, "notified": notified.strip(), "notified_on": notified_on.isoformat(),
            "reply": reply_text if reply_text is not None else None,
            "reply_sha256": hashlib.sha256(reply_text.encode()).hexdigest() if reply_text is not None else None}
    e = Ledger(store).append("approval", {"run_id": _run_id(audit_id), "node_id": REPLY,
                                          "item_hash": approval_item_hash(item), "decision": "approved",
                                          "approver": recorded_by, "note": "", "audit": item})
    return e.seq


def reply_record(store: EvidenceStore, audit_id: str, report_hash: str) -> dict[str, Any] | None:
    for e in reversed(_entries(store, audit_id, REPLY)):
        item = e.body.get("audit") or {}
        if item.get("report_hash") == report_hash and e.body.get("item_hash") == approval_item_hash(item):
            return {**item, "recorded_by": e.body["approver"], "ledger_seq": e.seq}
    return None


# ------------------------------------------------------------------ 3. publication approval
def approve_publication(store: EvidenceStore, *, audit_id: str, report_hash: str, approver: str,
                        reviewed_hash: str, note: str = "") -> int:
    approver = _human(approver)
    if reviewed_hash != report_hash:
        raise AuditRefused(f"the report hash you reviewed ({reviewed_hash[:16]}) is not the current report "
                           f"({report_hash[:16]})")
    if reply_record(store, audit_id, report_hash) is None:
        raise AuditRefused("no right-of-reply record for this exact report: notify the authors first "
                           "(`chh audit reply`)")
    item = {"report_hash": report_hash}
    e = Ledger(store).append("approval", {"run_id": _run_id(audit_id), "node_id": PUBLICATION,
                                          "item_hash": approval_item_hash(item), "decision": "approved",
                                          "approver": approver, "note": note, "audit": item})
    return e.seq


def publication_approval(store: EvidenceStore, audit_id: str, report_hash: str) -> dict[str, Any] | None:
    reply = reply_record(store, audit_id, report_hash)
    if reply is None:
        return None
    want = approval_item_hash({"report_hash": report_hash})
    for e in reversed(_entries(store, audit_id, PUBLICATION)):
        if e.body.get("item_hash") == want and e.seq > reply["ledger_seq"]:
            return {"approver": e.body["approver"], "ledger_seq": e.seq}
    return None


# ------------------------------------------------------------------ the only export path
def publish(store: EvidenceStore, outcome: dict[str, Any], report: str, out_dir: Path, *,
            bundle_path: Path | None = None, redistribution: dict[str, bool] | None = None) -> dict[str, Any]:
    """Write the publication package for one audit, or refuse. `report` must be the report re-rendered now (its
    hash is what the approvals bind). The bundle (code + data) is included only if a human declared both
    redistributable in the approved spec; otherwise only its hash is."""
    audit_id = outcome["audit_id"]
    report_hash = hashlib.sha256(report.encode("utf-8")).hexdigest()
    if report_hash != outcome.get("report_hash"):
        raise AuditRefused("the report no longer renders to the hash recorded for this audit (refused)")
    reply = reply_record(store, audit_id, report_hash)
    if reply is None:
        raise AuditRefused("refused: no right-of-reply record for this report (`chh audit reply`)")
    approval = publication_approval(store, audit_id, report_hash)
    if approval is None:
        raise AuditRefused("refused: no human publication approval for this report after its right-of-reply "
                           "record (`chh audit approve-publication`)")
    cert_hash = outcome.get("cert_hash")
    if cert_hash and ScopedKernel(store).status(cert_hash) != "active":
        raise AuditRefused("refused: the certificate behind this verdict was retracted")
    # VG Table 10 (GOAL §1.3): the reporting checklist travels with the report; a missing item blocks publication
    from ..publication import blockers as checklist_blockers
    from .checklist import audit_checklist
    from .checklist import render as render_audit_checklist
    if not cert_hash:
        raise AuditRefused("refused: no certificate behind this outcome, so there is no reporting checklist to "
                           "publish with it (VG Table 10)")
    bundle_hash = hashlib.sha256(Path(bundle_path).read_bytes()).hexdigest() if bundle_path is not None else None
    checklist = audit_checklist(store, outcome, bundle_hash, reply, approval)
    blocked = checklist_blockers(checklist, is_loop=False)
    if blocked:
        raise AuditRefused("refused: reporting checklist incomplete (VG Table 10): " + "; ".join(blocked))
    checklist_md = render_audit_checklist(checklist)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report, encoding="utf-8", newline="")
    (out_dir / "checklist.md").write_text(checklist_md, encoding="utf-8", newline="")
    red = redistribution or {}
    manifest: dict[str, Any] = {"audit_id": audit_id, "verdict": outcome["verdict"], "report_sha256": report_hash,
                                "cert_hash": cert_hash, "right_of_reply": reply, "publication_approval": approval,
                                "citations": outcome.get("citations"), "checklist": checklist,
                                "checklist_sha256": hashlib.sha256(checklist_md.encode("utf-8")).hexdigest()}
    if bundle_path is not None:
        manifest["bundle_sha256"] = hashlib.sha256(Path(bundle_path).read_bytes()).hexdigest()
        if red.get("code") and red.get("data"):
            shutil.copyfile(bundle_path, out_dir / "bundle.zip")
            manifest["bundle"] = "bundle.zip"
        else:
            manifest["bundle"] = None
            manifest["bundle_withheld"] = ("code or data not declared redistributable: hash-only package "
                                           "(the bundle holds the code, the data and the hidden labels)")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=1), encoding="utf-8",
                                           newline="")
    return manifest
