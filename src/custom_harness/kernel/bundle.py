"""Reproducibility Bundle (GOAL §1.3, L2): everything needed to re-check a certificate on a clean machine.

A bundle is a zip: manifest.json + blobs/<sha256>. It holds the claim and certificate, the ledger
entries that matter (registrations, every pre-registration, every data access to the involved datasets,
the certificate entry), the evidence records, the runs and their full traces, and every blob they refer
to (code, data, predictions, canaries). Bundle hash = sha256 of manifest.json.

`reproduce_bundle` rebuilds a fresh store from the bundle alone, re-runs the Kernel with a fresh
replayer (programs are re-executed from the bundled bytes) and compares the result with the bundled
certificate.

Ledger (format chh-bundle/2): the bundle carries the WHOLE ledger prefix up to and including the certificate
entry, and reproduction checks it as one unbroken hash chain from genesis, so no entry (e.g. an early label
read) can be left out or replaced without breaking the chain. Its head hash is reported, so it can be compared
with a published anchor (`chh ledger-anchor`). Entry bodies hold ids, hashes and purposes, never data. The
bundle also carries the public files and canaries of every registered dataset (the Kernel's shared-example and
canary checks look at all of them). Old chh-bundle/1 bundles carry an excerpt: they are still re-checked, but
the report labels the ledger check "EXCERPT ONLY" (per-entry hashes and order, not an unbroken chain).

Reading is bounded (zip bombs): per-member and total uncompressed size caps, a member-count cap and a
compression-ratio cap; a bundle over any cap is refused with a BundleError naming the cap (fail closed).
The environment is re-created from the recorded description, not from a pinned image digest unless one was
recorded.

A bundle CONTAINS THE HIDDEN LABELS of its targets (a third party needs them to re-check). Treat it like a
task's private/ directory: never mount it, or anything derived from it, into an agent-visible place.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..records import Evidence, EvidenceKind, Run, RunStatus, TraceStep
from ..store.evidence import EvidenceStore
from .certificate import CHECK_IDS, Certificate
from .checks import KernelConfig, Replayer
from .claim import Claim, canonical_json
from .execution import ExecRecord
from .kernel import Kernel
from .ledger import Entry, verify_chain
from .vault import CertificateRecord

FORMAT = "chh-bundle/2"
LEGACY_FORMATS = frozenset({"chh-bundle/1"})  # ledger excerpt only: re-checked, labelled "EXCERPT ONLY"


@dataclass(frozen=True)
class ReadLimits:
    """Caps on what reproduction reads from a bundle (all uncompressed bytes)."""

    member_bytes: int = 512 * 2**20
    total_bytes: int = 2 * 2**30
    members: int = 100_000
    ratio: float = 200.0  # uncompressed / compressed, enforced for members larger than `ratio_floor`
    ratio_floor: int = 8 * 2**20


DEFAULT_LIMITS = ReadLimits()


class BundleError(ValueError):
    pass


def build_bundle(kernel: Kernel, cert_hash: str, out_path: Path) -> str:
    cert = kernel.certificate(cert_hash)
    claim = Claim.model_validate(cert.claim)
    store = kernel.store
    blobs: set[str] = {claim.subject.hash, claim.comparator.hash}
    evidence: list[dict[str, Any]] = []
    run_ids: set[str] = set()
    dataset_ids = set(claim.generalization_targets) | set(claim.development) | set(claim.scope.datasets)
    for eid in cert.evidence_ids:
        try:
            ev = store.get_evidence(eid)
        except KeyError:
            continue  # the certificate already records unresolvable evidence (K0)
        evidence.append(ev.model_dump(mode="json"))
        blobs.add(ev.payload_ref)
        try:
            doc = store.blobs.get_json(ev.payload_ref)
            if ev.kind == EvidenceKind.PROOF:  # a computational claim's certificate: its artifact bytes travel along
                blobs.add(CertificateRecord.model_validate(doc).hash)
                continue
            rec = ExecRecord.model_validate(doc)
        except (ValueError, OSError):
            continue
        blobs |= {rec.preds_ref, rec.artifact_hash} | {r.blob for r in rec.inputs.values()}
        dataset_ids |= {rec.target} | {r.dataset_id for r in rec.inputs.values()}
        run_ids.add(rec.run_id)
    all_entries = kernel.ledger.entries()
    cert_seq = next(e.seq for e in all_entries if e.kind == "certificate" and e.body.get("cert_hash") == cert_hash)
    # the WHOLE prefix up to the certificate (an unbroken chain from genesis): the ledger as it was when the
    # certificate was issued, with nothing left out
    entries = [e for e in all_entries if e.seq <= cert_seq]
    for e in entries:
        b = e.body
        if e.kind == "register_dataset":  # private files only of the claim's datasets; public files of all
            blobs |= {f["blob"] for f in b["files"].values() if b.get("dataset_id") in dataset_ids or not f["private"]}
            if b.get("canary_ref"):
                blobs.add(b["canary_ref"])
        if e.kind == "certificate" and b.get("claim_hash") == cert.claim_hash:
            blobs.add(b["blob"])  # this certificate, and earlier ones of the same claim (K3 reads them, R1-F3)
    runs = []
    for rid in sorted(run_ids):
        try:
            run = store.get_run(rid)
        except KeyError:
            continue
        status, detail = store.run_status(rid)
        runs.append({"run": run.model_dump(mode="json"), "status": status.value, "detail": detail,
                     "steps": [s.model_dump(mode="json") for s in store.trace(rid)]})
    manifest = {
        "format": FORMAT, "cert_hash": cert_hash, "claim_hash": cert.claim_hash, "label": cert.label.value,
        "kernel_config": cert.config, "ledger": [list(e.row()) for e in entries], "ledger_head": entries[-1].hash,
        "evidence": evidence,
        "runs": runs, "blobs": sorted(blobs),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
    }
    data = canonical_json(manifest)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", data)
        for h in sorted(blobs):
            try:
                z.writestr(f"blobs/{h}", store.blobs.get(h))
            except OSError:
                continue  # missing blobs are reported by K0 when the bundle is re-checked
    bundle_hash = hashlib.sha256(data).hexdigest()
    kernel.ledger.append("bundle", {"cert_hash": cert_hash, "bundle_hash": bundle_hash})
    return bundle_hash


@dataclass
class ReproduceReport:
    reproduced: bool
    bundle_hash: str
    original_label: str
    reproduced_label: str | None
    problems: list[str] = field(default_factory=list)
    certificate: Certificate | None = None
    ledger_check: str = ""  # what was verified about the bundled ledger (full chain, or EXCERPT ONLY for v1)
    ledger_head: str | None = None  # hash of the last bundled ledger entry (compare with a published anchor)


def reproduce_bundle(path: Path, replayer_factory: Callable[[], Replayer], workdir: Path | None = None,
                     limits: ReadLimits = DEFAULT_LIMITS) -> ReproduceReport:
    """Re-check a bundle from its bytes alone. reproduced = same label AND the same status on every check.
    Raises BundleError for an unsupported format or a bundle over a read limit (refused, never partly read)."""
    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="chh-reproduce-")
        workdir = Path(tmp.name)
    try:
        return _reproduce(Path(path), replayer_factory, Path(workdir), limits)
    finally:
        if tmp is not None:
            tmp.cleanup()


class _Reader:
    """Bounded member reads: a zip bomb is refused before it is held in memory."""

    def __init__(self, z: zipfile.ZipFile, limits: ReadLimits):
        self.z, self.limits, self.total = z, limits, 0
        infos = z.infolist()
        if len(infos) > limits.members:
            raise BundleError(f"bundle has {len(infos)} members, more than the cap of {limits.members}")
        names = [i.filename for i in infos]
        dups = sorted({n for n in names if names.count(n) > 1})
        if dups:  # a dict keeps the LAST member of a name; another reader may show the first: refuse (fail closed)
            raise BundleError(f"bundle has duplicate member name(s) {dups[:5]}: refused")
        self.infos = {i.filename: i for i in infos}

    def read(self, name: str) -> bytes:
        info, lim = self.infos[name], self.limits
        if info.file_size > lim.member_bytes:
            raise BundleError(f"bundle member {name} declares {info.file_size} bytes, over the per-member cap of "
                              f"{lim.member_bytes}")
        if info.file_size > lim.ratio_floor and info.file_size > lim.ratio * max(info.compress_size, 1):
            raise BundleError(f"bundle member {name} has a compression ratio over {lim.ratio:g} "
                              f"({info.file_size} bytes from {info.compress_size}): refused as a possible zip bomb")
        self.total += info.file_size
        if self.total > lim.total_bytes:
            raise BundleError(f"bundle content exceeds the total cap of {lim.total_bytes} bytes (at member {name})")
        # zipfile never returns more than the DECLARED size (ZipExtFile truncates at file_size, and a stream that
        # does not match the declared size and CRC raises BadZipFile): checking declared sizes bounds memory
        return self.z.read(info)


def _reproduce(path: Path, replayer_factory: Callable[[], Replayer], workdir: Path, limits: ReadLimits
               ) -> ReproduceReport:
    with zipfile.ZipFile(path) as z:
        reader = _Reader(z, limits)
        if "manifest.json" not in reader.infos:
            raise BundleError("bundle has no manifest.json")
        data = reader.read("manifest.json")
        manifest = json.loads(data)
        bundle_hash = hashlib.sha256(data).hexdigest()
        fmt = manifest.get("format")
        if fmt != FORMAT and fmt not in LEGACY_FORMATS:
            raise BundleError(f"unsupported bundle format {fmt!r}")
        store = EvidenceStore(workdir / "store")
        problems: list[str] = []
        try:
            for h in manifest["blobs"]:
                if f"blobs/{h}" not in reader.infos:
                    problems.append(f"blob {h[:12]} missing from the bundle")
                    continue
                if store.blobs.put(reader.read(f"blobs/{h}")) != h:
                    problems.append(f"blob {h[:12]} does not match its hash (tampered)")
        except BaseException:  # any failure here (a read limit, a malformed manifest) closes the store
            store.close()
            raise
    full = fmt == FORMAT
    try:
        entries = [Entry.from_row((int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), float(r[5])))
                   for r in manifest["ledger"]]
        problems += verify_chain(entries, full=full)
        head = entries[-1].hash if entries else None
        ledger_check = (f"unbroken hash chain from genesis: {len(entries)} entries, head {head}" if full else
                        f"EXCERPT ONLY ({fmt}): {len(entries)} entries, each checked against its own hash and for "
                        f"order; NOT an unbroken chain from genesis, so omitted entries cannot be detected")
        for e in entries:
            store.ledger_import(e.row())
        for item in manifest["runs"]:
            store.add_run(Run.model_validate(item["run"]))
            for s in item["steps"]:
                store.add_step(TraceStep.model_validate(s))
            store.set_run_status(item["run"]["id"], RunStatus(item["status"]), item["detail"])
        for ev in manifest["evidence"]:
            try:
                store.add_evidence(Evidence.model_validate(ev))
            except ValueError as e:  # its payload blob is missing: K0 will fail that record
                problems.append(f"evidence {ev.get('id')} not importable: {e}")
        entry = next((e for e in entries if e.kind == "certificate" and e.body["cert_hash"] == manifest["cert_hash"]),
                     None)
        if entry is None:
            return ReproduceReport(False, bundle_hash, manifest["label"], None,
                                   [*problems, "certificate entry missing from the bundle"], None, ledger_check, head)
        try:
            original = Certificate.model_validate(store.blobs.get_json(entry.body["blob"]))
        except OSError:
            return ReproduceReport(False, bundle_hash, manifest["label"], None,
                                   [*problems, "certificate content missing from the bundle"], None, ledger_check,
                                   head)
        kernel = Kernel(store, config=KernelConfig(**manifest["kernel_config"]), replayer=replayer_factory())
        again = kernel.certify(Claim.model_validate(original.claim), list(original.evidence_ids), full_chain=full)
        for kid in CHECK_IDS:
            if again.checks[kid].status != original.checks[kid].status:
                problems.append(f"{kid}: {original.checks[kid].status} originally, {again.checks[kid].status} now "
                                f"({'; '.join(again.checks[kid].reasons)[:300]})")
        if again.label != original.label:
            problems.append(f"label {original.label.value} originally, {again.label.value} now")
        return ReproduceReport(not problems, bundle_hash, original.label.value, again.label.value, problems, again,
                               ledger_check, head)
    finally:
        store.close()
