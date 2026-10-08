"""Verifier synthesis in the research loop (GOAL §5 L3 "Verifier synthesis"; `verify/synthesis.py`).

`research.synthesize_verifier` (params: `problem`, optional `role`, default "verifier_author", falling back to
"executor"): for a soft problem, the model bound to the role writes a verifier spec in the closed vocabulary
(property tests, invariants, a small simulator). The call goes through `ModuleContext.llm` (budget-checked and
traced). Trusted code then parses it, runs the fault-injection validation against the problem's suite and computes
its tier from the Kernel ledger. The spec is stored as a blob; state gets a record:

    verifier_synthesis: {problem, ref, verifier_hash, suite_hash, suite_authorship, validated, tier, error}

What state carries is a HINT for the next nodes, never trusted: `usable_tier(ctx, ref)` re-reads the blob,
re-parses it through the vocabulary, re-validates it and recomputes the tier from the ledger at every use, so a
later (evolvable) node that rewrites `tier` in state changes nothing. Validation is reported as one bit
(`validated`); which golden cases or planted faults failed is NOT put in state (the sealed half must stay unseen by
the author). Stated limit: a loop that re-runs synthesis can still learn one bit per attempt about the sealed half.

The tier is capped at T2 (`l3_config.SYNTH_UNREVIEWED_TIER_CAP`) unless a human approved both the verifier and its
suite in the ledger; every suite in code is AGENT-AUTHORED, so today every synthesized verifier is at most T2.
"""

from __future__ import annotations

from typing import Any

from ..gateway.base import ChatRequest
from ..runtime import register
from ..runtime.modules import ModuleContext
from ..tiers import Tier
from ..verify import synthesis as S

ROLE = "verifier_author"


def usable_tier(ctx: ModuleContext, ref: str) -> Tier | None:
    """The tier of the stored verifier at `ref`, recomputed now (None = unusable or unreadable)."""
    try:
        v = S.from_record(ctx.store.blobs.get_json(ref))
    except (S.SynthesisError, OSError, ValueError, KeyError):
        return None
    return S.tier(v, ctx.kernel().ledger)


@register("research.synthesize_verifier")
def synthesize_verifier(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    problem = str(params.get("problem", ""))
    role = str(params.get("role", ROLE))
    out: dict[str, Any] = {"problem": problem, "ref": None, "verifier_hash": None, "suite_hash": None,
                           "suite_authorship": None, "validated": False, "tier": None, "error": None}
    backend = ctx.backends.get(role) or ctx.backends.get("executor")
    if backend is None:
        out["error"] = f"no backend bound to role {role!r}"
        return {"verifier_synthesis": out}

    def complete(req: ChatRequest) -> Any:
        return ctx.llm(role, req)

    try:
        v = S.synthesize(backend, problem, complete=complete)
    except S.SynthesisError as e:
        out["error"] = f"spec refused: {e}"[:500]
        return {"verifier_synthesis": out}
    ledger = ctx.kernel().ledger
    val = S.validate(v)
    t = S.tier(v, ledger)
    out.update(ref=ctx.store.blobs.put_json(S.to_record(v)), verifier_hash=v.hash(), suite_hash=val.suite_hash,
               suite_authorship=S.suite_status(problem, ledger), validated=val.passed,
               tier=t.name if t is not None else None)
    return {"verifier_synthesis": out}


__all__ = ["ROLE", "synthesize_verifier", "usable_tier"]
