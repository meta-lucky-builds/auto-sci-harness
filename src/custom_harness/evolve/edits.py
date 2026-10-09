"""Typed harness edits (PLAN L6). Edits are JSON-pointer operations on the HarnessSpec, with a
declared edit type, a rationale citing trace evidence, and a predicted effect (for calibration, §8.5).

Protected surfaces (verifier wiring, integrity audit, task setup) cannot be edited automatically:
changing verifiers is a human-only action under the tier policy (PLAN §5).
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..runtime.spec import HarnessSpec, LLMNode, MapNode, ToolNode
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
    evidence: list[str] = Field(default_factory=list)  # trace refs / failure-cluster ids (claims.py citation forms)
    predicted: PredictedEffect = Field(default_factory=PredictedEffect)
    # the JSON-pointer paths the proposer says its ops change (claims.check_claimed_diff); None = not stated, which
    # the loop refuses (fail closed). Not part of the novelty signature (loop.edit_signature: type + ops only).
    claimed_change: list[str] | None = None


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
                                  "figures",
                                  # critic-answer check and recall (research/critic_answers.py, research/recall.py;
                                  # tests/test_evolve_hardening.py checks these keys are really written)
                                  "critic_answers", "critic_evidence", "recalled", "recall_log",
                                  # hypothesis front (research/front.py): what admission enforces, its record, the
                                  # ranked problems, the run's literature pool and the diversity record
                                  "hypothesis_front", "hypothesis_admission", "problems", "literature_pool",
                                  "diversity"})
# Approval-node outcomes (`<node_id>_approval`, `<node_id>_decision`) are written only by the engine; node ids vary,
# so any key with these suffixes is reserved. (The engine also discards such values before an approval node runs.)
RESERVED_OUTPUT_SUFFIXES = ("_approval", "_decision")
# Modules whose outputs include reserved keys (kept complete by test_reserved_writing_modules_are_declared, which
# runs the seed harness and checks every module step's output keys). Existing nodes may keep using them; an edit
# may not ADD a node with one of them, nor switch a node to one.
RESERVED_WRITING_MODULES = frozenset({
    "research.setup", "research.profile_data", "research.baseline_dev", "research.socratic_filter",
    "research.next_hypothesis", "research.run_candidate", "research.claim", "research.confirm", "research.certify",
    "research.replica_dev", "research.report", "research.audit",
    # the critic-answer check and recall write reserved keys (ids checked against the registry by a test)
    "research.critic_evidence", "research.check_critic_answers", "research.recall",
    # hypothesis front (research/front.py)
    "research.formulate", "research.hypothesis_front", "research.admit_rank", "research.diversity_select"})
# Critic-chain nodes whose DEFINITION is human-only (their wiring stays evolvable): an edit could otherwise switch the
# critic off by a parameter (socratic `max_questions` 0) or change what checks the answers. Removing one is refused too.
PINNED_NODES = ("socratic", "critic_evidence", "check_answers")
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
    for nid in PINNED_NODES:
        if nid in spec.nodes and (nid not in new.nodes or new.nodes[nid] != spec.nodes[nid]):
            raise EditRejected(f"critic-chain node '{nid}' is human-only (its definition cannot be edited or removed)")
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
    _check_map_flow(spec, new, tool_keys)


def _check_map_flow(spec: HarnessSpec, new: HarnessSpec, tool_keys: set[str]) -> None:
    """The same rules through a MapNode (R2 evolve gap for maps). A map renders each element of `items_key` into its
    body's templates under `item_key`, so a body reference to the item is a reference to `items_key`; a map whose
    body is a tool node carries tool output into the map's `output_key`, which is then tool output itself. Checked
    for every map that an edit adds or changes, or whose body it changes (a human-written map is judged again when
    an edit changes its body)."""
    changed = set(_changed(spec, new))
    maps = {nid: n for nid, n in new.nodes.items() if isinstance(n, MapNode)}
    tool_bodied = {m.output_key for m in maps.values() if isinstance(new.nodes.get(m.body), ToolNode)}
    for mid, m in maps.items():
        if mid not in changed and m.body not in changed:
            continue
        body = new.nodes.get(m.body)
        items_root = m.items_key.split(".")[0]
        for path in (p for t in _templates(body) for p in references(t)):
            if path.split(".")[0] != m.item_key:
                continue
            if items_root in UNRENDERABLE_KEYS or items_root.startswith("_"):
                raise EditRejected(f"map '{mid}' would render '{m.items_key}' into body '{m.body}' as '{path}' "
                                   "(unfiltered, private, host or engine state is never rendered by an evolved "
                                   "template)")
            if isinstance(body, LLMNode) and (items_root in tool_keys | tool_bodied
                                              or items_root.startswith(TOOL_OUTPUT_PREFIX)):
                raise EditRejected(f"map '{mid}' would render tool output '{m.items_key}' into the model prompt of "
                                   f"body '{m.body}' (tool output only routes control flow)")
        if isinstance(body, ToolNode):
            if not m.output_key.startswith(TOOL_OUTPUT_PREFIX):
                raise EditRejected(f"map '{mid}' has tool body '{m.body}', so it must write a '{TOOL_OUTPUT_PREFIX}*' "
                                   f"key, not '{m.output_key}' (its output is tool output)")
    judged = {m.output_key for mid, m in maps.items() if m.output_key in tool_bodied
              and (mid in changed or m.body in changed)}
    for nid, n in new.nodes.items():  # a tool-bodied map's output reaches no model prompt
        if not isinstance(n, LLMNode):
            continue
        keys = tool_bodied if nid in changed else judged  # unchanged prompts are judged against changed maps only
        hit = [p for t in _templates(n) for p in references(t) if p.split(".")[0] in keys]
        if hit:
            raise EditRejected(f"node '{nid}' would render tool output {hit} (the output of a map with a tool body) "
                               "into a model prompt")


def mechanisms(edit: Edit, spec: HarnessSpec) -> list[list[str]] | None:
    """The mechanisms an edit changes (GOAL §5 L6.6 "one mechanism per candidate"), defined on the EFFECT of its ops,
    not on how they are written:

      - an anchor is a node whose definition changed (added, removed or modified), a node whose ordered list of
        OUTGOING edges changed (a routing decision belongs to its source node), or a top-level spec field other than
        `nodes`/`edges` that changed (`spec:<field>`, e.g. `spec:max_steps`);
      - a node the edit ADDS is merged with every node that gains an edge into it, and a node it REMOVES with every
        node that loses an edge into it (inserting or deleting a step is one mechanism with its wiring).

    Each connected group of anchors is one mechanism. Returns the groups (sorted), or None when the ops cannot be
    applied (apply_edit reports that)."""
    old = copy.deepcopy(spec.model_dump(mode="json"))
    doc = copy.deepcopy(old)
    try:
        for op in edit.ops:
            _apply_op(doc, op)
    except (EditRejected, ValueError, IndexError, TypeError, KeyError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), dict) or not isinstance(doc.get("edges"), list):
        return None
    anchors: set[str] = set()
    for key in set(old) | set(doc):
        if key not in ("nodes", "edges") and old.get(key) != doc.get(key):
            anchors.add(f"spec:{key}")
    on, nn = old["nodes"], doc["nodes"]
    added, removed = set(nn) - set(on), set(on) - set(nn)
    anchors |= {n for n in set(on) | set(nn) if on.get(n) != nn.get(n)}

    def out_edges(edges: list[Any]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in edges:
            src = str(e.get("source")) if isinstance(e, dict) else "?"
            out.setdefault(src, []).append(json.dumps(e, sort_keys=True, default=str))
        return out

    oe, ne = out_edges(old["edges"]), out_edges(doc["edges"])
    anchors |= {s for s in set(oe) | set(ne) if oe.get(s) != ne.get(s)}
    parent = {a: a for a in anchors}

    def find(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: str, b: str) -> None:
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    for edges, nodes in ((doc["edges"], added), (old["edges"], removed)):
        for e in edges:
            if isinstance(e, dict) and e.get("target") in nodes:
                union(str(e.get("source")), str(e["target"]))
    groups: dict[str, list[str]] = {}
    for a in sorted(anchors):
        groups.setdefault(find(a), []).append(a)
    return sorted(groups.values())


def check_one_mechanism(edit: Edit, spec: HarnessSpec) -> None:
    """Refuse a bundle that changes more than one mechanism (see `mechanisms`). An edit that cannot be applied is
    left to apply_edit, which rejects it with the precise reason."""
    groups = mechanisms(edit, spec)
    if groups is not None and len(groups) > 1:
        raise EditRejected(f"one mechanism per candidate: this edit changes {len(groups)} separate mechanisms "
                           f"{[g if len(g) > 1 else g[0] for g in groups][:6]}; propose them as separate edits")


_CLUSTER_ID = re.compile(r"^[CD]\d{1,4}\Z")  # \Z, not $: `$` also matches before a trailing newline
_CANARY = re.compile(r"CHH-CANARY-", re.I)


def denied_tokens(tasks: Iterable[Any], canaries: Iterable[str] = ()) -> list[str]:
    """What no proposer output may contain: ids of every gate/sealed task, names (>= 3 chars) of held-out families
    and domains (they appear in no evolve task), and the caller's canaries. The proposer never sees any of these,
    so their presence in its output means a leak or a covert channel."""
    tasks = list(tasks)
    evolve_fam = {getattr(t, "family", None) for t in tasks if t.split == "evolve"}
    evolve_dom = {getattr(t, "domain", None) for t in tasks if t.split == "evolve"}
    out = {t.task_id for t in tasks if t.split != "evolve"}
    for t in tasks:
        if t.split == "evolve":
            continue
        for name, seen in ((getattr(t, "family", None), evolve_fam), (getattr(t, "domain", None), evolve_dom)):
            if isinstance(name, str) and len(name) >= 3 and name not in seen:
                out.add(name)
    out |= {c for c in canaries if c}
    return sorted(out)


def _token_hit(text: str, tokens: Iterable[str]) -> bool:
    low = text.lower()
    for tok in tokens:
        t = tok.lower()
        i = low.find(t)
        while i >= 0:
            before = low[i - 1] if i > 0 else " "
            after = low[i + len(t)] if i + len(t) < len(low) else " "
            if not (before.isalnum() or before in "_-") and not (after.isalnum() or after in "_-"):
                return True
            i = low.find(t, i + 1)
    return False


class ScreenRejected(EditRejected):
    """Refused by `screen_prediction`. `about_ops`: the CHANGE itself (its ops) carries the denied content, so the
    novelty gate should refuse it again; otherwise only its free text did, and the same change re-worded is fine."""

    def __init__(self, msg: str, *, about_ops: bool):
        super().__init__(msg)
        self.about_ops = about_ops


def screen_prediction(edit: Edit, *, task_ids: Iterable[str], deny: Iterable[str], max_text_chars: int,
                      max_delta: float, delta_decimals: int) -> tuple[Edit, list[str]]:
    """The covert-channel screen on a proposed edit (GOAL §5 L6.2: "typed edits with a predicted effect", done
    safely). Rejects (EditRejected, with a reason that never quotes the text) an edit any of whose text (ops,
    rationale, evidence, predicted clusters) carries a denied token (`denied_tokens`), a canary or a private-data
    pattern. Otherwise returns a sanitised copy plus notes on what was changed:
      - failure_clusters_fixed keeps only diagnosis-style ids (C<n> / D<n>);
      - tasks_at_risk keeps only exact ids of this campaign's tasks (anything else is free text);
      - evidence keeps at most 20 items of at most 64 chars; rationale is cut to `max_text_chars`;
      - expected_delta must be finite (else 0), is clipped to +-max_delta and rounded to `delta_decimals`: a float's
        low-order digits are otherwise a 50-bit channel into the ledger.
    tasks_at_risk is screened by the exact-id rule, not the deny list: naming a task is what that field is for."""
    deny = list(deny)
    texts = [(True, json.dumps([o.model_dump() for o in edit.ops], default=str)),
             *((False, t) for t in (edit.rationale, *edit.evidence, *edit.predicted.failure_clusters_fixed,
                                    *(edit.claimed_change or ())))]
    for in_ops, text in texts:
        where = "edit ops" if in_ops else "edit text"
        if _token_hit(text, deny):
            raise ScreenRejected(f"{where} name a held-out task, family, domain or canary (covert channel or leak; "
                                 "content withheld)", about_ops=in_ops)
        if _CANARY.search(text) or any(p.search(text) for p in _LEAK_PATTERNS):
            raise ScreenRejected(f"{where} carry a canary or a private-data pattern (content withheld)",
                                 about_ops=in_ops)
    notes: list[str] = []
    known = set(task_ids)
    clusters = [c for c in edit.predicted.failure_clusters_fixed if _CLUSTER_ID.match(c)]
    risk = [t for t in edit.predicted.tasks_at_risk if t in known]
    evidence = [e for e in edit.evidence if len(e) <= 64][:20]
    # claimed paths: bounded like evidence. A dropped path is noted; the loop rejects an edit whose citations or
    # claimed paths were dropped (loop.UNVERIFIABLE_IF_DROPPED), since a dropped FALSE claim could not be checked
    claimed = None if edit.claimed_change is None else [p for p in edit.claimed_change if len(p) <= 200][:32]
    rationale = edit.rationale[:max_text_chars]
    delta = edit.predicted.expected_delta
    if not math.isfinite(delta):
        delta = 0.0
        notes.append("expected_delta not finite: set to 0")
    clipped = round(max(-max_delta, min(max_delta, delta)), delta_decimals)
    if clipped != delta:
        notes.append(f"expected_delta clipped/rounded to {clipped}")
    for name, before, after in (("failure_clusters_fixed", edit.predicted.failure_clusters_fixed, clusters),
                                ("tasks_at_risk", edit.predicted.tasks_at_risk, risk),
                                ("evidence", edit.evidence, evidence),
                                ("claimed_change", edit.claimed_change or [], claimed or [])):
        if len(after) != len(before):
            notes.append(f"{name}: {len(before) - len(after)} item(s) dropped")
    if rationale != edit.rationale:
        notes.append(f"rationale cut to {max_text_chars} chars")
    clean = edit.model_copy(update={"rationale": rationale, "evidence": evidence, "claimed_change": claimed,
                                    "predicted": PredictedEffect(failure_clusters_fixed=clusters,
                                                                 expected_delta=clipped, tasks_at_risk=risk)})
    return clean, notes


def withheld(edit: Edit) -> dict[str, Any]:
    """What the lineage may keep of an edit refused by `screen_prediction`: type and op paths only (the refused text
    must not be written to the store, from which later exports are built)."""
    return {"type": edit.type.value, "ops": [{"op": o.op, "path": "<withheld>"} for o in edit.ops],
            "rationale": "<withheld>", "evidence": [], "predicted": PredictedEffect().model_dump(), "withheld": True}


def terminal_nodes(spec: HarnessSpec) -> set[str]:
    """Nodes where a run can end: no outgoing edges, or an edge to END."""
    out = set()
    for nid in spec.nodes:
        edges = spec.outgoing(nid)
        if not edges or any(e.target == "END" for e in edges):
            out.add(nid)
    return out
