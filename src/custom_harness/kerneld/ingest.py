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
  minted_heldout         a Court case's held-out target, MINTED here after its claim is frozen and anchored (GOAL
                         §16.2, kerneld/mint.py; int18 stage-a): train DEV, target FUTURE, Kernel.record_mint.

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

from ..kernel import DatasetRole, Kernel, KernelError, Vault, VaultError
from .protocol import INGEST_KINDS, MEASUREMENT_PROBLEMS

__all__ = ["INGEST_KINDS", "MEASUREMENT_PROBLEMS", "RECORD_KIND", "IngestRefused", "minted_heldout", "openml_split",
           "research_task", "returned_measurements", "task_private_dir"]
RECORD_KIND = "kerneld_ingest"


class IngestRefused(Exception):
    def __init__(self, code: str, problems: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.problems = problems


def _key(kind: str, subject: str, claim_hash: str | None, sha: str) -> str:
    return f"{kind}|{subject}|{claim_hash or '-'}|{sha}"


def _done(vault: Vault, key: str) -> dict[str, Any] | None:
    """The result of a COMPLETED ingest with this exact key (kind, subject, claim, input sha), or None. Idempotency
    (reviewer finding 14): the same ingest again returns the recorded result and appends nothing."""
    rows = vault.store.records_where(RECORD_KIND, "key", key)
    return dict(rows[-1]["result"]) if rows and isinstance(rows[-1].get("result"), dict) else None


def _publish(vault: Vault, key: str, run_id: str, kind: str, subject: str, datasets: list[str],
             result: dict[str, Any]) -> dict[str, Any]:
    """Appended LAST, after every registration succeeded: the record marks a complete ingest (atomic publication).
    A crash before it leaves only idempotent registrations, which the retried ingest re-finds and completes."""
    vault.store.append_record(RECORD_KIND, {"key": key, "run_id": run_id, "kind": kind, "subject": subject,
                                            "datasets": datasets, "result": result, "at": time.time()})
    return result


def _precheck(vault: Vault, plan: list[tuple[str, DatasetRole, dict[str, bytes], dict[str, bytes]]]) -> None:
    """Every Vault refusal the registrations in `plan` could meet, checked BEFORE the first one is written (so an
    ingest never stops half-registered on a content conflict): the same id with other content or role, and
    laundering (bytes private in one dataset and public in another, including within this plan). Mirrors
    kernel/vault.register_dataset's rules (read-only use of kernel/)."""
    from ..kernel.vault import FileRef, dataset_hash
    known = vault.datasets()
    seen: dict[str, bool] = {}  # blob -> private, across the plan
    for ds_id, role, public, private in plan:
        if set(public) & set(private):
            raise IngestRefused("ingest_refused")
        files = {n: FileRef(blob=hashlib.sha256(b).hexdigest(), private=False) for n, b in public.items()}
        files |= {n: FileRef(blob=hashlib.sha256(b).hexdigest(), private=True) for n, b in private.items()}
        old = known.get(ds_id)
        # The Vault records a private file by its KEYED digest (s4-kernel), so old.hash is the hash of the sealed refs
        # and never equals dataset_hash(files) of plain sha256 refs; datasets() returns the files resolved to content
        # addresses, so the content comparison is on those (an unresolvable ref stays keyed: no match, refused). A
        # record written before s4 holds the plain hash. (int18 merge bug, s4-kernel x s4-kerneld, found by stage-c:
        # every second ingest of already-registered data with private files was refused as ingest_refused.)
        if old is not None and (dataset_hash(files) not in (old.hash, dataset_hash(old.files)) or old.role != role):
            raise IngestRefused("ingest_refused")
        for ref in files.values():
            if seen.get(ref.blob, ref.private) != ref.private:
                raise IngestRefused("ingest_refused")
            seen[ref.blob] = ref.private
            for other in known.values():
                if other.dataset_id != ds_id and any(o.blob == ref.blob and o.private != ref.private
                                                     for o in other.files.values()):
                    raise IngestRefused("ingest_refused")


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
    from ..research.truth_copies import (
        TRUTH_COPIES_FILE,
        TruthCopyRecordError,
        declare_task_truth_copies,
        read_truth_copies,
    )
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
    canary_bytes = b""
    if canary_path.exists() or canary_path.is_symlink():
        try:
            canary_bytes = _read(canary_path)  # no symlink
            canary = canary_bytes.decode("utf-8").strip()  # undecodable -> closed code
        except UnicodeDecodeError:
            raise IngestRefused("ingest_refused") from None
    copies_path = truth_file(root, TRUTH_COPIES_FILE)
    copies_bytes = _read(copies_path) if copies_path.exists() or copies_path.is_symlink() else b""
    key = _key("research_task", task_id, None, digest(train, test_x, test_y, baseline, canary_bytes, copies_bytes))
    done = _done(vault, key)
    if done is not None:
        return done
    # every check BEFORE the first write (atomicity, reviewer finding 14): truth-copy records, canary consistency,
    # content/role conflicts and laundering
    try:
        read_truth_copies(task, root)
    except TruthCopyRecordError:  # a ValueError: malformed record or a file the task does not have
        raise IngestRefused("ingest_refused") from None
    _precheck(vault, [(ids["train"], DatasetRole.DEV, {task.train_file: train}, {}),
                      (ids["test"], DatasetRole.TEST, {task.test_features_file: test_x},
                       {task.test_labels_file: test_y})])
    if ids["test"] in vault.datasets() and vault.canary(ids["test"]) != canary:  # shared data share the canary
        raise IngestRefused("canary_mismatch")
    known_art = vault.artifacts().get(ids["baseline"])
    if known_art is not None and known_art.hash != hashlib.sha256(baseline).hexdigest():
        raise IngestRefused("ingest_refused")
    try:
        vault.register_dataset(ids["train"], DatasetRole.DEV, public={task.train_file: train}, private={})
        vault.register_dataset(ids["test"], DatasetRole.TEST, public={task.test_features_file: test_x},
                               private={task.test_labels_file: test_y}, canary=canary)
        vault.register_artifact(ids["baseline"], baseline, "baseline")
    except VaultError:  # pre-checked above; a race with another registrant still fails closed
        raise IngestRefused("ingest_refused") from None
    if vault.canary(ids["test"]) != canary:  # a concurrent first registration with another canary
        raise IngestRefused("canary_mismatch")
    # GOAL §16.5.4: every exporter-recorded public copy of the truth becomes a Kernel truth-copy declaration (K3)
    try:
        declare_task_truth_copies(Kernel(vault.store), task, root, ids)  # idempotent (research/truth_copies.py)
    except TruthCopyRecordError:
        raise IngestRefused("ingest_refused") from None
    return _publish(vault, key, run_id, "research_task", task_id, [ids["train"], ids["test"]], {"ids": ids})


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
    key = _key("openml_split", ds.dataset_id, claim_hash, ds.sha256)
    done = _done(vault, key)
    if done is not None:
        return done
    _precheck(vault, [(ds.train_id, DatasetRole.DEV, {"train.csv": sp.train}, {}),
                      (ds.dataset_id, DatasetRole.EXTERNAL, {"test_X.csv": sp.test_x}, {"test_y.csv": sp.test_y})])
    try:
        vault.register_dataset(ds.train_id, DatasetRole.DEV, public={"train.csv": sp.train}, private={})
        vault.register_dataset(ds.dataset_id, DatasetRole.EXTERNAL, public={"test_X.csv": sp.test_x},
                               private={"test_y.csv": sp.test_y}, canary=f"CHH-AUDIT-CANARY-{ds.dataset_id}")
    except VaultError:
        raise IngestRefused("ingest_refused") from None
    return _publish(vault, key, run_id, "openml_split", ds.dataset_id, [ds.train_id, ds.dataset_id],
                    {"n_train": sp.n_train, "n_test": sp.n_test})


# ------------------------------------------------------------------------------------------- minted held-out (Court)
def minted_heldout(vault: Vault, run_id: str, subject: str, claim_hash: str, family: str, n_train: int, n_test: int,
                   beacon_round: str, beacon_value: str, anchor_file: str) -> dict[str, Any]:
    """Mint and register a Court case's held-out target AFTER its claim is frozen (GOAL §16.2, kerneld/mint.py):
      - the claim must be pre-registered and `subject` one of its targets, `<subject>-train` its development set;
      - the claim must have frozen exactly this protocol (family, sizes, beacon ROUND) in scope condition `protocol`;
      - the pre-registration must be anchored in `anchor_file` (research/suite.require_anchored: the ledger verifies,
        its anchors match, and an anchored entry names the claim hash) BEFORE anything is minted;
      - the truth is built here, in memory, from draw("heldout_mint", prereg_hash=claim, beacon=round:value) and
        registered: train DEV (public), the target FUTURE (test_X public; test_y and the mint record private);
      - Kernel.record_mint ledgers the mint (one mint per frozen hash: a second beacon is refused as beacon shopping;
        K1 then applies the delta floor to the target).
    Returns sizes and ids only. Refusals are closed codes."""
    from .mint import MintSpecError, beacon_value_problems, draw_beacon, minted_files, minted_protocol
    targets = vault.registered_targets(claim_hash)
    if targets is None:
        raise IngestRefused("not_registered")
    if subject not in targets:
        raise IngestRefused("token_scope")
    train_id = f"{subject}-train"
    claim = next((e.body.get("claim") or {} for e in vault.ledger.entries("prereg")
                  if e.body.get("claim_hash") == claim_hash), {})
    try:
        protocol = minted_protocol(family, n_train, n_test, beacon_round)
    except MintSpecError:
        raise IngestRefused("bad_request") from None
    if ((claim.get("scope") or {}).get("conditions") or {}).get("protocol") != protocol \
            or train_id not in (claim.get("development") or []):
        raise IngestRefused("token_scope")  # not the protocol (or development set) this claim froze
    if beacon_value_problems(beacon_round, beacon_value):
        raise IngestRefused("bad_request")
    beacon = draw_beacon(beacon_round, beacon_value)
    key = _key("minted_heldout", subject, claim_hash, hashlib.sha256(beacon.encode()).hexdigest())
    done = _done(vault, key)
    if done is not None:
        return done
    earlier = [e for e in vault.ledger.entries("mint") if e.body.get("frozen_hash") == claim_hash]
    if earlier and earlier[0].body.get("beacon") != beacon:
        raise IngestRefused("already_registered")  # minted before (another beacon): beacon shopping
    from ..research.suite import MintRefused, require_anchored
    try:
        anchor = require_anchored(claim_hash, vault.ledger.entries(), Path(anchor_file))
    except MintRefused:
        raise IngestRefused("not_anchored") from None
    files = minted_files(family, claim_hash, beacon, n_train, n_test)
    canary = f"CHH-MINT-CANARY-{subject}"
    _precheck(vault, [(train_id, DatasetRole.DEV, {"train.csv": files.train}, {}),
                      (subject, DatasetRole.FUTURE, {"test_X.csv": files.test_x},
                       {"test_y.csv": files.test_y, "mint.json": files.record})])
    try:
        vault.register_dataset(train_id, DatasetRole.DEV, public={"train.csv": files.train}, private={})
        vault.register_dataset(subject, DatasetRole.FUTURE, public={"test_X.csv": files.test_x},
                               private={"test_y.csv": files.test_y, "mint.json": files.record}, canary=canary)
        # the same beacon again after a crash before _publish: the same bytes, no new entry, nothing lost (GOAL 18.6)
        mint = earlier[0] if earlier else Kernel(vault.store).record_mint(subject, claim_hash, beacon, files.task_id)
    except (VaultError, KernelError):  # pre-checked above; a race still fails closed
        raise IngestRefused("ingest_refused") from None
    return _publish(vault, key, run_id, "minted_heldout", subject, [train_id, subject],
                    {"n_train": files.n_train, "n_test": files.n_test, "task_id": files.task_id,
                     "mint_seq": mint.seq, "anchor_length": int(anchor["length"])})


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
    data = _read(Path(file))
    key = _key("returned_measurements", dataset_id, claim_hash,
               hashlib.sha256(data + b"\0" + attestation.encode()).hexdigest())
    done = _done(vault, key)
    if done is not None:  # the same return again: the recorded result, nothing appended
        return done
    if dataset_id in vault.datasets():
        raise IngestRefused("already_registered")
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
    return _publish(vault, key, run_id, "returned_measurements", dataset_id, [dataset_id], {"data_sha256": data_sha})
