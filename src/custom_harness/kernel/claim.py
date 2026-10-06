"""Claim Language v1 (GOAL §4.2): a scientific claim is a typed object, never prose.

A Claim states what is asserted (kind, subject vs comparator, metric, minimum effect), where it must hold
(generalization targets never used in development), how it will be tested (analysis plan), what is
assumed, and how surprising success would be (risk). Its content hash is what gets pre-registered.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ClaimKind(StrEnum):
    IMPROVEMENT = "improvement"  # subject beats comparator by >= min_effect
    ABLATION_EFFECT = "ablation_effect"  # full method (subject) beats its ablation (comparator) by >= min_effect
    NEGATIVE_RESULT = "negative_result"  # subject does NOT beat comparator by min_effect
    EQUIVALENCE = "equivalence"  # |subject - comparator| < min_effect
    SCALING_LAW = "scaling_law"  # declared in the language; not checkable by Kernel v1
    REDISCOVERY = "rediscovery"  # declared in the language; not checkable by Kernel v1


SUPPORTED_KINDS = frozenset({ClaimKind.IMPROVEMENT, ClaimKind.ABLATION_EFFECT, ClaimKind.NEGATIVE_RESULT,
                             ClaimKind.EQUIVALENCE})


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

    def canonical(self) -> bytes:
        return canonical_json(self.model_dump(mode="json"))

    def claim_hash(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """One byte string per value: sorted keys, no whitespace, no NaN (rejected, never silently encoded)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
