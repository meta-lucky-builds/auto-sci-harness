"""Claim Language v1 (GOAL §4.2): a scientific claim is a typed object, never prose.

A Claim states what is asserted (kind, subject vs comparator, metric, minimum effect), where it must hold
(generalization targets never used in development), how it will be tested (analysis plan), what is
assumed, and how surprising success would be (risk). Its content hash is what gets pre-registered.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer


class ClaimKind(StrEnum):
    IMPROVEMENT = "improvement"  # subject beats comparator by >= min_effect
    ABLATION_EFFECT = "ablation_effect"  # full method (subject) beats its ablation (comparator) by >= min_effect
    NEGATIVE_RESULT = "negative_result"  # subject does NOT beat comparator by min_effect
    EQUIVALENCE = "equivalence"  # |subject - comparator| < min_effect
    SCALING_LAW = "scaling_law"  # declared in the language; not checkable by Kernel v1
    REDISCOVERY = "rediscovery"  # declared in the language; not checkable by Kernel v1
    # computational class only: "the exact value of the computation on the registered instance (subject) is `value`"
    COMPUTED_VALUE = "computed_value"
    # formal class only: "the human-approved Lean statement (subject) holds" (Lean 4 adapter, T0)
    THEOREM = "theorem"


SUPPORTED_KINDS = frozenset({ClaimKind.IMPROVEMENT, ClaimKind.ABLATION_EFFECT, ClaimKind.NEGATIVE_RESULT,
                             ClaimKind.EQUIVALENCE})


class EvidenceClass(StrEnum):
    """GOAL §4.7: the class of evidence a claim rests on; it fixes the strongest tier the claim can reach and which
    (human-approved) domain adapter must check it. Only classes served by an approved adapter can be certified."""

    FORMAL = "formal"  # Lean (T0)
    COMPUTATIONAL = "computational"  # exact solvers with re-checkable certificates (T1)
    DATA = "data"  # held-out / time-split / external data (T2, T4)
    SIMULATED = "simulated"  # holds only within the named simulator (T1/T2 within it)
    PHYSICAL = "physical"  # human-executed via an approved protocol; returned data checked (T3)


class _Model(BaseModel):
    # allow_inf_nan=False: a claim can never carry NaN/inf, so it can always be hashed and checked
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ArtifactRef(_Model):
    artifact_id: str
    hash: str  # sha256 of the artifact bytes (blob hash)


class MetricRef(_Model):
    id: str
    higher_is_better: bool


class Effect(_Model):
    min_effect: float  # delta, in metric units, sign-corrected (positive = subject better)
    predicted_lo: float  # predicted effect interval (the claimant's forecast)
    predicted_hi: float
    probability: float  # declared P(the claim's criterion is met on the targets); scored by the ledger


class Scope(_Model):
    datasets: tuple[str, ...]  # dataset ids describing where the claim is asserted to hold
    conditions: dict[str, str] = Field(default_factory=dict)


class AnalysisPlan(_Model):
    seeds: tuple[int, ...]
    alpha: float = 0.05  # one-sided level for the effect criterion (before multiplicity correction)
    test: Literal["paired_t_seeds+bca_examples"] = "paired_t_seeds+bca_examples"
    n_comparisons: int = 1  # claims tested on the same targets in this family (Bonferroni)
    stopping_rule: Literal["fixed_n"] = "fixed_n"
    n_boot: int = 2000
    # "sampled_seed": the Kernel re-executes one seed per arm and target, chosen from a hash of the evidence
    # AFTER it is fixed (a program that is deterministic only for a predictable seed is caught); "all": every seed
    reproduce: Literal["sampled_seed", "all"] = "sampled_seed"
    reproduce_tolerance: float = 0.0  # max |metric difference| on re-execution; 0 = identical predictions
    # K3 reliability demonstration (§16.4): the claim's program fails (does not reproduce) at most eps_max of the time,
    # demonstrated at confidence 1 - beta by ceil(ln beta / ln(1 - eps_max)) consecutive clean replays. None = the
    # Kernel's preset, frozen into the pre-registration. Omitted from dumps when unset (older claims keep their hash).
    eps_max: float | None = None
    beta: float | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_reliability(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        doc: dict[str, Any] = handler(self)
        for key in ("eps_max", "beta"):
            if doc.get(key) is None:
                doc.pop(key, None)
        return doc


class Risk(_Model):
    prior_probability: float | None = None  # P(criterion met) before this work; None = use the noise model only


class Claim(_Model):
    schema_version: Literal[1] = 1
    kind: ClaimKind
    statement: str  # human-readable rendering; informational, the typed fields are what is checked
    subject: ArtifactRef
    comparator: ArtifactRef
    metric: MetricRef
    effect: Effect
    scope: Scope
    development: tuple[str, ...]  # dataset ids used during development
    generalization_targets: tuple[str, ...]  # dataset ids NOT used in development (held out)
    analysis_plan: AnalysisPlan
    assumptions: tuple[str, ...] = ()
    risk: Risk = Field(default_factory=Risk)
    resolve_by: float  # epoch seconds; an unresolved prediction past this is scored as a miss
    evidence_class: EvidenceClass = EvidenceClass.DATA
    # computed_value claims only: the claimed exact value, a rational "p" or "p/q" in lowest terms (never a float).
    # Omitted from every dump when unset, so data claims, their pre-registrations and certificates keep their bytes.
    value: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_value(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        doc: dict[str, Any] = handler(self)
        if doc.get("value") is None:
            doc.pop("value", None)
        return doc

    def canonical(self) -> bytes:
        doc = self.model_dump(mode="json")
        if doc["evidence_class"] == EvidenceClass.DATA.value:
            del doc["evidence_class"]  # the default is omitted: claims hashed before the field existed keep their hash
        return canonical_json(doc)

    def claim_hash(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()


_EXACT = re.compile(r"-?(0|[1-9][0-9]*)(/[1-9][0-9]*)?")


def exact_value(text: str | None) -> Fraction | None:
    """The claimed exact value, or None unless `text` is a rational in lowest terms ("3", "-13/5"; not "2.6",
    "26/10", "-0", "1/0"): one spelling per value, so equal claims hash equally and nothing is rounded."""
    if text is None or _EXACT.fullmatch(text) is None or len(text) > 1000:
        return None
    value = Fraction(text)
    return value if str(value) == text else None


def canonical_json(obj: Any) -> bytes:
    """One byte string per value: sorted keys, no whitespace, no NaN (rejected, never silently encoded)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
