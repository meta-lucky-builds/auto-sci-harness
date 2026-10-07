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

from ..runtime.spec import HarnessSpec, LLMNode, ToolNode
from ..runtime.template import references


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
RESERVED_OUTPUT_KEYS = frozenset({"task", "datasets", "dev_folds", "dev_scheme", "profile", "replication_refused",
                                  "model_policy",
                                  "claim", "claim_hash", "prereg_seq", "evidence_ids",
                                  "confirm_failures",
                                  "certificate", "checklist", "publishable", "publish_blockers", "report",
                                  "report_ref", "audit", "best", "attempts", "n_attempts", "baseline_dev",
                                  "baseline_dev_preds_ref", "dev_score", "route", "input", "best_code",
                                  "n_improves", "critique", "replication_brief", "replica_impl", "replica_best",
                                  "replica_claim", "replica_claim_hash", "replica_prereg_seq",
                                  "replica_evidence_ids", "replica_confirm_failures", "replica_certificate",
                                  "replica_checklist", "replica_publishable", "replica_publish_blockers",
                                  # audit I3 inputs (research/claims.py: citation_audit)
                                  "citations", "literature_snapshot",
                                  # audit I6 input (research/claims.py: _figure_audit)
                                  "figures"})
# Approval-node outcomes (`<node_id>_approval`, `<node_id>_decision`) are written only by the engine; node ids vary,
# so any key with these suffixes is reserved. (The engine also discards such values before an approval node runs.)
RESERVED_OUTPUT_SUFFIXES = ("_approval", "_decision")
# Modules whose outputs include reserved keys (kept complete by test_reserved_writing_modules_are_declared, which
# runs the seed harness and checks every module step's output keys). Existing nodes may keep using them; an edit
# may not ADD a node with one of them, nor switch a node to one.
RESERVED_WRITING_MODULES = frozenset({
    "research.setup", "research.profile_data", "research.baseline_dev", "research.socratic_filter",
    "research.next_hypothesis", "research.run_candidate", "research.claim", "research.confirm", "research.certify",
    "research.replica_dev", "research.report", "research.audit"})
# protected nodes the evolvable loop legitimately routes to (when to claim; when to give up and report)
OPEN_PROTECTED_TARGETS = ("claim", "report")


HUMAN_ONLY_MODULE_PREFIXES = ("physical.",)  # the physical-claim path (GOAL §4.7): screen, prereg, data checks

# Invariant 5 (policy.FAMILY_SEPARATION) is checked on role -> backend bindings, so WHICH node runs under which role
# is part of the policy, not an evolution surface (R2-1): an edit may not change the role of an existing LLM node,
# and a new LLM node may only run as an AUTHOR role. A judging/replicating role (judge, replicator, or any role an
# edit invents) is human-only; so are the outputs its nodes write (no other node may write them, e.g. a proposer-
# backed node writing the critic's `critique_raw`).
EVOLVABLE_LLM_ROLES = ("proposer", "executor")
# State a NEW or CHANGED template (LLM prompt/system, tool command) may not render (R2-5, generalised):
#   critique_raw  the critic's unfiltered output; prompts get only `critique` (questions kept by socratic_filter)
#   input         run inputs (host paths such as task_dir, whose private/ directory holds hidden labels)
#   _*            engine-internal and private state (_private*, _last_error, _module_llm_calls, _seed, ...)
# plus every key a tool node writes (taint rule, see TOOL_OUTPUT_PREFIX).
UNRENDERABLE_KEYS = frozenset({"critique_raw", "input"})
# A tool node runs a command in the run's workspace, which holds the public TRAINING file (and whatever else the
# sandbox can read). Its output is therefore tainted: a tool node added or changed by an edit must write a fresh
# key with this prefix (no module reads such keys), and no LLM prompt may render any tool-written key, so tool
# output can route control flow (edge conditions) but never reach a model (known limitation "a tool node could put
# training rows into a prompt").
TOOL_OUTPUT_PREFIX = "tool_"
# Domain adapters are not an edit surface (GOAL §5 "Domain adapters", invariant 13): an edit whose ops name or
# reference one is rejected before anything else. Matched in op paths, dict keys and string values (lower-cased).
_ADAPTER_WORDS = re.compile(r"adapters?\b|evidence[_ ]?class|approve[_-]adapter")


def _approval_nodes(spec: HarnessSpec) -> set[str]:
    """Human-only nodes: approval nodes and nodes of the physical-claim path; none may be added, removed, changed or
    re-wired by an edit."""
    return {nid for nid, node in spec.nodes.items()
            if node.kind == "approval" or str(getattr(node, "module", "")).startswith(HUMAN_ONLY_MODULE_PREFIXES)}


def _edge_set(edges: list[dict[str, Any]], target: str) -> list[str]:
    return sorted(json.dumps(e, sort_keys=True) for e in edges if e["target"] == target)


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


def _adapter_tokens() -> tuple[frozenset[str], tuple[str, ...]]:
    """(adapter names, substrings naming adapter code) from the adapter registry itself, so a new adapter is
    covered without touching this file."""
    from ..kernel.adapters import REGISTRY
    names = {n.lower() for n in REGISTRY} | {s.name.lower() for s, _ in REGISTRY.values()}
    subs = {"custom_harness.kernel", "custom_harness/kernel", "kernel.adapters", "kernel/adapters"}
    for spec, _ in REGISTRY.values():
        for mod in (*spec.modules, *([spec.exact_module] if spec.exact_module else [])):
            subs |= {mod.lower(), mod.lower().replace(".", "/") + ".py"}
    return frozenset(names), tuple(sorted(subs))


def _strings_and_keys(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in (str(k), *_strings_and_keys(v))]
    if isinstance(obj, (list, tuple)):
        return [s for v in obj for s in _strings_and_keys(v)]
    return [str(obj)] if obj is not None else []


def adapter_references(edit: Edit) -> list[str]:
    """Why an edit names or references a domain adapter (empty = it does not): adapter ids (an op path segment, a
    key or a whole string value equal to a registered adapter name), adapter or Kernel module paths, the words
    adapter(s)/evidence_class anywhere in a path, key or value. Conservative on purpose: a prompt merely mentioning
    "adapter" is rejected too (fail closed; adapters are human-only, GOAL invariant 13)."""
    names, subs = _adapter_tokens()
    found: list[str] = []
    for o in edit.ops:
        segments = [p.lower() for p in _split(o.path)]
        for text in [o.path.lower(), *(s.lower() for s in _strings_and_keys(o.value))]:
            if _ADAPTER_WORDS.search(text):
                found.append(f"{o.path}: mentions an adapter or an evidence class")
            if any(s in text.replace("\\", "/") for s in subs):
                found.append(f"{o.path}: names adapter/Kernel code")
            if text.strip() in names:
                found.append(f"{o.path}: names adapter {text.strip()!r}")
        if any(seg in names for seg in segments):
            found.append(f"{o.path}: path names an adapter")
    return list(dict.fromkeys(found))


def check_edit(edit: Edit, spec: HarnessSpec, protected_nodes: tuple[str, ...] = DEFAULT_PROTECTED_NODES) -> None:
    """Static safety checks before any evaluation is spent."""
    refs = adapter_references(edit)
    if refs:  # GOAL §5 "Domain adapters": an edit that touches an adapter is rejected statically
        raise EditRejected("domain adapters are human-only, not an edit surface: " + "; ".join(refs[:3]))
    blob = json.dumps([o.model_dump() for o in edit.ops], default=str)
    for pat in _LEAK_PATTERNS:
        if pat.search(blob):
            raise EditRejected(f"edit references private data ({pat.pattern})")
    for o in edit.ops:
        parts = _split(o.path)
        if parts[0] == "nodes" and len(parts) > 1 and parts[1] in protected_nodes:
            raise EditRejected(f"node '{parts[1]}' is protected (verifier/audit/setup changes are human-only)")
        if parts[0] == "nodes" and len(parts) > 1 and parts[1] in _approval_nodes(spec):
            raise EditRejected(f"approval node '{parts[1]}' is human-only")
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
        try:
            _apply_op(doc, op)
        except EditRejected:
            raise
        except (ValueError, IndexError, TypeError, KeyError) as e:  # fail closed: a malformed edit is rejected
            raise EditRejected(f"cannot apply {op.op} at {op.path}: {type(e).__name__}: {e}") from e
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
    # protected wiring: edges out of a protected node, and into it (except the two protected nodes the evolvable
    # loop routes to: claim and report), are human-only, so no evolvable node can sit between protected steps
    old_edges, new_edges = spec.model_dump(mode="json")["edges"], new.model_dump(mode="json")["edges"]
    for nid in protected_nodes:
        if nid not in spec.nodes:
            continue
        if [e for e in new_edges if e["source"] == nid] != [e for e in old_edges if e["source"] == nid]:
            raise EditRejected(f"edges out of protected node '{nid}' are human-only")
        if nid not in OPEN_PROTECTED_TARGETS and _edge_set(new_edges, nid) != _edge_set(old_edges, nid):
            raise EditRejected(f"edges into protected node '{nid}' are human-only")
    # approval nodes are human-only surfaces (invariant 15): none added, removed or changed, and their wiring (in and
    # out) fixed, so an edit can neither bypass a human decision nor change what it routes to
    old_appr, new_appr = _approval_nodes(spec), _approval_nodes(new)
    if new_appr != old_appr:
        raise EditRejected(f"approval nodes are human-only: {sorted(old_appr ^ new_appr)} added or removed")
    for nid in old_appr:
        if new.nodes[nid] != spec.nodes[nid]:
            raise EditRejected(f"approval node '{nid}' is human-only")
        if [e for e in new_edges if e["source"] == nid] != [e for e in old_edges if e["source"] == nid]:
            raise EditRejected(f"edges out of approval node '{nid}' are human-only")
        if _edge_set(new_edges, nid) != _edge_set(old_edges, nid):
            raise EditRejected(f"edges into approval node '{nid}' are human-only")
    protected_modules = {getattr(spec.nodes[n], "module", None) for n in protected_nodes if n in spec.nodes} - {None}
    for nid, node in new.nodes.items():
        if nid in spec.nodes and node == spec.nodes[nid]:
            continue  # unchanged nodes were already accepted
        key = getattr(node, "output_key", None)
        if key is not None and (key in RESERVED_OUTPUT_KEYS or key.startswith("_")
                                or key.endswith(RESERVED_OUTPUT_SUFFIXES)):
            raise EditRejected(f"node '{nid}' would write reserved state key '{key}'")
        if getattr(node, "module", None) in protected_modules:
            raise EditRejected(f"node '{nid}' reuses a protected module outside its protected node")
        module = getattr(node, "module", None)
        same_module = nid in spec.nodes and getattr(spec.nodes[nid], "module", None) == module
        if module in RESERVED_WRITING_MODULES and not same_module:
            # a module returns whatever keys it likes; output_key checks cannot see them
            raise EditRejected(f"node '{nid}' would add module {module!r}, which writes reserved state")
    _check_roles(spec, new)
    _check_information_flow(spec, new)
    for nid in terminal_required:
        if nid in spec.nodes:
            enders = terminal_nodes(new)
            if enders != {nid}:
                raise EditRejected(f"runs must end only at '{nid}'; edited spec can end at {sorted(enders)}")
    return new


def _changed(spec: HarnessSpec, new: HarnessSpec) -> list[str]:
    return [nid for nid, node in new.nodes.items() if nid not in spec.nodes or spec.nodes[nid] != node]


def _check_roles(spec: HarnessSpec, new: HarnessSpec) -> None:
    """R2-1: roles of existing LLM nodes are fixed; new LLM nodes run only as author roles; outputs of judging /
    replicating LLM nodes are written only by those nodes."""
    bound = {n.output_key: nid for nid, n in spec.nodes.items()
             if isinstance(n, LLMNode) and n.role not in EVOLVABLE_LLM_ROLES}
    for nid in _changed(spec, new):
        node, old = new.nodes[nid], spec.nodes.get(nid)
        if isinstance(node, LLMNode):
            if isinstance(old, LLMNode) and node.role != old.role:
                raise EditRejected(f"node '{nid}': changing an LLM node's role ({old.role!r} -> {node.role!r}) is "
                                   "human-only (invariant 5 is checked on role bindings)")
            if not isinstance(old, LLMNode) and node.role not in EVOLVABLE_LLM_ROLES:
                raise EditRejected(f"node '{nid}': a new LLM node may only run as {list(EVOLVABLE_LLM_ROLES)}, "
                                   f"not {node.role!r} (judging/replicating roles are human-only)")
        key = getattr(node, "output_key", None)
        if key in bound and bound[key] != nid:
            raise EditRejected(f"node '{nid}' would write '{key}', the output of the judging/replicating LLM node "
                               f"'{bound[key]}' (role-bound output, invariant 5)")


def _templates(node: Any) -> list[str]:
    if isinstance(node, LLMNode):
        return [node.system, node.prompt]
    if isinstance(node, ToolNode):
        return list(node.command)
    return []


def _check_information_flow(spec: HarnessSpec, new: HarnessSpec) -> None:
    """R2-5 generalised + the tool-node taint rule: what a new or changed template may render, and where tool output
    may go. Unchanged nodes were written by a human and are not re-judged here."""
    tool_keys = {n.output_key for n in new.nodes.values() if isinstance(n, ToolNode)}
    changed = _changed(spec, new)
    for nid in changed:
        node = new.nodes[nid]
        for path in (p for t in _templates(node) for p in references(t)):
            root = path.split(".")[0]
            if root in UNRENDERABLE_KEYS or root.startswith("_"):
                raise EditRejected(f"node '{nid}' would render '{path}' (unfiltered, private, host or engine "
                                   "state is never rendered by an evolved template)")
            if root in tool_keys and isinstance(node, LLMNode):
                raise EditRejected(f"node '{nid}' would render tool output '{path}' into a model prompt (tool "
                                   "nodes can read task data; their output only routes control flow)")
        if isinstance(node, ToolNode):
            key = node.output_key
            if not key.startswith(TOOL_OUTPUT_PREFIX):
                raise EditRejected(f"tool node '{nid}' must write a '{TOOL_OUTPUT_PREFIX}*' key, not '{key}' (no "
                                   "module or prompt may consume tool output)")
            writers = [m for m, n in new.nodes.items() if m != nid and getattr(n, "output_key", None) == key]
            if writers:
                raise EditRejected(f"tool node '{nid}' writes '{key}', which node(s) {writers} also write")
            readers = [m for m, n in new.nodes.items() if isinstance(n, LLMNode)
                       and any(p.split(".")[0] == key for t in _templates(n) for p in references(t))]
            if readers:
                raise EditRejected(f"tool node '{nid}' output '{key}' is rendered into model prompt(s) of {readers}")


def terminal_nodes(spec: HarnessSpec) -> set[str]:
    """Nodes where a run can end: no outgoing edges, or an edge to END."""
    out = set()
    for nid in spec.nodes:
        edges = spec.outgoing(nid)
        if not edges or any(e.target == "END" for e in edges):
            out.add(nid)
    return out
