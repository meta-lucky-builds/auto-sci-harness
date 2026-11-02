"""Structured diagnosis (GOAL §5 L6.3; AutoSaddler first-divergence + Self-Harness causal signature).

Deterministic, no LLM: a failing run is compared with a SUCCEEDING run of the same task, step by step, on what the
harness decided at each step — which node ran, where it routed, and whether it raised an error (normalised
signature) — and on what each step WROTE (its output content, embargoed). The first index where the decisions differ
is the divergence; everything before it was common to both runs, so the divergence is the earliest point at which the
failure can be attributed to the harness's behaviour rather than to the task. The result is a causal signature:

    cause        what the failing run did at that step (error signature, route taken, node visited, "ended", or the
                 output keys whose content differs)
    criticality  "fatal" (the failing run earned no credit: score 0 / not completed) or "degrading" (it earned less)
    mechanism    "error" | "route" | "path" | "truncated" | "content"
    content      the first step where the two runs' OUTPUT CONTENT differs, at or before the decision divergence
                 (None when the content was the same up to it); output key names only, never values
    verifier     each run's verifier outcome (score, verified, status, certificate label) and the Kernel checks that
                 FAILED in the failing run's certificates (names only), each with its citation reference
    evidence     every reference above, in the citation forms of claims.py: "<run_id>#<step>", "<run_id>#verdict",
                 "<run_id>#<step>/<check>"

Content level: when node, route and error agree at every step, the first step whose output content differs is the
divergence (mechanism "content"). Content is compared after the export's own reduction (export._clean: no `_private*`
keys; export.embargo: certificates reduced to their label, report text withheld) and with run-specific identifiers
(hashes, refs, ledger sequence numbers, run/claim ids) normalised away, so two runs that wrote the same thing do not
diverge on their ids. Only the differing KEY NAMES reach the signature: the proposer reads the values, if it needs
them, in the audited export (traces/<run_id>.json).

Verifier-evidence linkage (enforced): a signature is kept only if every reference it carries resolves — the step
exists in that run's trace (by its own index), the run has a verifier outcome, the cited check failed in a
certificate summary built exactly as the export builds it (export._cert_summary). A signature that cannot be linked
is dropped and counted (`unlinked`), never shown. For the agentic proposer the signatures are re-linked against the
export actually written (`link_digest`), so every reference in its prompt resolves in its mount.

Embargo: the diagnosis reads only the runs it is given, and `propose.diagnose` drops every run that is not of an
evolve-split task. Held-out (gate/sealed) results never enter it; within evolve runs it sees what the export may show.

Limits (stated, not hidden): a divergence is a correlate located in time, not a proven cause: the succeeding run may
have differed for reasons (seed, sampling) that no edit controls. Content that differs only in a normalised id, or
inside an embargoed certificate/report, is not a content divergence.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from ..records import TraceStep
from ..store import EvidenceStore
from .claims import check_claims as _check_claims
from .claims import check_ref, step_ref, verdict_ref
from .edits import Edit
from .evaluate import _signature
from .export import _cert_summary, _clean, embargo

MECHANISMS = ("error", "route", "path", "truncated", "content")
# decision keys: compared by _key (route, error), not as content
_DECISION_KEYS = frozenset({"route", "run_error"})
# run-specific bookkeeping that differs between two runs that did the same thing
_VOLATILE_KEYS = frozenset({"hash", "claim_hash", "replica_claim_hash", "prereg_seq", "replica_prereg_seq",
                            "evidence_ids", "replica_evidence_ids", "embargoed"})
_ID_LIKE = re.compile(r"^(?:[a-z]+_[0-9a-f]{16}|[0-9a-f]{16,128})\Z")


def labelled_trace(store: EvidenceStore, run_id: str) -> list[TraceStep]:
    """The run's trace as the evolver may see it (runtime/taint.py step labels, as the export applies them): a step
    whose output carries target labels keeps only its output KEY names (values withheld, `route` and `run_error`
    withheld) and its error is withheld; a step that ran because of a target-dependent routing decision also has its
    node id and its whole output withheld. Fail closed by time for runs without stored labels (`step_labels`)."""
    from ..runtime.taint import step_labels
    from .export import EMBARGOED
    labels = step_labels(store, run_id)
    out: list[TraceStep] = []
    for st in store.trace(run_id):
        lab = labels.get(st.index)
        if lab is None or not lab.tainted:
            out.append(st)
            continue
        upd: dict[str, Any] = {"error": EMBARGOED if st.error is not None else None}
        if lab.control:
            upd.update(node_id=EMBARGOED, output={})
        elif isinstance(st.output, dict):
            upd["output"] = {k: EMBARGOED for k in st.output}
        else:
            upd["output"] = {}
        out.append(st.model_copy(update=upd))
    return out


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


def _norm(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items() if k not in _VOLATILE_KEYS and not str(k).endswith("_ref")}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str) and _ID_LIKE.match(v):
        return "<id>"
    return v


def _content(step: TraceStep) -> dict[str, str]:
    """A step's output as the export shows it (cleaned, embargoed), ids normalised, one canonical string per key."""
    out = step.output if isinstance(step.output, dict) else {"output": step.output}
    doc = _norm(embargo(_clean(out)))
    return {str(k): json.dumps(v, sort_keys=True, default=str) for k, v in doc.items() if k not in _DECISION_KEYS}


def content_keys(failing: TraceStep, succeeding: TraceStep) -> list[str]:
    """Output keys whose (embargoed, id-normalised) content differs between the two steps."""
    f, s = _content(failing), _content(succeeding)
    return sorted(k for k in set(f) | set(s) if f.get(k) != s.get(k))


def _first_content(failing: list[TraceStep], succeeding: list[TraceStep], upto: int) -> dict[str, Any] | None:
    for j in range(min(upto, len(failing), len(succeeding))):
        f, s = failing[j], succeeding[j]
        if f.node_id != s.node_id:
            return None
        keys = content_keys(f, s)
        if keys:
            return {"step": j, "node": f.node_id, "keys": keys[:10],
                    "evidence": [step_ref(f.run_id, f.index), step_ref(s.run_id, s.index)]}
    return None


def first_divergence(failing: list[TraceStep], succeeding: list[TraceStep], *, failing_score: float | None
                     ) -> dict[str, Any] | None:
    """Causal signature at the first step where the failing trace differs from the succeeding one in node, route or
    error (with the first CONTENT difference at or before it); else at the first step whose output content differs;
    None if both runs did and wrote the same at every step."""
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
        return _signature_dict(i, fn, cause, criticality, mechanism, failing, succeeding,
                               _first_content(failing, succeeding, i + 1))
    if len(failing) != len(succeeding):
        node = failing[-1].node_id if failing else "?"
        if len(failing) < len(succeeding):
            cause = f"run ended after node '{node}' where the succeeding run continued to '{succeeding[n].node_id}'"
        else:
            cause = f"run continued to '{failing[n].node_id}' where the succeeding run ended"
        return _signature_dict(n, node, cause, criticality, "truncated", failing, succeeding,
                               _first_content(failing, succeeding, n))
    content = _first_content(failing, succeeding, n)
    if content is not None:
        cause = f"{content['node']}: output content differs from the succeeding run's in {content['keys']}"
        return _signature_dict(content["step"], content["node"], cause, criticality, "content", failing, succeeding,
                               content)
    return None


def _signature_dict(i: int, node: str, cause: str, criticality: str, mechanism: str, failing: list[TraceStep],
                    succeeding: list[TraceStep], content: dict[str, Any] | None) -> dict[str, Any]:
    # references use each step's OWN index (a trace with a gap must not make a position cite another step)
    refs = [step_ref(t[i].run_id, t[i].index) for t in (failing, succeeding) if i < len(t)]
    if i >= len(failing) and failing:  # truncated: the failing run's last step
        refs.insert(0, step_ref(failing[-1].run_id, failing[-1].index))
    later = [step_ref(st.run_id, st.index) for st in failing[i + 1:] if _error_sig(st)][:3]
    extra = [r for r in (content or {}).get("evidence", []) if r not in refs]
    return {"step": i, "node": node, "cause": cause, "criticality": criticality, "mechanism": mechanism,
            "content": None if content is None else {k: content[k] for k in ("step", "node", "keys")},
            "evidence": [*refs, *later, *extra]}


def known_ids(digest: dict[str, Any]) -> set[str]:
    return {c["id"] for c in digest.get("clusters", [])} | {d["id"] for d in digest.get("divergences", [])}


def check_claims(edit: Edit, digest: dict[str, Any]) -> dict[str, Any]:
    """Citations of an edit against the diagnosis it was given (claims.check_claims; kept for existing callers)."""
    return _check_claims(edit, digest)


# ---------------------------------------------------------------------------------------------- verifier linkage
def failed_checks(store: EvidenceStore, run_id: str) -> list[tuple[int, str]]:
    """(step, check) for every Kernel check that FAILED in a certificate the run produced, read exactly as the export
    summarises certificates (export._cert_summary), so a reference built from it resolves in the export."""
    cert_blobs: dict[str, Any] = {}
    for _seq, _kind, body, *_ in store.ledger_rows("certificate"):
        b = json.loads(body)
        if isinstance(b.get("cert_hash"), str):
            cert_blobs[b["cert_hash"]] = b.get("blob")
    out: list[tuple[int, str]] = []
    for st in store.trace(run_id):
        o = st.output if isinstance(st.output, dict) else {}
        for key in ("certificate", "replica_certificate"):
            if isinstance(o.get(key), dict):
                summary = _cert_summary(store, run_id, st.index, key, o[key], cert_blobs)
                out += [(st.index, str(c)) for c in summary["failed"]]
    return out


def _verifier(o: Any, checks: list[tuple[int, str]]) -> dict[str, Any]:
    return {"ref": verdict_ref(o.run_id), "score": o.score, "verified": o.verified, "status": o.status,
            "cert_label": o.cert_label, "failed_checks": [check_ref(o.run_id, i, c) for i, c in checks][:5]}


def resolvable_refs(trace: list[TraceStep], outcome: Any, checks: list[tuple[int, str]]) -> set[str]:
    """What a run contributes to the export: its trace steps, its verdict (it has an outcome), its failed checks."""
    return ({step_ref(st.run_id, st.index) for st in trace} | {verdict_ref(outcome.run_id)}
            | {check_ref(outcome.run_id, i, c) for i, c in checks})


def link(sig: dict[str, Any], refs: set[str]) -> bool:
    """A signature is linked iff it carries the failing run's verdict and every reference it carries resolves."""
    ver = sig.get("verifier") or {}
    failing = (ver.get("failing") or {}).get("ref")
    ev = sig.get("evidence") or []
    return bool(failing) and failing in ev and all(isinstance(r, str) and r in refs for r in ev)


def link_digest(digest: dict[str, Any], refs: set[str]) -> dict[str, Any]:
    """The digest with only the signatures whose every reference resolves in `refs` (e.g. claims.export_refs of the
    export the proposer reads); the others are dropped and counted in `unlinked`. Ids are kept (not renumbered), so a
    dropped D<n> is simply absent and citing it fails."""
    kept = [d for d in digest.get("divergences", []) if link(d, refs)]
    return {**digest, "divergences": kept,
            "unlinked": int(digest.get("unlinked", 0)) + len(digest.get("divergences", [])) - len(kept)}


def pair_divergences(failing: Iterable[Any], references: Iterable[Any], trace: Any, limit: int = 10, *,
                     checks: Callable[[str], list[tuple[int, str]]] | None = None
                     ) -> tuple[list[dict[str, Any]], list[str], int]:
    """For each failing outcome, the best-scoring succeeding reference of the SAME task (score strictly higher;
    ties broken by run id, so the choice is deterministic). Returns (divergences D0.., task ids without a
    reference, number of signatures dropped because their evidence did not link). `checks(run_id)`: failed Kernel
    checks of the run (default: none)."""
    checks = checks or (lambda _run_id: [])
    by_task: dict[str, list[Any]] = {}
    for r in references:
        if r.score is not None and r.run_id:
            by_task.setdefault(r.task_id, []).append(r)
    out: list[dict[str, Any]] = []
    missing: list[str] = []
    unlinked = 0
    for o in sorted(failing, key=lambda o: (o.task_id, o.seed, o.run_id)):
        if o.infra_failed or not o.run_id:
            continue
        better = [r for r in by_task.get(o.task_id, []) if r.run_id != o.run_id and r.score > (o.score or 0.0)]
        if not better:
            if o.task_id not in missing:
                missing.append(o.task_id)
            continue
        ref = sorted(better, key=lambda r: (-r.score, r.run_id))[0]
        f_trace, s_trace = trace(o.run_id), trace(ref.run_id)
        sig = first_divergence(f_trace, s_trace, failing_score=o.score)
        if sig is None:
            continue
        f_checks, s_checks = checks(o.run_id), checks(ref.run_id)
        sig["verifier"] = {"failing": _verifier(o, f_checks), "reference": _verifier(ref, s_checks)}
        sig["evidence"] = [*sig["evidence"], verdict_ref(o.run_id), verdict_ref(ref.run_id),
                           *sig["verifier"]["failing"]["failed_checks"]]
        if not link(sig, resolvable_refs(f_trace, o, f_checks) | resolvable_refs(s_trace, ref, s_checks)):
            unlinked += 1  # fail closed: a signature citing something that does not exist is never shown
            continue
        out.append({"id": f"D{len(out)}", "task": o.task_id, "failing_run": o.run_id, "reference_run": ref.run_id,
                    **sig})
        if len(out) >= limit:
            break
    return out, missing, unlinked
