"""TEST STAND-IN for kerneld's private side inside a registered test module.

Since the kerneld boundary (s4-kerneld, GOAL §16.5.2) a module's `ctx.kernel()` is a ScopedKernel on the HOST store:
it refuses private ingest (`register_dataset` with private files) and logged private reads outside a Kernel grant.
Several tests (taint, release, recall, critic, leak, campaign replay) need a module step that registers a private
dataset and makes a logged private read during the run, which in production only kerneld (a separate process on the
same store) does. This helper opens the store a second time, as kerneld does (the ledger append is BEGIN IMMEDIATE,
safe across connections and processes), and hands the test module a Vault on it. It is test code only: production
modules never get it (the kerneld ratchet in tests/test_kerneld_boundary.py scans src/, not tests/).

Added by stage-c (2026-10-23): on int18 these modules called `ctx.kernel().vault.register_dataset(..., private=...)`
and every such run failed with PrivateDataRefused."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def kerneld_vault(ctx: Any) -> Iterator[Any]:
    """A Vault on a second, raw connection to the run's store (kerneld's view)."""
    from custom_harness.kernel import Vault
    from custom_harness.store import EvidenceStore

    store = EvidenceStore(Path(ctx.kernel().store.root))
    try:
        yield Vault(store)
    finally:
        store.close()
