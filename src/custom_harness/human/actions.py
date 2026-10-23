"""HUMAN actions whose writer lives elsewhere (the Kernel, the vault, the studio): the stable API a UI calls.

Each function checks the actor (guard.require_human: a named human, not from inside a run), checks that the item the
human reviewed is still the current one (the hash they name), then calls the one writer of that ledger kind:

- `approve_adapter`  -> Kernel.approve_adapter (kind `adapter_approval`, bound to the adapter content hash);
- `approve_role`     -> Kernel.approve_role (kind `role_approval`, bound to the dataset content hash);
- `open_alpha_account` -> Kernel.open_alpha_account (kind `alpha_open`, bound to the target's dataset hash);
- `kill_run`         -> studio/kill.kill_run (kind `run_kill`).

The existing CLI commands (`chh approve-adapter`, `chh approve-role`, `chh alpha open`, `chh kill`) call the same
writers; human/cli.py `check_legacy_actor` gives them the same actor check and the optional hash binding.
"""

from __future__ import annotations

from ..kernel.ledger import Entry
from ..store import EvidenceStore
from .guard import HumanActionRefused, require_human


def refuse_if_killed(store: EvidenceStore, run_id: str) -> None:
    from ..studio.kill import kill_entry, killed_detail
    kill = kill_entry(store, run_id)
    if kill is not None:
        raise HumanActionRefused(f"run {run_id} was {killed_detail(kill)}")


def adapter_hash_now(name: str) -> str:
    from ..kernel.adapters import REGISTRY, adapter_hash
    if name not in REGISTRY:
        raise HumanActionRefused(f"unknown adapter {name!r}; known: {sorted(REGISTRY)}")
    return adapter_hash(REGISTRY[name][0])


def approve_adapter(store: EvidenceStore, name: str, *, approver: str, adapter_hash: str) -> Entry:
    """HUMAN action (invariant 13): approve the adapter whose CURRENT content hash is `adapter_hash`; the Kernel runs
    its validation first and refuses an adapter that fails it."""
    who = require_human(approver, what="the approver")
    now = adapter_hash_now(name)
    if adapter_hash != now:
        raise HumanActionRefused(f"adapter {name} hashes to {now}, not the {adapter_hash} you reviewed")
    from ..kernel import AdapterError
    from ..kerneld.scope import ScopedKernel
    try:
        return ScopedKernel(store).approve_adapter(name, approver=who)
    except AdapterError as e:
        raise HumanActionRefused(str(e)) from None


def dataset_hash_now(store: EvidenceStore, dataset_id: str) -> str:
    from ..kernel.vault import VaultError
    from ..kerneld.scope import ScopedKernel
    try:
        return ScopedKernel(store).vault.dataset(dataset_id).hash
    except VaultError as e:
        raise HumanActionRefused(str(e)) from None


def approve_role(store: EvidenceStore, dataset_id: str, *, approver: str, dataset_hash: str,
                 note: str = "") -> Entry:
    """HUMAN action: the dataset with this content hash really is external/future data (needed for VALIDATED)."""
    who = require_human(approver, what="the approver")
    now = dataset_hash_now(store, dataset_id)
    if dataset_hash != now:
        raise HumanActionRefused(f"dataset {dataset_id} hashes to {now}, not the {dataset_hash} you reviewed")
    from ..kernel.vault import VaultError
    from ..kerneld.scope import ScopedKernel
    try:
        return ScopedKernel(store).approve_role(dataset_id, approver=who, note=note)
    except VaultError as e:
        raise HumanActionRefused(str(e)) from None


def open_alpha_account(store: EvidenceStore, dataset_id: str, *, alpha: float, slots: int, approver: str,
                       dataset_hash: str) -> Entry:
    """HUMAN action (GOAL §16.4): give the held-out target with this content hash a fixed budget of slots."""
    who = require_human(approver, what="the approver")
    now = dataset_hash_now(store, dataset_id)
    if dataset_hash != now:
        raise HumanActionRefused(f"dataset {dataset_id} hashes to {now}, not the {dataset_hash} you reviewed")
    from ..kernel import KernelError
    from ..kernel.vault import VaultError
    from ..kerneld.scope import ScopedKernel
    try:
        return ScopedKernel(store).open_alpha_account(dataset_id, alpha, slots, approver=who)
    except (KernelError, VaultError) as e:
        raise HumanActionRefused(str(e)) from None


def kill_run(store: EvidenceStore, run_id: str, *, by: str, reason_code: str = "human_stop") -> Entry:
    """HUMAN action: stop a run safely (studio/kill.py)."""
    who = require_human(by, what="the human stopping the run")
    from ..studio.kill import KillRefused
    from ..studio.kill import kill_run as _kill
    try:
        return _kill(store, run_id, by=who, reason_code=reason_code)
    except KillRefused as e:
        raise HumanActionRefused(str(e)) from None
