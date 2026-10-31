"""The one display path of Court output and the feedback split (GOAL 18.5; UPGRADES N14, N15).

Every view of a Court outcome, the owner's included, goes through `view`, which records a release before it returns.
The researcher sees how many looks remain. What is shown:

  - checks that do not depend on held-out truth: in full, with their reasons (`TRUTH_INDEPENDENT`);
  - checks that do: nothing but the verdict bit and, when the claim failed, one closed reason class (`REASON_CLASS`);
  - the full autopsy (the whole certificate, every number) only once the target is retired. It is then recorded as a
    continuous release, which marks the target adaptively exposed.

`TRUTH_INDEPENDENT` is the table the split rests on. A check is on it only if its result cannot change with the held-out
labels: K0 (the artifacts and records exist), K1 (the claim's form), K2 (ledger order), K5 (which code and metric ran),
K8 (which files were inputs). K3, K4, K6, K7 and K9 read scores or labels, so they are truth-dependent. GOAL 18.5 and
UPGRADES N14 say this table is a human decision: it is the orchestrator's proposal until the owner has reviewed it.

Each (case, viewer) pair is one release, however many times that viewer looks: the same outcome tells the same person
nothing new.
"""

from __future__ import annotations

import getpass
import time
from typing import Any

from ..kernel import alpha
from ..kernel.certificate import CHECK_IDS, Certificate
from ..kerneld.releases import Releases
from ..kerneld.scope import ScopedKernel
from ..store import EvidenceStore

VIEW_KIND = "court_view"
RETIRED_KIND = "court_retired"
TRUTH_INDEPENDENT = ("K0", "K1", "K2", "K5", "K8")
TRUTH_DEPENDENT = ("K3", "K4", "K6", "K7", "K9")
REASON_CLASS = {"K3": "NOT_REPRODUCED", "K4": "PROCEDURE_NOT_VALID", "K6": "EFFECT_NOT_SHOWN",
                "K7": "OUTCOME_TOO_PREDICTABLE", "K9": "LEAK_FOUND"}  # the index is released as a `count`
OTHER_CLASS = "FAILED_INDEPENDENT_CHECK"  # the failure is one of the checks shown in full


class DisplayRefused(RuntimeError):
    pass


def emit_outcome(store: EvidenceStore, label: str, extra: str | int | None, targets: list[str], *, channel: str,
                 label_note: str, extra_note: str, key: str) -> None:
    """The ledgered releases of one outcome: the verdict label, then at most one closed value as a `count`."""
    rel = Releases.live(store)
    rel.emit(label, targets=targets, channel=channel, alphabet="verdict_bit", note=label_note, key=key)
    if extra is not None:
        rel.emit(extra, targets=targets, channel=channel, alphabet="count", note=extra_note, key=key)


def retired(store: EvidenceStore, target: str) -> bool:
    if store.records_where(RETIRED_KIND, "target", target):
        return True
    ledger = ScopedKernel(store).ledger
    return any(alpha.retired(ledger, a) for a in alpha.accounts(ledger, {target}))


def retire(store: EvidenceStore, target: str, by: str) -> dict[str, Any]:
    """HUMAN: retire a target: no further claim may use it, and its full autopsy may be read."""
    if not by.strip():
        raise DisplayRefused("name the human retiring the target")
    body = {"target": target, "by": by, "at": time.time()}
    store.append_record(RETIRED_KIND, body)
    return body


def looks_remaining(store: EvidenceStore, case: dict[str, Any]) -> int:
    """Looks left on the case's target: an alpha-account's free slots, else one per claim until a label is read."""
    kernel = ScopedKernel(store)
    target = str(case["target"])
    if retired(store, target):
        return 0
    accounts = alpha.accounts(kernel.ledger, {target})
    if accounts:
        return accounts[0].slots - alpha.charged(kernel.ledger, accounts[0])
    read = any(a.body.get("claim_hash") == case["claim_hash"] and a.body.get("purpose") == "evaluate"
               for a in kernel.vault.accesses())
    return 0 if read else 1


def split_checks(cert: Certificate) -> tuple[dict[str, Any], str | None]:
    """(the checks shown in full, the reason class). `None` as reason class means nothing failed."""
    shown = {k: {"status": cert.checks[k].status, "reasons": list(cert.checks[k].reasons)}
             for k in TRUTH_INDEPENDENT}
    failed = cert.failed()
    if not failed:
        return shown, None
    return shown, next((REASON_CLASS[k] for k in TRUTH_DEPENDENT if k in failed), OTHER_CLASS)


def _first_view(store: EvidenceStore, case_id: str, viewer: str, kind: str) -> bool:
    rows = store.records_where(VIEW_KIND, "case_id", case_id)
    return not any(r.get("viewer") == viewer and r.get("kind") == kind for r in rows)


def record_report_view(store: EvidenceStore, audit_id: str, targets: list[str]) -> None:
    """A Claim Audit report states effects and intervals on public data (RETROSPECTIVE by construction). It stays a
    full report, and showing it counts as what it is: a continuous release on its targets."""
    viewer = getpass.getuser()
    if _first_view(store, audit_id, viewer, "report"):
        Releases.live(store).emit(1.0, targets=targets, channel="report", alphabet="continuous",
                                  note="court:audit_report", key=f"report:{audit_id}:{viewer}")
        store.append_record(VIEW_KIND, {"case_id": audit_id, "viewer": viewer, "kind": "report", "at": time.time()})


def view(store: EvidenceStore, case_id: str, viewer: str) -> dict[str, Any]:
    """What `viewer` may see of a Court case, after recording it (module docstring)."""
    from .levels import level
    from .minted import case, latest
    if not viewer.strip():
        raise DisplayRefused("every view names its viewer")
    c = case(store, case_id)
    target = str(c["target"])
    out: dict[str, Any] = {"case_id": case_id, "role": c.get("role"), "route": c.get("route", "minted"),
                           "target": target, "looks_remaining": looks_remaining(store, c), "retired": False}
    o = latest(store, case_id, "outcome")
    if o is None:
        return {**out, "state": "no outcome yet"}
    if not o.get("cert_hash"):
        reason = o.get("reason") if not o.get("labels_read") else "a failure after a held-out label was read"
        return {**out, "state": "COULD NOT RUN", "reason": reason, "labels_read": bool(o.get("labels_read"))}
    cert = ScopedKernel(store).certificate(str(o["cert_hash"]))
    shown, reason_class = split_checks(cert)
    if _first_view(store, case_id, viewer, "verdict"):
        extra = None if reason_class is None else [*REASON_CLASS.values(), OTHER_CLASS].index(reason_class)
        emit_outcome(store, cert.label.value, extra, [target], channel="report", label_note="court:view",
                     extra_note="court:view_reason", key=f"view:{case_id}:{viewer}")
        store.append_record(VIEW_KIND, {"case_id": case_id, "viewer": viewer, "kind": "verdict", "at": time.time()})
    out.update(state="DECIDED", label=cert.label.value, badge=level(store, str(o["cert_hash"])).badge,
               checks=shown, reason_class=reason_class, truth_dependent="verdict and reason class only; the autopsy "
               "opens when the target is retired")
    if retired(store, target):
        if _first_view(store, case_id, viewer, "autopsy"):
            Releases.live(store).emit(1.0, targets=[target], channel="report", alphabet="continuous",
                                      note="court:autopsy", key=f"autopsy:{case_id}:{viewer}")
            store.append_record(VIEW_KIND, {"case_id": case_id, "viewer": viewer, "kind": "autopsy",
                                            "at": time.time()})
        out.update(retired=True, autopsy=cert.model_dump(mode="json"), exposed=True)
    return out


__all__ = ["CHECK_IDS", "DisplayRefused", "emit_outcome", "looks_remaining", "record_report_view", "retire", "retired",
           "split_checks", "view"]
