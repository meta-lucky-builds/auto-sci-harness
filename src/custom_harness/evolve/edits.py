"""Typed harness edits (PLAN L6). Edits are JSON-pointer operations on the HarnessSpec, with a
declared edit type, a rationale citing trace evidence, and a predicted effect (for calibration, §8.5).

Protected surfaces (verifier wiring, integrity audit, task setup) cannot be edited automatically:
changing verifiers is a human-only action under the tier policy (PLAN §5).
"""

from __future__ import annotations

import copy
import json
import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..runtime.spec import HarnessSpec


class EditType(StrEnum):
    CONTRACT = "contract"
    HOP = "hop"
    CONTROL_FLOW = "control_flow"
    CONTEXT_POLICY = "context_policy"
    TOOL = "tool"
    SUBAGENT_ROLE = "subagent_role"
    MEMORY_POLICY = "memory_policy"
    PROMPT_TEXT = "prompt_text"
    BUDGET_POLICY = "budget_policy"
    ROUTING = "routing"
    PARAMS = "params"


class PredictedEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    failure_clusters_fixed: list[str] = Field(default_factory=list)
    expected_delta: float = 0.0  # predicted change of the objective on the evolve split
    tasks_at_risk: list[str] = Field(default_factory=list)


class EditOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["replace", "add", "remove"]
    path: str  # JSON pointer into the spec, e.g. /nodes/implement/prompt
    value: Any = None


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: EditType
    ops: list[EditOp]
    rationale: str
    evidence: list[str] = Field(default_factory=list)  # trace refs / failure-cluster ids
    predicted: PredictedEffect = Field(default_factory=PredictedEffect)


class EditRejected(ValueError):
    pass


# Task setup, the Kernel-integrated end of the loop and the integrity audit are human-only surfaces.
DEFAULT_PROTECTED_NODES = ("setup", "claim", "confirm", "certify", "replicate", "replica_dev", "replica_claim",
                           "replica_confirm", "replica_certify", "report", "audit")
# State keys that only trusted code may write: an added node writing one could fake a claim, a candidate,
# a certificate or an audit result (state that scoring and reporting depend on).
RESERVED_OUTPUT_KEYS = frozenset({"task", "datasets", "claim", "claim_hash", "prereg_seq", "evidence_ids",
                                  "confirm_failures",
                                  "certificate", "checklist", "publishable", "publish_blockers", "report",
                                  "report_ref", "audit", "best", "attempts", "n_attempts", "baseline_dev",
                                  "baseline_dev_preds_ref", "dev_score", "route", "input", "best_code",
                                  "n_improves", "critique", "replication_brief", "replica_impl", "replica_best",
                                  "replica_claim", "replica_claim_hash", "replica_prereg_seq",
                                  "replica_evidence_ids", "replica_confirm_failures", "replica_certificate",
                                  "replica_checklist", "replica_publishable", "replica_publish_blockers"})
_LEAK_PATTERNS = [re.compile(p, re.I) for p in (r"_private", r"test_y", r"/private/", r"\bprivate[/\\]")]


def _split(path: str) -> list[str]:
    if not path.startswith("/"):
        raise EditRejected(f"path must be a JSON pointer: {path}")
    return [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]


def _apply_op(doc: Any, op: EditOp) -> None:
    parts = _split(op.path)
    parent = doc
    for p in parts[:-1]:
        if isinstance(parent, list):
            parent = parent[int(p)]
        elif isinstance(parent, dict):
            if p not in parent:
                if op.op == "add":
                    parent[p] = {}
                else:
                    raise EditRejected(f"path not found: {op.path}")
            parent = parent[p]
        else:
            raise EditRejected(f"cannot descend into {type(parent).__name__} at {op.path}")
    last = parts[-1]
    if isinstance(parent, list):
        if op.op == "add":
            idx = len(parent) if last == "-" else int(last)
            parent.insert(idx, op.value)
        elif op.op == "replace":
            parent[int(last)] = op.value
        else:
            del parent[int(last)]
    elif isinstance(parent, dict):
        if op.op in ("replace", "remove") and last not in parent:
            raise EditRejected(f"path not found: {op.path}")
        if op.op == "remove":
            del parent[last]
        else:
            parent[last] = op.value
    else:
        raise EditRejected(f"cannot edit inside {type(parent).__name__} at {op.path}")


def check_edit(edit: Edit, spec: HarnessSpec, protected_nodes: tuple[str, ...] = DEFAULT_PROTECTED_NODES) -> None:
    """Static safety checks before any evaluation is spent."""
    blob = json.dumps([o.model_dump() for o in edit.ops], default=str)
    for pat in _LEAK_PATTERNS:
        if pat.search(blob):
            raise EditRejected(f"edit references private data ({pat.pattern})")
    for o in edit.ops:
        parts = _split(o.path)
        if parts[0] == "nodes" and len(parts) > 1 and parts[1] in protected_nodes:
            raise EditRejected(f"node '{parts[1]}' is protected (verifier/audit/setup changes are human-only)")
        if parts[0] == "edges":
            # an edge edit must not disconnect protected nodes: checked after application
            pass
        if parts[0] in ("id",):
            raise EditRejected("harness id is not editable")


def apply_edit(spec: HarnessSpec, edit: Edit,
               protected_nodes: tuple[str, ...] = DEFAULT_PROTECTED_NODES,
               terminal_required: tuple[str, ...] = ("audit",)) -> HarnessSpec:
    """Apply an edit, or raise EditRejected. Invariants: protected nodes unchanged and reachable;
    the integrity audit is the only node where a run can end (no edit may bypass it)."""
    check_edit(edit, spec, protected_nodes)
    doc = copy.deepcopy(spec.model_dump(mode="json"))
    for op in edit.ops:
        _apply_op(doc, op)
    try:
        new = HarnessSpec.model_validate(doc)
    except ValidationError as e:
        raise EditRejected(f"edited spec is invalid: {e.errors()[:3]}") from e
    # protected nodes must stay present, unchanged and reachable
    for nid in protected_nodes:
        if nid in spec.nodes:
            if nid not in new.nodes or new.nodes[nid] != spec.nodes[nid]:
                raise EditRejected(f"protected node '{nid}' was changed or removed")
            if nid != new.entry and not any(e.target == nid for e in new.edges):
                raise EditRejected(f"protected node '{nid}' became unreachable")
    if spec.entry in protected_nodes and new.entry != spec.entry:
        raise EditRejected("entry must remain the protected setup node")
    protected_modules = {getattr(spec.nodes[n], "module", None) for n in protected_nodes if n in spec.nodes} - {None}
    for nid, node in new.nodes.items():
        if nid in spec.nodes and node == spec.nodes[nid]:
            continue  # unchanged nodes were already accepted
        key = getattr(node, "output_key", None)
        if key is not None and (key in RESERVED_OUTPUT_KEYS or key.startswith("_")):
            raise EditRejected(f"node '{nid}' would write reserved state key '{key}'")
        if getattr(node, "module", None) in protected_modules:
            raise EditRejected(f"node '{nid}' reuses a protected module outside its protected node")
    for nid in terminal_required:
        if nid in spec.nodes:
            enders = terminal_nodes(new)
            if enders != {nid}:
                raise EditRejected(f"runs must end only at '{nid}'; edited spec can end at {sorted(enders)}")
    return new


def terminal_nodes(spec: HarnessSpec) -> set[str]:
    """Nodes where a run can end: no outgoing edges, or an edge to END."""
    out = set()
    for nid in spec.nodes:
        edges = spec.outgoing(nid)
        if not edges or any(e.target == "END" for e in edges):
            out.add(nid)
    return out
