"""Certificates (GOAL §4.4): the Kernel's only output. Immutable and content-addressed; a later
contrary finding is recorded as a retraction in the ledger, never by editing a certificate."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .claim import canonical_json

CHECK_IDS = ("K0", "K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9")
CHECK_NAMES = {
    "K0": "provenance", "K1": "well-formed", "K2": "pre-registered", "K3": "reproduced",
    "K4": "statistically valid procedure", "K5": "comparator integrity", "K6": "held-out confirmation",
    "K7": "risk", "K8": "background consistency", "K9": "leak & canary audit",
}

# Verification Gap (arXiv 2608.05179) ladder mapping, stated in every report (GOAL §1.3).
VG_LADDER = {
    "CERTIFIED-VALIDATED": "external/future-data confirmation with re-execution and statistics (VG: held-out "
                           "empirical validation)",
    "CERTIFIED-INTERNAL": "sealed internal held-out confirmation with re-execution and statistics (VG: "
                          "statistical evaluation on held-out data; not external validation)",
    "CERTIFIED-EXACT": "exact, re-checkable certificate of the stated computation on the registered instance (VG: "
                       "executable/exact verification; says nothing about the computation's importance or whether "
                       "the instance models anything real)",
    "CERTIFIED-SIMULATED": "re-executed, converged simulation in the named pinned simulator (VG: executable "
                           "verification within the simulator; certifies the simulator's answer, not that the "
                           "simulator matches reality)",
    "CERTIFIED-FORMAL": "Lean 4 kernel check of a proof of the human-approved statement (VG: formal verification; "
                        "holds given Lean's kernel and the listed axioms; says nothing about whether the statement "
                        "formalises the intended claim beyond the human approval)",
    "REJECTED": "no verification claimed",
}


class Label(StrEnum):
    CERTIFIED_VALIDATED = "CERTIFIED-VALIDATED"
    CERTIFIED_INTERNAL = "CERTIFIED-INTERNAL"
    CERTIFIED_EXACT = "CERTIFIED-EXACT"  # computational class: an exact certificate re-checked by an approved adapter
    CERTIFIED_SIMULATED = "CERTIFIED-SIMULATED"  # simulated class: holds in the named simulator only (GOAL §4.7)
    CERTIFIED_FORMAL = "CERTIFIED-FORMAL"  # formal class: a Lean check record re-checked by the approved adapter
    SUPPORTED = "SUPPORTED"  # judge/retrospective evidence only; not issued by Kernel v1
    REJECTED = "REJECTED"


class CheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["pass", "fail", "n/a"]
    reasons: tuple[str, ...] = ()  # why it failed (or why n/a)
    observations: tuple[str, ...] = ()  # facts recorded while checking (numbers, notes)


class Assumption(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    tested_by: str | None = None  # check id, or None for an untested assumption


class Certificate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    kernel_version: str
    claim_hash: str
    claim: dict[str, Any]
    label: Label
    tier: int  # verifier tier (tiers.Tier); T2 for a certified ML claim, T1 for a computational one, T6 when rejected
    checks: dict[str, CheckResult]
    assumptions: tuple[Assumption, ...]
    evidence_ids: tuple[str, ...]
    verifier_ids: tuple[str, ...]
    prereg_seq: int | None
    resolution: dict[str, Any]  # outcome of the claim's prediction (see predictions.py)
    issued_at: float
    vg_ladder: str
    config: dict[str, Any] = Field(default_factory=dict)  # the Kernel configuration that was applied
    # the domain adapters (name, version, content hash, approval ledger seq) this certificate relied on (GOAL §4.4)
    adapters: tuple[dict[str, Any], ...] = ()

    def cert_hash(self) -> str:
        doc = self.model_dump(mode="json")
        if not doc["adapters"]:
            del doc["adapters"]  # omitted when empty: certificates issued before the field existed keep their hash
        return hashlib.sha256(canonical_json(doc)).hexdigest()

    def failed(self) -> list[str]:
        return [k for k in CHECK_IDS if self.checks[k].status == "fail"]
