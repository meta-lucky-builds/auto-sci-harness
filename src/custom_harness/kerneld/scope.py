"""The Kernel as host code may hold it (GOAL §16.5.2): private reads only inside per-claim grants.

On a `HostStore` (kerneld/hoststore.py) private blobs are refused unless a grant names them. This module is the only
place that opens grants:

- `ScopedKernel.certify(claim, ...)` (and so `verify` / `verify_status`, which re-derive through `certify`): the
  private files of THIS claim's generalization targets, logged reads of purpose `kernel` on those targets only, and
  the canary blobs of every dataset (K9 scans for all of them; a canary is a marker, not a label).
- `ScopedKernel.bundle(cert_hash, out)`: the private files of the datasets the bundle packs (the claim's targets,
  development and scope datasets, and the targets and inputs of its evaluation records: kernel/bundle.py's rule) and
  every canary blob. No read is logged (the bundle reads blobs, not the vault), so no purpose is granted.
- `screening_canaries(vault)`: canary blobs only, for the host's content screens (memory admission, citation and
  recall screening) that must look for canary tokens in text.

Every host construction of the Kernel is a `ScopedKernel` (ratchet: no bare `Kernel(` outside kernel/ and the
privileged kerneld modules). A bare Kernel on a HostStore fails closed: its private reads raise, and a check that
raises is a FAILED check (kernel/kernel.py), never a pass. On a plain EvidenceStore (the Kernel's own process, the
test worlds) the grants change nothing.

A grant is a contextvar: it covers the calling thread/context for the duration of the call, including callbacks the
Kernel makes (the replayer). That is the stated limit of an in-process boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ..kernel import Certificate, Claim, ExecRecord, Kernel
from .hoststore import PrivateDataRefused, _grant, host_store

BUNDLE_RECORD = "kerneld_bundle"


def _private_files(vault: Any, datasets: Iterable[str]) -> set[str]:
    recs = vault.datasets()
    return {f.blob for d in set(datasets) if d in recs for f in recs[d].files.values() if f.private}


def _canary_blobs(vault: Any) -> set[str]:
    return {r.canary_ref for r in vault.datasets().values() if r.canary_ref}


class _UngrantedReplayer:
    """A replayer whose calls run under an EMPTY grant (s4-kerneld-review finding 1): the replayer is chosen by the
    caller, so it must not inherit the certify grant (it would read the targets' labels). Replays need public data
    only (kernel/execution.py reads inputs with read_public). Attributes the Kernel probes (`run_inputs`) are wrapped
    the same way; a missing one stays missing (getattr default)."""

    def __init__(self, inner: Any):
        self.__inner = inner

    def replay(self, record: Any, vault: Any) -> Any:
        with _grant((), why="replay"):
            return self.__inner.replay(record, vault)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.__inner, name)
        if not callable(attr):
            return attr

        def call(*a: Any, **kw: Any) -> Any:
            with _grant((), why="replay"):
                return attr(*a, **kw)
        return call


class ScopedKernel(Kernel):
    """The Evidence Kernel for host code: identical checks, private reads only inside the grants above. Whatever store
    it is given, it runs on the HostStore view of it, and its replayer runs without any grant."""

    def __init__(self, store: Any, **kw: Any):
        replayer = kw.pop("replayer", None)
        super().__init__(host_store(store), replayer=_UngrantedReplayer(replayer) if replayer is not None else None,
                         **kw)

    def certify(self, claim: Claim, evidence_ids: Sequence[str], *, full_chain: bool = True, record: bool = True,
                as_of: int | None = None) -> Certificate:
        targets = tuple(claim.generalization_targets)
        with _grant(_private_files(self.vault, targets) | _canary_blobs(self.vault), targets, ("kernel",),
                    why="certify"):
            return super().certify(claim, list(evidence_ids), full_chain=full_chain, record=record, as_of=as_of)

    def bundle(self, cert_hash: str, out_path: Path | str) -> str:
        """`kernel.bundle.build_bundle` under a grant for exactly the private files the bundle packs."""
        from ..kernel.bundle import build_bundle
        datasets = _bundle_datasets(self, cert_hash)
        packed = _private_files(self.vault, datasets)
        with _grant(packed | _canary_blobs(self.vault), why="bundle"):
            bundle_hash = build_bundle(self, cert_hash, Path(out_path))
        # accountability (s4-kerneld-review finding 2): the bundle carries private files to a file on disk without a
        # vault read, so each bundling is recorded (not hash-chained: ledger kinds are the Kernel's closed set)
        self.store.append_record(BUNDLE_RECORD, {"cert_hash": cert_hash, "bundle_hash": bundle_hash,
                                                 "path": str(Path(out_path)), "datasets": sorted(datasets),
                                                 "private_blobs": sorted(packed)})
        return bundle_hash


def _bundle_datasets(kernel: Kernel, cert_hash: str) -> set[str]:
    """The datasets whose private files kernel/bundle.py packs (mirror of its rule; if that rule ever widens, the
    bundle fails closed with PrivateDataRefused instead of silently packing more)."""
    cert = kernel.certificate(cert_hash)
    claim = Claim.model_validate(cert.claim)
    out = set(claim.generalization_targets) | set(claim.development) | set(claim.scope.datasets)
    for eid in cert.evidence_ids:
        try:
            rec = ExecRecord.model_validate(kernel.store.blobs.get_json(kernel.store.get_evidence(eid).payload_ref))
        except (KeyError, ValueError, OSError, PrivateDataRefused):
            continue  # unresolvable or not an evaluation record (a proof): it names no dataset
        out |= {rec.target} | {r.dataset_id for r in rec.inputs.values()}
    return out


def screening_canaries(vault: Any) -> dict[str, str]:
    """{dataset id: canary token} of every registered dataset with a canary, for host CONTENT SCREENS only (they must
    find canary tokens in text). Labels are never readable this way: the grant names canary blobs only."""
    refs = {d: r.canary_ref for d, r in vault.datasets().items() if r.canary_ref}
    with _grant(refs.values(), why="screen"):
        return {d: vault.store.blobs.get(ref).decode("utf-8", errors="replace") for d, ref in refs.items()}
