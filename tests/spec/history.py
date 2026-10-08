"""Abstract ledger histories: the shared vocabulary of the clean-room Kernel model and the differential tests.

This file defines DATA SHAPES only (what happened, in ledger order). It encodes no Kernel rule. The clean-room
model (`kernel_model.py`, written from GOAL.md §4 and docs/kernel.md without reading kernel/checks.py) computes
verdicts from a History; `test_kernel_differential.py` replays the same History against the real Kernel.

Physical meaning of each field (how the differential harness builds the real store from it):
- Every dataset has exactly ONE public CSV file. kind "features" = columns x1,x2 (feature rows only);
  kind "labelled" = columns x1,x2,label (the same rows WITH their labels: a public file that carries labels).
  `examples` are ids of example rows drawn from one global pool; an example id always has the same x1,x2 and the
  same label wherever it appears. Two datasets "share an example" when the same id is in both public files.
- `private` is None (no private file) or a token naming the private label file's bytes: two datasets with the
  same token have byte-identical private label files ("the same labels under another id").
- A dataset's content is (kind, examples, private): two registrations with equal triples have identical content
  (the same content hash). File names are fixed, so names never differ.
- Artifacts are code blobs; `kind` is "baseline" or "method".
- A claim's subject is a method artifact, its comparator a baseline artifact; seeds, alpha and n_comparisons are
  its analysis plan; `reproduce` is always "all" (every record is re-executed); the replayer always reproduces
  the stored predictions exactly.
- `strong` describes the data the arms produce: True = the subject predicts every label correctly and the
  comparator predicts a constant; False = both arms produce identical predictions. (The Kernel's statistical
  criterion itself is out of the model's scope; see the model's docstring for how it is treated.)
- A Read is one logged private-file read (the label file) through the vault. `allowed` reads only: a read the
  vault refused leaves no ledger entry and is not in the History.
- A Certify event is a recorded certification of one claim with the given evidence records. Reads the Kernel
  itself makes while certifying (purpose "kernel") are appended to the History right after the Certify event by
  the harness, as the Read events they are.
- An ApproveRole event is a human's role approval of a dataset (only external/future datasets can be approved).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Role = Literal["dev", "test", "sealed", "external", "future"]
Purpose = Literal["develop", "evaluate", "audit", "kernel", "leakscan", "reproduce"]
Arm = Literal["subject", "comparator"]
Kind = Literal["improvement", "ablation_effect", "negative_result", "equivalence"]


@dataclass(frozen=True)
class Dataset:
    id: str
    role: Role
    kind: Literal["features", "labelled"]
    examples: frozenset[int]
    private: str | None


@dataclass(frozen=True)
class Artifact:
    id: str
    kind: Literal["baseline", "method"]


@dataclass(frozen=True)
class ClaimSpec:
    key: str  # stands for the claim hash: equal keys = the same claim
    kind: Kind
    subject: str  # artifact id
    comparator: str  # artifact id
    development: tuple[str, ...]
    targets: tuple[str, ...]
    seeds: tuple[int, ...]
    alpha: float
    n_comparisons: int
    min_effect: float
    strong: bool


@dataclass(frozen=True)
class Prereg:
    claim: ClaimSpec


@dataclass(frozen=True)
class Read:
    dataset: str
    purpose: Purpose
    claim: str | None = None  # claim key the read names (evaluate/kernel reads must name one)
    arm: Arm | None = None
    seed: int | None = None


@dataclass(frozen=True)
class Record:
    """One evaluation record submitted as evidence: arm `arm` with seed `seed` scored on target `target`."""

    arm: Arm
    seed: int
    target: str


@dataclass(frozen=True)
class Certify:
    claim: ClaimSpec
    records: tuple[Record, ...]


@dataclass(frozen=True)
class ApproveRole:
    dataset: str


Event = Dataset | Artifact | Prereg | Read | Certify | ApproveRole
History = tuple[Event, ...]

LABELS = ("CERTIFIED-VALIDATED", "CERTIFIED-INTERNAL", "REJECTED")


@dataclass(frozen=True)
class Verdict:
    """The model's (or the Kernel's) verdict for one Certify: every check's status and the certificate label."""

    checks: dict[str, Literal["pass", "fail"]]  # K0..K9
    label: str  # one of LABELS
    family: dict[str, int]  # per claim target: the Bonferroni family size m used for it
