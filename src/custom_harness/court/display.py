"""The one display path of Court output and the feedback split (GOAL 18.5; UPGRADES N14, N15).

Every view of a Court outcome, the owner's included, goes through `view`, which records a release before it returns.
The researcher sees how many looks remain. What is shown:

  - checks that do not depend on held-out truth: in full, with their reasons (`TRUTH_INDEPENDENT`);
  - checks that do: nothing but the verdict bit and, when the claim failed, one closed reason class (`REASON_CLASS`);
  - the full autopsy (the whole certificate, every number, the rerun command) only once the target is retired. It is
    then recorded as a continuous release, which marks the target adaptively exposed.

`TRUTH_INDEPENDENT` is the table the split rests on. A check is on it only if its result cannot change with the held-out
labels and its reasons print nothing derived from them: K0 (the artifacts and records exist), K1 (the claim's form), K5
(which code and metric ran). K3, K4, K6, K7 and K9 read scores or labels. K8 prints the score and the label count, and
K2's early-read test compares held-out file bytes (`checks.related_targets`), so both are truth-dependent too. One
known remainder: K1's p0 rests on that same byte comparison, but it is fixed in the ledger at registration, before any
look, so K1 tells nothing new after one (docs/court_rehearsal.md). GOAL 18.5 and UPGRADES N14 say this table is a human
decision: it is a proposal until the owner has reviewed it.

Each (case, viewer) pair is one release, however many times that viewer looks: the same outcome tells the same person
nothing new. Two later facts are not "the same outcome" and get their own release:

  - the badges of a case can change once its replica or re-validation case has an outcome. That outcome rests on the
    other case's target, so showing it is a `count` release on that target (`_related_targets`);
  - a COULD NOT RUN after a held-out label was read says that the run reached the labels and then failed. It is a
    verdict bit on the target (`_failure_release`).

Every surface that shows a Court case goes through this module. `view` is the display; `progress`, `public_case` and
`listed` say only where a case stands and what was fixed before any label was read; `seen`, `track_record` and
`released_scores` read back what was already released; `require_autopsy` and `autopsy` are the gates of the surfaces
that show a whole certificate (`chh explain`, `chh bundle`, the living paper). tests/test_experience_ratchet.py
keeps every other reader of a certificate on a closed list.
"""

from __future__ import annotations

import getpass
import time
from typing import Any

from ..human.guard import HumanActionRefused, require_human
from ..kernel import alpha
from ..kernel.certificate import CHECK_IDS, Certificate
from ..kerneld.releases import Releases
from ..kerneld.scope import BUNDLE_RECORD, ScopedKernel
from ..store import EvidenceStore

VIEW_KIND = "court_view"
RETIRED_KIND = "court_retired"
TRUTH_INDEPENDENT = ("K0", "K1", "K5")
TRUTH_DEPENDENT = ("K2", "K3", "K4", "K6", "K7", "K8", "K9")
REASON_CLASS = {"K2": "NOT_FIXED_FIRST", "K3": "NOT_REPRODUCED", "K4": "PROCEDURE_NOT_VALID", "K6": "EFFECT_NOT_SHOWN",
                "K7": "OUTCOME_TOO_PREDICTABLE", "K8": "RULES_OF_THE_RUN_BROKEN", "K9": "LEAK_FOUND"}
OTHER_CLASS = "FAILED_INDEPENDENT_CHECK"  # the failure is one of the checks shown in full
# The index of a class is released as a `count` and stays in the ledger, so classes are only ever added at the end.
REASON_RELEASE = ("NOT_REPRODUCED", "PROCEDURE_NOT_VALID", "EFFECT_NOT_SHOWN", "OUTCOME_TOO_PREDICTABLE", "LEAK_FOUND",
                  OTHER_CLASS, "NOT_FIXED_FIRST", "RULES_OF_THE_RUN_BROKEN")


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
    try:
        name = require_human(by, what="the human retiring the target")
    except HumanActionRefused as e:
        raise DisplayRefused(str(e)) from None
    body = {"target": target, "by": name, "at": time.time()}
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


def looks_on_target(store: EvidenceStore, target: str) -> int:
    """Looks left on a target, before any case of this claim exists: free alpha slots, else one if no case used it."""
    from .minted import RECORD_KIND
    if retired(store, target):
        return 0
    ledger = ScopedKernel(store).ledger
    accounts = alpha.accounts(ledger, {target})
    if accounts:
        return accounts[0].slots - alpha.charged(ledger, accounts[0])
    used = [r for r in store.records_where(RECORD_KIND, "target", target) if r.get("event") == "registered"]
    return 0 if used else 1


def split_checks(cert: Certificate) -> tuple[dict[str, Any], str | None]:
    """(the checks shown in full, the reason class). `None` as reason class means nothing failed."""
    shown = {k: {"status": cert.checks[k].status, "reasons": list(cert.checks[k].reasons)}
             for k in TRUTH_INDEPENDENT}
    failed = cert.failed()
    if not failed:
        return shown, None
    return shown, next((REASON_CLASS[k] for k in TRUTH_DEPENDENT if k in failed), OTHER_CLASS)


def _views(store: EvidenceStore, case_id: str, viewer: str, kind: str) -> list[dict[str, Any]]:
    rows = store.records_where(VIEW_KIND, "case_id", case_id)
    return [r for r in rows if r.get("viewer") == viewer and r.get("kind") == kind]


def _first_view(store: EvidenceStore, case_id: str, viewer: str, kind: str) -> bool:
    return not _views(store, case_id, viewer, kind)


def _related_targets(store: EvidenceStore, case_id: str) -> list[str]:
    """Targets of the case's replica and re-validation cases that have an outcome: what its badges also rest on."""
    from .minted import latest, related_cases
    rows = [*related_cases(store, case_id, "replica"), *related_cases(store, case_id, "revalidation")]
    return sorted({str(r["target"]) for r in rows if latest(store, str(r["case_id"]), "outcome") is not None})


def record_report_view(store: EvidenceStore, audit_id: str, targets: list[str], viewer: str | None = None) -> None:
    """A Claim Audit report states effects and intervals on public data (RETROSPECTIVE by construction). It stays a
    full report, and showing it counts as what it is: a continuous release on its targets."""
    viewer = viewer or getpass.getuser()
    if _first_view(store, audit_id, viewer, "report"):
        Releases.live(store).emit(1.0, targets=targets, channel="report", alphabet="continuous",
                                  note="court:audit_report", key=f"report:{audit_id}:{viewer}")
        store.append_record(VIEW_KIND, {"case_id": audit_id, "viewer": viewer, "kind": "report", "at": time.time()})


def _claim_text(store: EvidenceStore, claim_hash: str) -> dict[str, Any]:
    """The words and numbers fixed at pre-registration: public, whatever the outcome."""
    entry = next(e for e in ScopedKernel(store).ledger.entries("prereg") if e.body.get("claim_hash") == claim_hash)
    claim = entry.body["claim"]
    cond = claim["scope"]["conditions"]
    return {"title": cond.get("paper_title"), "sentence": cond.get("paper_sentence"), "statement": claim["statement"],
            "metric": claim["metric"]["id"], "min_effect": claim["effect"]["min_effect"],
            "probability": claim["effect"]["probability"], "seeds": list(claim["analysis_plan"]["seeds"])}


def public_case(store: EvidenceStore, case_id: str) -> dict[str, Any]:
    """What was fixed before any label was read. No outcome field: this is safe on any surface."""
    from .minted import case
    c = case(store, case_id)
    keys = ("case_id", "role", "route", "contract", "promotion_id", "session_id", "of_case", "claim_hash", "target",
            "family", "n_train", "n_test", "beacon_round", "project", "part", "at")
    return {**{k: c.get(k) for k in keys}, "claim": _claim_text(store, str(c["claim_hash"]))}


def listed(store: EvidenceStore) -> list[dict[str, Any]]:
    """Every registered case, oldest first, as `public_case`."""
    from .minted import RECORD_KIND
    return [public_case(store, str(r["case_id"])) for r in store.records(RECORD_KIND) if r.get("event") == "registered"]


def is_case(store: EvidenceStore, case_id: str) -> bool:
    from .minted import events
    return bool(events(store, case_id))


def progress(store: EvidenceStore, case_id: str) -> str:
    """Where a case stands, without saying anything about its outcome. It shows no looks and records no release."""
    from .minted import case, latest
    c = case(store, case_id)
    if latest(store, case_id, "outcome") is not None:
        return "outcome_recorded"
    if c.get("route") != "sealed" and latest(store, case_id, "minted") is None:
        return "needs_beacon"
    if ScopedKernel(store).vault.role_approval(str(c["target"])) is None:
        return "needs_role_approval"
    return "ready_to_run"


def seen(store: EvidenceStore, case_id: str, viewer: str) -> dict[str, Any] | None:
    """What `viewer` was last shown of the latest outcome of a case, or None. Reads back; releases nothing."""
    from .minted import latest
    o = latest(store, case_id, "outcome")
    rows = [*_views(store, case_id, viewer, "verdict"), *_views(store, case_id, viewer, "failure")]
    rows = [r for r in rows if o is not None and r.get("outcome_at") == o.get("at")]
    return max(rows, key=lambda r: float(r["at"])) if rows else None


def results_seen(store: EvidenceStore, viewer: str) -> int:
    """How many cases' outcomes this viewer was shown."""
    return len({r["case_id"] for r in store.records(VIEW_KIND) if r.get("viewer") == viewer
                and r.get("kind") in ("verdict", "failure")})


def _shown(store: EvidenceStore, case_id: str, viewer: str, kind: str, o: dict[str, Any], **body: Any) -> None:
    store.append_record(VIEW_KIND, {"case_id": case_id, "viewer": viewer, "kind": kind, "outcome_at": o.get("at"),
                                    "at": time.time(), **body})


def _failure_release(store: EvidenceStore, case_id: str, target: str, viewer: str, o: dict[str, Any]) -> None:
    """A COULD NOT RUN seen by this viewer: one verdict bit if labels were read, nothing to release otherwise."""
    if seen(store, case_id, viewer) is None:
        if o.get("labels_read"):
            Releases.live(store).emit("COULD NOT RUN", targets=[target], channel="report", alphabet="verdict_bit",
                                      note="court:view_failure", key=f"view:{case_id}:{viewer}")
        _shown(store, case_id, viewer, "failure", o, label="COULD NOT RUN", badge="NONE", reason_class=None)


def _verdict_release(store: EvidenceStore, case_id: str, target: str, viewer: str, o: dict[str, Any], label: str,
                     reason_class: str | None, badges: Any) -> None:
    """The releases of a decided outcome, once per (case, viewer) and again only when the badges changed."""
    last = seen(store, case_id, viewer)
    if last is not None and last.get("badge") == badges.badge:
        return
    if last is None:
        extra = None if reason_class is None else REASON_RELEASE.index(reason_class)
        emit_outcome(store, label, extra, [target], channel="report", label_note="court:view",
                     extra_note="court:view_reason", key=f"view:{case_id}:{viewer}")
    related = _related_targets(store, case_id)
    if related:
        Releases.live(store).emit(badges.release_value(), targets=related, channel="report", alphabet="count",
                                  note="court:view_badges", key=f"view:{case_id}:{viewer}:{badges.badge}")
    _shown(store, case_id, viewer, "verdict", o, label=label, badge=badges.badge, reason_class=reason_class)


def view(store: EvidenceStore, case_id: str, viewer: str) -> dict[str, Any]:
    """What `viewer` may see of a Court case, after recording it (module docstring)."""
    from .levels import level
    from .minted import case, latest
    if not viewer.strip():
        raise DisplayRefused("every view names its viewer")
    c = case(store, case_id)
    target = str(c["target"])
    out: dict[str, Any] = {"case_id": case_id, "role": c.get("role"), "route": c.get("route", "minted"),
                           "target": target, "looks_remaining": looks_remaining(store, c), "retired": False,
                           "claim": _claim_text(store, str(c["claim_hash"]))}
    o = latest(store, case_id, "outcome")
    if o is None:
        return {**out, "state": "no outcome yet"}
    if not o.get("cert_hash"):
        _failure_release(store, case_id, target, viewer, o)
        reason = o.get("reason") if not o.get("labels_read") else "a failure after a held-out label was read"
        return {**out, "state": "COULD NOT RUN", "reason": reason, "labels_read": bool(o.get("labels_read"))}
    cert = ScopedKernel(store).certificate(str(o["cert_hash"]))
    shown, reason_class = split_checks(cert)
    res = level(store, str(o["cert_hash"]))
    _verdict_release(store, case_id, target, viewer, o, cert.label.value, reason_class, res)
    out.update(state="DECIDED", label=cert.label.value, badge=res.badge, badges=list(res.badges),
               held=[k for k, v in res.held.items() if v], retrospective=res.retrospective,
               revalidation=res.revalidation,
               data_kind=sorted({str(f["freshness"]) for f in res.facts.get("targets", {}).values()}),
               checks=shown, reason_class=reason_class, truth_dependent="verdict and reason class only; the autopsy "
               "opens when the target is retired")
    if retired(store, target):
        if _first_view(store, case_id, viewer, "autopsy"):
            Releases.live(store).emit(1.0, targets=[target], channel="report", alphabet="continuous",
                                      note="court:autopsy", key=f"autopsy:{case_id}:{viewer}")
            store.append_record(VIEW_KIND, {"case_id": case_id, "viewer": viewer, "kind": "autopsy",
                                            "at": time.time()})
        out.update(retired=True, autopsy=cert.model_dump(mode="json"), exposed=True, cert_hash=str(o["cert_hash"]),
                   rerun=_rerun(store, str(o["cert_hash"])))
    return out


def _rerun(store: EvidenceStore, cert_hash: str) -> str | None:
    """The one command that reruns a certificate. Its bundle carries the labels, so only the autopsy names it."""
    rows = store.records_where(BUNDLE_RECORD, "cert_hash", cert_hash)
    return f"chh reproduce {rows[-1]['path']} --docker" if rows else None


def autopsy(store: EvidenceStore, case_id: str, viewer: str) -> dict[str, Any]:
    """The whole certificate of a case with a retired target (recorded by `view`). A refusal opens nothing: it records
    no view and no release."""
    from .minted import case
    target = str(case(store, case_id)["target"])
    if not retired(store, target):
        raise DisplayRefused(f"case {case_id}: the whole certificate opens when its target is retired "
                             f"(`chh court retire {target} --by NAME`, human)")
    out = view(store, case_id, viewer)
    if "autopsy" not in out:
        raise DisplayRefused(f"case {case_id} has no certificate")
    return out


def require_autopsy(store: EvidenceStore, cert_ref: str, viewer: str) -> None:
    """The gate of a surface that shows or packs a whole certificate. `cert_ref` may be a hash prefix. Certificates
    with no Court case pass (the research route has its own embargo)."""
    from .minted import case_of_claim
    for e in ScopedKernel(store).ledger.entries("certificate"):
        if str(e.body.get("cert_hash", "")).startswith(cert_ref):
            own = case_of_claim(store, str(e.body.get("claim_hash")))
            if own is not None:
                autopsy(store, str(own["case_id"]), viewer)


def view_audit(store: EvidenceStore, audit_id: str, viewer: str) -> dict[str, Any] | None:
    """A Claim Audit outcome and its full report (`record_report_view`), or None if there is no outcome yet. An audit
    is on public data, so it has no feedback split: it can never hold VALIDATED."""
    from ..audit.claim_audit import current_report, latest_outcome
    from .levels import level
    o = latest_outcome(store, audit_id)
    if o is None:
        return None
    out = {"audit_id": audit_id, "claim_hash": o["claim_hash"], "verdict": o.get("verdict"),
           "report": current_report(store, o), "badges": [], "held": [],
           "claim": _claim_text(store, str(o["claim_hash"]))}
    if o.get("cert_hash"):
        cert = ScopedKernel(store).certificate(str(o["cert_hash"]))
        record_report_view(store, audit_id, list(cert.claim.get("generalization_targets") or []), viewer)
        res = level(store, str(o["cert_hash"]))
        out.update(badges=list(res.badges), held=[k for k, v in res.held.items() if v])
    return out


def exposure(store: EvidenceStore, claim_hash: str) -> str:
    """What a surface other than `view` may say about a claim's certificate. `open`: anything (no Court case, or its
    target is retired). `label`: the label, because someone was shown it. `none`: nothing."""
    from .minted import case_of_claim
    own = case_of_claim(store, claim_hash)
    if own is None or retired(store, str(own["target"])):
        return "open"
    return "label" if store.records_where(VIEW_KIND, "case_id", own["case_id"]) else "none"


def released_scores(store: EvidenceStore, scores: list[Any]) -> list[Any]:
    """Prediction scores a report may use: a Court claim counts only once someone was shown its outcome."""
    return [s for s in scores if exposure(store, s.claim_hash) != "none"]


def track_record(store: EvidenceStore, viewer: str) -> dict[str, Any]:
    """Calibration over the Court claims whose outcome this viewer was already shown. Releases nothing."""
    from ..kernel.predictions import calibration, score_predictions, vds
    from .minted import RECORD_KIND
    mine = {str(r["claim_hash"]) for r in store.records(RECORD_KIND) if r.get("event") == "registered"
            and (seen(store, str(r["case_id"]), viewer) or {}).get("label") not in (None, "COULD NOT RUN")}
    scores = [s for s in score_predictions(ScopedKernel(store).ledger, time.time()) if s.claim_hash in mine]
    return {"evidence_bits": vds(scores), **calibration(scores)}


__all__ = ["CHECK_IDS", "DisplayRefused", "autopsy", "emit_outcome", "exposure", "is_case", "listed", "looks_on_target",
           "looks_remaining", "progress", "public_case", "record_report_view", "released_scores", "require_autopsy",
           "results_seen", "retire", "retired", "seen", "split_checks", "track_record", "view", "view_audit"]
