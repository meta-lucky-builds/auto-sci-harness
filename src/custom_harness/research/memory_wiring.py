"""L5 memory in the research loop (GOAL §5 L5; docs/memory.md "integration hooks").

- `research.recall_memory` (before `hypothesize`): readable memory entries are rendered by `MemoryStore.render` as
  quoted, untrusted DATA inside the store's wrapper and put in state as `memory.context`, which the hypothesize
  prompt quotes. The render screen gets the run's `_private*` values (`ctx.secrets`, set by the engine) besides the
  vault's canaries, so an entry carrying a canary or a private value of this run never reaches a prompt: it is
  withheld (counted, by id) and the rest is rendered. Retracted certificates are honoured because every read
  syncs the store first (their dependent entries are REVOKED and unreadable).
- `after_certify` (called by `research.certify`): `harvest_negatives()` turns every verified failure in the Kernel
  ledger into negative memory (gated as usual), and `record_outcome()` counts this run's certificate for every
  playbook/method entry that was rendered into this run's prompt ("used" = shown to the proposer: the harness
  cannot tell whether the model followed it; stated limit).

Memory never changes a certificate or a route: a memory failure is recorded in state and the loop goes on.
"""

from __future__ import annotations

from typing import Any

from ..errors import InfraError
from ..kernel import Kernel, KernelError
from ..memory import MemoryRefused, MemoryStore, NotReadable, harvest_negatives
from ..memory.model import SKILL_KINDS
from ..runtime import register
from ..runtime.modules import ModuleContext

MEMORY_NAMESPACE = "research"  # the research loop's namespace in the memory store
MAX_RECALL = 8  # UNCALIBRATED (owner decision, GOAL §13.8): entries rendered into one prompt
MAX_CONTEXT_CHARS = 12_000  # UNCALIBRATED (owner decision, GOAL §13.8): rendered memory per prompt (under the
#                             template's truncation limit, so the wrapper's END line is never cut off)
EMPTY = "(no memory entries)"
_ERRORS = (NotReadable, MemoryRefused, KernelError, InfraError, ValueError)


@register("research.recall_memory")
def recall_memory(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext) -> dict[str, Any]:
    ns = str(params.get("namespace", MEMORY_NAMESPACE))
    k = int(params.get("max_entries", MAX_RECALL))
    reader = f"run:{ctx.run_id}"
    try:
        store = MemoryStore(ctx.kernel(), namespace=ns)
        entries = store.query(reader=reader, purpose="hypothesize prompt")[:k]
        kept, withheld = [], []
        for e in entries:  # screened one by one, so one bad entry withholds itself, not the others
            try:
                store.render([e], reader=reader, secrets=ctx.secrets)
            except NotReadable:
                withheld.append(e.id)
                continue
            kept.append(e)
        text = store.render(kept, reader=reader, secrets=ctx.secrets) if kept else EMPTY
        while kept and len(text) > MAX_CONTEXT_CHARS:  # drop the lowest-ranked entries until it fits
            kept.pop()
            text = store.render(kept, reader=reader, secrets=ctx.secrets) if kept else EMPTY
    except _ERRORS as e:
        return {"memory": {"namespace": ns, "context": EMPTY, "entries": [], "skills": [], "withheld": [],
                           "error": f"{type(e).__name__}: {e}"[:500]}}
    return {"memory": {"namespace": ns, "context": text, "entries": [e.id for e in kept],
                       "skills": [e.id for e in kept if e.kind in SKILL_KINDS], "withheld": withheld,
                       "error": None}}


def after_certify(kernel: Kernel, state: dict[str, Any], cert_hash: str, secrets: tuple[str, ...],
                  namespace: str | None = None) -> dict[str, Any]:
    """Harvest negative memory from verified failures, then count this certificate as an outcome of every skill
    rendered into this run's prompt. Returns a record for state; never raises for a memory problem."""
    ns = namespace or str((state.get("memory") or {}).get("namespace") or MEMORY_NAMESPACE)
    out: dict[str, Any] = {"namespace": ns, "harvested": [], "outcomes": [], "error": None}
    try:
        store = MemoryStore(kernel, namespace=ns)
        out["harvested"] = [{"entry": r.entry_id, "status": r.status.value, "passed": r.passed}
                            for r in harvest_negatives(store, secrets=secrets)]
        for eid in (state.get("memory") or {}).get("skills") or []:
            try:
                m = store.record_outcome(eid, cert_hash)
            except ValueError as e:  # refused by the store (already counted, integrity failure, unreadable...)
                out["outcomes"].append({"entry": eid, "refused": str(e)[:300]})
                continue
            out["outcomes"].append({"entry": eid, "successes": m.successes, "failures": m.failures,
                                    "level": m.level})
    except _ERRORS as e:
        out["error"] = f"{type(e).__name__}: {e}"[:500]
    return out


def exploit_check(kernel: Kernel, spawn: Any, targets: dict[str, str], *, min_retained: float = 0.5) -> Any:
    """The L3 exploit quarantine for skills: research.perturbed.PerturbedTaskEvaluator behind the memory package's
    PerturbedEvaluatorCheck (built here because only allowlisted modules may import the memory package). Pass it as
    MemoryStore(..., exploit_check=...)."""
    from ..memory import PerturbedEvaluatorCheck
    from .perturbed import PerturbedTaskEvaluator
    return PerturbedEvaluatorCheck(PerturbedTaskEvaluator(kernel, spawn, targets), min_retained=min_retained)


__all__ = ["EMPTY", "MAX_RECALL", "MEMORY_NAMESPACE", "after_certify", "exploit_check", "recall_memory"]
