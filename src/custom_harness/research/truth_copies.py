"""Truth-copy declarations at task registration (GOAL §16.5.4, invariant 18; s4-integrity item 2).

The suite exporter (research/suite.write_task) records every public column that carries a copy of the held-out labels
in the task's truth dir (`truth_copies.json`: [{"file", "column"}]; never in an agent-readable file). At ingest the
TRUSTED process that registers the task's data turns each record into `Kernel.declare_truth_copy`, so K3's
non-interference replay runs on every record given such a file.

Where it is called (coordination with s4-kerneld, 2026-10-15): task data are registered INSIDE kerneld
(kerneld/ingest.research_task), so the call belongs there, right after the vault registration:

    from ..research.truth_copies import declare_task_truth_copies
    declare_task_truth_copies(Kernel(vault.store), task, root, ids)

`kerneld/` is s4-kerneld's code; this module only offers the function (pure apart from the Kernel call) and the
exporter side. Until that one call is added, production declares nothing (stated in docs/integrity_s4.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..truth_layout import truth_file

TRUTH_COPIES_FILE = "truth_copies.json"
TRUTH_COPY_DECLARER = "task ingest (exporter record truth_copies.json)"


class TruthCopyRecordError(ValueError):
    """The exporter's record is malformed or names a file the task does not have (fail closed: no registration)."""


def read_truth_copies(task: Any, task_dir: Path) -> list[tuple[str, str]]:
    """The exporter's (file, column) records for `task_dir` (empty when the exporter found none). Reads only the
    truth dir (truth_layout), so it runs only where the truth may be read (the trusted ingest process)."""
    p = truth_file(task_dir, TRUTH_COPIES_FILE)
    if not p.exists():
        return []
    try:
        decls = json.loads(p.read_text(encoding="utf-8"))
        pairs = [(str(d["file"]), str(d["column"])) for d in decls]
    except (ValueError, KeyError, TypeError) as e:
        raise TruthCopyRecordError(f"task {task.id}: malformed {TRUTH_COPIES_FILE} ({type(e).__name__})") from None
    allowed = {task.train_file, task.test_features_file}
    bad = sorted(f for f, _c in pairs if f not in allowed)
    if bad:
        raise TruthCopyRecordError(f"task {task.id}: {TRUTH_COPIES_FILE} names {bad}, not public files of the task")
    return sorted(set(pairs))


def declare_task_truth_copies(kernel: Any, task: Any, task_dir: Path, ids: dict[str, str]) -> list[Any]:
    """Declare every recorded copy of the task's held-out truth (target = the test dataset `ids["test"]`) to the
    Kernel. An identical earlier declaration is not repeated (before s4-kernel's idempotent API, a duplicate would
    make K3 perturb one column twice). Returns the ledger entries."""
    by_file = {task.train_file: ids["train"], task.test_features_file: ids["test"]}
    out = []
    for file, column in read_truth_copies(task, task_dir):
        body = {"target": ids["test"], "dataset_id": by_file[file], "file": file, "column": column}
        old = [e for e in kernel.ledger.entries("truth_copy") if {k: e.body.get(k) for k in body} == body]
        out.append(old[0] if old else kernel.declare_truth_copy(ids["test"], by_file[file], file, column,
                                                                TRUTH_COPY_DECLARER))
    return out
