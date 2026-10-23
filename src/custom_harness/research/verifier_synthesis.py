"""Verifier synthesis in the research loop (GOAL §5 L3 "Verifier synthesis"; `verify/synthesis.py`).

`research.synthesize_verifier` (params: `problem`, optional `role`, default "verifier_author", falling back to
"executor"): for a soft problem, the model bound to the role writes a verifier spec in the closed vocabulary
(property tests, invariants, a small simulator). The call goes through `ModuleContext.llm` (budget-checked and
traced). Trusted code then parses it, runs the fault-injection validation against the problem's suite and computes
its tier from the Kernel ledger. The spec is stored as a blob; state gets a record:

    verifier_synthesis: {problem, ref, verifier_hash, suite_authorship, validated, tier, error}

What state carries is a HINT for the next nodes, never trusted: `usable_tier(ctx, ref)` re-reads the blob,
re-parses it through the vocabulary, re-validates it and recomputes the tier from the ledger at every use, so a
later (evolvable) node that rewrites `tier` in state changes nothing. Validation is reported as one bit
(`validated`); which golden cases or planted faults failed is NOT put in state (the sealed half must stay unseen by
the author), and no hash over the suite (it covers the sealed cases and would allow offline guess-checking).
Looks at the sealed half (GOAL invariant 20; s3-verifiers review M4, closed by s4-integrity): every validation of a NEW
verifier (problem, verifier hash) is a look, recorded BEFORE it is taken as an append-only `sealed_look` store record
(the store is never mounted for agents), and a problem allows at most `SYNTH_MAX_SEALED_LOOKS` distinct verifiers:
after that the module refuses to validate (validated False, error "sealed-look budget spent"), so a loop re-running
synthesis learns at most that many bits about the sealed half. Re-validating an already-looked verifier (usable_tier)
reveals nothing new (validation is deterministic) and is not counted again. The module is pinned to human-only nodes
(evolve/edits.HUMAN_ONLY_MODULE_PREFIXES): no evolved harness may add, move or re-parameterise it. Stated limit: the
record is in the evidence store, not the Kernel ledger (a `sealed_look` ledger kind is requested from s4-kernel).

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
LOOK_KIND = "sealed_look"
# Distinct verifiers per problem that may be validated against the sealed half (one bit each). UNCALIBRATED (owner
# decision, GOAL §13.8).
SYNTH_MAX_SEALED_LOOKS = 8


def _looked(ctx: ModuleContext, problem: str) -> set[str]:
    return {str(r.get("verifier_hash")) for r in ctx.store.records_where(LOOK_KIND, "problem", problem)}


def record_sealed_look(ctx: ModuleContext, problem: str, verifier_hash: str) -> bool:
    """Record (before it happens) a look at `problem`'s sealed half by a new verifier. False = the problem's look
    budget is spent: the caller must not validate. An already-recorded verifier is allowed again, uncounted."""
    seen = _looked(ctx, problem)
    if verifier_hash in seen:
        return True
    if len(seen) >= SYNTH_MAX_SEALED_LOOKS:
        return False
    ctx.store.append_record(LOOK_KIND, {"problem": problem, "verifier_hash": verifier_hash, "run_id": ctx.run_id,
                                        "half": "sealed", "by": "research.synthesize_verifier"})
    return True


def usable_tier(ctx: ModuleContext, ref: str) -> Tier | None:
    """The tier of the stored verifier at `ref`, recomputed now (None = unusable or unreadable)."""
    try:
        v = S.from_record(ctx.store.blobs.get_json(ref))
    except (S.SynthesisError, OSError, ValueError, KeyError):
        return None
    if v.hash() not in _looked(ctx, v.problem):  # a verifier never validated by the module: no unrecorded look
        return None
    return S.tier(v, ctx.kernel().ledger)


@register("research.synthesize_verifier")
def synthesize_verifier(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    problem = str(params.get("problem", ""))
    role = str(params.get("role", ROLE))
    out: dict[str, Any] = {"problem": problem, "ref": None, "verifier_hash": None, "suite_authorship": None,
                           "validated": False, "tier": None, "error": None}
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
    ref = ctx.store.blobs.put_json(S.to_record(v))
    if not record_sealed_look(ctx, problem, v.hash()):
        out.update(ref=ref, verifier_hash=v.hash(), error=f"sealed-look budget spent ({SYNTH_MAX_SEALED_LOOKS} "
                                                           f"verifiers validated for this problem)")
        return {"verifier_synthesis": out}
    val = S.validate(v)
    t = S.tier(v, ledger)
    out.update(ref=ref, verifier_hash=v.hash(),
               suite_authorship=S.suite_status(problem, ledger), validated=val.passed,
               tier=t.name if t is not None else None)
    return {"verifier_synthesis": out}


__all__ = ["LOOK_KIND", "ROLE", "SYNTH_MAX_SEALED_LOOKS", "record_sealed_look", "synthesize_verifier", "usable_tier"]
