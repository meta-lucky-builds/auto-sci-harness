"""The honest corpus (GOAL §16.6, invariant 20): claim histories the verifier MUST certify, each paired with a
planted-fault counterpart it MUST reject.

An item is one certified (or rejected) claim with everything needed to re-derive its verdict:

    <corpus>/<version>/<half>/<item id>/
        store.zip       the whole evidence store (ledger, vault, evidence, blobs) right after the certificate
        bundle.zip      the certificate's Reproducibility Bundle (kernel/bundle.py), for reproduce_bundle
        ledger.jsonl    the ledger rows up to and including the certificate, one JSON row per line (human-readable)
        expected.json   the expected verdict: role (honest / fault), label, every check's status, the fault planted

Items are built deterministically, with no model call, by `scripts/honest_corpus.py` from the clean-room spec
worlds (tests/spec), the Kernel test world (tests/kernel_world.py) and the scripted research run (tests/test_research).
The committed manifest (`redteam/honest_corpus/manifest.json`) lists every item id, its pair and its half; for DEV
items it also carries the expected record, for SEALED items only a commitment (sha256 of the expected record), so
the sealed labels are never committed. The sealed half's files live under custom-harness-data, outside every
agent mount; reading them goes through the look ledger (`looks.require`), and fails closed without a recorded look.

Honest limits:
- Sealing is accounting only (owner decision 2026-10-08): the sealed files are readable on this machine and the
  builder that regenerates them is committed.
- A re-check is a re-derivation of the Kernel's verdict on the stored ledger AS OF the certificate (the same
  operation resweep applies to archived certificates). Spec-world items replay stored predictions (their K3 is a
  formality); Kernel-world and research items re-execute trusted repository programs in LocalSandbox, which needs
  test mode (CHH_ALLOW_UNISOLATED=1); without it those items are UNCHECKABLE and a screen over them fails closed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..kernel import Claim, Kernel, KernelConfig
from ..kernel.checks import Replayer
from ..kernel.execution import ExecRecord, SandboxReplayer
from ..kernel.vault import Vault
from ..store import EvidenceStore
from . import looks

SET_NAME = "honest-corpus"
VERSION = "v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "redteam" / "honest_corpus" / "manifest.json"
DEFAULT_LOOKS = REPO_ROOT / "redteam" / "sealed_looks.jsonl"


def default_root() -> Path:
    """Where the corpus files live: custom-harness-data/assure/honest-corpus (outside the repository)."""
    from ..config import data_dir
    return data_dir() / "assure" / "honest-corpus"


class CorpusError(RuntimeError):
    pass


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def commitment(expected: dict[str, Any]) -> str:
    """sha256 of the expected record: committed for sealed items in place of the record itself."""
    return hashlib.sha256(canonical(expected)).hexdigest()


def digest(expected: dict[str, Any]) -> str:
    """The deterministic part of an item (recipe, claim hash, expected label and checks): equal for two builds."""
    keep = {k: expected[k] for k in ("id", "pair", "role", "source", "recipe", "claim_hash", "label", "checks",
                                     "fault")}
    return hashlib.sha256(canonical(keep)).hexdigest()


# ------------------------------------------------------------------ store packing
def pack_store(store_root: Path, out: Path) -> None:
    """Zip a CLOSED store directory, with sorted names and a fixed timestamp."""
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in sorted(p for p in store_root.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(f.relative_to(store_root).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())


def unpack_store(zip_path: Path, dest: Path) -> Path:
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            target = (dest / name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise CorpusError(f"{zip_path}: unsafe path {name!r} in the store archive")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))
    return dest


def ledger_lines(kernel: Kernel, last_seq: int) -> str:
    out = io.StringIO()
    for e in kernel.ledger.entries():
        if e.seq <= last_seq:
            out.write(json.dumps({"seq": e.seq, "kind": e.kind, "body": e.body, "prev": e.prev, "hash": e.hash},
                                 sort_keys=True) + "\n")
    return out.getvalue()


# ------------------------------------------------------------------ replayers
class StoredPredictionsReplayer:
    """Returns the stored predictions (spec-world items only: the clean-room harness's replayer)."""

    def __init__(self) -> None:
        self.calls = 0

    def replay(self, record: ExecRecord, vault: Vault) -> list[str] | None:
        self.calls += 1
        return list(vault.store.blobs.get_json(record.preds_ref))


class Uncheckable(RuntimeError):
    """The item cannot be re-derived here (e.g. its programs need a sandbox this process may not use)."""


def replayer_for(kind: str, workdir: Path, trusted: set[str], store: EvidenceStore) -> Replayer:
    if kind == "stored":
        return StoredPredictionsReplayer()
    if kind != "local-trusted":
        raise Uncheckable(f"unknown replayer kind {kind!r}")
    if os.environ.get("CHH_ALLOW_UNISOLATED") != "1":
        raise Uncheckable("this item re-executes programs in LocalSandbox, which needs test mode "
                          "(CHH_ALLOW_UNISOLATED=1); without it the item is UNCHECKABLE")
    from ..sandbox import LocalSandbox
    inner = SandboxReplayer(lambda key: LocalSandbox(workdir / "replay" / key, i_understand_no_isolation=True))

    class _Pinned:  # only the corpus's pinned trusted programs may run unisolated (fail closed on anything else)
        calls = 0

        def replay(self, record: ExecRecord, vault: Vault) -> list[str] | None:
            if record.artifact_hash not in trusted:
                raise Uncheckable(f"program {record.artifact_hash[:12]} is not a pinned trusted corpus program")
            return inner.replay(record, vault)

    return _Pinned()


# ------------------------------------------------------------------ items
@dataclass(frozen=True)
class Item:
    id: str
    half: str
    dir: Path
    expected: dict[str, Any]


def manifest(path: Path = MANIFEST) -> dict[str, Any]:
    if not path.exists():
        raise CorpusError(f"no corpus manifest at {path}: build it with scripts/honest_corpus.py")
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def _load(root: Path, version: str, half: str, entry: dict[str, Any]) -> Item:
    d = root / version / half / str(entry["id"])
    try:
        expected = json.loads((d / "expected.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CorpusError(f"item {entry['id']}: unreadable expected.json ({type(e).__name__})") from None
    if half == "sealed" and commitment(expected) != entry.get("commitment"):
        raise CorpusError(f"sealed item {entry['id']}: expected record does not match its committed hash")
    if half == "dev" and expected != entry.get("expected"):
        raise CorpusError(f"dev item {entry['id']}: expected record differs from the committed manifest")
    return Item(str(entry["id"]), half, d, expected)


def dev_items(root: Path | None = None, man: dict[str, Any] | None = None) -> list[Item]:
    man = man or manifest()
    root = root or default_root()
    return [_load(root, man["version"], "dev", e) for e in man["items"] if e["half"] == "dev"]


def sealed_items(look: looks.Look, root: Path | None = None, man: dict[str, Any] | None = None) -> list[Item]:
    """The sealed half; refuses unless `look` is a recorded look covering every sealed item (fail closed)."""
    man = man or manifest()
    entries = [e for e in man["items"] if e["half"] == "sealed"]
    if look.set_name != SET_NAME:
        raise looks.LookError(f"a look at {look.set_name!r} does not open the {SET_NAME!r} sealed half")
    looks.require(look, [str(e["id"]) for e in entries])
    root = root or default_root()
    return [_load(root, man["version"], "sealed", e) for e in entries]


def open_sealed(by: str, reason: str, *, ledger: Path = DEFAULT_LOOKS, man: dict[str, Any] | None = None,
                head: str = "", purpose: str = "check") -> looks.Look:
    man = man or manifest()
    ids = [str(e["id"]) for e in man["items"] if e["half"] == "sealed"]
    return looks.open_look(ledger, SET_NAME, ids, by, reason, head,
                           extra={"purpose": purpose, "corpus_version": man["version"]})


@contextmanager
def opened(item: Item) -> Iterator[tuple[EvidenceStore, Path]]:
    """A scratch copy of the item's store (re-derivation logs Kernel reads; the corpus itself stays pristine)."""
    with tempfile.TemporaryDirectory(prefix=f"corpus-{item.id}-") as tmp:
        work = Path(tmp)
        unpack_store(item.dir / "store.zip", work / "store")
        store = EvidenceStore(work / "store")
        try:
            yield store, work
        finally:
            store.close()


@dataclass(frozen=True)
class Recheck:
    item: str
    role: str
    expected_label: str
    label: str | None  # None = uncheckable
    checks: dict[str, str]
    uncheckable: str | None = None

    @property
    def certified(self) -> bool:
        return bool(self.label and self.label.startswith("CERTIFIED"))

    @property
    def flipped(self) -> bool:
        """An honest item no longer certified, or a planted fault now certified (or a label change either way)."""
        return self.label is not None and self.label != self.expected_label


def recheck(item: Item, config: KernelConfig | None = None, trusted: set[str] | None = None,
            overrides: dict[str, Any] | None = None) -> Recheck:
    """Re-derive the item's verdict as of its certificate under `config` (default: the rules it was issued under,
    with `overrides` applied on top: a resweep rule)."""
    exp = item.expected
    try:
        with opened(item) as (store, work):
            cert_cfg = config if config is not None else dataclasses.replace(KernelConfig(**exp["config"]),
                                                                             **(overrides or {}))
            rep = replayer_for(exp["replayer"], work, trusted if trusted is not None else set(exp.get("trusted", [])),
                               store)
            kernel = Kernel(store, config=cert_cfg, replayer=rep, clock=lambda: 1.0)
            claim = Claim.model_validate(kernel.certificate(exp["cert_hash"]).claim)
            again = kernel.certify(claim, list(exp["evidence_ids"]), as_of=int(exp["cert_seq"]) - 1)
    except Uncheckable as e:
        return Recheck(item.id, exp["role"], exp["label"], None, {}, str(e))
    return Recheck(item.id, exp["role"], exp["label"], again.label.value,
                   {k: v.status for k, v in again.checks.items()})


def check_all(items: list[Item], config: KernelConfig | None = None,
              progress: Callable[[Recheck], None] | None = None,
              overrides: dict[str, Any] | None = None) -> list[Recheck]:
    out = []
    for it in items:
        r = recheck(it, config, overrides=overrides)
        out.append(r)
        if progress:
            progress(r)
    return out


def summary(results: list[Recheck]) -> dict[str, Any]:
    honest = [r for r in results if r.role == "honest"]
    faults = [r for r in results if r.role == "fault"]
    return {"items": len(results), "honest": len(honest), "faults": len(faults),
            "uncheckable": [r.item for r in results if r.label is None],
            "honest_flips": [r.item for r in honest if r.label is not None and not r.certified],
            "fault_escapes": [r.item for r in faults if r.certified],
            "label_changes": [r.item for r in results if r.flipped]}
