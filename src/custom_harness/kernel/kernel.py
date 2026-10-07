"""The Evidence Kernel (GOAL §4): pre-register claims, certify them with K0-K9, retract certificates.

The Kernel is the ONLY component allowed to mint a Certificate (GOAL §3). It contains no model call:
tests/test_kernel_quality.py fails if anything under custom_harness.kernel imports the gateway, the
runtime engine, the research loop or the evolver.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict

from ..errors import InfraError
from ..records import Evidence, EvidenceKind
from ..store.evidence import EvidenceStore
from ..tiers import Tier
from .certificate import CHECK_IDS, VG_LADDER, Assumption, Certificate, CheckResult, Label
from .checks import ALL_CHECKS, Context, KernelConfig, Replayer, is_external
from .claim import Claim
from .execution import ExecRecord
from .ledger import Entry, Ledger
from .vault import Vault

STANDARD_ASSUMPTIONS = (
    Assumption(text="held-out examples are an i.i.d. sample of the population the claim is about", tested_by=None),
    Assumption(text="held-out labels are correct", tested_by=None),
    Assumption(text="no verbatim leak of held-out labels, canaries or private files into code, prompts or traces",
               tested_by="K9"),
    Assumption(text="the program's output depends only on its declared inputs, code and seed", tested_by="K3"),
)


class KernelError(RuntimeError):
    pass


class Kernel:
    def __init__(self, store: EvidenceStore, *, config: KernelConfig | None = None,
                 replayer: Replayer | None = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.ledger = Ledger(store)
        self.vault = Vault(store, self.ledger)
        self.config = config or KernelConfig()
        self.replayer = replayer
        self.clock = clock

    # ------------------------------------------------------------------ pre-registration
    def preregister(self, claim: Claim) -> Entry:
        """Commit the claim (and its analysis plan) to the ledger. Idempotent per claim hash."""
        h = claim.claim_hash()
        for e in self.ledger.entries("prereg"):
            if e.body.get("claim_hash") == h:
                return e
        return self.ledger.append("prereg", {"claim_hash": h, "claim": claim.model_dump(mode="json")})

    def registered_claim(self, claim_hash: str) -> Claim:
        for e in self.ledger.entries("prereg"):
            if e.body.get("claim_hash") == claim_hash:
                return Claim.model_validate(e.body["claim"])
        raise KernelError(f"claim {claim_hash[:12]} was never pre-registered")

    # ------------------------------------------------------------------ evidence
    def record_evaluation(self, record: ExecRecord) -> str:
        """Store one held-out evaluation as T1 metric evidence; returns the evidence id."""
        payload = self.store.blobs.put_json(record.model_dump(mode="json"))
        ev = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T1_EXECUTABLE, verifier_id=f"metric:{record.metric}",
                      produced_by_run=record.run_id, payload_ref=payload,
                      summary=f"{record.arm} seed={record.seed} {record.target} {record.metric}={record.score:.6g}")
        self.store.add_evidence(ev)
        return ev.id

    # ------------------------------------------------------------------ certification
    def certify(self, claim: Claim, evidence_ids: list[str] | tuple[str, ...], *,
                full_chain: bool = True, record: bool = True) -> Certificate:
        """Run K0-K9 and issue an immutable certificate (recorded in the ledger). A check that raises is a
        FAILED check. full_chain=False is only for reproducing from a bundle (a ledger excerpt)."""
        ctx = Context(claim=claim, claim_hash=claim.claim_hash(), store=self.store, vault=self.vault,
                      ledger=self.ledger, config=self.config, replayer=self.replayer,
                      evidence_ids=tuple(evidence_ids), full_chain=full_chain)
        results: dict[str, CheckResult] = {}
        registered = self.vault.registered(ctx.claim_hash)
        for kid in CHECK_IDS:
            if not registered and kid not in ("K0", "K1", "K2"):
                # never touch held-out data for an unregistered claim: certify must not be a free look
                results[kid] = CheckResult(status="fail", reasons=(
                    "not evaluated: the claim was never pre-registered (held-out data are not read for it)",))
                continue
            try:
                results[kid] = ALL_CHECKS[kid](ctx)
            except InfraError:
                # the environment broke (e.g. Docker down during K3 re-execution): issue NO certificate, so the
                # claim is judged on a retry instead of being rejected for an outage (infra != policy, GOAL L1)
                raise
            except Exception as e:  # noqa: BLE001  fail closed: a crashing check never passes
                results[kid] = CheckResult(status="fail", reasons=(f"check raised {type(e).__name__}: {e}",))
        ok = all(r.status == "pass" for r in results.values())  # fail closed: only an explicit pass counts
        label = (Label.CERTIFIED_VALIDATED if is_external(ctx) else Label.CERTIFIED_INTERNAL) if ok else Label.REJECTED
        observed = [Assumption(text=f"{kid}: {o}", tested_by=kid) for kid in ("K4",)
                    for o in results[kid].observations if "seed-invariant" in o]
        stats = {t: {"estimate": s.example.estimate, "lo": s.example.lo, "hi": s.example.hi, "met": s.met,
                     "p_null": s.p_null, "alpha_eff": s.alpha_eff} for t, s in ctx.stats.items()}
        p_nulls = [s.p_null for s in ctx.stats.values() if s.p_null is not None]
        resolution = {"outcome": ok, "declared_probability": claim.effect.probability,
                      "p_null": max(p_nulls) if p_nulls else None, "targets": stats}
        cert = Certificate(
            kernel_version=self.config.version, claim_hash=ctx.claim_hash, claim=claim.model_dump(mode="json"),
            label=label, tier=int(Tier.T2_STATISTICAL if ok else Tier.T6_NONE), checks=results,
            assumptions=(*STANDARD_ASSUMPTIONS, *observed,
                         *(Assumption(text=a, tested_by=None) for a in claim.assumptions)),
            evidence_ids=tuple(evidence_ids),
            verifier_ids=("kernel:K0-K9", *sorted({f"metric:{r.metric}" for _, r in ctx.records}),
                          "stats:paired-t-seeds", "stats:bca-examples"),
            prereg_seq=ctx.prereg.seq if ctx.prereg else None, resolution=resolution, issued_at=self.clock(),
            vg_ladder=VG_LADDER.get(label.value, VG_LADDER["REJECTED"]), config=asdict(self.config))
        if not record:
            return cert
        blob = self.store.blobs.put_json(cert.model_dump(mode="json"))
        self.ledger.append("certificate", {"cert_hash": cert.cert_hash(), "claim_hash": ctx.claim_hash,
                                           "label": label.value, "blob": blob, "resolution": resolution})
        return cert

    # ------------------------------------------------------------------ lookup / retraction
    def certificate(self, cert_hash: str) -> Certificate:
        """Look up a certificate. Its content must hash to the id it is filed under (catches a mis-filed or
        edited blob). A forger with write access could still file a self-consistent fake: use verify()."""
        for e in self.ledger.entries("certificate"):
            if e.body["cert_hash"] == cert_hash:
                cert = Certificate.model_validate(self.store.blobs.get_json(e.body["blob"]))
                if cert.cert_hash() != cert_hash or cert.claim_hash != e.body.get("claim_hash"):
                    raise KernelError(f"certificate {cert_hash[:12]}: content does not match its ledger entry")
                return cert
        raise KernelError(f"no certificate {cert_hash[:12]}")

    def verify(self, cert_hash: str) -> list[str]:
        """Re-derive a certificate from the evidence (re-running every check, including re-execution) and
        report every difference. Empty list = the certificate is what the Kernel issues today. This, not the
        ledger entry, is what makes a certificate trustworthy: the ledger is tamper-evident, not authenticated."""
        cert = self.certificate(cert_hash)
        again = self.certify(Claim.model_validate(cert.claim), list(cert.evidence_ids), record=False)
        problems = [f"{k}: {cert.checks[k].status} recorded, {again.checks[k].status} re-derived"
                    for k in CHECK_IDS if cert.checks[k].status != again.checks[k].status]
        if again.label != cert.label:
            problems.append(f"label {cert.label.value} recorded, {again.label.value} re-derived")
        return problems

    def retract(self, cert_hash: str, reason: str) -> Entry:
        self.certificate(cert_hash)  # must exist
        if not reason.strip():
            raise KernelError("a retraction needs a reason")
        return self.ledger.append("retraction", {"cert_hash": cert_hash, "reason": reason})

    def status(self, cert_hash: str) -> str:
        self.certificate(cert_hash)
        retracted = any(e.body["cert_hash"] == cert_hash for e in self.ledger.entries("retraction"))
        return "retracted" if retracted else "active"
