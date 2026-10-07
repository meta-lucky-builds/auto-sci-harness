"""Per-target alpha-accounts (GOAL §16.4, invariant 17): test-set wear as an explicit, human-set budget.

A HUMAN opens an account on a held-out target (`alpha_open`: total alpha_T, S_T slots). Every claim pre-registered on
the target (or on a related one: shared examples or private labels, the K4 family) reserves one slot
(`alpha_reserve`) and is tested at the FIXED level alpha_T / S_T, whatever the order in which claims are certified
(R2-2 fixed by construction). When the slots run out the target RETIRES (`alpha_retire`): no further claim can be
registered on it; new evidence needs fresh data. FWER on the target <= S_T * alpha_T / S_T = alpha_T (Bonferroni over a
fixed-size family; with releases, each claim's level is further divided by the transcripts reachable at its
registration, see releases.py). Targets without an account keep the declared-n_comparisons rule.
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


def retired(ledger: Ledger, account: Account) -> bool:
    return (any(e.body.get("account") == account.seq for e in ledger.entries("alpha_retire"))
            or len(reservations(ledger, account)) >= account.slots)


def holds_slot(ledger: Ledger, account: Account, claim_hash: str) -> bool:
    return any(e.body.get("claim_hash") == claim_hash for e in reservations(ledger, account))
