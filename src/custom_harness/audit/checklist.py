"""The VG reporting checklist of a Claim Audit (GOAL §1.3, VG Table 10: "a missing item blocks publication").

`audit publish` is the only export path of an audit. Until 2026-10-09 it wrote `report.md` (verdict, effects, checks,
assumptions) with NO reporting checklist: traces, cost, selection policy and the human-in-the-loop points were never
stated, and nothing blocked that. The checklist is filled here by the Kernel's `fill` from the certificate and the
store (outside the Kernel), plus the loop-label item (`publication.py`): an audit is not a loop, so it says so and may
not claim MECHANICAL or VALIDATED. `gate.publish` refuses unless `publication.blockers` is empty, and ships the
checklist (`checklist.md`, its sha256 and the items in `manifest.json`) next to the report.
An audit without a certificate (COULD NOT RUN) has no checklist to fill and is refused (fail closed; owner decision
whether such a notice may be published another way).
"""

from __future__ import annotations

from typing import Any

from ..kernel import Kernel
from ..kernel.checklist import fill
from ..kernel.checklist import render as render_checklist
from ..publication import NOT_A_LOOP_PREFIX, render_loop_item
from ..records import Cost
from ..store import EvidenceStore

NOT_A_LOOP = (f"{NOT_A_LOOP_PREFIX} a Claim Audit is one pre-registered confirmation of a published claim "
              f"(GOAL §16.10), not an optimisation loop")


def _models(store: EvidenceStore, run_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    for rid in run_ids:
        for step in store.trace(rid):
            provs = [step.provenance.model_dump(mode="json")] if step.provenance else []
            out = step.output if isinstance(step.output, dict) else {}
            provs += [c.get("provenance", {}) for c in out.get("_module_llm_calls", []) or []]
            seen |= {f"{p.get('backend')}/{p.get('model')} ({p.get('mode')})" for p in provs if p}
    return sorted(seen)


def audit_checklist(store: EvidenceStore, outcome: dict[str, Any], bundle_hash: str | None,
                    reply: dict[str, Any] | None, approval: dict[str, Any] | None) -> dict[str, str | None]:
    """The filled checklist of a certified audit outcome (KeyError/KernelError if its certificate is absent)."""
    from .gate import FIDELITY, _entries  # the gate's own approval reader (no import cycle at module load)
    kernel = Kernel(store)
    cert = kernel.certificate(outcome["cert_hash"])
    produced = [store.get_evidence(e).produced_by_run for e in cert.evidence_ids]
    run_ids = sorted({r for r in produced if r})  # evidence without a producing run has no trace: `traces` says so
    run_costs: dict[str, Cost] = {r: store.run_cost(r) for r in run_ids}
    fid = [e for e in _entries(store, outcome["audit_id"], FIDELITY)
           if (e.body.get("audit") or {}).get("claim_hash") == outcome["claim_hash"]]
    roles = []
    for ds in cert.claim["generalization_targets"]:
        try:
            ra = kernel.vault.role_approval(ds)
        except (KeyError, ValueError):
            ra = None
        roles.append(f"{ds}: role approved by {ra.body.get('approver')} (ledger {ra.seq})" if ra else
                     f"{ds}: NO role approval found")
    hitl = [f"claim fidelity approved by {fid[-1].body.get('approver')} (ledger {fid[-1].seq})" if fid else
            "claim fidelity: NO approval found", *roles,
            f"right of reply recorded by {reply['recorded_by']} (ledger {reply['ledger_seq']})" if reply else
            "right of reply: NOT recorded",
            f"publication approved by {approval['approver']} (ledger {approval['ledger_seq']})" if approval else
            "publication: NOT approved"]
    # a missing human step is no answer: the item stays empty and blocks publication (fail closed)
    complete = bool(fid) and all("NO role approval" not in r for r in roles) and bool(reply) and bool(approval)
    checklist = fill(cert, run_costs=run_costs, models=_models(store, run_ids), bundle_hash=bundle_hash,
                     selection_policy=("no selection among alternatives: one claim typed by a human from the "
                                       "paper's sentence and pre-registered before the fresh data were read"),
                     human_in_the_loop="; ".join(hitl) if complete else None,
                     novelty="not applicable: a Claim Audit confirms an already published claim and makes no new "
                             "claim")
    checklist["loop_label"] = NOT_A_LOOP
    return checklist


def render(checklist: dict[str, Any]) -> str:
    return render_checklist(checklist).rstrip("\n") + "\n" + render_loop_item(checklist) + "\n"
