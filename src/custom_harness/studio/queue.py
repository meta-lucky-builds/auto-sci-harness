"""Approval queue (GOAL §5 L9): every item waiting for a HUMAN decision, rebuilt from the store. Read-only.

Kept for its callers; the queue itself lives in `custom_harness.human.queue` (one queue for every human action:
approval nodes and physical protocols, adapters, statements, roles, alpha-accounts, verifier and suite reviews,
feature rulings, provider sign-offs, human-held checks). Each item names the exact hash its action binds (`binds`)
and the command that takes it (`decide`).
"""

from __future__ import annotations

from typing import Any

from ..store import EvidenceStore


def pending(store: EvidenceStore) -> list[dict[str, Any]]:
    from ..human.queue import pending as _pending
    return _pending(store)
