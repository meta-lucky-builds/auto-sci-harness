"""The verification-badges engine (GOAL §17.1, owner decisions 2026-10-23): REPRODUCIBLE, REPLICATED, VALIDATED and
RE-VALIDATED, plus the RETROSPECTIVE cap. Computed per certificate BY CODE ONLY from the Kernel's certificate, the
Kernel ledger, the vault and the Court case records. No model is called; nothing here grants or changes a
certificate.

The badges are SEPARATE, not a ladder: a claim holds any combination. For every badge it does not hold, `missing`
names exactly what is missing (an honest rejection names the failing checks).

  REPRODUCIBLE  K0 and K3 PASS on the certificate (K3 re-executed the programs in the sandbox and every seed reproduced
                its score; §16.4 zero-failure replays), the certificate is not retracted, and a Reproducibility Bundle
                of it is recorded in the ledger (`bundle` entry). "Across machines" is NOT checked yet (the Kernel
                re-executes on one machine); the badge text says so.
  REPLICATED    a DIFFERENT AGENT re-implemented the method and its own claim was certified and is REPRODUCIBLE: a Court
                replica case of this case (court/replicate.py: a separate replicator call, outside the author's Lab
                session, given the claim text and never the code; a brief that carries code lines and a structurally
                similar replica are refused). A different model family is NOT required: the badge names the families
                ("REPLICATED (anthropic -> openai, different family)" is the stronger form; "same family" and "family
                unknown" are shown as such) and the invariant-5 status is recorded as a note, never a block.
  VALIDATED     decided ONLY by the fresh-data test: label CERTIFIED-VALIDATED (K6 on human-approved external/future
                data), every Kernel check K0-K9 passed, and every target is fresh: `minted` (a `mint` ledger entry for
                this claim hash and target, registered after the pre-registration), `future` (role FUTURE, registered
                after the pre-registration) or `sealed` (external/future data that is neither: it rests on the human
                role approval that it was sealed when the Lab worked; listed as an untested assumption) or
                `sealed-at-start` (court/sealed.py: the ledger shows the seal before every session of the project; a
                seal that comes after a session is `seal-after-lab` and never fresh). It does NOT require REPLICATED
                or REPRODUCIBLE. Data claims only.
  RE-VALIDATED  the claim passes the fresh-data test a SECOND time, on a second, independent fresh dataset, with the
                claim and the frozen code unchanged:
                  - a re-validation case (`court_case`, role `revalidation`, `of_case` = this case) DECLARED (pre-
                    registered) before any held-out label of the first test was read: its pre-registration sits in
                    the ledger before the first `evaluate` read of this claim (court/minted.declare_revalidation also
                    refuses later declarations; one per case);
                  - the same claim core (`claim_core`: kind, class, subject and comparator code hashes, metric, effect,
                    analysis plan, risk, assumptions, wording, budget, wrapper and protocol except the beacon round);
                  - independent data: its targets are disjoint from this claim's and not related to them (no shared
                    example row, no shared private file: checks.related_targets), minted under a different frozen
                    hash and a different beacon round;
                  - its certificate passes the same fresh-data test (the VALIDATED rule) and this claim is VALIDATED.
                A failed re-validation (a certificate that does not pass, or labels read and no certificate) is shown
                as "validated once, failed re-validation", never hidden. The route is generic: a sealed second part
                (seal at project start) or a second future batch can be registered as a `revalidation` case too; the
                checks above do not depend on minting.
  RETROSPECTIVE a cap, not a badge: a target that is a fixed public dataset (a Claim Audit OpenML split, or a seal whose
                researcher did not state the data is private: a networked Lab could have downloaded it) means the claim
                can never hold VALIDATED or RE-VALIDATED (§17.1).

VALIDATED is also denied when the comparator collapsed on the held-out data (court/comparator.py): the Kernel's
certificate stands as a record of what ran, but beating a sabotaged comparator is not a finding.

Built, NOT test-verified (int18, stage-a).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..kernel import KernelError
from ..kernel.certificate import CHECK_IDS, Certificate, Label
from ..kernel.vault import DatasetRole
from ..kerneld.scope import ScopedKernel
from ..store import EvidenceStore
from .minted import case_of_claim, latest
from .sealed import seal_fact as sealed_fact

BADGES = ("REPRODUCIBLE", "REPLICATED", "VALIDATED", "RE-VALIDATED")
MEANING = {
    "REPRODUCIBLE": "re-executed from the frozen bundle (pinned environment, offline) with the same result across "
                    "seeds (K0, K3, bundle); across machines NOT checked yet; reproducible does NOT mean true",
    "REPLICATED": "a different agent re-implemented it from the claim text alone (never the code) and its claim was "
                  "certified; a different model family is the stronger form",
    "VALIDATED": "pre-registered, then confirmed on fresh held-out data that did not exist or was sealed when the Lab "
                 "worked (minted after the freeze, future or sealed data), with valid statistics (K0-K9): a finding",
    "RE-VALIDATED": "confirmed a second time on a second, independent fresh dataset declared before the first "
                    "verdict, with the claim and the frozen code unchanged",
    "RETROSPECTIVE": "tested on a fixed public dataset a networked Lab could have downloaded: never VALIDATED or "
                     "RE-VALIDATED",
}
INGEST_RECORD = "kerneld_ingest"  # kerneld/ingest.RECORD_KIND (read only)
REVALIDATION_STATES = ("not applicable", "none", "declared", "passed", "failed", "invalid")


@dataclass
class LevelResult:
    cert_hash: str
    claim_hash: str | None
    label: str | None
    held: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(BADGES, False))
    badges: list[str] = field(default_factory=list)  # held badges with their form text, in BADGES order
    retrospective: bool = False
    revalidation: str = "not applicable"  # one of REVALIDATION_STATES
    badge: str = "NONE"  # the display string
    meaning: dict[str, str] = field(default_factory=dict)  # meaning of each held badge
    missing: dict[str, list[str]] = field(default_factory=dict)  # badge -> what is missing for it
    notes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def release_value(self) -> str:
        """What the Lab may be told (a ledgered `count` release, lab/promote.release_verdict): the held badges and
        the re-validation outcome only, never families, reasons or numbers. At most 16 values (2 x 2 x (1 + 3)), the
        transcripts one `count` release may tell (KernelConfig.count_alphabet); RETROSPECTIVE is not part of it (the
        route is public in the trace, and then VALIDATED and RE-VALIDATED are always false)."""
        parts = [b for b in BADGES if self.held[b]]
        if self.held["VALIDATED"] and self.revalidation == "failed":
            parts.append("RE-VALIDATION-FAILED")
        return "+".join(parts) or "NONE"


def _passed(cert: Certificate, ids: tuple[str, ...]) -> list[str]:
    """The checks among `ids` that did not PASS (n/a counts as not passed here)."""
    return [k for k in ids if k not in cert.checks or cert.checks[k].status != "pass"]


def classify_target(ingest_kind: str | None, minted: bool, registered_after_prereg: bool,
                    role: DatasetRole | None, sealed_at_start: bool = False, public_source: bool = False) -> str:
    """How a target's data came to exist relative to its claim (pure; module docstring):
    retrospective (a public-dataset ingest, or a seal of public data) | sealed-at-start (a ledger fact, court/sealed.py)
    | seal-after-lab | minted | future | sealed | internal."""
    external = role in (DatasetRole.EXTERNAL, DatasetRole.FUTURE)
    if ingest_kind == "openml_split" or public_source:
        return "retrospective"  # a fixed public dataset a networked Lab could have downloaded
    if ingest_kind == "sealed_split":
        return "sealed-at-start" if sealed_at_start else "seal-after-lab"
    if minted and registered_after_prereg:
        return "minted"
    if role == DatasetRole.FUTURE and registered_after_prereg:
        return "future"
    if external:
        return "sealed"  # rests on the human role approval (sealed/private when the Lab worked): untested
    return "internal"  # TEST / SEALED / DEV roles: CERTIFIED-INTERNAL at best


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
    own = case_of_claim(store, cert.claim_hash)
    session = None if own is None else str(own.get("session_id"))
    out: dict[str, dict[str, Any]] = {}
    for t in cert.claim.get("generalization_targets") or []:
        rec = datasets.get(t)
        reg = regs.get(t)
        minted = (t, cert.claim_hash) in mints
        after = prereg is not None and reg is not None and reg > prereg
        ingest = public_kinds.get(t)
        fact = sealed_fact(store, kernel, t, session) if ingest == "sealed_split" else {}
        # only a stated `private` escapes the cap: a seal with no statement is not fresh by assumption
        public = fact.get("project") is not None and fact.get("provenance") != "private"
        kind = classify_target(ingest, minted, after, rec.role if rec is not None else None,
                               bool(fact.get("at_start")), public)
        out[t] = {"seal": fact, "role": rec.role.value if rec is not None else None, "registered_seq": reg,
                  "prereg_seq": prereg,
                  "registered_after_prereg": after, "minted_for_this_claim": minted, "ingest_kind": ingest,
                  "freshness": kind, "role_approved": rec is not None and kernel.vault.role_approval(t) is not None}
    return out


# ---------------------------------------------------------------- the four badges, each on its own
def reproducible_missing(kernel: ScopedKernel, cert: Certificate, cert_hash: str) -> list[str]:
    out: list[str] = []
    if kernel.status(cert_hash) == "retracted":
        out.append("the certificate is retracted")
    if cert.label == Label.REJECTED:
        out.append(f"the Kernel REJECTED the claim (failed: {', '.join(cert.failed()) or 'none named'})")
    out += [f"{k} did not pass" for k in _passed(cert, ("K0", "K3"))]
    if not any(e.body.get("cert_hash") == cert_hash for e in kernel.ledger.entries("bundle")):
        out.append("no Reproducibility Bundle of this certificate is recorded (`chh bundle CERT`)")
    return out


def validated_missing(store: EvidenceStore, kernel: ScopedKernel, cert: Certificate,
                      facts: dict[str, Any] | None = None) -> list[str]:
    """The fresh-data test (module docstring, VALIDATED). Empty = passed. `facts` collects the per-target freshness
    and untested assumptions when given."""
    out: list[str] = []
    is_data = cert.claim.get("evidence_class", "data") == "data"
    fresh = target_freshness(store, kernel, cert) if is_data else {}
    if facts is not None:
        facts["targets"] = fresh
    if not is_data:
        out.append(f"VALIDATED is defined for data claims on held-out data (GOAL 17.1); this is a "
                   f"{cert.claim.get('evidence_class')} claim")
    if kernel.status(cert.cert_hash()) == "retracted":
        out.append("the certificate is retracted")
    if cert.label != Label.CERTIFIED_VALIDATED:
        out.append(f"label is {cert.label.value}, not CERTIFIED-VALIDATED (K6 on human-approved external/future data)")
    out += [f"{k} did not pass" for k in _passed(cert, tuple(CHECK_IDS))]
    own = case_of_claim(store, cert.claim_hash)
    outcome = None if own is None else latest(store, str(own["case_id"]), "outcome")
    if outcome is not None and outcome.get("comparator_held_out"):
        out.append(str(outcome["comparator_held_out"]))
    for t, f in fresh.items():
        if f["freshness"] == "retrospective":
            out.append(f"RETROSPECTIVE: target {t} is a fixed public dataset")
        elif f["freshness"] not in ("minted", "future", "sealed", "sealed-at-start"):
            out.append(f"target {t} is {f['freshness']}: not minted after the freeze, future or sealed external data")
        if f["freshness"] == "sealed" and facts is not None:
            facts.setdefault("untested_assumptions", []).append(
                f"{t}: sealed or private when the Lab worked (the human role approval; not checked by code)")
        if f["freshness"] == "sealed-at-start" and facts is not None:
            facts.setdefault("untested_assumptions", []).append(
                f"{t}: the data is private, as the researcher stated at the seal (not checked by code)")
    return out


def replication_facts(store: EvidenceStore, cert_hash: str, claim_hash: str) -> dict[str, Any]:
    """Replica cases of the Court case whose claim is `claim_hash` (court/replicate.py) and whether one counts: its
    certificate is CERTIFIED and REPRODUCIBLE. Families and the invariant-5 status are recorded, never required."""
    from . import minted as C
    own = C.case_of_claim(store, claim_hash)
    if own is None:
        return {"case": None, "replicas": [], "counts": False, "form": None,
                "why": "no Court case for this claim (replication is recorded for Court cases only)"}
    kernel = ScopedKernel(store)
    rows = []
    for r in C.related_cases(store, own["case_id"], "replica"):
        o = C.latest(store, str(r["case_id"]), "outcome")
        rep_cert = (o or {}).get("cert_hash")
        reproducible = False
        if rep_cert:
            try:
                reproducible = not reproducible_missing(kernel, kernel.certificate(str(rep_cert)), str(rep_cert))
            except (KernelError, KeyError, ValueError, OSError):
                reproducible = False
        certified = o is not None and str(o.get("label", "")).startswith("CERTIFIED") \
            and not o.get("comparator_held_out")
        fams = r.get("families") or {}
        author, replicator = str(fams.get("executor", "unknown")), str(fams.get("replicator", "unknown"))
        if author.startswith("unknown") or replicator.startswith("unknown"):
            relation = "family unknown"
        else:
            relation = "same family" if author == replicator else "different family"
        rows.append({"case_id": r["case_id"], "cert_hash": rep_cert, "label": (o or {}).get("label"),
                     "reproducible": reproducible, "invariant_5": str(r.get("invariant_5", "UNKNOWN")),
                     "form": f"{author} -> {replicator}, {relation}", "relation": relation,
                     "counts": certified and reproducible})
    counted = [x for x in rows if x["counts"]]
    best = next((x for x in counted if x["relation"] == "different family"), counted[0] if counted else None)
    why = "" if counted else ("no replica case" if not rows else "no replica that is certified and reproducible")
    return {"case": own["case_id"], "replicas": rows, "counts": bool(counted), "form": best["form"] if best else None,
            "invariant_5": best["invariant_5"] if best else None, "why": why}


_ROUND = re.compile(r"\s*(beacon_round|part)=\S+")


def claim_core(claim: dict[str, Any]) -> dict[str, Any]:
    """What must be UNCHANGED between a claim and its re-validation: everything except where it is tested (targets,
    development set, the statement that renders them, the case id, the Lab link and the beacon round)."""
    cond = dict((claim.get("scope") or {}).get("conditions") or {})
    return {"kind": claim.get("kind"), "evidence_class": claim.get("evidence_class"),
            "subject": (claim.get("subject") or {}).get("hash"),
            "comparator": (claim.get("comparator") or {}).get("hash"),
            "metric": claim.get("metric"), "effect": claim.get("effect"), "analysis_plan": claim.get("analysis_plan"),
            "risk": claim.get("risk"), "assumptions": claim.get("assumptions"), "resolve_by": claim.get("resolve_by"),
            "conditions": {k: (_ROUND.sub("", str(v)) if k == "protocol" else v) for k, v in sorted(cond.items())
                           if k not in ("audit_id", "paper_doi")}}


def revalidation_facts(store: EvidenceStore, cert: Certificate) -> dict[str, Any]:
    """The re-validation of this certificate's Court case (module docstring, RE-VALIDATED): state and reasons."""
    from ..kernel.checks import related_targets
    from . import minted as C
    own = C.case_of_claim(store, cert.claim_hash)
    if own is None:
        return {"state": "none", "why": ["no Court case for this claim (re-validation is recorded for Court cases)"]}
    kernel = ScopedKernel(store)
    revs = C.related_cases(store, own["case_id"], "revalidation")
    if not revs:
        return {"state": "none", "why": ["no re-validation was declared before the first verdict"]}
    r = revs[0]  # one per case (court/minted.declare_revalidation)
    why: list[str] = []
    first_read = min((e.seq for e in kernel.vault.accesses()
                      if e.body.get("claim_hash") == cert.claim_hash and e.body.get("purpose") == "evaluate"),
                     default=None)
    if first_read is not None and int(r.get("prereg_seq", 0)) > first_read:
        why.append("the re-validation was pre-registered after the first test read held-out labels")
    o = C.latest(store, str(r["case_id"]), "outcome")
    facts: dict[str, Any] = {"case": r["case_id"], "first_evaluate_seq": first_read,
                             "revalidation_prereg_seq": r.get("prereg_seq")}
    if o is None or (not o.get("cert_hash") and not o.get("labels_read")):
        return {**facts, "state": "declared", "why": why + ["the second test has not been run"]}
    if not o.get("cert_hash"):
        return {**facts, "state": "failed", "why": why + ["labels were read and no certificate was issued"]}
    try:
        second = kernel.certificate(str(o["cert_hash"]))
    except (KernelError, KeyError, ValueError, OSError) as e:
        return {**facts, "state": "failed", "why": why + [f"second certificate unreadable: {type(e).__name__}"]}
    if claim_core(second.claim) != claim_core(cert.claim):
        why.append("the second claim is not the same claim (claim core or frozen code differs)")
    t1 = set(cert.claim.get("generalization_targets") or [])
    t2 = set(second.claim.get("generalization_targets") or [])
    related = set().union(*(related_targets(kernel.vault, t) for t in t1)) if t1 else set()
    if not t2 or t1 & t2 or related & t2:
        why.append("the second dataset is not disjoint from (or is related to) the first")
    mints = {str(e.body.get("dataset_id")): str(e.body.get("frozen_hash")) for e in kernel.ledger.entries("mint")}
    if any(mints.get(t) for t in t1 | t2) and \
            len({mints.get(t) for t in t1} | {mints.get(t) for t in t2}) < 2:
        why.append("both datasets were minted under the same frozen hash")
    if r.get("route") != "sealed" and r.get("beacon_round") is not None \
            and r.get("beacon_round") == own.get("beacon_round"):
        why.append("both mints used the same beacon round")  # sealed parts share one draw and are disjoint by rows
    second_val = validated_missing(store, kernel, second)
    facts["second_cert"] = o["cert_hash"]
    if why:  # not a valid re-validation whatever its outcome: shown as such, never counted
        return {**facts, "state": "invalid", "why": why + second_val}
    return {**facts, "state": "passed" if not second_val else "failed", "why": second_val}


def level(store: EvidenceStore, cert_hash: str) -> LevelResult:
    """The badges of one certificate (module docstring). Pure reading; never raises for a bad certificate."""
    kernel = ScopedKernel(store)
    try:
        cert = kernel.certificate(cert_hash)
    except (KernelError, KeyError, ValueError, OSError) as e:
        return LevelResult(cert_hash, None, None, missing={b: [f"certificate unreadable: {type(e).__name__}"]
                                                          for b in BADGES})
    res = LevelResult(cert_hash, cert.claim_hash, cert.label.value)
    missing: dict[str, list[str]] = {}
    forms: dict[str, str] = {}
    # REPRODUCIBLE
    missing["REPRODUCIBLE"] = reproducible_missing(kernel, cert, cert_hash)
    forms["REPRODUCIBLE"] = "REPRODUCIBLE (one machine; across machines not checked)"
    res.facts["bundle_hashes"] = [e.body.get("bundle_hash") for e in kernel.ledger.entries("bundle")
                                  if e.body.get("cert_hash") == cert_hash]
    # REPLICATED (any model family; the family is shown, invariant 5 is a note)
    rf = replication_facts(store, cert_hash, cert.claim_hash)
    res.facts["replication"] = rf
    missing["REPLICATED"] = [] if rf["counts"] else [rf["why"]]
    if rf["counts"]:
        forms["REPLICATED"] = f"REPLICATED ({rf['form']})"
        res.notes.append(f"invariant 5 (replicator of another model family): {rf['invariant_5']}")
    # VALIDATED: the fresh-data test only
    missing["VALIDATED"] = validated_missing(store, kernel, cert, res.facts)
    forms["VALIDATED"] = "VALIDATED"
    res.retrospective = any(f["freshness"] == "retrospective" for f in res.facts.get("targets", {}).values())
    # RE-VALIDATED
    if missing["VALIDATED"]:
        res.revalidation = "not applicable"
        missing["RE-VALIDATED"] = ["the claim is not VALIDATED (the first fresh-data test)"]
    else:
        rv = revalidation_facts(store, cert)
        res.facts["revalidation"] = rv
        res.revalidation = str(rv["state"])
        missing["RE-VALIDATED"] = [] if rv["state"] == "passed" else (rv["why"] or [rv["state"]])
    forms["RE-VALIDATED"] = "RE-VALIDATED"
    res.held = {b: not missing[b] for b in BADGES}
    res.badges = [forms[b] for b in BADGES if res.held[b]]
    res.missing = {b: v for b, v in missing.items() if v}
    res.meaning = {b: MEANING[b] for b in BADGES if res.held[b]}
    shown = [b for b in BADGES if res.held[b]]
    if res.held["VALIDATED"] and res.revalidation == "failed":
        shown.append("validated once, failed re-validation")
    elif res.held["VALIDATED"] and res.revalidation == "invalid":
        shown.append("re-validation not valid (see missing)")
    res.badge = " + ".join(shown) or "NONE"
    if res.retrospective:
        res.badge += " (RETROSPECTIVE)"
        res.meaning["RETROSPECTIVE"] = MEANING["RETROSPECTIVE"]
    if res.held["REPRODUCIBLE"]:
        res.notes.append("REPRODUCIBLE: across machines NOT checked (one machine)")
    return res


def level_of_case(store: EvidenceStore, case_id: str) -> LevelResult | dict[str, Any]:
    from . import minted as C
    o = C.latest(store, case_id, "outcome")
    if o is None or not o.get("cert_hash"):
        return {"case_id": case_id, "badge": "NONE", "why": (o or {}).get("reason") or "no certificate yet"}
    return level(store, str(o["cert_hash"]))
