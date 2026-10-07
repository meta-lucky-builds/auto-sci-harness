"""The Evidence Kernel (GOAL §4): pre-register claims, certify them with K0-K9, retract certificates.

The Kernel is the ONLY component allowed to mint a Certificate (GOAL §3). It contains no model call:
tests/test_kernel_quality.py fails if anything under custom_harness.kernel imports the gateway, the
runtime engine, the research loop or the evolver.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal

from ..errors import InfraError
from ..records import Evidence, EvidenceKind
from ..store.evidence import EvidenceStore
from ..tiers import Tier
from . import adapters, alpha
from .certificate import CHECK_IDS, VG_LADDER, Assumption, Certificate, CheckResult, Label
from .checks import (
    Context,
    KernelConfig,
    Replayer,
    applicability,
    checks_for,
    fixed_terms,
    is_external,
    related_targets,
)
from .claim import Claim, EvidenceClass
from .execution import ExecRecord
from .ledger import AsOfLedger, Entry, Ledger
from .reasons import Code, UncodedReason, exception_name
from .releases import NOTE, Alphabet, Channel
from .simulated import SIMULATED_ASSUMPTIONS
from .vault import EXTERNAL_ROLES, HELD_OUT_ROLES, CertificateRecord, Vault, VaultError

STANDARD_ASSUMPTIONS = (
    Assumption(text="held-out examples are an i.i.d. sample of the population the claim is about", tested_by=None),
    Assumption(text="held-out labels are correct", tested_by=None),
    Assumption(text="no leak of held-out labels, canaries or private files into code, prompts or traces (K9 detects "
                    "verbatim, reversed, URL-, hex- and base64-encoded copies and long label runs; other "
                    "transformations are not detected)", tested_by="K9"),
    Assumption(text="the program's output depends only on its declared inputs, code and seed", tested_by="K3"),
)
COMPUTATIONAL_ASSUMPTIONS = (
    Assumption(text="the registered problem instance encodes the problem the claim is about (statement fidelity)",
               tested_by=None),
    Assumption(text="the approved adapter's exact checker is correct (human-approved; fault-injection validated)",
               tested_by=None),
    Assumption(text="the certificate is checked in exact rational arithmetic from the stored bytes (no floats)",
               tested_by="K3"),
)
FORMAL_ASSUMPTIONS = (
    Assumption(text="the human-approved statement formalises the intended claim (statement fidelity to the intent)",
               tested_by=None),
    Assumption(text="the Lean 4 kernel of the pinned toolchain is sound, and the pinned Mathlib build in the sandbox "
                    "image is the one its commit names", tested_by=None),
    Assumption(text="the check record was produced by the approved adapter's sandbox runner from the stored statement "
                    "and proof (the Kernel re-checks the recorded output; it does not re-run Lean)", tested_by=None),
    Assumption(text="the proved theorem's type equals the approved statement's, and its axioms are allowlisted "
                    "(comparator and axiom audit in the record)", tested_by="K3"),
)


RELEASE_ASSUMPTION = Assumption(
    text="the release ledger is complete: no target-derived information reached an agent-visible surface except as a "
         "ledgered release counted by K4 (side channels such as timing are not logged)", tested_by=None)


class KernelError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifyResult:
    """`valid`: the certificate is what the Kernel issues both as of its own ledger prefix and today. `stale`: it
    re-derives as issued from its own prefix, but later events or a changed Kernel rule change today's verdict.
    `forged`: it does not re-derive from the ledger as it stood when it was issued, under the same rules."""

    status: Literal["valid", "stale", "forged"]
    forged: tuple[str, ...]
    stale: tuple[str, ...]


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
    def approve_adapter(self, name: str, approver: str) -> Entry:
        """Human approval of a domain adapter's current content (CLI: `chh approve-adapter`); validation must pass."""
        return adapters.approve(self.ledger, name, approver)

    def approve_role(self, dataset_id: str, approver: str, note: str = "") -> Entry:
        """Human approval of a dataset's external/future role (CLI: `chh approve-role`); see Vault.approve_role."""
        return self.vault.approve_role(dataset_id, approver, note)

    def preregister(self, claim: Claim) -> Entry:
        """Commit the claim (and its analysis plan) to the ledger with the terms its verdict depends on frozen (§16.4,
        invariant 17): p0, the reference forecast and the K3 reliability requirement. A forecast q < p0 is refused (F2).
        On a target with an alpha-account the claim reserves a slot; a retired target refuses it. Idempotent."""
        h = claim.claim_hash()
        for e in self.ledger.entries("prereg"):
            if e.body.get("claim_hash") == h:
                return e
        terms = fixed_terms(self.ledger, self.vault, claim, self.config)
        if claim.effect.probability < terms["p0"]:
            raise KernelError(f"declared probability {claim.effect.probability} is below the claim's level p0 = "
                              f"{terms['p0']:.4g} (a forecast below p0 would earn VDS by failing)")
        accounts = {a.seq: a for t in claim.generalization_targets
                    for a in alpha.accounts(self.ledger, related_targets(self.vault, t))}
        spent = sorted({a.dataset_id for a in accounts.values() if alpha.retired(self.ledger, a)})
        if spent:
            raise KernelError(f"target(s) {spent} are retired (alpha-account closed): register on fresh data")
        entry = self.ledger.append("prereg", {"claim_hash": h, "claim": claim.model_dump(mode="json"), "fixed": terms})
        for a in sorted(accounts.values(), key=lambda a: a.seq):
            n = len(alpha.reservations(self.ledger, a)) + 1
            self.ledger.append("alpha_reserve", {"account": a.seq, "dataset_id": a.dataset_id, "claim_hash": h,
                                                 "slot": n, "level": a.level})
            if n >= a.slots:
                self.ledger.append("alpha_retire", {"account": a.seq, "dataset_id": a.dataset_id,
                                                    "code": "slots_exhausted", "approver": None})
        return entry

    # ------------------------------------------------------------------ alpha-accounts (§16.4) and releases (§16.5.3)
    def open_alpha_account(self, dataset_id: str, alpha_total: float, slots: int, approver: str) -> Entry:
        """HUMAN action: give a held-out target a fixed budget of `slots` claims, each tested at alpha_total / slots.
        Refused once any claim is registered on the target or a related one (their tests were not budgeted)."""
        rec = self.vault.dataset(dataset_id)
        if rec.role not in HELD_OUT_ROLES:
            raise KernelError(f"{dataset_id!r} has role {rec.role.value!r}: only held-out targets get an alpha-account")
        if not approver.strip():
            raise KernelError("an alpha-account must name the human who opened it")
        if not (0 < alpha_total <= self.config.max_alpha and slots >= 1):
            raise KernelError(f"an alpha-account needs alpha in (0, {self.config.max_alpha}] and >= 1 slot")
        related = related_targets(self.vault, dataset_id)
        if alpha.accounts(self.ledger, related):
            raise KernelError(f"{dataset_id!r} (or a target sharing its held-out examples) already has an account")
        if any(related & set(e.body.get("claim", {}).get("generalization_targets", []))
               for e in self.ledger.entries("prereg")):
            raise KernelError(f"claims are already registered on {dataset_id!r} or a related target: open the account "
                              f"before the first claim")
        return self.ledger.append("alpha_open", {"dataset_id": dataset_id, "dataset_hash": rec.hash,
                                                 "alpha": alpha_total, "slots": slots, "approver": approver})

    def retire_alpha_account(self, dataset_id: str, approver: str) -> Entry:
        """HUMAN action: retire a target early (no further claims can be registered on it)."""
        accts = alpha.accounts(self.ledger, {dataset_id})
        if not accts or not approver.strip():
            raise KernelError(f"no alpha-account on {dataset_id!r}, or no approver named")
        return self.ledger.append("alpha_retire", {"account": accts[0].seq, "dataset_id": dataset_id,
                                                   "code": "human", "approver": approver})

    def record_release(self, target_dataset_id: str, channel: str, alphabet: str, note: str) -> Entry:
        """Log one transfer of target-derived information to an agent-visible surface (invariant 18). K4 counts the
        transcripts releases allow; a `continuous` release adaptively exposes the target (K6)."""
        try:
            self.vault.dataset(target_dataset_id)
            ch, ab = Channel(channel), Alphabet(alphabet)
        except (VaultError, ValueError):
            raise KernelError("release refused: unregistered target, or a channel/alphabet outside the closed set") \
                from None
        if NOTE.fullmatch(note) is None:
            raise KernelError("a release note is a short lowercase code ([a-z][a-z0-9_.:-]{0,79}), never free text")
        return self.ledger.append("release", {"dataset_id": target_dataset_id, "channel": ch.value,
                                              "alphabet": ab.value, "note": note})

    def releases(self, target_dataset_id: str) -> list[Entry]:
        """Releases on the target and on every target sharing its held-out examples (the K4 family)."""
        related = related_targets(self.vault, target_dataset_id)
        return [e for e in self.ledger.entries("release") if e.body.get("dataset_id") in related]

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

    def record_certificate(self, claim: Claim, artifact_id: str) -> str:
        """Link a registered certificate artifact (e.g. an exact solver's primal/dual certificate) to a computational
        claim as PROOF evidence; returns the evidence id. Nothing is trusted here: K0-K3 resolve and re-check it."""
        art = self.vault.artifacts().get(artifact_id)
        if art is None:
            raise KernelError(f"certificate artifact {artifact_id!r} is not registered")
        rec = CertificateRecord(claim_hash=claim.claim_hash(), artifact_id=artifact_id, hash=art.hash)
        ev = Evidence(kind=EvidenceKind.PROOF, tier=Tier.T1_EXECUTABLE, verifier_id=f"certificate:{claim.metric.id}",
                      produced_by_run=None, payload_ref=self.store.blobs.put_json(rec.model_dump(mode="json")),
                      summary=f"certificate {artifact_id} for claim {rec.claim_hash[:12]}")
        self.store.add_evidence(ev)
        return ev.id

    # ------------------------------------------------------------------ certification
    def certify(self, claim: Claim, evidence_ids: list[str] | tuple[str, ...], *,
                full_chain: bool = True, record: bool = True, as_of: int | None = None) -> Certificate:
        """Run K0-K9 and issue an immutable certificate (recorded in the ledger). A check that raises is a
        FAILED check. full_chain=False is only for reproducing from a bundle (a ledger excerpt). as_of=seq re-derives
        on the ledger as it stood up to entry seq (never recorded)."""
        ledger = self.ledger if as_of is None else AsOfLedger(self.store, as_of)
        vault = self.vault if as_of is None else Vault(self.store, ledger)
        record = record and as_of is None
        ctx = Context(claim=claim, claim_hash=claim.claim_hash(), store=self.store, vault=vault,
                      ledger=ledger, config=self.config, replayer=self.replayer,
                      evidence_ids=tuple(evidence_ids), full_chain=full_chain)
        results: dict[str, CheckResult] = {}
        registered = vault.registered(ctx.claim_hash)
        table, impl = applicability(claim.evidence_class), checks_for(claim.evidence_class)
        computational = claim.evidence_class == EvidenceClass.COMPUTATIONAL
        simulated = claim.evidence_class == EvidenceClass.SIMULATED
        formal = claim.evidence_class == EvidenceClass.FORMAL
        for kid in CHECK_IDS:
            na = table[kid]
            if na is not None:  # the human-owned table says this check does not apply to the class: record why
                results[kid] = CheckResult(status="n/a", reasons=(na,), codes=(Code.NOT_APPLICABLE,))
                continue
            if not registered and kid not in ("K0", "K1", "K2"):
                # never touch held-out data for an unregistered claim: certify must not be a free look
                results[kid] = CheckResult(status="fail", codes=(Code.NOT_EVALUATED_UNREGISTERED,), reasons=(
                    "not evaluated: the claim was never pre-registered (held-out data are not read for it)",))
                continue
            try:
                results[kid] = impl[kid](ctx)
                if len(results[kid].codes) != len(results[kid].reasons):  # §16.5.8: every reason carries a code
                    raise UncodedReason("uncoded Kernel reason")
            except InfraError:
                # the environment broke (e.g. Docker down during K3 re-execution): issue NO certificate, so the
                # claim is judged on a retry instead of being rejected for an outage (infra != policy, GOAL L1)
                raise
            except Exception as e:  # noqa: BLE001  fail closed: a crashing check never passes
                # §16.5.8: the exception's message never enters a reason (it could carry data out of the Kernel)
                code = Code.CHECK_UNCODED if isinstance(e, UncodedReason) else Code.CHECK_RAISED
                results[kid] = CheckResult(status="fail", codes=(code,),
                                           reasons=(f"check raised {exception_name(e)}",))
        # fail closed: only an explicit pass counts, or an n/a where the TABLE (not the check) says it does not apply
        ok = all(r.status == "pass" or (r.status == "n/a" and table[k] is not None) for k, r in results.items())
        if not ok:
            label = Label.REJECTED
        elif computational:
            label = Label.CERTIFIED_EXACT
        elif simulated:  # holds in the named simulator only; T2 (conservative, UNCALIBRATED: T1 allowed by §4.7)
            label = Label.CERTIFIED_SIMULATED
        elif formal:
            label = Label.CERTIFIED_FORMAL
        else:
            label = Label.CERTIFIED_VALIDATED if is_external(ctx) else Label.CERTIFIED_INTERNAL
        observed = [Assumption(text=f"{kid}: {o}", tested_by=kid) for kid in ("K4",)
                    for o in results[kid].observations if "seed-invariant" in o]
        if formal and ok:  # the certificate lists every axiom the theorem relies on (untested, GOAL §4.7)
            observed += [Assumption(text=f"K3: {o}", tested_by=None) for o in results["K3"].observations
                         if o.startswith("relies on axiom ")]
        stats = {t: {"estimate": s.example.estimate, "lo": s.example.lo, "hi": s.example.hi, "met": s.met,
                     "p_null": s.p_null, "alpha_eff": s.alpha_eff} for t, s in ctx.stats.items()}
        p_nulls = [s.p_null for s in ctx.stats.values() if s.p_null is not None]
        resolution = {"outcome": ok, "declared_probability": claim.effect.probability,
                      "p_null": max(p_nulls) if p_nulls else None, "targets": stats}
        tier = (Tier.T6_NONE if not ok else Tier.T1_EXECUTABLE if computational else Tier.T0_FORMAL if formal
                else Tier.T2_STATISTICAL)
        # §16.7: a field only the claimant's pipeline reported (no human approved it) caps the tier
        self_reported = tuple(sorted(
            f"role {d.role.value!r} of target {t!r} is not human-approved" for t, d in vault.datasets().items()
            if t in claim.generalization_targets and d.role in EXTERNAL_ROLES and vault.role_approval(t) is None))
        if ok and self_reported:
            tier = Tier(max(int(tier), self.config.self_reported_tier_cap))
        verifiers = (("kernel:K0-K9", f"exact:{claim.metric.id}") if computational else
                     ("kernel:K0-K9", f"sim:{claim.metric.id}") if simulated else
                     ("kernel:K0-K9", f"lean:{claim.metric.id}") if formal else
                     ("kernel:K0-K9", *sorted({f"metric:{r.metric}" for _, r in ctx.records}),
                      "stats:paired-t-seeds", "stats:bca-examples"))
        cert = Certificate(
            kernel_version=self.config.version, claim_hash=ctx.claim_hash, claim=claim.model_dump(mode="json"),
            label=label, tier=int(tier), checks=results,
            assumptions=(*(COMPUTATIONAL_ASSUMPTIONS if computational else SIMULATED_ASSUMPTIONS if simulated
                           else FORMAL_ASSUMPTIONS if formal else (*STANDARD_ASSUMPTIONS, RELEASE_ASSUMPTION)),
                         *observed,
                         *(Assumption(text=a, tested_by=None) for a in claim.assumptions)),
            evidence_ids=tuple(evidence_ids), verifier_ids=verifiers,
            prereg_seq=ctx.prereg.seq if ctx.prereg else None, resolution=resolution, issued_at=self.clock(),
            vg_ladder=VG_LADDER.get(label.value, VG_LADDER["REJECTED"]), config=asdict(self.config),
            adapters=(ctx.adapter,) if ctx.adapter else (), self_reported=self_reported)
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
        """Every problem with a certificate, prefixed `forged:` or `stale:` (see verify_status). Empty list = the
        certificate is what the Kernel issues both as of its own ledger prefix and today; callers that must not rely
        on a stale certificate (the memory gate) keep using this strict form."""
        res = self.verify_status(cert_hash)
        return [f"forged: {p}" for p in res.forged] + [f"stale: {p}" for p in res.stale]

    def verify_status(self, cert_hash: str) -> VerifyResult:
        """Re-derive a certificate (re-running every check, including re-execution) as of its OWN ledger prefix
        (forgery detection) and on today's ledger (staleness). Later honest events, such as a new claim on the same
        test set, make a certificate STALE, never "forged" (§16.4, invariant 17). A certificate issued under other
        Kernel rules (version or config) cannot be told apart from a forgery by re-derivation: it is reported STALE
        with that reason, and is not valid either way. This, not the ledger entry, is what makes a certificate
        trustworthy: the ledger is tamper-evident, not authenticated."""
        cert = self.certificate(cert_hash)
        entry = next(e for e in self.ledger.entries("certificate") if e.body["cert_hash"] == cert_hash)
        claim, ids = Claim.model_validate(cert.claim), list(cert.evidence_ids)
        then = _differences(cert, self.certify(claim, ids, as_of=entry.seq - 1))
        if then and (cert.kernel_version != self.config.version or cert.config != asdict(self.config)):
            return VerifyResult("stale", (), (f"issued under Kernel {cert.kernel_version} with other rules; today's "
                                              f"Kernel {self.config.version} re-derives differently", *then))
        if then:
            return VerifyResult("forged", tuple(then), ())
        now = _differences(cert, self.certify(claim, ids, record=False))
        return VerifyResult("stale" if now else "valid", (), tuple(now))

    def retract(self, cert_hash: str, reason: str) -> Entry:
        self.certificate(cert_hash)  # must exist
        if not reason.strip():
            raise KernelError("a retraction needs a reason")
        return self.ledger.append("retraction", {"cert_hash": cert_hash, "reason": reason})

    def status(self, cert_hash: str) -> str:
        self.certificate(cert_hash)
        retracted = any(e.body["cert_hash"] == cert_hash for e in self.ledger.entries("retraction"))
        return "retracted" if retracted else "active"


def _differences(cert: Certificate, again: Certificate) -> list[str]:
    problems = [f"{k}: {cert.checks[k].status} recorded, {again.checks[k].status} re-derived"
                for k in CHECK_IDS if cert.checks[k].status != again.checks[k].status]
    if again.label != cert.label:
        problems.append(f"label {cert.label.value} recorded, {again.label.value} re-derived")
    if again.tier != cert.tier:
        problems.append(f"tier {cert.tier} recorded, {again.tier} re-derived")
    return problems
