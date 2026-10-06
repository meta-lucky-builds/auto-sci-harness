"""Data vault: registered datasets/artifacts, and a logged read path for private files (GOAL §4.3 K2, K6).

Every dataset is registered (content-addressed) with a role. Its files are PUBLIC (may be shown to agents,
e.g. test features) or PRIVATE (labels; never agent-visible). Every read of a private file goes through
`read`, which first appends a `data_access` entry to the ledger. K2 then checks that a claim was
pre-registered before any private file of its targets was touched, K4 that every held-out evaluation
was reported, and K6 that no target was ever used for development.

The guarantee holds for code that uses the vault. A private file read some other way leaves no entry:
that is why private files live outside every agent mount (invariant 2) and the leak audit looks for them.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..store.evidence import EvidenceStore
from .claim import canonical_json
from .ledger import Entry, Ledger


class DatasetRole(StrEnum):
    DEV = "dev"  # used during development
    TEST = "test"  # internal held-out test set
    SEALED = "sealed"  # internal, touched only by finalize-style confirmation
    EXTERNAL = "external"  # a dataset from outside the project's development distribution
    FUTURE = "future"  # data that did not exist at pre-registration time (time-split)


HELD_OUT_ROLES = frozenset({DatasetRole.TEST, DatasetRole.SEALED, DatasetRole.EXTERNAL, DatasetRole.FUTURE})
EXTERNAL_ROLES = frozenset({DatasetRole.EXTERNAL, DatasetRole.FUTURE})

Purpose = Literal["develop", "evaluate", "audit", "kernel", "reproduce"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FileRef(_Model):
    blob: str
    private: bool


class DatasetRecord(_Model):
    dataset_id: str
    role: DatasetRole
    files: dict[str, FileRef]
    hash: str  # sha256 over the canonical {name: {blob, private}} mapping
    canary_ref: str | None = None  # blob holding a canary token that marks this dataset's private files
    seq: int = 0  # ledger position of the registration


class ArtifactRecord(_Model):
    artifact_id: str
    hash: str  # blob hash of the artifact bytes
    kind: str  # e.g. "baseline", "method"
    seq: int = 0


def dataset_hash(files: dict[str, FileRef]) -> str:
    return hashlib.sha256(canonical_json({k: v.model_dump() for k, v in sorted(files.items())})).hexdigest()


class VaultError(ValueError):
    pass


class Vault:
    def __init__(self, store: EvidenceStore, ledger: Ledger | None = None):
        self.store = store
        self.ledger = ledger or Ledger(store)

    # ------------------------------------------------------------ registration
    def register_dataset(self, dataset_id: str, role: DatasetRole, public: dict[str, bytes],
                         private: dict[str, bytes], canary: str | None = None) -> DatasetRecord:
        overlap = set(public) & set(private)
        if overlap:
            raise VaultError(f"file(s) both public and private: {sorted(overlap)}")
        files = {name: FileRef(blob=self.store.blobs.put(data), private=False) for name, data in public.items()}
        files |= {name: FileRef(blob=self.store.blobs.put(data), private=True) for name, data in private.items()}
        h = dataset_hash(files)
        existing = self.datasets().get(dataset_id)
        if existing is not None:
            if existing.hash != h or existing.role != role:
                raise VaultError(f"dataset {dataset_id!r} already registered with different content or role")
            return existing
        rec = DatasetRecord(dataset_id=dataset_id, role=role, files=files, hash=h,
                            canary_ref=self.store.blobs.put(canary.encode()) if canary else None)
        entry = self.ledger.append("register_dataset", rec.model_dump(mode="json", exclude={"seq"}))
        return rec.model_copy(update={"seq": entry.seq})

    def register_artifact(self, artifact_id: str, data: bytes, kind: str) -> ArtifactRecord:
        h = self.store.blobs.put(data)
        existing = self.artifacts().get(artifact_id)
        if existing is not None:
            if existing.hash != h:
                raise VaultError(f"artifact {artifact_id!r} already registered with different content")
            return existing
        entry = self.ledger.append("register_artifact", {"artifact_id": artifact_id, "hash": h, "kind": kind})
        return ArtifactRecord(artifact_id=artifact_id, hash=h, kind=kind, seq=entry.seq)

    # ------------------------------------------------------------ lookup
    def datasets(self) -> dict[str, DatasetRecord]:
        out = {}
        for e in self.ledger.entries("register_dataset"):
            out[e.body["dataset_id"]] = DatasetRecord.model_validate({**e.body, "seq": e.seq})
        return out

    def artifacts(self) -> dict[str, ArtifactRecord]:
        out = {}
        for e in self.ledger.entries("register_artifact"):
            out[e.body["artifact_id"]] = ArtifactRecord.model_validate({**e.body, "seq": e.seq})
        return out

    def dataset(self, dataset_id: str) -> DatasetRecord:
        try:
            return self.datasets()[dataset_id]
        except KeyError:
            raise VaultError(f"unregistered dataset {dataset_id!r}") from None

    def canary(self, dataset_id: str) -> str | None:
        ref = self.dataset(dataset_id).canary_ref
        return self.store.blobs.get(ref).decode() if ref else None

    # ------------------------------------------------------------ reads
    def read_public(self, dataset_id: str, name: str) -> bytes:
        ref = self._file(dataset_id, name)
        if ref.private:
            raise VaultError(f"{dataset_id}/{name} is private: use read() with a purpose")
        return self.store.blobs.get(ref.blob)

    def read(self, dataset_id: str, name: str, *, purpose: Purpose, run_id: str | None = None,
             claim_hash: str | None = None, arm: str | None = None, seed: int | None = None) -> bytes:
        """Logged read. The ledger entry is written BEFORE the bytes are returned."""
        ref = self._file(dataset_id, name)
        if purpose == "evaluate" and (claim_hash is None or arm is None or seed is None):
            raise VaultError("an 'evaluate' read must name the claim, the arm and the seed")
        rec = self.dataset(dataset_id)
        self.ledger.append("data_access", {"dataset_id": dataset_id, "dataset_hash": rec.hash, "file": name,
                                           "private": ref.private, "purpose": purpose, "run_id": run_id,
                                           "claim_hash": claim_hash, "arm": arm, "seed": seed})
        return self.store.blobs.get(ref.blob)

    def accesses(self, dataset_id: str | None = None) -> list[Entry]:
        es = self.ledger.entries("data_access")
        return [e for e in es if dataset_id is None or e.body["dataset_id"] == dataset_id]

    def _file(self, dataset_id: str, name: str) -> FileRef:
        rec = self.dataset(dataset_id)
        if name not in rec.files:
            raise VaultError(f"dataset {dataset_id!r} has no file {name!r}")
        return rec.files[name]

