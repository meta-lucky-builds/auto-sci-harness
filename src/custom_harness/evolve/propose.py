"""Trace-grounded diagnosis and edit proposal (PLAN L6.1-6.2; Meta-Harness / Self-Harness / AutoSaddler)."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from pydantic import ValidationError

from ..gateway import Backend, ChatRequest, Message
from ..runtime.engine import parse_json_output
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .edits import DEFAULT_PROTECTED_NODES, Edit, EditType
from .evaluate import RunOutcome


def diagnose(outcomes: list[RunOutcome], store: EvidenceStore, max_examples: int = 2,
             excerpt_chars: int = 1500) -> dict[str, Any]:
    """Cluster failure signatures across runs and attach raw trace excerpts (full traces > scalars, P1).
    Infra-failed runs are left out: the environment breaking is not something a harness edit can fix,
    and showing it would invite edits that 'fix' Docker outages."""
    n_infra = sum(o.infra_failed for o in outcomes)
    outcomes = [o for o in outcomes if not o.infra_failed]
    clusters: Counter[str] = Counter()
    examples: dict[str, list[dict]] = {}
    for o in outcomes:
        for sig in set(o.failure_signatures):
            clusters[sig] += 1
            if len(examples.setdefault(sig, [])) < max_examples:
                excerpt = []
                for step in store.trace(o.run_id):
                    if step.error or (isinstance(step.output, dict) and step.output.get("run_error")):
                        excerpt.append({"node": step.node_id, "error": step.error,
                                        "run_error": (step.output or {}).get("run_error")})
                examples[sig].append({"run_id": o.run_id, "task": o.task_id,
                                      "excerpt": json.dumps(excerpt, default=str)[:excerpt_chars]})
    named = {f"C{i}": sig for i, (sig, _) in enumerate(clusters.most_common())}
    return {
        "n_runs": len(outcomes),
        "n_excluded_infra": n_infra,
        "mean_score": sum(o.score for o in outcomes) / max(1, len(outcomes)),
        "verified_rate": sum(o.verified for o in outcomes) / max(1, len(outcomes)),
        "clusters": [{"id": cid, "signature": sig, "count": clusters[sig], "examples": examples[sig]}
                     for cid, sig in named.items()],
    }


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
            "tasks_at_risk": {"type": "array", "items": {"type": "string"}}},
        "required": ["type", "ops", "rationale", "evidence", "failure_clusters_fixed", "expected_delta",
                     "tasks_at_risk"]}}},
    "required": ["edits"],
}


def build_prompt(spec: HarnessSpec, digest: dict, ledger: list[dict], n_edits: int,
                 protected: tuple[str, ...]) -> str:
    return f"""You improve an autonomous ML-research harness by editing its typed spec.

OBJECTIVE per task run: 0 if the run fails or the integrity audit fails; otherwise
1.0*(verified hidden-test improvement over baseline) + 0.5*clip(relative test improvement, -1, 1).
Edits are accepted only if they improve the objective significantly on the tasks you see AND do not
regress on held-out tasks you never see. Edits that overfit to specific tasks will be rejected.

CURRENT SPEC (JSON):
{spec.model_dump_json(indent=1)}

PROTECTED NODES (cannot be edited; verifier/audit changes are human-only): {list(protected)}

DIAGNOSIS from full traces of the current harness:
{json.dumps(digest, indent=1)[:12000]}

PAST EDITS (accepted/rejected, with measured effect; learn from them):
{json.dumps(ledger[-15:], indent=1, default=str)[:4000]}

Propose up to {n_edits} DISTINCT edits. Prefer structural edits (contracts, control flow, routing,
information flow between nodes, context policy, params) over cosmetic prompt rewording.
Each op is a JSON-pointer operation on the spec; `value_json` is the new value encoded as a JSON string
(use "null" for remove). Cite failure cluster ids in `evidence`, and predict the objective change.
Never mention task-specific values, file names of held-out data, or anything under `_private`.
Return JSON {{"edits": [...]}}."""


def propose_edits(backend: Backend, spec: HarnessSpec, digest: dict, ledger: list[dict], *, n_edits: int = 3,
                  protected: tuple[str, ...] = DEFAULT_PROTECTED_NODES) -> tuple[list[Edit], list[str], Any]:
    """Returns (valid edits, problems, raw response). Malformed edits are reported, not guessed."""
    resp = backend.complete(ChatRequest(
        messages=[Message(role="user", content=build_prompt(spec, digest, ledger, n_edits, protected))],
        json_schema=EDIT_SCHEMA))
    problems: list[str] = []
    edits: list[Edit] = []
    try:
        raw = parse_json_output(resp.text)
    except json.JSONDecodeError as e:
        return [], [f"proposer output not JSON: {e}"], resp
    for i, item in enumerate(raw.get("edits", [])[:n_edits]):
        try:
            ops = [{"op": o["op"], "path": o["path"],
                    "value": None if o["op"] == "remove" else json.loads(o["value_json"])}
                   for o in item["ops"]]
            edits.append(Edit.model_validate({
                "type": item["type"], "ops": ops, "rationale": item["rationale"],
                "evidence": item.get("evidence", []),
                "predicted": {"failure_clusters_fixed": item.get("failure_clusters_fixed", []),
                              "expected_delta": item.get("expected_delta", 0.0),
                              "tasks_at_risk": item.get("tasks_at_risk", [])}}))
        except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as e:
            problems.append(f"edit {i} malformed: {type(e).__name__}: {str(e)[:200]}")
    return edits, problems, resp
