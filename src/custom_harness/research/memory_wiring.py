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

- `after_certify` also runs the production skill writer (`memory/writer.py`, s3-verifiers) for a CERTIFIED
  certificate of this run: a playbook entry citing the certificate, quoting the certified candidate's method text as
  data, proposed (quarantined) and admitted only through the store's gate with the exploit check against the
  perturbed evaluator (`exploit_check` below, built from the run's sandbox and the task's public development data).
  Without a sandbox spawn or a target column the writer is not run and the record says why (no skill without the
  check). With the human-owned tier table (`tiers.DEFAULT_POLICY[ADMIT_SKILL]` = T1) a T2 research certificate is
  below the skill floor, so the gate REJECTS such skills until the owner sets a T2 floor (stated, not hidden).

Memory never changes a certificate or a route: a memory failure is recorded in state and the loop goes on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..errors import InfraError
from ..kernel import Kernel, KernelError
from ..memory import MemoryRefused, MemoryStore, NotReadable
from ..memory.model import SKILL_KINDS
from ..memory.store import CERTIFIED
from ..memory.writer import SkillWriter, WriterRefused
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


def _hypothesis_text(state: dict[str, Any], best: dict[str, Any]) -> str:
    hyps = {str(h.get("id")): h for h in state.get("hypotheses") or [] if isinstance(h, dict)}
    for key in ("hypothesis_id", "root_hypothesis"):
        h = hyps.get(str(best.get(key)))
        if h and isinstance(h.get("idea"), str):
            return str(h["idea"])
    return str(best.get("notes") or "(no method text recorded)")


def write_skill(kernel: Kernel, state: dict[str, Any], cert_hash: str, secrets: tuple[str, ...], ns: str, *,
                run_id: str | None, spawn: Callable[[str], Any] | None, targets: Mapping[str, str] | None,
                candidate_key: str = "best") -> dict[str, Any]:
    """The production skill write for this run's certificate (memory/writer.py). Returns a record for state."""
    if kernel.certificate(cert_hash).label not in CERTIFIED:
        return {"written": False, "reason": "not a certified outcome"}
    if not run_id or spawn is None or not targets:
        return {"written": False, "reason": "no perturbed evaluator available (sandbox spawn / target column): "
                                            "skills are not written without the exploit check"}
    best = state.get(candidate_key)
    if not isinstance(best, dict) or not isinstance(best.get("code_ref"), str):
        return {"written": False, "reason": f"no certified candidate {candidate_key!r} in state"}
    store = MemoryStore(kernel, namespace=ns, exploit_check=exploit_check(kernel, spawn, dict(targets)))
    try:
        r = SkillWriter(store).write_playbook(cert_hash, run_id=run_id, method=_hypothesis_text(state, best),
                                              method_subject_hash=best["code_ref"], secrets=secrets)
    except (WriterRefused, MemoryRefused) as e:
        return {"written": False, "reason": str(e)[:300]}
    return {"written": True, "entry": r.entry_id, "status": r.status.value, "passed": r.passed,
            "reasons": [x[:200] for x in r.reasons[:3]]}


def after_certify(kernel: Kernel, state: dict[str, Any], cert_hash: str, secrets: tuple[str, ...],
                  namespace: str | None = None, *, run_id: str | None = None,
                  spawn: Callable[[str], Any] | None = None, targets: Mapping[str, str] | None = None,
                  candidate_key: str = "best") -> dict[str, Any]:
    """Harvest negative memory from verified failures and write a skill for a certified outcome of this run (both
    through the production writer), then count this certificate as an outcome of every skill rendered into this
    run's prompt. Returns a record for state; never raises for a memory problem."""
    ns = namespace or str((state.get("memory") or {}).get("namespace") or MEMORY_NAMESPACE)
    out: dict[str, Any] = {"namespace": ns, "harvested": [], "outcomes": [], "skill": None, "error": None}
    try:
        store = MemoryStore(kernel, namespace=ns)
        out["harvested"] = [{"entry": r.entry_id, "status": r.status.value, "passed": r.passed}
                            for r in SkillWriter(store).write_negatives(secrets=secrets)]
        out["skill"] = write_skill(kernel, state, cert_hash, secrets, ns, run_id=run_id, spawn=spawn,
                                   targets=targets, candidate_key=candidate_key)
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


__all__ = ["EMPTY", "MAX_RECALL", "MEMORY_NAMESPACE", "after_certify", "exploit_check", "recall_memory",
           "write_skill"]
