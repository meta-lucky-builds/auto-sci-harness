"""Per-target alpha-accounts (GOAL §16.4, invariant 17): test-set wear as an explicit, human-set budget.

A HUMAN opens an account on a held-out target (`alpha_open`: total alpha_T, S_T slots). Every claim pre-registered on
the target (or on a related one: shared examples or private labels, the K4 family) reserves one slot
(`alpha_reserve`) and is tested at the FIXED level alpha_T / S_T, whatever the order in which claims are registered,
released or certified (R2-2 fixed by construction). Releases are paid in SLOTS, not in level: a claim registered after
releases that allow |T| transcripts is charged |T| slots at registration (the union bound over every claim the
transcript could have selected), and is refused when the account cannot pay. When the slots run out the target
RETIRES (`alpha_retire`). FWER on the target <= (sum of charges) * alpha_T / S_T <= alpha_T: docs/alpha_accounts.md
states the argument, its assumptions and why an additive charge would be invalid. Targets without an account keep
the declared-n_comparisons rule and the transcript bound in K4.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import Entry, Ledger


@dataclass(frozen=True)
class Account:
    seq: int  # ledger seq of the alpha_open entry (the account id)
    dataset_id: str
    alpha: float
    slots: int

    @property
    def level(self) -> float:
        return self.alpha / self.slots


def accounts(ledger: Ledger, datasets: set[str], before: int | None = None) -> list[Account]:
    """Accounts opened on any of `datasets` (optionally only those opened before ledger seq `before`)."""
    return [Account(e.seq, str(e.body["dataset_id"]), float(e.body["alpha"]), int(e.body["slots"]))
            for e in ledger.entries("alpha_open")
            if e.body.get("dataset_id") in datasets and (before is None or e.seq < before)]


def reservations(ledger: Ledger, account: Account) -> list[Entry]:
    return [e for e in ledger.entries("alpha_reserve") if e.body.get("account") == account.seq]


def charged(ledger: Ledger, account: Account) -> int:
    """Slots the account has paid (a reservation recorded before charges existed counts 1; RT-S4-3: every reservation
    costs at least 1, so a 0 or negative charge written around preregister cannot hide an overdraft)."""
    return sum(max(1, int(e.body.get("charge", 1))) for e in reservations(ledger, account))


def retired(ledger: Ledger, account: Account) -> bool:
    return (any(e.body.get("account") == account.seq for e in ledger.entries("alpha_retire"))
            or charged(ledger, account) >= account.slots)


def holds_slot(ledger: Ledger, account: Account, claim_hash: str) -> bool:
    return any(e.body.get("claim_hash") == claim_hash for e in reservations(ledger, account))
