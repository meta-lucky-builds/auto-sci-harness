"""Trace-grounded diagnosis and edit proposal (PLAN L6.1-6.2; Meta-Harness / Self-Harness / AutoSaddler)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from typing import Any, NamedTuple

from pydantic import ValidationError

from ..gateway import Backend, ChatRequest, Message
from ..runtime.engine import parse_json_output
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .diagnosis import failed_checks, labelled_trace, pair_divergences
from .edits import DEFAULT_PROTECTED_NODES, Edit, EditType
from .evaluate import RunOutcome


def diagnose(outcomes: list[RunOutcome], store: EvidenceStore, max_examples: int = 2,
             excerpt_chars: int = 1500, references: Iterable[RunOutcome] | None = None,
             evolve_ids: set[str] | None = None) -> dict[str, Any]:
    """Cluster failure signatures across runs and attach raw trace excerpts (full traces > scalars, P1).
    Infra-failed runs are left out: the environment breaking is not something a harness edit can fix,
    and showing it would invite edits that 'fix' Docker outages.

    `divergences` (L6.3): for each run that scored below a run of the SAME task among `outcomes` + `references`,
    the first-divergence causal signature with its verifier evidence (diagnosis.py), ids D0, D1...; `no_reference`
    lists tasks with no better-scoring run to compare against; `unlinked` counts signatures dropped because a
    reference did not resolve.

    `evolve_ids` (the loop always passes it): every outcome or reference of a task outside it is dropped before
    anything is read (verdict embargo: gate/sealed runs never reach the diagnosis), counted in `n_excluded_split`.
    Without it the caller vouches that every run is of an evolve-split task."""
    n_split = 0
    if evolve_ids is not None:
        refs_in, outcomes_in = list(references or []), list(outcomes)
        outcomes = [o for o in outcomes if _evolve_run(o, evolve_ids, store)]
        references = [o for o in refs_in if _evolve_run(o, evolve_ids, store)]
        n_split = len(refs_in) + len(outcomes_in) - len(outcomes) - len(references)
    n_infra = sum(o.infra_failed for o in outcomes)
    outcomes = [o for o in outcomes if not o.infra_failed]
    clusters: Counter[str] = Counter()
    examples: dict[str, list[dict]] = {}
    for o in outcomes:
        for sig in set(o.failure_signatures):
            clusters[sig] += 1
            if len(examples.setdefault(sig, [])) < max_examples:
                excerpt = []
                # labelled steps withheld (runtime/taint.py): an error text may quote a target-derived value
                for step in labelled_trace(store, o.run_id):
                    if step.error or (isinstance(step.output, dict) and step.output.get("run_error")):
                        excerpt.append({"node": step.node_id, "error": step.error,
                                        "run_error": (step.output or {}).get("run_error")})
                examples[sig].append({"run_id": o.run_id, "task": o.task_id,
                                      "excerpt": json.dumps(excerpt, default=str)[:excerpt_chars]})
    named = {f"C{i}": sig for i, (sig, _) in enumerate(clusters.most_common())}
    divergences, no_reference, unlinked = pair_divergences(
        outcomes, [*outcomes, *(references or [])], lambda r: labelled_trace(store, r),
        checks=lambda r: failed_checks(store, r))
    return {
        "n_runs": len(outcomes),
        "n_excluded_infra": n_infra,
        "n_excluded_split": n_split,
        "mean_score": sum(o.score or 0.0 for o in outcomes) / max(1, len(outcomes)),  # infra runs removed above
        "verified_rate": sum(o.verified for o in outcomes) / max(1, len(outcomes)),
        # before the clusters: the prompt truncates the digest, and the signatures are the compact part
        "divergences": divergences,
        "no_reference": no_reference,
        "unlinked": unlinked,
        "clusters": [{"id": cid, "signature": sig, "count": clusters[sig], "examples": examples[sig]}
                     for cid, sig in named.items()],
    }


def _evolve_run(o: RunOutcome, evolve_ids: set[str], store: EvidenceStore) -> bool:
    """An evolve-split run: its outcome's task AND its run record's own task are evolve tasks (an outcome filed under
    the wrong task id, e.g. a gate run in an evaluator cache slot of an evolve task, is not trusted)."""
    if o.task_id not in evolve_ids:
        return False
    if not o.run_id:
        return True  # no run record: nothing of the run can be read
    try:
        return store.get_run(o.run_id).task_id == o.task_id
    except KeyError:
        return False


EDIT_SCHEMA = {
    "type": "object",
    "properties": {"edits": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": [t.value for t in EditType]},
            "ops": {"type": "array", "items": {"type": "object", "properties": {
                "op": {"type": "string", "enum": ["replace", "add", "remove"]},
                "path": {"type": "string"},
                "value_json": {"type": "string"}},
                "required": ["op", "path", "value_json"]}},
            "rationale": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "failure_clusters_fixed": {"type": "array", "items": {"type": "string"}},
            "expected_delta": {"type": "number"},
            "tasks_at_risk": {"type": "array", "items": {"type": "string"}},
            "claimed_change": {"type": "array", "items": {"type": "string"}}},
        "required": ["type", "ops", "rationale", "evidence", "failure_clusters_fixed", "expected_delta",
                     "tasks_at_risk", "claimed_change"]}},
        "decline": {"type": "string"}},
    "required": ["edits"],
}


DEFAULT_OBJECTIVE = """OBJECTIVE per task run: 0 if the run fails or the integrity audit fails; otherwise
1.0*(verified hidden-test improvement over baseline) + 0.5*clip(relative test improvement, -1, 1).
Edits are accepted only if they improve the objective significantly on the tasks you see AND do not
regress on held-out tasks you never see. Edits that overfit to specific tasks will be rejected."""


def build_prompt(spec: HarnessSpec, digest: dict, ledger: list[dict], n_edits: int,
                 protected: tuple[str, ...], objective: str = DEFAULT_OBJECTIVE) -> str:
    return f"""You improve an autonomous ML-research harness by editing its typed spec.

{objective}

CURRENT SPEC (JSON):
{spec.model_dump_json(indent=1)}

PROTECTED NODES (cannot be edited; verifier/audit changes are human-only): {list(protected)}

DIAGNOSIS from full traces of the current harness. `clusters` (ids C0, C1...) group failure signatures;
`divergences` (ids D0, D1...) give, for a failing run, the FIRST DIVERGENCE from a better run of the same task:
the step, the cause, its criticality and mechanism (error / route / path / truncated / content), the first output
CONTENT difference (key names), and the verifier evidence (each run's verdict, the Kernel checks that failed), with
references.
{json.dumps(digest, indent=1)[:12000]}

PAST EDITS (accepted/rejected, with measured effect; learn from them):
{json.dumps(ledger[-15:], indent=1, default=str)[:4000]}

Propose up to {n_edits} DISTINCT edits. Prefer structural edits (contracts, control flow, routing,
information flow between nodes, context policy, params) over cosmetic prompt rewording.
Each op is a JSON-pointer operation on the spec; `value_json` is the new value encoded as a JSON string
(use "null" for remove). Predict the objective change.
CITATIONS: `failure_clusters_fixed` holds cluster/divergence ids (C<n>, D<n>); `evidence` holds only citations of
these forms: C<n>, D<n>, `<run_id>#<step>` (a trace step), `<run_id>#verdict` (a run's verifier outcome),
`<run_id>#<step>/<check>` (a Kernel check that failed). Every citation must resolve in the diagnosis (or in the
evidence export, if you can read one); an edit with ANY citation that resolves to nothing is rejected unevaluated.
CLAIMED CHANGE: `claimed_change` lists the JSON-pointer path of EVERY spec value your ops change, and nothing else
(dict keys are followed down to the changed value; a list counts as one value, so cite the list's path; an added or
removed key is its own path). It is compared with the real diff of the spec; a missing or different claim is
rejected unevaluated.
Edits identical to a previously rejected edit are refused without evaluation.
Never mention task-specific values, file names of held-out data, or anything under `_private`.
If no edit is supported by the evidence, you may DECLINE: return {{"edits": [], "decline": "<reason>"}}.
Return JSON {{"edits": [...]}}."""


class Proposal(NamedTuple):
    edits: list[Edit]
    problems: list[str]
    raw: Any
    decline: str | None  # the proposer's reason for proposing nothing (a decision, not an error)


def propose_edits(backend: Backend, spec: HarnessSpec, digest: dict, ledger: list[dict], *, n_edits: int = 3,
                  protected: tuple[str, ...] = DEFAULT_PROTECTED_NODES) -> tuple[list[Edit], list[str], Any]:
    """Returns (valid edits, problems, raw response). Malformed edits are reported, not guessed."""
    p = propose(backend, spec, digest, ledger, n_edits=n_edits, protected=protected)
    return p.edits, p.problems, p.raw


def propose(backend: Backend, spec: HarnessSpec, digest: dict, ledger: list[dict], *, n_edits: int = 3,
            protected: tuple[str, ...] = DEFAULT_PROTECTED_NODES, objective: str = DEFAULT_OBJECTIVE) -> Proposal:
    """One proposer call. A decline is recognised only with ZERO edits and a non-empty string reason; a decline
    alongside edits is ignored (the edits are evaluated as usual)."""
    resp = backend.complete(ChatRequest(
        messages=[Message(role="user", content=build_prompt(spec, digest, ledger, n_edits, protected, objective))],
        json_schema=EDIT_SCHEMA))
    try:
        raw = parse_json_output(resp.text)
    except json.JSONDecodeError as e:
        return Proposal([], [f"proposer output not JSON: {e}"], resp, None)
    return parse_proposal(raw, n_edits, resp)


# the typed-edit fields of EDIT_SCHEMA (anything else in a proposer's edit is refused, not ignored)
_ITEM_KEYS = frozenset({"type", "ops", "rationale", "evidence", "failure_clusters_fixed", "expected_delta",
                        "tasks_at_risk", "claimed_change"})
_OP_KEYS = frozenset({"op", "path", "value_json"})


def parse_proposal(raw: Any, n_edits: int, resp: Any) -> Proposal:
    """Typed edits from a proposer's decoded JSON answer ({"edits": [...], "decline": ...} in EDIT_SCHEMA form).
    Anything that is not a typed edit (unknown edit type or op, a free-form patch, missing fields) is reported as
    malformed, never guessed. Shared by the one-shot proposer and the agentic one (proposer_agent.py)."""
    problems: list[str] = []
    edits: list[Edit] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("edits", []), list):
        return Proposal([], [f"proposer output is not an object with an 'edits' list: {type(raw).__name__}"], resp,
                        None)
    for i, item in enumerate(raw.get("edits", [])[:n_edits]):
        try:
            if not isinstance(item, dict) or set(item) - _ITEM_KEYS:
                raise TypeError(f"not a typed edit: unknown field(s) {sorted(set(item) - _ITEM_KEYS)[:5]}"
                                if isinstance(item, dict) else "an edit is a JSON object")
            if any(not isinstance(o, dict) or set(o) - _OP_KEYS for o in item["ops"]):
                raise TypeError("not a typed edit: an op has fields other than op/path/value_json")
            ops = [{"op": o["op"], "path": o["path"],
                    "value": None if o["op"] == "remove" else json.loads(o["value_json"])}
                   for o in item["ops"]]
            edits.append(Edit.model_validate({
                "type": item["type"], "ops": ops, "rationale": item["rationale"],
                "evidence": item.get("evidence", []), "claimed_change": item.get("claimed_change"),
                "predicted": {"failure_clusters_fixed": item.get("failure_clusters_fixed", []),
                              "expected_delta": item.get("expected_delta", 0.0),
                              "tasks_at_risk": item.get("tasks_at_risk", [])}}))
        except (KeyError, TypeError, AttributeError, json.JSONDecodeError, ValidationError) as e:
            problems.append(f"edit {i} malformed: {type(e).__name__}: {str(e)[:200]}")
    reason = raw.get("decline")
    decline = reason.strip()[:500] if isinstance(reason, str) and reason.strip() and not raw.get("edits") else None
    return Proposal(edits, problems, resp, decline)
