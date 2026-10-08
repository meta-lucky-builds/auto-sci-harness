"""Trajectories, verifier sets and verdicts for the L8 export (GOAL §5 L8).

- A `Trajectory` is an ordered list of role-tagged steps with a closed `outcome`; its `digest()` is a content hash over
  everything that would become training data (steps, provenance, outcome, task). A verdict names the digest it judged,
  so a trajectory edited after certification is no longer certified.
- A `VerifierSet` is a DECLARED, named, non-empty set of verifiers, each `(id, adapter_hash, tier)`. Ids are compared
  case- and whitespace-insensitively, adapter hashes must be 64-hex and are compared lower-case.
- `declare_disjoint(A, B)` refuses two sets that share a verifier id OR an adapter hash (the same code under another
  name is the same verifier), and any member weaker than `config.EXPORT_TIER_FLOOR` (invariant 4).
- `certified_by(t, S, verdicts)`: EVERY member of S has a passing verdict with its exact id and adapter hash on this
  exact trajectory digest, and none of them has a failing verdict on it (a fail vetoes a later pass). Missing,
  failed or other-version verdicts mean "not certified" (fail closed).

Not built here (stated, not hidden): the verdicts are inputs. Nothing in this module proves a verdict came from the
named verifier; wiring verdicts to Kernel certificates (which record adapter hashes, invariant 13) is a later step.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..records import Provenance
from ..tiers import Tier
from . import config as C


class ExportRefused(Exception):
    """A declaration or export the L8 rules refuse. `code` is a closed string (see the raising sites)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


OUTCOMES = frozenset({"certified", "negative", "rejected", "failed", "infra_failed"})
ROLES = frozenset({"system", "user", "assistant", "tool"})
_HEX64 = re.compile(r"[0-9a-f]{64}")  # used with fullmatch: no trailing newline or space slips through


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


def norm_id(verifier_id: str) -> str:
    return verifier_id.strip().casefold()


@dataclass(frozen=True)
class Step:
    role: str
    content: str
    provenance: Provenance | None = None  # who produced this content; required on assistant steps for export

    def __post_init__(self) -> None:
        if self.role not in ROLES or not isinstance(self.content, str):
            raise ExportRefused("BAD_TRAJECTORY", f"step role must be one of {sorted(ROLES)} with text content")


@dataclass(frozen=True)
class Trajectory:
    id: str
    task_id: str
    outcome: str
    steps: tuple[Step, ...]

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ExportRefused("BAD_TRAJECTORY", f"outcome {self.outcome!r} is not one of {sorted(OUTCOMES)}")
        if not self.id or not self.task_id:
            raise ExportRefused("BAD_TRAJECTORY", "a trajectory has an id and a task id")

    def digest(self) -> str:
        return _sha({"id": self.id, "task_id": self.task_id, "outcome": self.outcome,
                     "steps": [{"role": s.role, "content": s.content,
                                "provenance": s.provenance.model_dump(mode="json") if s.provenance else None}
                               for s in self.steps]})


@dataclass(frozen=True)
class VerifierRef:
    id: str
    adapter_hash: str
    tier: Tier

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not isinstance(self.adapter_hash, str) or not norm_id(self.id) \
                or not _HEX64.fullmatch(self.adapter_hash.lower()):
            raise ExportRefused("SET_INVALID", f"verifier {self.id!r} needs a non-empty id and a 64-hex adapter hash")


@dataclass(frozen=True)
class VerifierSet:
    name: str
    members: tuple[VerifierRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(self.members))  # a copy: the caller's list cannot change it later
        for m in self.members:
            if not isinstance(m, VerifierRef):
                raise ExportRefused("SET_INVALID", f"set {self.name!r}: {m!r} is not a VerifierRef")

    def digest(self) -> str:
        return _sha({"name": self.name, "members": sorted([norm_id(m.id), m.adapter_hash.lower(), int(m.tier)]
                                                          for m in self.members)})


@dataclass(frozen=True)
class DeclaredPair:
    """Two declared, disjoint verifier sets: A (the harness evolves against it) and B (the weights' set)."""
    a: VerifierSet
    b: VerifierSet

    def __post_init__(self) -> None:  # every pair is checked, however it was built
        _check_disjoint(self.a, self.b)

    def digest(self) -> str:
        return _sha({"A": self.a.digest(), "B": self.b.digest()})


def declare_disjoint(a: VerifierSet, b: VerifierSet) -> DeclaredPair:
    return DeclaredPair(a, b)


def _check_disjoint(a: VerifierSet, b: VerifierSet) -> None:
    if not isinstance(a, VerifierSet) or not isinstance(b, VerifierSet):
        raise ExportRefused("SET_INVALID", "a pair is two VerifierSets")
    for s in (a, b):
        if not s.members:
            raise ExportRefused("SET_INVALID", f"verifier set {s.name!r} is empty: an empty set certifies nothing")
        weak = [m.id for m in s.members if m.tier > C.EXPORT_TIER_FLOOR]
        if weak:
            raise ExportRefused("TIER_BELOW_FLOOR", f"set {s.name!r}: {weak} are weaker than the export floor "
                                                    f"{C.EXPORT_TIER_FLOOR.name} (invariant 4)")
    if a.name.strip().casefold() == b.name.strip().casefold():
        raise ExportRefused("SET_INVALID", f"both sets are named {a.name!r}")
    shared_ids = {norm_id(m.id) for m in a.members} & {norm_id(m.id) for m in b.members}
    shared_hashes = {m.adapter_hash.lower() for m in a.members} & {m.adapter_hash.lower() for m in b.members}
    if shared_ids or shared_hashes:
        raise ExportRefused("SETS_OVERLAP", f"sets {a.name!r} and {b.name!r} share verifier ids {sorted(shared_ids)} "
                                            f"/ adapter hashes {sorted(shared_hashes)}: they must be disjoint")


@dataclass(frozen=True)
class Verdict:
    verifier_id: str
    adapter_hash: str
    trajectory_digest: str
    passed: bool
    score: float | None = None


def certified_by(t: Trajectory, s: VerifierSet, verdicts: Iterable[Verdict]) -> bool:
    if not s.members:
        return False
    d = t.digest()
    mine = [((norm_id(v.verifier_id), v.adapter_hash.lower()), v.passed is True) for v in verdicts
            if v.trajectory_digest == d]
    passing = {k for k, ok in mine if ok}
    failing = {k for k, ok in mine if not ok}  # a failing verdict vetoes: no re-running a verifier until it passes
    return all((k := (norm_id(m.id), m.adapter_hash.lower())) in passing and k not in failing for m in s.members)
