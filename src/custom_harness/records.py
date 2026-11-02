"""Core data model (PLAN §3). All records are immutable once written to the Evidence store."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from .tiers import Tier


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def content_hash(obj: Any) -> str:
    data = obj if isinstance(obj, bytes) else json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(data).hexdigest()


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AccessMode(StrEnum):
    OPEN_WEIGHTS = "ow"
    API_KEY = "api"
    SUBSCRIPTION = "byos"
    MOCK = "mock"  # tests only; never valid for scorecard runs


class Cost(Frozen):
    """Provider-neutral cost units (PLAN §14.4).

    On a TraceStep, wall_s is the measured elapsed time of the step's successful (or final policy-failed)
    attempt; time lost to infrastructure failures is not charged (GOAL L1)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0
    gpu_hours: float = 0.0
    quota_units: float = 0.0
    wall_s: float = 0.0
    # True when a number is an estimate rather than reported by the backend.
    estimated: bool = False

    def __add__(self, other: Cost) -> Cost:
        return Cost(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            usd=self.usd + other.usd,
            gpu_hours=self.gpu_hours + other.gpu_hours,
            quota_units=self.quota_units + other.quota_units,
            wall_s=self.wall_s + other.wall_s,
            estimated=self.estimated or other.estimated,
        )


class Provenance(Frozen):
    """Which backend produced an output; drives the L8 training filter (PLAN §14.3)."""

    backend: str
    provider: str
    model: str
    mode: AccessMode
    training_on_outputs: str  # "allowed" | "forbidden" | "unknown"
    # True when the output was replayed from a cassette (gateway/cassette.py), not produced by a model. Replayed
    # output is never evidence (GOAL §10 invariant 21): the Kernel refuses it. Serialised ONLY when True, so every
    # provenance stored before the field existed keeps its bytes and hashes.
    replayed: bool = False

    @model_serializer(mode="wrap")
    def _omit_unset_replayed(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        doc: dict[str, Any] = handler(self)
        if not doc.get("replayed"):
            doc.pop("replayed", None)
        return doc


class StepKind(StrEnum):
    LLM = "llm"
    TOOL = "tool"
    MODULE = "module"
    CONTROL = "control"
    APPROVAL = "approval"


class TraceStep(Frozen):
    run_id: str
    index: int
    node_id: str
    kind: StepKind
    input: dict[str, Any]
    output: dict[str, Any]
    error: str | None = None
    provenance: Provenance | None = None
    cost: Cost = Cost()
    started_at: float
    ended_at: float
    checkpoint_ref: str | None = None
    # ABLATED marker (custom_harness/ablation.py): the switches the step's run was ablated with. Stamped by the store
    # from the run record, never by the writer. Serialised ONLY when non-empty, so every production step keeps its
    # bytes and hashes.
    ablated: tuple[str, ...] = ()

    @model_serializer(mode="wrap")
    def _omit_unablated(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        doc: dict[str, Any] = handler(self)
        if not doc.get("ablated"):
            doc.pop("ablated", None)
        return doc


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    STEP_LIMIT = "step_limit"
    # The environment failed (sandbox daemon, provider outage) after retries: the run says nothing about
    # the harness, so evaluation excludes it instead of scoring it (errors.py, CR P0-1).
    INFRA_FAILED = "infra_failed"
    # Waiting at an approval node for a human decision (`chh approve`, then `chh resume`). Neither completed nor
    # failed: evaluation scores it 0 (no credit without the human step), it is not an infrastructure failure.
    PAUSED = "paused"


class Run(Frozen):
    id: str = Field(default_factory=lambda: new_id("run"))
    harness_id: str
    harness_hash: str
    task_id: str
    seed: int = 0
    started_at: float = Field(default_factory=time.time)
    # what `resume` needs to continue a run in a new process (None on runs recorded before M0)
    inputs_ref: str | None = None  # blob of the initial state
    workspace: str | None = None  # sandbox workspace path on this host
    forked_from: tuple[str, int] | None = None
    # ABLATED marker (custom_harness/ablation.py): the capability switches this run was run with switched OFF. Only
    # `Engine(ablation=...)` sets it, and only the scorecard runner's ablation entry point can make a non-empty set.
    # Nothing an ablated run produces is certified, persisted or accepted. Serialised ONLY when non-empty, so every
    # production run keeps its bytes and hashes.
    ablated: tuple[str, ...] = ()

    @model_serializer(mode="wrap")
    def _omit_unablated(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        doc: dict[str, Any] = handler(self)
        if not doc.get("ablated"):
            doc.pop("ablated", None)
        return doc


class EvidenceKind(StrEnum):
    LOG = "log"
    METRIC = "metric"
    TEST = "test"
    PROOF = "proof"
    STAT = "stat"
    CITATION = "citation"
    PHYSICAL = "physical"
    JUDGMENT = "judgment"


class Evidence(Frozen):
    id: str = Field(default_factory=lambda: new_id("ev"))
    kind: EvidenceKind
    tier: Tier
    verifier_id: str
    produced_by_run: str | None
    payload_ref: str  # blob hash
    summary: str = ""
    created_at: float = Field(default_factory=time.time)


class ClaimStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    PUBLISHED = "published"
    RETRACTED = "retracted"


class Claim(Frozen):
    id: str = Field(default_factory=lambda: new_id("cl"))
    text: str
    evidence_ids: tuple[str, ...]
    created_at: float = Field(default_factory=time.time)
