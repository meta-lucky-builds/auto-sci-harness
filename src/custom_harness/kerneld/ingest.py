"""Private ingest, run INSIDE the kerneld process (GOAL §16.5.2): the only code that registers private data.

Each kind is typed: the host names WHERE the data are (paths) and what they are for; kerneld reads the bytes itself,
checks them, registers the dataset (the Vault writes the hash-chained `register_dataset` entry) and appends one
`kerneld_ingest` store record. Nothing private is returned: ids, counts, a sha256 the ledger already holds, or closed
codes. A refusal raises `IngestRefused(code)` with a code from protocol.ERRORS (or, for returned measurements, the
closed problem classes in MEASUREMENT_PROBLEMS).

Kinds:
  research_task          a research task directory (research/task.py layout): train + test features public,
                         test labels private (+ canary), baseline artifact. Returns the vault ids.
  openml_split           a Claim Audit's fresh dataset (audit/data.py): the pinned ARFF from the local cache is split
                         by row position and pin only; train (DEV, public) and the held-out split (EXTERNAL: features
                         public, labels private). Only for a claim that is already pre-registered.
  returned_measurements  `chh return-data` (GOAL §4.7): measurements humans returned for an approved physical
                         protocol, checked against it, registered as a FUTURE dataset with a provenance file.

The private file location of a research task is resolved in ONE place (`task_private_dir`), so a move of held-out
truth out of agent-readable roots (s4-integrity) changes only that function, and the new location is read by this
process only.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ..kernel import DatasetRole, Kernel, Vault, VaultError
from .protocol import INGEST_KINDS, MEASUREMENT_PROBLEMS

__all__ = ["INGEST_KINDS", "MEASUREMENT_PROBLEMS", "RECORD_KIND", "IngestRefused", "openml_split", "research_task",
           "returned_measurements", "task_private_dir"]
RECORD_KIND = "kerneld_ingest"


class IngestRefused(Exception):
    def __init__(self, code: str, problems: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.problems = problems


def _record(vault: Vault, run_id: str, kind: str, subject: str, datasets: list[str]) -> None:
    vault.store.append_record(RECORD_KIND, {"run_id": run_id, "kind": kind, "subject": subject,
                                            "datasets": datasets, "at": time.time()})


def _read(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise IngestRefused("not_found")
    return path.read_bytes()


# ------------------------------------------------------------------------------------------- research task
def task_private_dir(task_dir: Path) -> Path:
    """Where a task's held-out truth lives (labels, canary, truth-copy records): `truth_layout.truth_dir` (s4-integrity:
    outside every agent-readable root). It refuses an agent-readable location with TruthLayoutError, which the ingest
    maps to the closed code `truth_layout` (never to `not_found`). Only this process calls this function."""
    from ..truth_layout import truth_dir
    return Path(truth_dir(task_dir))


def research_task(vault: Vault, run_id: str, task_id: str, task_dir: str) -> dict[str, Any]:
    """Register a research task (module docstring). A truth location the layout refuses (agent-readable, or still in
    the unmigrated legacy place) is `truth_layout`; a missing file `not_found`; the task's truth-copy records
    (s4-integrity research/truth_copies.py) are declared to the Kernel right after the registration, so K3's
    non-interference replay perturbs every declared copy; a malformed record is `ingest_refused`."""
    from ..truth_layout import TruthLayoutError
    try:
        return _research_task(vault, run_id, task_id, Path(task_dir))
    except TruthLayoutError:  # a PermissionError (OSError): caught FIRST so it never reads as a missing file
        raise IngestRefused("truth_layout") from None


def _research_task(vault: Vault, run_id: str, task_id: str, root: Path) -> dict[str, Any]:
    from ..research.task import load_task
    from ..research.truth_copies import TruthCopyRecordError, declare_task_truth_copies
    from ..truth_layout import TruthLayoutError, truth_file
    try:
        task = load_task(root)  # checks the labels exist where the layout says (truth_file)
    except TruthLayoutError:
        raise
    except (OSError, ValueError):
        raise IngestRefused("not_found") from None
    if task.id != task_id:
        raise IngestRefused("token_scope")
    train = _read(root / "public" / task.train_file)
    test_x = _read(root / "public" / task.test_features_file)
    test_y = _read(truth_file(root, task.test_labels_file))
    baseline = _read(root / task.baseline_file)

    def digest(*parts: bytes) -> str:  # fixed-length per-file digests: no separator ambiguity (as before, 2026-10-08)
        return hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in parts)).hexdigest()

    ids = task.dataset_ids(digest(train, test_x, test_y), digest(train, test_x, test_y, baseline))
    canary_path = truth_file(root, task.canary_file)
    canary = None
    if canary_path.exists() or canary_path.is_symlink():
        try:
            canary = _read(canary_path).decode("utf-8").strip()  # no symlink; undecodable -> closed code
        except UnicodeDecodeError:
            raise IngestRefused("ingest_refused") from None
    try:
        vault.register_dataset(ids["train"], DatasetRole.DEV, public={task.train_file: train}, private={})
        vault.register_dataset(ids["test"], DatasetRole.TEST, public={task.test_features_file: test_x},
                               private={task.test_labels_file: test_y}, canary=canary)
    except VaultError:  # same id with other content or role, or laundering (bytes private here, public elsewhere)
        raise IngestRefused("ingest_refused") from None
    if vault.canary(ids["test"]) != canary:  # tasks sharing data must share the canary (else it is never scanned)
        raise IngestRefused("canary_mismatch")
    try:
        vault.register_artifact(ids["baseline"], baseline, "baseline")
    except VaultError:
        raise IngestRefused("ingest_refused") from None
    # GOAL §16.5.4: every exporter-recorded public copy of the truth becomes a Kernel truth-copy declaration (K3)
    try:
        declare_task_truth_copies(Kernel(vault.store), task, root, ids)
    except TruthCopyRecordError:  # a ValueError: malformed record or a file the task does not have
        raise IngestRefused("ingest_refused") from None
    _record(vault, run_id, "research_task", task_id, [ids["train"], ids["test"]])
    return {"ids": ids}


# ------------------------------------------------------------------------------------------- claim audit
def openml_split(vault: Vault, run_id: str, subject: str, claim_hash: str, cache_root: str, spec: dict[str, Any]
                 ) -> dict[str, Any]:
    from ..audit import data as D
    from ..audit.spec import OpenMLDataset
    try:
        ds = OpenMLDataset.model_validate(spec)
    except ValueError:
        raise IngestRefused("bad_request") from None
    if ds.dataset_id != subject:  # the token names the held-out dataset this ingest may create
        raise IngestRefused("token_scope")
    targets = vault.registered_targets(claim_hash)
    if targets is None:  # fresh data are touched only after the claim is committed
        raise IngestRefused("not_registered")
    if ds.dataset_id not in targets:  # the held-out split may only be one of THIS claim's targets
        raise IngestRefused("token_scope")
    try:
        raw = D.OpenMLCache(Path(cache_root)).load(ds)  # re-hashed against the pin on every read
        sp = D.split(D.to_table(raw, ds.target), ds.sha256)
    except D.DataUnavailable as e:
        raise IngestRefused("not_found" if e.needs_owner else "data_unavailable") from None
    except (ValueError, UnicodeDecodeError):
        raise IngestRefused("data_unavailable") from None
    try:
        vault.register_dataset(ds.train_id, DatasetRole.DEV, public={"train.csv": sp.train}, private={})
        vault.register_dataset(ds.dataset_id, DatasetRole.EXTERNAL, public={"test_X.csv": sp.test_x},
                               private={"test_y.csv": sp.test_y}, canary=f"CHH-AUDIT-CANARY-{ds.dataset_id}")
    except VaultError:
        raise IngestRefused("ingest_refused") from None
    _record(vault, run_id, "openml_split", ds.dataset_id, [ds.train_id, ds.dataset_id])
    return {"n_train": sp.n_train, "n_test": sp.n_test}


# ------------------------------------------------------------------------------------------- returned measurements
def _problem_class(p: str) -> str:
    if p.startswith("data is not UTF-8"):
        return "not_utf8"
    if p.startswith("columns "):
        return "columns"
    if " values for " in p:
        return "row_length"
    if p.endswith("is not a finite number"):
        return "non_finite"
    if p.startswith("arm ") and "replicates" in p:
        return "replicates"
    return "other"


def returned_measurements(vault: Vault, run_id: str, dataset_id: str, file: str, protocol: dict[str, Any],
                          claim_hash: str, attestation: str, approved_protocol_seq: int) -> dict[str, Any]:
    from ..research.physical import Protocol, check_measurements, protocol_hash, returned_dataset_id
    try:
        proto = Protocol.model_validate(protocol)
    except ValueError:
        raise IngestRefused("bad_request") from None
    p_hash = protocol_hash(proto)
    if returned_dataset_id(p_hash) != dataset_id:
        raise IngestRefused("token_scope")
    if not attestation.strip():
        raise IngestRefused("bad_request")
    if dataset_id in vault.datasets():
        raise IngestRefused("already_registered")
    data = _read(Path(file))
    problems = check_measurements(proto, data)
    if problems:  # closed classes only: a problem message can quote a measured value
        raise IngestRefused("measurements_refused", tuple(sorted({_problem_class(p) for p in problems})))
    data_sha = hashlib.sha256(data).hexdigest()
    provenance = {"protocol_hash": p_hash, "claim_hash": claim_hash, "run_id": run_id, "attestation": attestation,
                  "attestation_sha256": hashlib.sha256(attestation.encode()).hexdigest(), "data_sha256": data_sha,
                  "approved_protocol_seq": approved_protocol_seq}
    try:
        vault.register_dataset(dataset_id, DatasetRole.FUTURE,
                               public={"provenance.json": json.dumps(provenance, sort_keys=True).encode()},
                               private={proto.measurements.filename: data})
    except VaultError:
        raise IngestRefused("already_registered") from None
    _record(vault, run_id, "returned_measurements", dataset_id, [dataset_id])
    return {"data_sha256": data_sha}
