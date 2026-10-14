"""Approval queue (GOAL §5 L9): every item waiting for a HUMAN decision, rebuilt from the store. Read-only.

Four kinds, each with the exact command that decides it:
- `approval_node`: a PAUSED run waiting at an approval node, with no human decision for its exact item yet;
- `adapter`: a registered domain adapter whose CURRENT content has no human approval (it cannot be used);
- `role`: a dataset registered as external/future whose current role and content no human approved (it supports at
  most CERTIFIED-INTERNAL until approved);
- `statement`: a pre-registered formal claim whose Lean statement (by hash) no human approved (K1 fails).
"""

from __future__ import annotations

from typing import Any

from ..kernel import Kernel
from ..kernel.adapters import REGISTRY, adapter_hash
from ..kernel.claim import EvidenceClass
from ..kernel.vault import EXTERNAL_ROLES
from ..records import RunStatus
from ..store import EvidenceStore


def pending(store: EvidenceStore) -> list[dict[str, Any]]:
    from ..runtime.engine import approval_decision, paused_approval
    kernel = Kernel(store)
    out: list[dict[str, Any]] = []
    for run in reversed(store.list_runs(-1)):  # oldest first; SQLite LIMIT -1 = all rows
        status, _ = store.run_status(run.id)
        if status != RunStatus.PAUSED:
            continue
        pause = paused_approval(store, run.id)
        if pause is None:
            continue
        if approval_decision(store, run.id, pause["node_id"], pause["item_hash"]) is not None:
            continue  # decided; waiting only for `chh resume`
        out.append({"kind": "approval_node", "run_id": run.id, "node_id": pause["node_id"],
                    "item_hash": pause["item_hash"], "request_seq": pause.get("request_seq"),
                    "prompt": pause.get("prompt", ""),
                    "decide": f"chh approve {run.id} {pause['node_id']} --decision approve|reject --approver NAME "
                              f"--item-hash {pause['item_hash']}"})
    approvals = kernel.ledger.entries("adapter_approval")
    for name, (spec, _) in sorted(REGISTRY.items()):
        h = adapter_hash(spec)
        if not any(e.body.get("name") == name and e.body.get("hash") == h for e in approvals):
            out.append({"kind": "adapter", "name": name, "version": spec.version, "hash": h,
                        "decide": f"chh approve-adapter {name} --approver NAME"})
    for ds_id, rec in sorted(kernel.vault.datasets().items()):
        if rec.role in EXTERNAL_ROLES and kernel.vault.role_approval(ds_id) is None:
            out.append({"kind": "role", "dataset_id": ds_id, "role": rec.role.value, "dataset_hash": rec.hash,
                        "decide": f"chh approve-role {ds_id} --approver NAME --note WHY"})
    approved = {e.body.get("hash") for e in kernel.ledger.entries("statement_approval")
                if str(e.body.get("approver", "")).strip()}
    seen: set[str] = set()
    for e in kernel.ledger.entries("prereg"):
        claim = e.body.get("claim", {})
        if claim.get("evidence_class") != EvidenceClass.FORMAL.value:
            continue
        subject = claim.get("subject", {})
        h = subject.get("hash")
        if h in approved or h in seen:
            continue
        seen.add(h)
        out.append({"kind": "statement", "artifact_id": subject.get("artifact_id"), "statement_hash": h,
                    "claim_hash": e.body.get("claim_hash"), "prereg_seq": e.seq,
                    "decide": f"chh approve-statement FILE --artifact-id {subject.get('artifact_id')} --approver "
                              f"NAME --statement-hash {h}"})
    return out
