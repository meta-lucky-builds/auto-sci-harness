"""L5 memory data model (GOAL §5 L5): one entry type for memory, skills and playbooks.

A writer submits a `Proposal`. It cannot choose the entry id, status or tier: the id is the hash of the
proposal's content, the status comes only from the gate, and the tier is derived from Kernel certificates.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..kernel.claim import canonical_json
from ..tiers import DEFAULT_POLICY, Action, Tier

HEX64 = r"^[0-9a-f]{64}$"
ENTRY_ID = r"^mem_[0-9a-f]{64}$"
CertHash = Annotated[str, StringConstraints(pattern=HEX64)]
EntryId = Annotated[str, StringConstraints(pattern=ENTRY_ID)]


class Kind(StrEnum):
    POSITIVE = "positive"  # a certified finding
    NEGATIVE = "negative"  # a verified failure (generated automatically from Kernel rejections on K6 alone)
    DEAD_END = "dead_end"  # a verified failure with the workspace snapshot it ended in
    LINEAGE = "lineage"  # child artifact derived from parent artifact; the child is a certified claim's subject
    PLAYBOOK = "playbook"  # a skill: how to do something; maturity stats from Kernel outcomes
    METHOD = "method"  # method library entry (category path, MM-Agent style hierarchy) with outcome stats


SKILL_KINDS = frozenset({Kind.PLAYBOOK, Kind.METHOD})  # actionable entries: they also need the exploit check
FAILURE_KINDS = frozenset({Kind.NEGATIVE, Kind.DEAD_END})  # backed by REJECTED certificates (verified failures)


class RelationType(StrEnum):
    COMPLEMENTS = "complements"
    CONTRADICTS = "contradicts"  # the weaker-tier side is demoted; both on a tie
    SUPERSEDES = "supersedes"  # the target (same kind, no stronger tier) is demoted


class Status(StrEnum):
    QUARANTINED = "quarantined"  # proposed, not yet through the gate: unreadable
    ACTIVE = "active"
    DEMOTED = "demoted"  # superseded or contradicted: readable, ranked last and marked
    REJECTED = "rejected"  # failed the gate: unreadable, terminal
    REVOKED = "revoked"  # a parent (certificate, claim or entry) was revoked: unreadable, terminal
    RETIRED = "retired"  # unused, retired by a human: unreadable


READABLE = frozenset({Status.ACTIVE, Status.DEMOTED})
TERMINAL = frozenset({Status.REJECTED, Status.REVOKED})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Relation(_Frozen):
    type: RelationType
    target: str = Field(pattern=ENTRY_ID)


class Proposal(_Frozen):
    """What a (trusted) writer submits. Evidence = Kernel certificate hashes; parents = entry ids it derives from."""

    kind: Kind
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=4000)
    evidence: tuple[CertHash, ...] = Field(min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
    parents: tuple[EntryId, ...] = Field(default=(), max_length=64)
    relations: tuple[Relation, ...] = Field(default=(), max_length=64)
    snapshot_ref: str | None = Field(default=None, pattern=HEX64)

    def entry_id(self) -> str:
        return "mem_" + hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


class Entry(_Frozen):
    id: str
    writer: str
    proposal: Proposal

    # convenience views
    @property
    def kind(self) -> Kind:
        return self.proposal.kind

    @property
    def title(self) -> str:
        return self.proposal.title

    @property
    def text(self) -> str:
        return self.proposal.text

    @property
    def data(self) -> dict[str, Any]:
        return self.proposal.data

    @property
    def evidence(self) -> tuple[str, ...]:
        return self.proposal.evidence

    @property
    def parents(self) -> tuple[str, ...]:
        return self.proposal.parents

    @property
    def relations(self) -> tuple[Relation, ...]:
        return self.proposal.relations

    @property
    def snapshot_ref(self) -> str | None:
        return self.proposal.snapshot_ref


@dataclass(frozen=True)
class GateResult:
    entry_id: str
    passed: bool
    status: Status
    tier: Tier | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Maturity:
    successes: int
    failures: int

    @property
    def level(self) -> str:
        n = self.successes + self.failures
        if n < 3:
            return "nascent"
        rate = self.successes / n
        return "proven" if rate >= 0.7 else "failing" if rate <= 0.3 else "mixed"


def default_floors() -> dict[Kind, Tier]:
    """From the human-owned tier table (tiers.DEFAULT_POLICY): findings at the publish-verified floor, skills at the
    admit-skill floor. A kind whose floor is None (never automatic) is left out, so nothing of it is admitted."""
    floors: dict[Kind, Tier] = {}
    for kind in Kind:
        floor = DEFAULT_POLICY[Action.ADMIT_SKILL if kind in SKILL_KINDS else Action.PUBLISH_VERIFIED]
        if floor is not None:
            floors[kind] = floor
    return floors


@dataclass(frozen=True)
class MemoryPolicy:
    """Set by trusted code when the store is built. A kind with no floor admits nothing (fail closed)."""

    floors: Mapping[Kind, Tier] = field(default_factory=default_floors)


class MemoryRefused(ValueError):
    """A write refused before anything was stored (private data, canary, injection, malformed). The message
    names the category of the problem, never the offending value."""


class NotReadable(KeyError):
    """The entry does not exist, is not readable (quarantined, rejected, revoked, retired) or the log is broken."""
