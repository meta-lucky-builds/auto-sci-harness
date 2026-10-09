"""The production skill writer (GOAL §5 L5; invariant 4): the ONE path through which the research loop persists a skill
(a playbook entry) from a certified outcome, and negative memory from verified failures. docs/skill_writer.md.

Who calls it: only trusted wiring (`research/memory_wiring.after_certify`, called by the protected `research.certify`
node with the certificate hash it has just issued). No agent writes directly: the memory package is imported only by
allowlisted modules (tests/test_memory.py static check), and the writer's identity is the fixed `WRITER`.

What it checks BEFORE anything is proposed (a refusal raises `WriterRefused`; nothing is stored):
  - an exploit check is configured on the store (the gate would refuse the skill anyway; the writer never proposes one
    that cannot be checked);
  - the certificate exists, is CERTIFIED, re-derives (`Kernel.verify`) and is not retracted;
  - every evidence record of the certificate was produced by THIS run (`run_id`): a run cannot write a skill from
    another run's certificate;
  - the method text it quotes belongs to the certified subject: the caller states the content hash of the code the
    text describes, and it must equal the claim's subject hash.
What it writes: a deterministic `Kind.PLAYBOOK` proposal built from the Kernel certificate and claim (statement, label,
tier, metric, minimum effect), quoting the model-written method text as DATA (screened by the store's content screen,
rendered inside the untrusted-data wrapper, never deciding anything). Then the store's gate decides, with no model:
quarantine first (`propose`, unreadable), then `admit` (Kernel re-derivation, tier floor, exploit check against the
perturbed evaluator). The derivation graph records the certificate (and its claim), so a Kernel retraction revokes the
skill and everything derived from it (`MemoryStore.sync`).

Negative memory: `write_negatives` runs `harvest_negatives` under the same writer identity (verified failures only:
REJECTED on K6 alone).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..kernel import Claim, ExecRecord, KernelError
from .model import GateResult, Kind, MemoryRefused, Proposal
from .store import CERTIFIED, MemoryStore, harvest_negatives

WRITER = "skill-writer"
MAX_METHOD_CHARS = 2000


class WriterRefused(ValueError):
    """The writer refused before proposing anything."""


class SkillWriter:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.kernel = store.kernel

    def _evidence_runs(self, evidence_ids: Sequence[str]) -> set[str | None]:
        runs: set[str | None] = set()
        for eid in evidence_ids:
            try:
                ev = self.kernel.store.get_evidence(eid)
                rec = ExecRecord.model_validate(self.kernel.store.blobs.get_json(ev.payload_ref))
            except (KeyError, ValueError, OSError):
                runs.add(None)
                continue
            runs.add(rec.run_id)
        return runs

    def playbook_proposal(self, cert_hash: str, *, run_id: str, method: str, method_subject_hash: str) -> Proposal:
        if self.store.exploit_check is None:
            raise WriterRefused("no exploit check configured: skills are not written without the perturbed evaluator")
        try:
            cert = self.kernel.certificate(cert_hash)
        except KernelError as e:
            raise WriterRefused(f"no such certificate: {e}") from None
        if cert.label not in CERTIFIED:
            raise WriterRefused(f"certificate {cert_hash[:12]} is {cert.label.value}: only a certified outcome becomes "
                                "a skill")
        retracted = {e.body.get("cert_hash") for e in self.kernel.ledger.entries("retraction")}
        if cert_hash in retracted:
            raise WriterRefused(f"certificate {cert_hash[:12]} is retracted")
        if self.kernel.verify(cert_hash):
            raise WriterRefused(f"certificate {cert_hash[:12]} does not re-derive")
        runs = self._evidence_runs(cert.evidence_ids)
        if not cert.evidence_ids or runs != {run_id}:
            raise WriterRefused(f"certificate {cert_hash[:12]} was not produced by run {run_id!r} alone")
        claim = Claim.model_validate(cert.claim)
        if method_subject_hash != claim.subject.hash:
            raise WriterRefused("the method text does not describe the certified subject (content hash differs)")
        text = (f"Certified outcome ({cert.label.value}, tier T{cert.tier}): {claim.statement}. Metric "
                f"{claim.metric.id}, minimum effect {claim.effect.min_effect:.4g}. Method, as described by the model "
                f"that wrote the certified program (quoted, not verified): {method.strip()[:MAX_METHOD_CHARS]}")
        return Proposal(kind=Kind.PLAYBOOK, title=f"Playbook: {claim.statement}"[:200], text=text[:4000],
                        evidence=(cert_hash,),
                        data={"claim_hash": cert.claim_hash, "cert_hash": cert_hash, "run_id": run_id,
                              "subject_hash": claim.subject.hash, "metric": claim.metric.id,
                              "min_effect": claim.effect.min_effect})

    def write_playbook(self, cert_hash: str, *, run_id: str, method: str, method_subject_hash: str,
                       secrets: Sequence[str] = ()) -> GateResult:
        """Propose (quarantined) then admit through the store's gate. Raises WriterRefused / MemoryRefused when
        nothing was written."""
        prop = self.playbook_proposal(cert_hash, run_id=run_id, method=method,
                                      method_subject_hash=method_subject_hash)
        eid = self.store.propose(prop, writer=WRITER, secrets=secrets)
        return self.store.admit(eid, secrets=secrets)

    def write_negatives(self, secrets: Sequence[str] = ()) -> list[GateResult]:
        return harvest_negatives(self.store, writer=WRITER, secrets=secrets)


__all__ = ["MAX_METHOD_CHARS", "WRITER", "MemoryRefused", "SkillWriter", "WriterRefused"]
