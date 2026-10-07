"""Reproducibility Bundle (GOAL §1.3, L2): everything needed to re-check a certificate on a clean machine.

A bundle is a zip: manifest.json + blobs/<sha256>. It holds the claim and certificate, the ledger
entries that matter (registrations, every pre-registration, every data access to the involved datasets,
the certificate entry), the evidence records, the runs and their full traces, and every blob they refer
to (code, data, predictions, canaries). Bundle hash = sha256 of manifest.json.

`reproduce_bundle` rebuilds a fresh store from the bundle alone, re-runs the Kernel with a fresh
replayer (programs are re-executed from the bundled bytes) and compares the result with the bundled
certificate. Limits, stated: the ledger in a bundle is an EXCERPT, so each entry's own hash and the
ordering are verified, not the unbroken chain from genesis; the environment is re-created from the
recorded description, not from a pinned image digest unless one was recorded.

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

from ..records import Evidence, Run, RunStatus, TraceStep
from ..store.evidence import EvidenceStore
from .certificate import CHECK_IDS, Certificate
from .checks import KernelConfig, Replayer
from .claim import Claim, canonical_json
from .execution import ExecRecord
from .kernel import Kernel
from .ledger import Entry, verify_chain

FORMAT = "chh-bundle/1"


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
            rec = ExecRecord.model_validate(store.blobs.get_json(ev.payload_ref))
        except (ValueError, OSError):
            continue
        blobs |= {rec.preds_ref, rec.artifact_hash} | {r.blob for r in rec.inputs.values()}
        dataset_ids |= {rec.target} | {r.dataset_id for r in rec.inputs.values()}
        run_ids.add(rec.run_id)
    entries: list[Entry] = []
    all_entries = kernel.ledger.entries()
    cert_seq = next(e.seq for e in all_entries if e.kind == "certificate" and e.body.get("cert_hash") == cert_hash)
    for e in all_entries:
        b = e.body
        if e.seq > cert_seq:
            break  # the bundle captures the ledger as it was when the certificate was issued
        keep = (e.kind in ("prereg", "adapter_approval")  # approvals: K1 re-checks the adapter on reproduction
                or (e.kind == "register_dataset" and b.get("dataset_id") in dataset_ids)
                or (e.kind == "register_artifact" and b.get("hash") in blobs)
                or (e.kind == "data_access" and b.get("dataset_id") in dataset_ids)
                or (e.kind == "certificate" and b.get("cert_hash") == cert_hash))
        if keep:
            entries.append(e)
        if e.kind == "register_dataset" and b.get("dataset_id") in dataset_ids:
            blobs |= {f["blob"] for f in b["files"].values()}
            if b.get("canary_ref"):
                blobs.add(b["canary_ref"])
        if e.kind == "certificate" and b.get("cert_hash") == cert_hash:
            blobs.add(b["blob"])
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
        "kernel_config": cert.config, "ledger": [list(e.row()) for e in entries], "evidence": evidence,
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


def reproduce_bundle(path: Path, replayer_factory: Callable[[], Replayer], workdir: Path | None = None
                     ) -> ReproduceReport:
    """Re-check a bundle from its bytes alone. reproduced = same label AND the same status on every check."""
    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory(prefix="chh-reproduce-")
        workdir = Path(tmp.name)
    try:
        return _reproduce(Path(path), replayer_factory, Path(workdir))
    finally:
        if tmp is not None:
            tmp.cleanup()


def _reproduce(path: Path, replayer_factory: Callable[[], Replayer], workdir: Path) -> ReproduceReport:
    with zipfile.ZipFile(path) as z:
        data = z.read("manifest.json")
        manifest = json.loads(data)
        bundle_hash = hashlib.sha256(data).hexdigest()
        if manifest.get("format") != FORMAT:
            raise BundleError(f"unsupported bundle format {manifest.get('format')!r}")
        store = EvidenceStore(workdir / "store")
        problems: list[str] = []
        names = set(z.namelist())
        for h in manifest["blobs"]:
            if f"blobs/{h}" not in names:
                problems.append(f"blob {h[:12]} missing from the bundle")
                continue
            if store.blobs.put(z.read(f"blobs/{h}")) != h:
                problems.append(f"blob {h[:12]} does not match its hash (tampered)")
    try:
        entries = [Entry.from_row((int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), float(r[5])))
                   for r in manifest["ledger"]]
        problems += verify_chain(entries, full=False)
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
                                   [*problems, "certificate entry missing from the bundle"])
        try:
            original = Certificate.model_validate(store.blobs.get_json(entry.body["blob"]))
        except OSError:
            return ReproduceReport(False, bundle_hash, manifest["label"], None,
                                   [*problems, "certificate content missing from the bundle"])
        kernel = Kernel(store, config=KernelConfig(**manifest["kernel_config"]), replayer=replayer_factory())
        again = kernel.certify(Claim.model_validate(original.claim), list(original.evidence_ids), full_chain=False)
        for kid in CHECK_IDS:
            if again.checks[kid].status != original.checks[kid].status:
                problems.append(f"{kid}: {original.checks[kid].status} originally, {again.checks[kid].status} now "
                                f"({'; '.join(again.checks[kid].reasons)[:300]})")
        if again.label != original.label:
            problems.append(f"label {original.label.value} originally, {again.label.value} now")
        return ReproduceReport(not problems, bundle_hash, original.label.value, again.label.value, problems, again)
    finally:
        store.close()
