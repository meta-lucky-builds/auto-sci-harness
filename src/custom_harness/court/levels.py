"""The verification-levels engine (GOAL §17.1): REPRODUCIBLE / REPLICATED / VALIDATED, plus the RETROSPECTIVE cap,
computed per certificate BY CODE ONLY from the Kernel's certificate, the Kernel ledger, the vault and the Court case
records. No model is called; nothing here can grant a certificate or change one.

Levels are cumulative (a ladder; §17.1 "levels are cumulative"): each level requires every requirement of the level
below it. The reported level is the highest whose requirements all hold; for every level not reached, `missing`
names exactly what is missing (an honest rejection names the failing check).

  NONE          the certificate is REJECTED, retracted or unreadable (the failing checks are named)
  REPRODUCIBLE  Kernel basis K0, K3 and the bundle: K0 and K3 PASS on the certificate (K3 re-executed the programs in
                the sandbox and every seed reproduced its score; §16.4 zero-failure replays), the certificate is not
                retracted, and a Reproducibility Bundle of it is recorded in the ledger (`bundle` entry). Stated, not
                checked: "across machines" (the Kernel re-executes on one machine; `chh reproduce` elsewhere records
                nothing yet).
  REPLICATED    + an independent re-implementation agreed: a Court replica case of this case (court/replicate.py:
                written by the replicator from the claim text alone, never the code; structural similarity below the
                refusal threshold) whose own certificate is CERTIFIED and REPRODUCIBLE, and invariant 5 MET (the
                replicator is not of the author's model family). A replica with invariant 5 NOT MET is recorded and
                shown, but does not count (invariant 5 is a hard invariant, §10).
  VALIDATED     + every Kernel check K0-K9 passed with label CERTIFIED-VALIDATED (K6 on external/future data whose role
                a human approved), and EVERY target is fresh: `minted` (a `mint` ledger entry for this claim hash and
                target, registered after the pre-registration), `future` (role FUTURE, registered after the
                pre-registration) or `sealed` (an external/future dataset that is neither: VALIDATED then rests on the
                human role approval that it was sealed or private when the Lab worked; listed as an untested
                assumption in `facts`). Data claims only.
  RETROSPECTIVE a cap, not a level: a target that is a fixed public dataset (a Claim Audit OpenML split: a networked
                Lab could have downloaded it) caps the claim at REPLICATED and labels it RETROSPECTIVE. It never
                reaches VALIDATED (§17.1). Internal held-out data (role TEST / SEALED) is neither: such a claim is
                CERTIFIED-INTERNAL at best, so it stops below VALIDATED on the label.

Built, NOT test-verified (int18, stage-a).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..kernel import KernelError
from ..kernel.certificate import CHECK_IDS, Certificate, Label
from ..kernel.vault import DatasetRole
from ..kerneld.scope import ScopedKernel
from ..store import EvidenceStore

LEVELS = ("NONE", "REPRODUCIBLE", "REPLICATED", "VALIDATED")
MEANING = {
    "NONE": "no verification level: the certificate is rejected, retracted or unreadable",
    "REPRODUCIBLE": "re-executed from the frozen bundle (pinned environment, offline) with the same result across "
                    "seeds (K0, K3, bundle); reproducible does NOT mean true",
    "REPLICATED": "an independent agent of another model family re-implemented it from the claim text alone and "
                  "agreed (a certified replica)",
    "VALIDATED": "pre-registered, then confirmed on held-out data that did not exist when the Lab worked (minted "
                 "after the freeze, or future data), with valid statistics (K0-K9): a finding",
    "RETROSPECTIVE": "tested on a fixed public dataset a networked Lab could have downloaded: capped at REPLICATED, "
                     "never VALIDATED",
}
INGEST_RECORD = "kerneld_ingest"  # kerneld/ingest.RECORD_KIND (read only)


@dataclass
class LevelResult:
    cert_hash: str
    claim_hash: str | None
    label: str | None
    level: str = "NONE"
    retrospective: bool = False
    badge: str = "NONE"
    meaning: str = MEANING["NONE"]
    missing: dict[str, list[str]] = field(default_factory=dict)  # level -> what is missing for it
    facts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _passed(cert: Certificate, ids: tuple[str, ...]) -> list[str]:
    """The checks among `ids` that did not PASS (n/a counts as not passed here)."""
    return [k for k in ids if k not in cert.checks or cert.checks[k].status != "pass"]


def classify_target(ingest_kind: str | None, minted: bool, registered_after_prereg: bool,
                    role: DatasetRole | None) -> str:
    """How a target's data came to exist relative to its claim (pure; module docstring):
    retrospective (a public-dataset ingest) | minted | future | sealed | internal."""
    external = role in (DatasetRole.EXTERNAL, DatasetRole.FUTURE)
    if ingest_kind == "openml_split":
        return "retrospective"  # a fixed public dataset a networked Lab could have downloaded
    if minted and registered_after_prereg:
        return "minted"
    if role == DatasetRole.FUTURE and registered_after_prereg:
        return "future"
    if external:
        return "sealed"  # rests on the human role approval (sealed/private when the Lab worked): untested
    return "internal"  # TEST / SEALED / DEV roles: CERTIFIED-INTERNAL at best


def ladder(missing: dict[str, list[str]]) -> str:
    """The highest level whose requirements (and every lower level's) all hold: cumulative levels (§17.1)."""
    reached = "NONE"
    for lv in LEVELS[1:]:
        if missing.get(lv):
            break
        reached = lv
    return reached


def target_freshness(store: EvidenceStore, kernel: ScopedKernel, cert: Certificate) -> dict[str, dict[str, Any]]:
    """Per target: how its data came to exist relative to the claim's pre-registration (code-only facts)."""
    prereg = cert.prereg_seq if isinstance(cert.prereg_seq, int) else None
    regs: dict[str, int] = {}
    for e in kernel.ledger.entries("register_dataset"):
        ds = e.body.get("dataset_id")
        if isinstance(ds, str):
            regs.setdefault(ds, e.seq)  # the FIRST registration
    mints = {(str(e.body.get("dataset_id")), str(e.body.get("frozen_hash"))): e.seq
             for e in kernel.ledger.entries("mint")}
    public_kinds: dict[str, str] = {}
    for r in store.records(INGEST_RECORD):
        for ds in r.get("datasets") or []:
            public_kinds.setdefault(str(ds), str(r.get("kind")))
    datasets = kernel.vault.datasets()
    out: dict[str, dict[str, Any]] = {}
    for t in cert.claim.get("generalization_targets") or []:
        rec = datasets.get(t)
        reg = regs.get(t)
        minted = (t, cert.claim_hash) in mints
        after = prereg is not None and reg is not None and reg > prereg
        ingest = public_kinds.get(t)
        role = rec.role.value if rec is not None else None
        kind = classify_target(ingest, minted, after, rec.role if rec is not None else None)
        out[t] = {"role": role, "registered_seq": reg, "prereg_seq": prereg, "registered_after_prereg": after,
                  "minted_for_this_claim": minted, "ingest_kind": ingest, "freshness": kind,
                  "role_approved": rec is not None and kernel.vault.role_approval(t) is not None}
    return out


def replication_facts(store: EvidenceStore, cert_hash: str, claim_hash: str) -> dict[str, Any]:
    """Replica cases of the Court case whose claim is `claim_hash` (court/replicate.py) and whether one counts."""
    from . import minted as C
    own = C.case_of_claim(store, claim_hash)
    if own is None:
        return {"case": None, "replicas": [], "counts": False, "why": "no Court case for this claim (replication is "
                                                                     "recorded for Court cases only)"}
    replicas = [r for r in store.records(C.RECORD_KIND)
                if r.get("event") == "registered" and r.get("role") == "replica"
                and r.get("of_case") == own["case_id"]]
    rows = []
    for r in replicas:
        o = C.latest(store, str(r["case_id"]), "outcome")
        rep_cert = (o or {}).get("cert_hash")
        # the replica's own level (_depth=1: REPRODUCIBLE at most; a replica of a replica is not followed)
        rep_level = level(store, str(rep_cert), _depth=1).level if rep_cert else "NONE"
        certified = o is not None and str(o.get("label", "")).startswith("CERTIFIED")
        inv5 = str(r.get("invariant_5", "UNKNOWN"))
        rows.append({"case_id": r["case_id"], "cert_hash": rep_cert, "label": (o or {}).get("label"),
                     "reproducible": rep_level != "NONE", "invariant_5": inv5,
                     "counts": certified and rep_level != "NONE" and inv5 == "met"})
    counts = any(x["counts"] for x in rows)
    why = "" if counts else ("no replica case" if not rows else
                             "no replica that is certified, reproducible and meets invariant 5")
    return {"case": own["case_id"], "replicas": rows, "counts": counts, "why": why}


def level(store: EvidenceStore, cert_hash: str, *, _depth: int = 0) -> LevelResult:
    """The verification level of one certificate (module docstring). Pure reading; never raises for a bad
    certificate (it is NONE with the reason)."""
    kernel = ScopedKernel(store)
    try:
        cert = kernel.certificate(cert_hash)
        status = kernel.status(cert_hash)
    except (KernelError, KeyError, ValueError, OSError) as e:
        return LevelResult(cert_hash, None, None, missing={"REPRODUCIBLE": [f"certificate unreadable: "
                                                                            f"{type(e).__name__}"]})
    res = LevelResult(cert_hash, cert.claim_hash, cert.label.value)
    missing: dict[str, list[str]] = {}
    # REPRODUCIBLE
    rep: list[str] = []
    if status == "retracted":
        rep.append("the certificate is retracted")
    if cert.label == Label.REJECTED:
        rep.append(f"the Kernel REJECTED the claim (failed: {', '.join(cert.failed()) or 'none named'})")
    rep += [f"{k} did not pass" for k in _passed(cert, ("K0", "K3"))]
    bundles = [e for e in kernel.ledger.entries("bundle") if e.body.get("cert_hash") == cert_hash]
    if not bundles:
        rep.append("no Reproducibility Bundle of this certificate is recorded (`chh bundle CERT`)")
    missing["REPRODUCIBLE"] = rep
    res.facts["bundle_hashes"] = [e.body.get("bundle_hash") for e in bundles]
    res.facts["across_machines"] = "NOT checked: the Kernel re-executed on one machine"
    # REPLICATED
    if _depth == 0:
        rf = replication_facts(store, cert_hash, cert.claim_hash)
        res.facts["replication"] = rf
        missing["REPLICATED"] = [] if rf["counts"] else [rf["why"]]
    else:
        missing["REPLICATED"] = ["not evaluated for a replica's own certificate"]
    # VALIDATED and the RETROSPECTIVE cap
    is_data = cert.claim.get("evidence_class", "data") == "data"
    fresh = target_freshness(store, kernel, cert) if is_data else {}
    res.facts["targets"] = fresh
    res.retrospective = is_data and any(f["freshness"] == "retrospective" for f in fresh.values())
    val: list[str] = []
    if not is_data:
        val.append("VALIDATED is defined for data claims on held-out data (GOAL 17.1); this is a "
                   f"{cert.claim.get('evidence_class')} claim")
    if cert.label != Label.CERTIFIED_VALIDATED:
        val.append(f"label is {cert.label.value}, not CERTIFIED-VALIDATED (K6 on human-approved external/future data)")
    val += [f"{k} did not pass" for k in _passed(cert, tuple(CHECK_IDS))]
    for t, f in fresh.items():
        if f["freshness"] not in ("minted", "future", "sealed"):
            val.append(f"target {t} is {f['freshness']}: not minted after the freeze, future or sealed external data")
        if f["freshness"] == "sealed":
            res.facts.setdefault("untested_assumptions", []).append(
                f"{t}: sealed or private when the Lab worked (the human role approval; not checked by code)")
    if res.retrospective:
        val.append("RETROSPECTIVE: a target is a fixed public dataset: capped at REPLICATED")
    missing["VALIDATED"] = val
    # the ladder
    reached = ladder(missing)
    res.level, res.missing = reached, {k: v for k, v in missing.items() if v}
    res.badge = reached + (" (RETROSPECTIVE)" if res.retrospective and reached != "NONE" else "")
    res.meaning = MEANING[reached] + (f"; {MEANING['RETROSPECTIVE']}" if res.retrospective else "")
    return res


def level_of_case(store: EvidenceStore, case_id: str) -> LevelResult | dict[str, Any]:
    from . import minted as C
    o = C.latest(store, case_id, "outcome")
    if o is None or not o.get("cert_hash"):
        return {"case_id": case_id, "level": "NONE", "why": (o or {}).get("reason") or "no certificate yet"}
    return level(store, str(o["cert_hash"]))
