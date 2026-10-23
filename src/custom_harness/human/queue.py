"""The approval queue (GOAL §5 L9): every pending HUMAN action across the system, rebuilt from the store and the
repository. READ-ONLY: it writes nothing; each action is taken by its own command, which records the human.

Every item has `kind`, `binds` (the exact hashed item the action will bind, invariant 15) and `decide` (the command
that takes it). Kinds, in order:

- `approval_node`        a PAUSED run waiting at an approval node with no decision for its exact item yet
- `physical_protocol`    the same, at `protocol_review` (a physical/empirical protocol, GOAL §4.7)
- `physical_data`        the same, at `data_received` (returned data; `chh return-data` first)
- `adapter`              a registered domain adapter whose CURRENT content no human approved (unusable until then)
- `statement`            a pre-registered formal claim whose Lean statement (by hash) no human approved
- `role`                 an external/future dataset whose current content and role no human approved
- `alpha_account`        a held-out target with no alpha-account and no claim yet (OPTIONAL: without one the target
                         keeps the declared-n_comparisons rule; it can only be opened before the first claim)
- `suite_review`         a verifier fault suite with no human decision on its current hash (all are AGENT-AUTHORED)
- `verifier_review`      a validated synthesized verifier with no human decision on (its hash, its suite's hash)
- `feature_ruling`       a feature routed by the latest provenance screen of a task with no current ruling
- `provider_signoff`     a usable provider registry entry with no recent ledgered human sign-off of its current hash
- `human_check`          invariant 22: no human-held check, or one whose hash no human registered in the ledger
- `human_check_run`      a registered human-held check with no passing ledgered run of its exact file

A source that cannot be read becomes a `queue_error` item (visible), never a silently shorter queue.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..records import RunStatus
from ..store import EvidenceStore

PHYSICAL_NODES = {"protocol_review": "physical_protocol", "data_received": "physical_data"}
KIND_ORDER = ("approval_node", "physical_protocol", "physical_data", "adapter", "statement", "role", "alpha_account",
              "suite_review", "verifier_review", "feature_ruling", "provider_signoff", "human_check",
              "human_check_run", "queue_error")


def _approval_nodes(store: EvidenceStore) -> list[dict[str, Any]]:
    from ..runtime.engine import approval_decision, paused_approval
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
        node, h = pause["node_id"], pause["item_hash"]
        decide = (f"chh approve {run.id} {node} --decision approve|reject --approver NAME --item-hash {h}")
        if node == "data_received":
            decide = f"chh return-data {run.id} --file F --attest TEXT (if not yet registered), then: {decide}"
        out.append({"kind": PHYSICAL_NODES.get(node, "approval_node"), "run_id": run.id, "node_id": node,
                    "item_hash": h, "request_seq": pause.get("request_seq"), "prompt": pause.get("prompt", ""),
                    "binds": {"run_id": run.id, "node_id": node, "item_hash": h}, "decide": decide})
    return out


def _adapters(store: EvidenceStore) -> list[dict[str, Any]]:
    from ..kernel import Ledger
    from ..kernel.adapters import REGISTRY, adapter_hash
    approvals = Ledger(store).entries("adapter_approval")
    out: list[dict[str, Any]] = []
    for name, (spec, _) in sorted(REGISTRY.items()):
        h = adapter_hash(spec)
        if not any(e.body.get("name") == name and e.body.get("hash") == h for e in approvals):
            out.append({"kind": "adapter", "name": name, "version": spec.version, "hash": h,
                        "binds": {"adapter_hash": h},
                        "decide": f"chh approve-adapter {name} --approver NAME --adapter-hash {h}"})
    return out


def _statements(store: EvidenceStore) -> list[dict[str, Any]]:
    from ..kernel import Ledger
    from ..kernel.claim import EvidenceClass
    ledger = Ledger(store)
    approved = {e.body.get("hash") for e in ledger.entries("statement_approval")
                if str(e.body.get("approver", "")).strip()}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for e in ledger.entries("prereg"):
        claim = e.body.get("claim", {})
        if claim.get("evidence_class") != EvidenceClass.FORMAL.value:
            continue
        subject = claim.get("subject", {})
        h = subject.get("hash")
        if h in approved or h in seen:
            continue
        seen.add(h)
        out.append({"kind": "statement", "artifact_id": subject.get("artifact_id"), "statement_hash": h,
                    "claim_hash": e.body.get("claim_hash"), "prereg_seq": e.seq, "binds": {"statement_hash": h},
                    "decide": f"chh approve-statement FILE --artifact-id {subject.get('artifact_id')} --approver "
                              f"NAME --statement-hash {h}"})
    return out


def _roles_and_alpha(store: EvidenceStore) -> list[dict[str, Any]]:
    from ..kernel import alpha as A
    from ..kernel.checks import related_targets
    from ..kernel.vault import EXTERNAL_ROLES, HELD_OUT_ROLES
    from ..kerneld.scope import ScopedKernel
    kernel = ScopedKernel(store)
    datasets = kernel.vault.datasets()
    preregs = kernel.ledger.entries("prereg")
    out: list[dict[str, Any]] = []
    for ds_id, rec in sorted(datasets.items()):
        if rec.role in EXTERNAL_ROLES and kernel.vault.role_approval(ds_id) is None:
            out.append({"kind": "role", "dataset_id": ds_id, "role": rec.role.value, "dataset_hash": rec.hash,
                        "binds": {"dataset_hash": rec.hash},
                        "decide": f"chh approve-role {ds_id} --approver NAME --note WHY --dataset-hash {rec.hash}"})
    for ds_id, rec in sorted(datasets.items()):
        if rec.role not in HELD_OUT_ROLES:
            continue
        related = related_targets(kernel.vault, ds_id)
        if A.accounts(kernel.ledger, related):
            continue
        if any(related & set(e.body.get("claim", {}).get("generalization_targets", [])) for e in preregs):
            continue  # too late: an account can only be opened before the first claim
        out.append({"kind": "alpha_account", "dataset_id": ds_id, "role": rec.role.value, "optional": True,
                    "binds": {"dataset_hash": rec.hash},
                    "decide": f"chh alpha open {ds_id} --alpha A --slots S --approver NAME --dataset-hash "
                              f"{rec.hash}"})
    return out


def pending(store: EvidenceStore, *, repo_root: Path | None = None, data_dir: Path | None = None,
            today: dt.date | None = None, provider_override: Path | None = None) -> list[dict[str, Any]]:
    """Every pending human action (read-only), grouped in KIND_ORDER."""
    from . import checks, features, providers, review
    sources: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
        ("approval_nodes", lambda: _approval_nodes(store)),
        ("adapters", lambda: _adapters(store)),
        ("statements", lambda: _statements(store)),
        ("roles_and_alpha", lambda: _roles_and_alpha(store)),
        ("suites", lambda: review.pending_suites(store)),
        ("verifiers", lambda: review.pending_verifiers(store)),
        ("feature_rulings", lambda: features.pending_rulings(store)),
        ("providers", lambda: providers.pending_signoffs(store, today=today, override=provider_override)),
        ("human_checks", lambda: checks.pending_checks(store, repo_root=repo_root, data_dir=data_dir)),
    ]
    items: list[dict[str, Any]] = []
    for name, fn in sources:
        try:
            items.extend(fn())
        except Exception as e:  # noqa: BLE001  a broken source is shown, never silently dropped
            items.append({"kind": "queue_error", "source": name, "error": f"{type(e).__name__}: {e}"[:300],
                          "binds": {}, "decide": "fix the source, then re-run chh queue"})
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    return sorted(items, key=lambda it: rank.get(str(it.get("kind")), len(rank)))  # stable within a kind


def counts(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[str(it["kind"])] = out.get(str(it["kind"]), 0) + 1
    return out
