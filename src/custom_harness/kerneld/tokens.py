"""kerneld's in-memory capability table (GOAL §16.5.2): run capabilities and narrow private-read tokens.

A run capability (random hex) is bound to one run id and is handed out by `open_run` only to the holder of the
launch secret. A token is bound to (run capability, purpose, dataset, private file, claim, arm, seed), fixed at
issue: host code holding a token can make exactly the read it names and no other. `evaluate` tokens are
single-use; `kernel` and `leakscan` tokens are reusable until `close_run`, which revokes the capability and every
token issued under it. Nothing here is persisted: a restarted kerneld knows no capability.

The issue-time checks (registered dataset, private file, pre-registered claim, claim target) duplicate the
Vault's own read-time checks on purpose, so a refused token never reaches the read path (no ledger entry).
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from .protocol import PURPOSES


class TokenError(Exception):
    """A refused capability operation; `.code` is a closed protocol error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _VaultLike(Protocol):
    def datasets(self) -> dict[str, Any]: ...
    def registered_targets(self, claim_hash: str | None) -> frozenset[str] | None: ...


@dataclass
class Token:
    run_cap: str
    purpose: str
    dataset_id: str
    file: str
    claim_hash: str | None
    arm: str | None
    seed: int | None
    spent: bool = False


class TokenTable:
    def __init__(self, vault: _VaultLike):
        self.vault = vault
        self.runs: dict[str, str] = {}  # run capability -> run id
        self.tokens: dict[str, Token] = {}

    # ----------------------------------------------------------------------------- run capabilities
    def open_run(self, run_id: str) -> str:
        cap = secrets.token_hex(32)
        self.runs[cap] = run_id
        return cap

    def run_id(self, run_cap: str) -> str:
        rid = self.runs.get(run_cap)
        if rid is None:
            raise TokenError("bad_token")
        return rid

    def close_run(self, run_cap: str) -> int:
        self.run_id(run_cap)
        del self.runs[run_cap]
        dead = [t for t, tok in self.tokens.items() if tok.run_cap == run_cap]
        for t in dead:
            del self.tokens[t]
        return len(dead)

    # ----------------------------------------------------------------------------- tokens
    def issue(self, run_cap: str, purpose: str, dataset_id: str, file: str, claim_hash: str | None = None,
              arm: str | None = None, seed: int | None = None) -> str:
        self.run_id(run_cap)
        if purpose not in PURPOSES:
            raise TokenError("bad_request")
        if purpose == "evaluate" and (claim_hash is None or arm is None or seed is None):
            raise TokenError("bad_request")
        rec = self.vault.datasets().get(dataset_id)
        if rec is None or file not in rec.files:
            raise TokenError("vault_error")
        if not rec.files[file].private:
            raise TokenError("not_private")
        if purpose in ("evaluate", "kernel"):
            targets = self.vault.registered_targets(claim_hash)
            if targets is None:
                raise TokenError("not_registered")
            if dataset_id not in targets:
                raise TokenError("not_target")
        token = secrets.token_hex(32)
        self.tokens[token] = Token(run_cap, purpose, dataset_id, file, claim_hash, arm, seed)
        return token

    def use(self, run_cap: str, token: str, purpose: str) -> Token:
        """The token's scope, for one use by `purpose` under `run_cap` (an evaluate token is then spent)."""
        tok = self.tokens.get(token)
        if tok is None or run_cap not in self.runs or not hmac.compare_digest(tok.run_cap, run_cap):
            raise TokenError("bad_token")
        if tok.purpose != purpose:
            raise TokenError("token_scope")
        if tok.spent:
            raise TokenError("token_spent")
        if purpose == "evaluate":
            tok.spent = True  # spent BEFORE the read: a failed score still uses up the token (fail closed)
        return tok
