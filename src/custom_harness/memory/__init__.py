"""NAIVE SKELETON (fail-first stage only; replaced by the real store): accepts every write, like Prime Agent."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from ..tiers import Tier

WRAPPER = "UNTRUSTED-MEMORY-DATA"


class Kind(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    DEAD_END = "dead_end"
    LINEAGE = "lineage"
    PLAYBOOK = "playbook"
    METHOD = "method"


class RelationType(StrEnum):
    COMPLEMENTS = "complements"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"


class Status(StrEnum):
    QUARANTINED = "quarantined"
    ACTIVE = "active"
    DEMOTED = "demoted"
    REJECTED = "rejected"
    REVOKED = "revoked"
    RETIRED = "retired"


class Relation(BaseModel):
    type: RelationType
    target: str


class Proposal(BaseModel):
    kind: Kind
    title: str
    text: str
    evidence: tuple[str, ...]
    data: dict[str, Any] = {}
    parents: tuple[str, ...] = ()
    relations: tuple[Relation, ...] = ()
    snapshot_ref: str | None = None


class MemoryRefused(ValueError):
    pass


class NotReadable(KeyError):
    pass


@dataclass
class ExploitVerdict:
    passed: bool
    nominal: float | None = None
    perturbed: float | None = None


class PerturbedEvaluatorCheck:
    def __init__(self, evaluator: Any, min_retained: float = 0.5):
        self.evaluator = evaluator

    def check(self, entry: Any) -> ExploitVerdict:
        return ExploitVerdict(True)


@dataclass
class MemoryPolicy:
    floors: Any = None


@dataclass
class GateResult:
    entry_id: str
    passed: bool
    status: Status
    tier: Tier | None
    reasons: tuple[str, ...] = ()


class MemoryStore:
    def __init__(self, kernel: Any, namespace: str = "global", policy: Any = None, exploit_check: Any = None):
        self.namespace = namespace
        self._e: dict[str, Any] = {}
        self._s: dict[str, Status] = {}

    def propose(self, p: Proposal, writer: str, secrets: Any = ()) -> str:
        eid = f"mem_{len(self._e)}"
        self._e[eid] = p
        self._s[eid] = Status.ACTIVE
        return eid

    def admit(self, eid: str) -> GateResult:
        return GateResult(eid, True, Status.ACTIVE, Tier.T2_STATISTICAL)

    def submit(self, p: Proposal, writer: str, secrets: Any = ()) -> GateResult:
        return self.admit(self.propose(p, writer, secrets))

    def status(self, eid: str) -> Status:
        return self._s[eid]

    def get(self, eid: str, reader: str, purpose: str = "") -> Any:
        return self._e[eid]

    def query(self, reader: str, include_demoted: bool = True) -> list:
        return list(self._e.values())


def cascade_closure(edges: Any, roots: Any) -> set:
    return set(roots)


def negative_proposal(kernel: Any, cert_hash: str) -> Any:
    return None


def harvest_negatives(store: Any) -> list:
    return []
