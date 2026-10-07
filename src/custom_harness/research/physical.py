"""The physical / empirical claim path, up to the Kernel boundary (GOAL §4.7, invariants 14-15).

    draft -> screen (human-owned prohibited list) -> prereg (protocol artifact + claim, BEFORE any approval)
          -> protocol_review (approval node: a human approves the exact protocol + claim)
          -> human executes, then `chh return-data` registers the attested data (refused unless the protocol was
             approved; columns, types and replicates checked against the protocol)
          -> data_received (approval node: a human confirms receipt of exactly that dataset)
          -> check_returned (fails closed if no data was registered) -> certify (Kernel)

The system never certifies a physical claim as done: the data enter only through the human CLI path, and the
attestation is an UNTESTED assumption on every report. No physical adapter is approved today, so the Kernel refuses
at K1 (fail closed); the certificate is computed but NOT recorded, so the pre-registered prediction stays pending
instead of being scored as a miss for a missing verifier. Nodes using these modules are human-only for the evolver
(evolve/edits.py).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from ..kernel import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    Effect,
    EvidenceClass,
    Kernel,
    MetricRef,
    Scope,
    canonical_json,
)
from ..runtime import register
from ..runtime.modules import ModuleContext

PROHIBITED_FILE = Path(__file__).resolve().parents[1] / "policy_prohibited.yaml"


class _M(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ClaimDraft(_M):
    statement: str
    kind: Literal["improvement"]
    treatment: str
    control: str
    metric: dict[str, Any]
    min_effect: float
    predicted_lo: float
    predicted_hi: float
    probability: float
    resolve_by: float
    assumptions: tuple[str, ...] = ()


class Column(_M):
    name: str
    type: Literal["text", "number"]
    unit: str | None = None


class Measurements(_M):
    format: Literal["csv"]
    filename: str
    columns: tuple[Column, ...]


class Analysis(_M):
    arm_column: str
    outcome_column: str
    treatment_label: str
    control_label: str
    test: str
    alpha: float
    min_replicates_per_arm: int
    plan: str


class Protocol(_M):
    schema_version: Literal[1]
    title: str
    claim: ClaimDraft
    procedure: str
    measurements: Measurements
    analysis: Analysis
    safety_notes: str


def protocol_hash(protocol: Protocol) -> str:
    return hashlib.sha256(canonical_json(protocol.model_dump(mode="json"))).hexdigest()


def returned_dataset_id(p_hash: str) -> str:
    return f"returned@{p_hash[:16]}"


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def _strings(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _strings(v)]
    return []


def screen(protocol: Protocol, policy_file: Path = PROHIBITED_FILE) -> list[str]:
    """Ids of the prohibited categories any protocol text matches (a coarse first line; the human is the screen)."""
    policy = yaml.safe_load(policy_file.read_text(encoding="utf-8"))
    text = " " + _normalise(" ".join(_strings(protocol.model_dump(mode="json")))) + " "
    return [c["id"] for c in policy["categories"] if any(re.search(p, text) for p in c["patterns"])]


def check_measurements(protocol: Protocol, data: bytes) -> list[str]:
    """Problems with returned data against the protocol: header, types, finite numbers, replicates per arm."""
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    except UnicodeDecodeError:
        return ["data is not UTF-8 text"]
    want = [c.name for c in protocol.measurements.columns]
    if not rows or rows[0] != want:
        return [f"columns {rows[0] if rows else []} differ from the protocol's {want}"]
    problems = []
    body = [r for r in rows[1:] if r]
    for i, r in enumerate(body, start=2):
        if len(r) != len(want):
            problems.append(f"line {i}: {len(r)} values for {len(want)} columns")
            continue
        for col, v in zip(protocol.measurements.columns, r, strict=True):
            if col.type == "number":
                try:
                    ok = math.isfinite(float(v))
                except ValueError:
                    ok = False
                if not ok:
                    problems.append(f"line {i}: {col.name}={v!r} is not a finite number")
    a = protocol.analysis
    ai = want.index(a.arm_column)
    for label in (a.treatment_label, a.control_label):
        n = sum(1 for r in body if len(r) == len(want) and r[ai] == label)
        if n < a.min_replicates_per_arm:
            problems.append(f"arm {label!r}: {n} replicates < the protocol's minimum {a.min_replicates_per_arm}")
    return problems


@register("physical.draft")
def draft(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    doc = yaml.safe_load(Path(state["input"]["protocol_file"]).read_text(encoding="utf-8"))
    return {"physical_protocol": Protocol.model_validate(doc).model_dump(mode="json")}


@register("physical.screen")
def screen_module(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    hits = screen(Protocol.model_validate(state["physical_protocol"]))
    return {"physical_screen": {"refused": bool(hits), "categories": hits}}


@register("physical.prereg")
def prereg(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    protocol = Protocol.model_validate(state["physical_protocol"])
    if screen(protocol):  # defence in depth: the screen node must have run and passed
        raise RuntimeError("protocol matches a prohibited category; it cannot be pre-registered")
    kernel = Kernel(ctx.store)
    h = protocol_hash(protocol)
    kernel.vault.register_artifact(f"protocol@{h[:16]}", canonical_json(protocol.model_dump(mode="json")), "protocol")
    d = protocol.claim
    arms = {}
    for role, text in (("treatment", d.treatment), ("control", d.control)):
        rec = kernel.vault.register_artifact(f"protocol@{h[:16]}/{role}", text.encode("utf-8"), "physical_arm")
        arms[role] = ArtifactRef(artifact_id=rec.artifact_id, hash=rec.hash)
    target = returned_dataset_id(h)
    claim = Claim(
        kind=ClaimKind.IMPROVEMENT, statement=d.statement, subject=arms["treatment"], comparator=arms["control"],
        metric=MetricRef(id=str(d.metric["id"]), higher_is_better=bool(d.metric["higher_is_better"])),
        effect=Effect(min_effect=d.min_effect, predicted_lo=d.predicted_lo, predicted_hi=d.predicted_hi,
                      probability=d.probability),
        scope=Scope(datasets=(target,), conditions={"protocol_hash": h}), development=(),
        generalization_targets=(target,),
        analysis_plan=AnalysisPlan(seeds=(0,), alpha=protocol.analysis.alpha),
        assumptions=(*d.assumptions, "the returned measurements were produced by the approved protocol "
                                     "(human attestation; untested by the system)"),
        resolve_by=d.resolve_by, evidence_class=EvidenceClass.PHYSICAL)
    kernel.preregister(claim)
    item = {"protocol_hash": h, "claim_hash": claim.claim_hash(), "statement": d.statement,
            "protocol": protocol.model_dump(mode="json")}
    return {"physical_claim": claim.model_dump(mode="json"), "physical_item": {**item, "dataset_id": target},
            "physical_data_item": {"protocol_hash": h, "dataset_id": target}}


def returned_dataset(ctx_store: Any, state: dict[str, Any]) -> Any:
    ds = Kernel(ctx_store).vault.datasets().get(state["physical_item"]["dataset_id"])
    return ds


@register("physical.check_returned")
def check_returned(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    ds = returned_dataset(ctx.store, state)
    if ds is None:  # a human confirmed receipt, but nothing came in through `chh return-data`
        raise RuntimeError("no returned data registered for this protocol (use `chh return-data` before confirming)")
    prov = json.loads(Kernel(ctx.store).vault.read_public(ds.dataset_id, "provenance.json"))
    if prov.get("protocol_hash") != state["physical_item"]["protocol_hash"]:
        raise RuntimeError("returned data belong to another protocol")
    return {"physical_returned": {"dataset_id": ds.dataset_id, "attestation": prov["attestation"],
                                  "data_sha256": prov["data_sha256"]}}


@register("physical.certify")
def certify(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    claim = Claim.model_validate(state["physical_claim"])
    cert = Kernel(ctx.store).certify(claim, [], record=False)  # not recorded: see the module docstring
    attestation = state["physical_returned"]["attestation"]
    return {"physical_certificate": {
        "label": cert.label.value, "failed": cert.failed(), "k1_reasons": list(cert.checks["K1"].reasons),
        "recorded": False,
        "untested_assumptions": [f"data provenance attested by: {attestation}",
                                 *(a.text for a in cert.assumptions if a.tested_by is None)]}}


@register("physical.refused")
def refused(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    scr = state.get("physical_screen") or {}
    why = (f"prohibited categories: {scr.get('categories')}" if scr.get("refused")
           else "the protocol was rejected at human review")
    return {"physical_outcome": {"refused": True, "reason": why}}
