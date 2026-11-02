"""HUMAN decisions on approval nodes and on Lean statements (GOAL §4.7, invariant 15).

The ONLY code paths that write an approval-node decision (ledger kind `approval` for a run's node) and a
`statement_approval` entry. `chh approve` and `chh approve-statement` are thin wrappers around them.

Approval node: the decision binds (run id, node id, sha256 of the canonical JSON of the item). The item is
recomputed from the PAUSED run's checkpoint, never taken from the trace; it must hash to what the engine recorded
when it paused, and, when the caller names a hash (`item_hash`, the hash the human reviewed), to that hash too. The
engine honours a decision only if it was appended after its own `approval_request` for that exact item
(runtime/engine.py `approval_decision`), so nothing written before the pause can approve it.

Physical/empirical protocols (GOAL §4.7) pause at `protocol_review` (the protocol + claim) and at `data_received`
(the returned dataset, registered by the human `chh return-data`). Both are approval nodes decided here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..kernel.ledger import Entry, Ledger
from ..records import RunStatus
from ..store import EvidenceStore
from .guard import HumanActionRefused, require_human

DECISIONS = {"approve": "approved", "reject": "rejected"}


@dataclass(frozen=True)
class NodeItem:
    run_id: str
    node_id: str
    prompt: str
    item_key: str
    item: Any
    item_hash: str


def node_item(store: EvidenceStore, run_id: str, node_id: str) -> NodeItem:
    """The item a PAUSED run waits on at `node_id`, recomputed from its checkpoint (read-only)."""
    from ..runtime import ApprovalNode, HarnessSpec
    from ..runtime.engine import approval_item_hash, paused_approval
    from ..runtime.template import lookup
    try:
        status, _ = store.run_status(run_id)
    except KeyError:
        raise HumanActionRefused(f"no run {run_id!r} in this store") from None
    if status != RunStatus.PAUSED:
        raise HumanActionRefused(f"run {run_id} is {status.value}, not paused")
    pause = paused_approval(store, run_id)
    if pause is None or pause["node_id"] != node_id:
        at = pause["node_id"] if pause else "?"
        raise HumanActionRefused(f"run {run_id} is not paused at node '{node_id}' (paused at '{at}')")
    run = store.get_run(run_id)
    node = HarnessSpec.model_validate(store.harness_spec(run.harness_hash)).nodes.get(node_id)
    if not isinstance(node, ApprovalNode):
        raise HumanActionRefused(f"node '{node_id}' is not an approval node")
    item = lookup(pause["state"], node.item_key)  # from the paused checkpoint, never trusted from the trace
    h = approval_item_hash(item)
    if h != pause.get("item_hash"):
        raise HumanActionRefused(f"the checkpointed item hashes to {h}, the pause recorded {pause.get('item_hash')}; "
                                 "store inconsistent")
    return NodeItem(run_id, node_id, node.prompt, node.item_key, item, h)


def approve_node(store: EvidenceStore, run_id: str, node_id: str, *, decision: str, approver: str,
                 item_hash: str | None, note: str = "") -> Entry:
    """HUMAN action: approve or reject the item run `run_id` is paused on at `node_id`.

    `item_hash` is the hash of the item the human reviewed; a UI must always pass it. None is accepted only for the
    legacy `chh approve` flow, which prints the item immediately before recording (an owner decision may make the
    hash mandatory there too)."""
    name = require_human(approver, what="the approver")
    if decision not in DECISIONS:
        raise HumanActionRefused(f"decision must be one of {sorted(DECISIONS)}")
    it = node_item(store, run_id, node_id)  # a killed run is FAILED, so this already refuses it ("not paused")
    from .actions import refuse_if_killed
    refuse_if_killed(store, run_id)  # defence in depth: the kill entry is the ledgered truth
    if item_hash is not None and item_hash != it.item_hash:
        raise HumanActionRefused(f"--item-hash {item_hash} does not match the item hash {it.item_hash}")
    return Ledger(store).append("approval", {"run_id": run_id, "node_id": node_id, "item_hash": it.item_hash,
                                             "decision": DECISIONS[decision], "approver": name, "note": note or ""})


@dataclass(frozen=True)
class StatementView:
    theorem: str
    source: str
    statement_hash: str


def statement_view(data: bytes) -> StatementView:
    """Parse and hygiene-check a chh-lean-statement/1 file; what the human must read before approving it."""
    from ..kernel.formal import FormalError, hygiene, parse_statement
    try:
        st = parse_statement(data)
    except FormalError as e:
        raise HumanActionRefused(str(e)) from None
    problems = hygiene(st.source, statement=True)
    if problems:
        raise HumanActionRefused("; ".join(problems))
    return StatementView(st.theorem, st.source, hashlib.sha256(data).hexdigest())


def approve_statement(store: EvidenceStore, data: bytes, *, artifact_id: str, approver: str,
                      statement_hash: str | None, note: str = "") -> Entry:
    """HUMAN action: approve exactly this Lean statement (bound to the sha256 of the file's bytes), register it as
    an artifact and ledger the approval. A formal claim's K1 requires it."""
    name = require_human(approver, what="the approver")
    view = statement_view(data)
    if statement_hash is not None and statement_hash != view.statement_hash:
        raise HumanActionRefused(f"--statement-hash {statement_hash} does not match the file's hash "
                                 f"{view.statement_hash}")
    from ..kernel.vault import VaultError
    from ..kerneld.scope import ScopedKernel
    kernel = ScopedKernel(store)
    try:
        kernel.vault.register_artifact(artifact_id, data, "lean-statement")
    except VaultError as e:
        raise HumanActionRefused(str(e)) from None
    return kernel.ledger.append("statement_approval", {"artifact_id": artifact_id, "hash": view.statement_hash,
                                                       "theorem": view.theorem, "approver": name,
                                                       "note": note or ""})


def render_item(it: NodeItem, max_chars: int) -> str:
    text = json.dumps(it.item, indent=1, ensure_ascii=False, sort_keys=True)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n...[{len(text) - max_chars} chars not shown]"
    return text
