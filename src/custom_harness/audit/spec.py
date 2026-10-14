"""The audit spec (`chh-audit/1` YAML): a HUMAN's typed reading of one published claim (GOAL §16.10).

"Method X beats baseline Y on fresh data by at least delta" is written by a human as YAML: the paper (DOI, title,
the verbatim sentence audited), the two arms' code, ONE budget for both, the seeds, the fresh OpenML datasets (each
pinned by the sha256 of its file) and, optionally, the paper's citations. Nothing here is model-written, and
nothing is trusted until a human approves the spec as faithful to the paper (`chh audit approve-fidelity`, bound to
`spec_hash`).

Both arms run through the SAME contract wrapper (audit/wrapper.py) under one contract: the per-arm schema has no
budget, argv or data fields, so a spec cannot give one arm more (`extra="forbid"` refuses them).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..kernel import AnalysisPlan, ArtifactRef, Claim, ClaimKind, Effect, MetricRef, Risk, Scope, canonical_json
from ..verify import metrics as M
from . import wrapper

FORMAT = "chh-audit/1"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Paper(_Model):
    doi: str
    title: str
    sentence: str  # the verbatim sentence of the paper this audit reads as a typed claim
    datasets: tuple[str, ...] = ()  # where the paper reports it (informational; never an audit target)
    reported_effect: float | None = None  # the paper's own number, in metric units (shown, never checked)


class ClaimPart(_Model):
    metric: str
    min_effect: float  # delta, in metric units, positive = method better
    probability: float  # the auditor's declared P(criterion met); scored by the prediction ledger
    seeds: tuple[int, ...]
    alpha: float = 0.05
    n_boot: int = 2000
    resolve_by: date

    @field_validator("metric")
    @classmethod
    def _metric(cls, v: str) -> str:
        if v not in M.METRICS:
            raise ValueError(f"metric {v!r} is not registered (known: {sorted(M.METRICS)})")
        return v


class Arm(_Model):
    path: str  # python file defining build_model(seed) -> object with fit(X, y) and predict(X)


class Budget(_Model):
    timeout_s: float = Field(gt=0, le=24 * 3600)


class OpenMLDataset(_Model):
    openml_id: int = Field(gt=0)
    sha256: str  # pin: sha256 of the ARFF file bytes; a different file is refused
    target: str  # the label attribute
    licence: str  # as stated by OpenML for this dataset (recorded; redistribution is decided separately)

    @field_validator("sha256")
    @classmethod
    def _hex(cls, v: str) -> str:
        if len(v) != 64 or any(ch not in "0123456789abcdef" for ch in v):
            raise ValueError("sha256 must be 64 lowercase hex digits")
        return v

    @property
    def dataset_id(self) -> str:
        return f"openml-{self.openml_id}-{self.sha256[:16]}"

    @property
    def train_id(self) -> str:
        return f"{self.dataset_id}-train"


class Citation(_Model):
    key: str
    id: str | None = None
    title: str | None = None
    year: int | None = None
    authors: tuple[str, ...] = ()
    quotes: tuple[str, ...] = ()


class Redistribution(_Model):
    """Whether the arms' code and the datasets may be redistributed in a published bundle (a HUMAN reads the
    licences; default no: a hash-only package is published instead)."""

    code: bool = False
    data: bool = False


class AuditSpec(_Model):
    format: Literal["chh-audit/1"]
    paper: Paper
    claim: ClaimPart
    method: Arm
    baseline: Arm
    budget: Budget
    datasets: tuple[OpenMLDataset, ...]
    citations: tuple[Citation, ...] = ()
    redistribution: Redistribution = Field(default_factory=Redistribution)

    @model_validator(mode="after")
    def _distinct(self) -> AuditSpec:
        ids = [d.openml_id for d in self.datasets]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("datasets must be non-empty and distinct")
        paper = {s.strip().lower() for s in self.paper.datasets}
        reused = sorted(f"openml:{i}" for i in ids if f"openml:{i}" in paper)
        if reused:
            raise ValueError(f"{reused}: a dataset the paper reports on is not fresh data for its audit")
        return self


class LoadedSpec(BaseModel):
    """A validated spec with the arms' bytes (each arm composed with the shared wrapper) and its hash."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    spec: AuditSpec
    source: bytes
    method_code: bytes  # arm code + wrapper: the program the method arm runs
    baseline_code: bytes

    @property
    def spec_hash(self) -> str:
        """Binds the YAML's typed content AND the arms' exact code (an edit to either needs a new approval)."""
        doc = {"spec": self.spec.model_dump(mode="json"),
               "method": hashlib.sha256(self.method_code).hexdigest(),
               "baseline": hashlib.sha256(self.baseline_code).hexdigest()}
        return hashlib.sha256(canonical_json(doc)).hexdigest()

    @property
    def audit_id(self) -> str:
        return self.spec_hash[:16]

    def artifact_ids(self) -> tuple[str, str]:
        return f"audit-{self.audit_id}-method", f"audit-{self.audit_id}-baseline"

    def claim(self) -> Claim:
        """The typed Kernel claim this spec stands for (deterministic: equal specs give equal claim hashes)."""
        s, c = self.spec, self.spec.claim
        mid, bid = self.artifact_ids()
        targets = tuple(d.dataset_id for d in s.datasets)
        trains = tuple(d.train_id for d in s.datasets)
        reported = s.paper.reported_effect
        hi = max(c.min_effect, reported) if reported is not None else c.min_effect
        conditions = {"audit_id": self.audit_id, "paper_doi": s.paper.doi, "paper_title": s.paper.title,
                      "paper_sentence": s.paper.sentence, "paper_datasets": ", ".join(s.paper.datasets),
                      "paper_reported_effect": "" if reported is None else repr(reported),
                      "budget_timeout_s": repr(s.budget.timeout_s), "wrapper_sha256": wrapper.wrapper_hash(),
                      "protocol": f"{FORMAT} fresh OpenML data, identical contract for both arms",
                      "licences": "; ".join(f"openml:{d.openml_id}={d.licence}" for d in s.datasets)}
        return Claim(
            kind=ClaimKind.IMPROVEMENT, statement=f"[audit of {s.paper.doi}] {s.paper.sentence}",
            subject=ArtifactRef(artifact_id=mid, hash=hashlib.sha256(self.method_code).hexdigest()),
            comparator=ArtifactRef(artifact_id=bid, hash=hashlib.sha256(self.baseline_code).hexdigest()),
            metric=MetricRef(id=c.metric, higher_is_better=M.HIGHER_IS_BETTER[c.metric]),
            effect=Effect(min_effect=c.min_effect, predicted_lo=c.min_effect, predicted_hi=hi,
                          probability=c.probability),
            scope=Scope(datasets=(*trains, *targets), conditions=conditions),
            development=trains, generalization_targets=targets,
            analysis_plan=AnalysisPlan(seeds=c.seeds, alpha=c.alpha, n_boot=c.n_boot),
            assumptions=("the typed claim is a faithful reading of the paper's sentence (statement fidelity: a "
                         "human approval, untested)",
                         "the contract wrapper and the two arm files reproduce the paper's method and baseline "
                         "(untested)",
                         "the identical contract equalises data, seeds and time budget, not the tuning effort "
                         "inside them (untested)",
                         "fresh OpenML datasets may come from other populations than the paper's: NOT CONFIRMED can "
                         "mean a narrower scope than claimed, not a wrong result"),
            risk=Risk(),
            resolve_by=datetime.combine(c.resolve_by, time(0, 0), tzinfo=UTC).timestamp())


class SpecError(ValueError):
    pass


def load_spec(path: Path) -> LoadedSpec:
    """Parse and validate a chh-audit/1 YAML file; arm paths are relative to the YAML's folder."""
    path = Path(path)
    source = path.read_bytes()
    try:
        doc: Any = yaml.safe_load(source.decode("utf-8"))
        spec = AuditSpec.model_validate(doc)
    except (yaml.YAMLError, UnicodeDecodeError, ValueError) as e:
        raise SpecError(f"{path.name}: not a valid {FORMAT} spec: {e}") from e
    base = path.parent
    try:
        method = (base / spec.method.path).read_bytes()
        baseline = (base / spec.baseline.path).read_bytes()
    except OSError as e:
        raise SpecError(f"arm code unreadable: {e}") from e
    if method == baseline:
        raise SpecError("method and baseline are the same code")
    return LoadedSpec(spec=spec, source=source, method_code=wrapper.compose(method),
                      baseline_code=wrapper.compose(baseline))
