"""Structured diagnosis (GOAL §5 L6.3; AutoSaddler first-divergence + Self-Harness causal signature).

Deterministic, no LLM: a failing run is compared with a SUCCEEDING run of the same task, step by step, on what the
harness decided at each step — which node ran, where it routed, and whether it raised an error (normalised
signature). The first index where these differ is the divergence; everything before it was common to both runs, so
the divergence is the earliest point at which the failure can be attributed to the harness's behaviour rather than to
the task. The result is a causal signature:

    cause        what the failing run did at that step (error signature, route taken, node visited, or "ended")
    criticality  "fatal" (the failing run earned no credit: score 0 / not completed) or "degrading" (it earned less)
    mechanism    "error" | "route" | "path" | "truncated"
    evidence     step references "<run_id>#<index>" in both runs (and later error steps of the failing run)

Limits (stated, not hidden): the comparison sees routing and errors, not the content of outputs. Two runs that took
the same path but wrote different code have NO divergence here (returned as None; the score difference is then in
content, which only the full traces show). A divergence is a correlate located in time, not a proven cause: the
succeeding run may have differed for reasons (seed, sampling) that no edit controls.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..records import TraceStep
from .edits import Edit
from .evaluate import _signature

MECHANISMS = ("error", "route", "path", "truncated")


def _error_sig(step: TraceStep) -> str | None:
    if step.error:
        return _signature(step.error.splitlines()[0] if step.error.strip() else "")
    run_err = step.output.get("run_error") if isinstance(step.output, dict) else None
    if run_err:
        last = [ln for ln in str(run_err).splitlines() if ln.strip()][-1:] or ["?"]
        return "solution: " + _signature(last[0])
    return None


def _route(step: TraceStep) -> str | None:
    r = step.output.get("route") if isinstance(step.output, dict) else None
    return None if r is None else str(r)


def _key(step: TraceStep) -> tuple[str, str | None, str | None]:
    return step.node_id, _route(step), _error_sig(step)


def first_divergence(failing: list[TraceStep], succeeding: list[TraceStep], *, failing_score: float | None
                     ) -> dict[str, Any] | None:
    """Causal signature at the first step where the failing trace differs from the succeeding one in node, route or
    error; None if both took the same path with the same errors (the difference lies in output content)."""
    f_id = failing[0].run_id if failing else "?"
    s_id = succeeding[0].run_id if succeeding else "?"
    criticality = "fatal" if not failing_score else "degrading"  # 0 or None (excluded) -> no credit
    n = min(len(failing), len(succeeding))
    for i in range(n):
        f, s = failing[i], succeeding[i]
        (fn, fr, fe), (sn, sr, se) = _key(f), _key(s)
        if (fn, fr, fe) == (sn, sr, se):
            continue
        if fn != sn:
            mechanism, cause = "path", f"ran node '{fn}' where the succeeding run ran '{sn}'"
        elif fe != se and fe is not None:
            mechanism, cause = "error", f"{fn}: {fe}"
        elif fe != se:  # the SUCCEEDING run erred here and the failing one did not: still a divergence
            mechanism, cause = "error", f"{fn}: no error where the succeeding run had '{se}'"
        else:
            mechanism, cause = "route", f"{fn} routed to '{fr}' (succeeding run: '{sr}')"
        return _signature_dict(i, fn, cause, criticality, mechanism, failing, f_id, s_id)
    if len(failing) != len(succeeding):
        node = failing[-1].node_id if failing else "?"
        if len(failing) < len(succeeding):
            cause = f"run ended after node '{node}' where the succeeding run continued to '{succeeding[n].node_id}'"
        else:
            cause = f"run continued to '{failing[n].node_id}' where the succeeding run ended"
        return _signature_dict(n, node, cause, criticality, "truncated", failing, f_id, s_id)
    return None


def _signature_dict(i: int, node: str, cause: str, criticality: str, mechanism: str, failing: list[TraceStep],
                    f_id: str, s_id: str) -> dict[str, Any]:
    later = [f"{f_id}#{st.index}" for st in failing[i + 1:] if _error_sig(st)][:3]
    return {"step": i, "node": node, "cause": cause, "criticality": criticality, "mechanism": mechanism,
            "evidence": [f"{f_id}#{i}", f"{s_id}#{i}", *later]}


def known_ids(digest: dict[str, Any]) -> set[str]:
    return {c["id"] for c in digest.get("clusters", [])} | {d["id"] for d in digest.get("divergences", [])}


def check_claims(edit: Edit, digest: dict[str, Any]) -> dict[str, Any]:
    """Check an edit's claimed failure clusters / cited evidence ids (C<n> clusters, D<n> divergences) against the
    diagnosis the proposer was given. An id that the diagnosis does not contain is an unsupported claim."""
    known = known_ids(digest)
    cited = sorted({x for x in [*edit.predicted.failure_clusters_fixed, *edit.evidence] if _looks_like_id(x)})
    supported = [x for x in cited if x in known]
    unsupported = [x for x in cited if x not in known]
    return {"supported": supported, "unsupported": unsupported, "passed": not unsupported}


def _looks_like_id(x: str) -> bool:
    return len(x) >= 2 and x[0] in "CD" and x[1:].isdigit()


def pair_divergences(failing: Iterable[Any], references: Iterable[Any], trace: Any, limit: int = 10
                     ) -> tuple[list[dict[str, Any]], list[str]]:
    """For each failing outcome, the best-scoring succeeding reference of the SAME task (score strictly higher;
    ties broken by run id, so the choice is deterministic). Returns (divergences D0.., task ids without a
    reference)."""
    by_task: dict[str, list[Any]] = {}
    for r in references:
        if r.score is not None and r.run_id:
            by_task.setdefault(r.task_id, []).append(r)
    out: list[dict[str, Any]] = []
    missing: list[str] = []
    for o in sorted(failing, key=lambda o: (o.task_id, o.seed, o.run_id)):
        if o.infra_failed or not o.run_id:
            continue
        better = [r for r in by_task.get(o.task_id, []) if r.run_id != o.run_id and r.score > (o.score or 0.0)]
        if not better:
            if o.task_id not in missing:
                missing.append(o.task_id)
            continue
        ref = sorted(better, key=lambda r: (-r.score, r.run_id))[0]
        sig = first_divergence(trace(o.run_id), trace(ref.run_id), failing_score=o.score)
        if sig is None:
            continue
        out.append({"id": f"D{len(out)}", "task": o.task_id, "failing_run": o.run_id, "reference_run": ref.run_id,
                    **sig})
        if len(out) >= limit:
            break
    return out, missing
