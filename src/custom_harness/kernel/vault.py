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

import contextlib
import hashlib
import hmac
import secrets
from enum import StrEnum
from pathlib import Path
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

# Declared by TRUSTED host code (never by agents, which have no vault access). The log provides accountability;
# it cannot enforce, inside one process, that a declared purpose is the true one.
#   develop   development use (peeking if before a claim's registration; never allowed on a claim's targets)
#   evaluate  scoring one arm/seed of a REGISTERED claim on one of ITS generalization targets (refused otherwise)
#   audit     human/inspection read (peeking for any claim registered later)
#   kernel    the Kernel or the integrity audit re-checking a REGISTERED claim on one of ITS targets (refused otherwise)
#   leakscan  building leak fingerprints (canary/label-window scan); never used to score or model. K2 lists such
#             reads but does not count them as peeking: a TRUST BOUNDARY on the host code (docs/kernel.md)
#   reproduce re-checking a bundle (K2 counts it as peeking like any unexcused early read)
Purpose = Literal["develop", "evaluate", "audit", "kernel", "leakscan", "reproduce"]


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


class CertificateRecord(_Model):
    """Evidence payload of a computational claim: the registered artifact that holds its exact certificate."""

    claim_hash: str
    artifact_id: str
    hash: str  # blob hash of the certificate bytes, as registered


def dataset_hash(files: dict[str, FileRef]) -> str:
    return hashlib.sha256(canonical_json({k: v.model_dump() for k, v in sorted(files.items())})).hexdigest()


class VaultError(ValueError):
    pass


KEYED = "hmac-sha256:"  # s4-kerneld review #7: how a ledger entry names a private file's bytes (never their sha256)


class Vault:
    def __init__(self, store: EvidenceStore, ledger: Ledger | None = None):
        self.store = store
        self.ledger = ledger or Ledger(store)
        self._index: dict[str, str] = {}

    # ------------------------------------------------------------ registration
    def register_dataset(self, dataset_id: str, role: DatasetRole, public: dict[str, bytes],
                         private: dict[str, bytes], canary: str | None = None) -> DatasetRecord:
        overlap = set(public) & set(private)
        if overlap:
            raise VaultError(f"file(s) both public and private: {sorted(overlap)}")
        files = {name: FileRef(blob=self.store.blobs.put(data), private=False) for name, data in public.items()}
        files |= {name: FileRef(blob=self.store.blobs.put(data), private=True) for name, data in private.items()}
        sealed = files | {n: FileRef(blob=self._seal(private[n], files[n].blob), private=True) for n in private}
        h = dataset_hash(sealed)  # the record names private bytes by keyed digest only (an offline guess cannot match)
        known = self.datasets()
        existing = known.get(dataset_id)
        if existing is not None:
            if existing.hash not in (h, dataset_hash(files)) or existing.role != role:  # (or registered before s4)
                raise VaultError(f"dataset {dataset_id!r} already registered with different content or role")
            return existing
        # laundering: private bytes of one dataset must never become a public file of another (or vice versa)
        for other in known.values():
            for oname, oref in other.files.items():
                for name, ref in files.items():
                    if ref.blob == oref.blob and ref.private != oref.private:
                        raise VaultError(f"{dataset_id}/{name} has the same bytes as {other.dataset_id}/{oname}, which "
                                         f"is {'private' if oref.private else 'public'} there")
        rec = DatasetRecord(dataset_id=dataset_id, role=role, files=sealed, hash=h,
                            canary_ref=self.store.blobs.put(canary.encode()) if canary else None)
        entry = self.ledger.append("register_dataset", rec.model_dump(mode="json", exclude={"seq"}))
        return rec.model_copy(update={"seq": entry.seq, "files": files})

    def _seal(self, data: bytes, blob: str) -> str:
        """HMAC-SHA256 of a private file under the Vault's key, <store>/private/vault.key (created on first use; in no
        ledger entry, export or bundle manifest), indexed to its content address in <store>/private/index (s4-kerneld
        review #7). Residual (stated): whoever holds the key (the Vault/kerneld process, the store's owner) can still
        confirm a guessed label file; the key's custody is the store directory's."""
        key = Path(self.store.root) / "private" / "vault.key"
        key.parent.joinpath("index").mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError), key.open("xb") as f:
            f.write(secrets.token_bytes(32))
        digest = hmac.new(key.read_bytes(), data, hashlib.sha256).hexdigest()
        key.parent.joinpath("index", digest).write_text(blob, encoding="ascii")
        return KEYED + digest

    def resolve(self, ref: FileRef) -> FileRef:
        """A keyed private file's content address from the Vault's index; unresolvable: left keyed (reads fail)."""
        idx = Path(self.store.root) / "private" / "index" / ref.blob.removeprefix(KEYED)
        if ref.blob.startswith(KEYED) and ref.blob not in self._index and idx.is_file():
            self._index[ref.blob] = idx.read_text(encoding="ascii").strip()  # immutable: cached per Vault
        return FileRef(blob=self._index.get(ref.blob, ref.blob), private=ref.private)

    def import_index(self, index: dict[str, str]) -> None:
        """Re-create index entries (digest hex -> content address) shipped in a bundle manifest (reproduce)."""
        for digest, blob in index.items():
            if digest.isalnum() and blob.isalnum():  # hex names only: no path out of the index directory
                (Path(self.store.root) / "private" / "index").mkdir(parents=True, exist_ok=True)
                (Path(self.store.root) / "private" / "index" / digest).write_text(blob, encoding="ascii")

    def approve_role(self, dataset_id: str, approver: str, note: str = "") -> Entry:
        """HUMAN action (CLI: `chh approve-role`): attest that a dataset registered as external/future really is data
        from outside the project's development distribution (or that did not exist at pre-registration time). The
        registrant's role alone never earns CERTIFIED-VALIDATED: a role can be typed by anyone who registers data.
        The approval binds the dataset's content hash and role; models and harness code never call this (trust
        boundary, like adapter approvals: the ledger entry names the approver, which is accountability)."""
        rec = self.dataset(dataset_id)
        if rec.role not in EXTERNAL_ROLES:
            raise VaultError(f"{dataset_id!r} has role {rec.role.value!r}: only external/future roles are approved")
        if not approver.strip():
            raise VaultError("a role approval must name the human approver")
        return self.ledger.append("role_approval", {"dataset_id": dataset_id, "dataset_hash": rec.hash,
                                                    "role": rec.role.value, "approver": approver, "note": note})

    def role_approval(self, dataset_id: str) -> Entry | None:
        """The latest human approval of the dataset's CURRENT role and content, or None."""
        rec = self.dataset(dataset_id)
        ok = [e for e in self.ledger.entries("role_approval") if e.body.get("dataset_id") == dataset_id
              and e.body.get("dataset_hash") == rec.hash and e.body.get("role") == rec.role.value]
        return ok[-1] if ok else None

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
            rec = DatasetRecord.model_validate({**e.body, "seq": e.seq})
            out[rec.dataset_id] = rec.model_copy(update={"files": {n: self.resolve(f) for n, f in rec.files.items()}})
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
        if purpose in ("evaluate", "kernel"):
            targets = self.registered_targets(claim_hash)
            if targets is None:
                raise VaultError(f"claim {str(claim_hash)[:12]} is not pre-registered: held-out data are only read "
                                 f"for registered claims")
            if dataset_id not in targets:  # R1-F1: a registered claim is no licence to read ANOTHER target's labels
                raise VaultError(f"{dataset_id!r} is not a generalization target of claim {str(claim_hash)[:12]}: "
                                 f"'{purpose}' reads are bound to the named claim's targets")
        rec = self.dataset(dataset_id)
        self.ledger.append("data_access", {"dataset_id": dataset_id, "dataset_hash": rec.hash, "file": name,
                                           "private": ref.private, "purpose": purpose, "run_id": run_id,
                                           "claim_hash": claim_hash, "arm": arm, "seed": seed})
        return self.store.blobs.get(ref.blob)

    def registered(self, claim_hash: str | None) -> bool:
        return self.registered_targets(claim_hash) is not None

    def registered_targets(self, claim_hash: str | None) -> frozenset[str] | None:
        """The generalization targets of a pre-registered claim, or None if it was never pre-registered."""
        for e in self.ledger.entries("prereg"):
            if claim_hash is not None and e.body.get("claim_hash") == claim_hash:
                return frozenset(e.body.get("claim", {}).get("generalization_targets", ()))
        return None

    def accesses(self, dataset_id: str | None = None) -> list[Entry]:
        es = self.ledger.entries("data_access")
        return [e for e in es if dataset_id is None or e.body.get("dataset_id") == dataset_id]

    def _file(self, dataset_id: str, name: str) -> FileRef:
        rec = self.dataset(dataset_id)
        if name not in rec.files:
            raise VaultError(f"dataset {dataset_id!r} has no file {name!r}")
        return rec.files[name]

