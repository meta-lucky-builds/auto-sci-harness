"""The host's store handle (GOAL §16.5.2): an EvidenceStore that cannot read private data or ingest it.

Host code (the CLI commands, the Engine, every module, evolve, studio, memory, audit) works on a `HostStore`. It is the
same SQLite database and blob directory as the Kernel's store, with four refusals, all raised as
`PrivateDataRefused` BEFORE anything is read or written:

1. `blobs.get` / `blobs.get_json` of a PRIVATE blob: the bytes of any private file of any registered dataset, or any
   dataset's canary blob. The digests come from the ledger's `register_dataset` entries (new ones read whenever the
   ledger head moves), so a dataset registered by the kerneld process is covered on the host's next read.
2. a `register_dataset` ledger entry that names a private file: private ingest is a kerneld op (`kerneld/ingest.py`,
   `RunHandle.ingest_*`). A public-only registration is allowed.
3. a `data_access` ledger entry with `private: true`: a private read is logged only by the code that may make it.
4. the same two kinds through `ledger_import`.

The in-process Kernel still re-checks claims on held-out data (K4 re-reads labels, K9 reads canaries, a bundle packs
the claim's private files). It may do so only inside a GRANT: `_grant(...)` opens one for the current thread/context,
naming exactly the private digests and datasets it covers and the read purposes it allows. Grants are opened only by
`kerneld/scope.py` (`ScopedKernel.certify`, `ScopedKernel.bundle`, `screening_canaries`), narrowed per claim; the
ratchet (tests/test_kerneld_boundary.py) checks no other module names `_grant`.

What this is (stated, not hidden): a CODE boundary inside one Python process, not an OS boundary. Host code that
walks private attributes, calls `BlobStore.get` unbound, or opens the blob files with `open()` can still read the
bytes; the ratchet makes each of those a visible violation, and the kerneld process (`server.py`) remains the only
place where host-initiated private reads (score, rescore, leak scan) and private ingest happen. See
docs/kerneld_boundary.md ("Process model").
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from ..kernel.vault import KEYED
from ..store.blobs import BlobStore
from ..store.evidence import EvidenceStore, LedgerRow


class PrivateDataRefused(RuntimeError):
    """Host code tried to read, log or ingest private data outside kerneld. Deliberately NOT an OSError or a
    ValueError: callers that treat those as "missing" or "malformed" must not turn a refusal into a silent skip."""


@dataclass(frozen=True)
class _Grant:
    digests: frozenset[str]  # private blobs readable under this grant
    datasets: frozenset[str]  # datasets whose private reads may be LOGGED under this grant
    purposes: frozenset[str]  # vault read purposes that may be logged under this grant
    why: str  # a code, for diagnostics only


_GRANT: ContextVar[_Grant | None] = ContextVar("chh_kerneld_grant", default=None)


@contextlib.contextmanager
def _grant(digests: Iterable[str], datasets: Iterable[str] = (), purposes: Iterable[str] = (), *,
           why: str) -> Iterator[None]:
    """Open a private-read grant for the current context (privileged: kerneld/scope.py only). A nested grant
    REPLACES the outer one for its duration (never widens it), and the outer one is restored after."""
    token = _GRANT.set(_Grant(frozenset(digests), frozenset(datasets), frozenset(purposes), why))
    try:
        yield
    finally:
        _GRANT.reset(token)


class _PrivateIndex:
    """Private digests (private files + canary blobs) of every registered dataset, from the ledger. Refreshed
    incrementally: one primary-key lookup (`max(seq)`) per read, and only the NEW `register_dataset` rows when the
    ledger grew (s4-kerneld-review finding 9: no full scan per blob read). Ledger rows are immutable and a private
    registration cannot be imported below the head through the HostStore (`_check_entry`), so new rows always have a
    higher seq."""

    _HEAD = "SELECT coalesce(max(seq), 0) FROM ledger"
    _NEW = "SELECT body FROM ledger WHERE kind='register_dataset' AND seq > ? AND seq <= ? ORDER BY seq"

    def __init__(self, rows: Callable[..., list[tuple]], store_root: Path):
        self.__rows = rows
        self.__seen = 0
        self.__private: set[str] = set()
        self.__keyed_index = Path(store_root) / "private" / "index"
        self.__unresolved: set[str] = set()  # keyed refs whose content address is not (yet) in the Vault's index

    def _add(self, blob: str) -> None:
        """A private file's ledger ref. Since s4-kernel the Vault records it as KEYED + HMAC digest (never the content
        address), so the content address that blobs.get is called with comes from the Vault's index
        (`<store>/private/index/<digest>`, written before the ledger entry). int18 merge bug found by stage-c: the
        index held only the keyed ref, so every private label blob was served to host code."""
        if not blob.startswith(KEYED):
            self.__private.add(blob)
            return
        idx = self.__keyed_index / blob.removeprefix(KEYED)
        try:
            address = idx.read_text(encoding="ascii").strip() if blob.removeprefix(KEYED).isalnum() else ""
        except OSError:
            address = ""
        if address:
            self.__private.add(address)
            self.__unresolved.discard(blob)
        else:
            self.__unresolved.add(blob)

    def _refresh(self) -> None:
        for blob in sorted(self.__unresolved):
            self._add(blob)
        head = int(self.__rows(self._HEAD)[0][0])
        if head <= self.__seen:
            return
        for (body,) in self.__rows(self._NEW, (self.__seen, head)):
            try:
                b = json.loads(body)
            except ValueError:
                continue  # a malformed entry names no blob; the Kernel's ledger checks report it
            if not isinstance(b, dict):
                continue
            files = b.get("files")
            for f in files.values() if isinstance(files, dict) else ():
                # fail closed: a file entry whose privacy is not exactly False counts as private
                if isinstance(f, dict) and f.get("private") is not False and isinstance(f.get("blob"), str):
                    self._add(f["blob"])
            if isinstance(b.get("canary_ref"), str):
                self.__private.add(b["canary_ref"])
        self.__seen = head

    def is_private(self, digest: str) -> bool:
        """True for a private digest. Fails closed: while any keyed private ref cannot be resolved to its content
        address, every digest counts as private (the store cannot tell which blob holds that file)."""
        self._refresh()
        return digest in self.__private or bool(self.__unresolved)


class HostBlobs(BlobStore):
    """The blob store with private digests refused (outside a grant). Writes and `has` are unchanged."""

    def __init__(self, root: Path, index: _PrivateIndex):
        self.root = Path(root)  # the directory exists: the Kernel's store created it
        self.__index = index

    def get(self, digest: str) -> bytes:
        if self.__index.is_private(digest):
            g = _GRANT.get()
            if g is None or digest not in g.digests:
                raise PrivateDataRefused(f"blob {digest[:12]} is private: host code reads it only through kerneld "
                                         f"(GOAL §16.5.2)")
        return super().get(digest)


class HostStore(EvidenceStore):
    """An EvidenceStore view for host code (module docstring). Shares the Kernel store's connection and lock."""

    def __init__(self, inner: EvidenceStore):
        self.root = inner.root
        self._db = inner._db
        self._lock = inner._lock  # no reference to `inner` is kept (s4-kerneld-review finding 8)
        self.__index = _PrivateIndex(self._rows, inner.root)
        self.blobs = HostBlobs(inner.blobs.root, self.__index)

    # ---------------------------------------------------------------- refusals
    def _check_entry(self, kind: str, body: str) -> None:
        if kind not in ("register_dataset", "data_access"):
            return
        try:
            b = json.loads(body)
        except ValueError:
            raise PrivateDataRefused(f"unparsable {kind} entry from host code") from None
        if not isinstance(b, dict):
            raise PrivateDataRefused(f"malformed {kind} entry from host code")
        if kind == "register_dataset":
            files = b.get("files") or {}
            if not isinstance(files, dict) or any(not isinstance(f, dict) or f.get("private", True)
                                                  for f in files.values()):
                raise PrivateDataRefused(f"dataset {b.get('dataset_id')!r} has private files: private data are "
                                         f"ingested only by kerneld (RunHandle.ingest_*)")
            if b.get("canary_ref"):
                raise PrivateDataRefused(f"dataset {b.get('dataset_id')!r} has a canary: ingested only by kerneld")
            return
        if b.get("private") is False:
            return
        g = _GRANT.get()
        if g is None or b.get("dataset_id") not in g.datasets or b.get("purpose") not in g.purposes:
            raise PrivateDataRefused(f"a private read of {b.get('dataset_id')!r} ({b.get('purpose')!r}) by host "
                                     f"code: private reads go through kerneld (GOAL §16.5.2)")

    def ledger_append(self, kind: str, body: str, entry_hash: Callable[[int, str, str, str, float], str]
                      ) -> LedgerRow:
        self._check_entry(kind, body)
        return super().ledger_append(kind, body, entry_hash)

    def ledger_import(self, row: LedgerRow) -> None:
        self._check_entry(row[1], row[2])
        super().ledger_import(row)

    def close(self) -> None:
        with self._lock:
            self._db.close()  # what EvidenceStore.close does; the HostStore holds no other handle


def host_store(store: EvidenceStore) -> HostStore:
    """The host view of a store (idempotent). Used by the Engine and ModuleContext on whatever store they get."""
    return store if isinstance(store, HostStore) else HostStore(store)


def open_host_store(root: Path | str) -> HostStore:
    """Open the store for host code (the CLI): the only EvidenceStore construction on the host path. Closing the
    HostStore closes the underlying store."""
    return HostStore(EvidenceStore(Path(root)))
