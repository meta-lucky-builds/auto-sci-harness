"""Claim checks on a proposed edit (GOAL §5 L6.3, AutoSaddler + Self-Harness): every citation must resolve to evidence
the proposer was shown, and the change it claims must be the change its typed ops really make. Deterministic, no LLM.
Both checks are ENFORCED by the loop (loop.evolve_generation): an edit failing either is rejected before any
evaluation, with lineage status `rejected_claims` (fail closed).

Citations (`Edit.evidence` and `Edit.predicted.failure_clusters_fixed`). The only citation forms are:
    C<n> / D<n>                 a failure cluster / first-divergence signature of the diagnosis the proposer was given
    <run_id>#<step>             a trace step of an evolve-split run
    <run_id>#verdict            that run's verifier outcome (score, verified, status, certificate label)
    <run_id>#<step>/<check>     a FAILED Kernel check of the certificate produced at that step
A citation resolves only if it names something in the material the proposer saw: the diagnosis (`digest_refs`: its
ids and every reference its signatures carry) and, for the agentic proposer, the read-only evidence export
(`export_refs`: the traces, scores and certificate summaries actually written there). Anything else - a forged run
id, a step past the end of a trace, a check that did not fail, free text - is unsupported, and one unsupported
citation rejects the edit. An edit citing nothing is not rejected by this check (it claims nothing).

Claimed diff (`Edit.claimed_change`): the JSON-pointer paths of every spec value the proposer says its ops change. The
real diff (`spec_diff`) is computed from the parent and the edited spec (both as validated JSON): dict keys are
recursed, a list is ONE value (its path is the list's path), a key added or removed is its own path. The claim must
equal the real diff as a set; a claimed path that walks into a list is cut at that list. A missing claim is refused
(fail closed): an edit whose effect is not stated cannot be checked against it. The check's own result never echoes
a false claimed path (only the real diff, derived from the ops, and a count); the claimed paths themselves are stored
with the edit like its other proposer text, after the same screen (edits.screen_prediction: deny list, canaries,
private patterns, at most 32 paths of at most 200 chars).

Limits (stated): a citation that resolves is evidence that EXISTS, not evidence that SUPPORTS the edit; whether the
cited step explains the failure is not checked by code. The claimed-diff check proves the proposer knows what its ops
change; it does not prove the change does what the rationale says.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .edits import Edit

ID = re.compile(r"^[CD]\d{1,4}\Z")  # \Z, not $: `$` also matches before a trailing newline
REF = re.compile(r"^(?P<run>[A-Za-z0-9_\-]{1,64})#"
                 r"(?:(?P<verdict>verdict)|(?P<step>\d{1,6})(?:/(?P<check>[A-Za-z0-9_.\-]{1,64}))?)\Z")


def step_ref(run_id: str, index: int) -> str:
    return f"{run_id}#{index}"


def verdict_ref(run_id: str) -> str:
    return f"{run_id}#verdict"


def check_ref(run_id: str, index: int, check: str) -> str:
    return f"{run_id}#{index}/{check}"


# ---------------------------------------------------------------------------------------------- citations
def digest_refs(digest: dict[str, Any]) -> set[str]:
    """Every id and reference the diagnosis shows: cluster ids, divergence ids and each signature's evidence refs."""
    out = {str(c.get("id")) for c in digest.get("clusters", []) if isinstance(c, dict)}
    for d in digest.get("divergences", []):
        if isinstance(d, dict):
            out.add(str(d.get("id")))
            out.update(str(r) for r in d.get("evidence", []) if isinstance(r, str))
    return out


def export_refs(root: Path) -> set[str]:
    """Every reference that resolves in a written evidence export (export.py layout): trace steps, verdicts of scored
    runs, and failed checks of certificate summaries. Fail closed: an unreadable file contributes nothing."""
    root = Path(root)
    out: set[str] = set()
    for p in sorted((root / "traces").glob("*.json")):
        doc = _load(p)
        run = doc.get("run", {}) if isinstance(doc, dict) else {}
        rid = run.get("id") if isinstance(run, dict) else None
        if not isinstance(rid, str) or p.stem != rid:
            continue
        for st in doc.get("steps", []):
            if isinstance(st, dict) and isinstance(st.get("index"), int):
                out.add(step_ref(rid, st["index"]))
    scores = _load(root / "scores.json")
    for s in scores if isinstance(scores, list) else []:
        if isinstance(s, dict) and isinstance(s.get("run_id"), str) and s["run_id"]:
            out.add(verdict_ref(s["run_id"]))
    certs = _load(root / "certificates.json")
    for c in certs if isinstance(certs, list) else []:
        if isinstance(c, dict) and isinstance(c.get("run_id"), str) and isinstance(c.get("step"), int):
            out.update(check_ref(c["run_id"], c["step"], str(k)) for k in c.get("failed", []))
    return out


def _load(p: Path) -> Any:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_citations(edit: Edit, refs: Iterable[str]) -> dict[str, Any]:
    known = set(refs)
    cited = sorted({*edit.evidence, *edit.predicted.failure_clusters_fixed})
    supported = [x for x in cited if (ID.match(x) or REF.match(x)) and x in known]
    unsupported = [x for x in cited if x not in supported]
    return {"supported": supported, "unsupported": unsupported, "passed": not unsupported}


# ---------------------------------------------------------------------------------------------- claimed diff
def _esc(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _unesc(seg: str) -> str:
    return seg.replace("~1", "/").replace("~0", "~")


def spec_diff(before: Any, after: Any, path: str = "") -> list[str]:
    """Paths of every changed value: dicts recursed, lists atomic, added/removed keys at their own path."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[str] = []
        for k in sorted(set(before) | set(after), key=str):
            p = f"{path}/{_esc(str(k))}"
            if k not in before or k not in after:
                out.append(p)
            else:
                out += spec_diff(before[k], after[k], p)
        return out
    return [] if _canon(before) == _canon(after) else [path or "/"]


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def canonical_claim(path: str, before: Any, after: Any) -> str:
    """A claimed path at the real diff's granularity: cut where it walks into a list (in either document)."""
    if not isinstance(path, str) or not path.startswith("/"):
        return str(path)
    if path == "/":
        return path
    segs = [_unesc(s) for s in path[1:].split("/")]
    docs = [before, after]
    kept: list[str] = []
    for s in segs:
        if any(isinstance(d, list) for d in docs):
            break
        kept.append(s)
        docs = [d.get(s) if isinstance(d, dict) else None for d in docs]
    return "/" + "/".join(_esc(s) for s in kept) if kept else path


def check_claimed_diff(edit: Edit, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    real = spec_diff(before, after)
    if edit.claimed_change is None:
        return {"real": real, "passed": False, "reason": "no claimed_change: the edit does not state what it changes "
                                                         "(fail closed)"}
    claimed = {canonical_claim(p, before, after) for p in edit.claimed_change}
    unclaimed = [p for p in real if p not in claimed]  # real changes the proposer did not declare (hidden changes)
    n_false = len(claimed - set(real))  # declared changes that did not happen (never echoed: proposer text)
    ok = not unclaimed and not n_false
    reason = "claimed change equals the real diff" if ok else (
        f"claimed change differs from the real diff: {len(unclaimed)} real change(s) not claimed {unclaimed[:10]}, "
        f"{n_false} claimed change(s) that the ops do not make")
    return {"real": real, "unclaimed": unclaimed, "n_false_claims": n_false, "passed": ok, "reason": reason}


# ---------------------------------------------------------------------------------------------- both
def check_claims(edit: Edit, digest: dict[str, Any] | None = None, *, refs: Iterable[str] = (),
                 before: dict[str, Any] | None = None, after: dict[str, Any] | None = None) -> dict[str, Any]:
    """Citations against `digest_refs(digest) | refs` (pass digest=None and the full set in `refs` when the material
    shown was not this digest, e.g. the agentic proposer's re-linked digest plus its export); with `before`/`after`
    (parent and edited spec as JSON) also the claimed diff. `passed` is the conjunction; `reason` says which part
    failed."""
    out = check_citations(edit, (digest_refs(digest) if digest else set()) | set(refs))
    reasons = [] if out["passed"] else [f"{len(out['unsupported'])} citation(s) resolve to nothing the proposer was "
                                        f"shown"]
    if before is not None and after is not None:
        cd = check_claimed_diff(edit, before, after)
        out["claimed_diff"] = cd
        if not cd["passed"]:
            reasons.append(cd["reason"])
        out["passed"] = out["passed"] and cd["passed"]
    out["reason"] = "; ".join(reasons) if reasons else "citations resolve" + (
        " and the claimed change equals the real diff" if "claimed_diff" in out else "")
    return out
