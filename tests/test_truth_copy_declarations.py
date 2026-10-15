"""Truth-copy declarations reach the Kernel in production (s4-integrity item 2; GOAL §16.5.4, invariant 18).

The suite exporter (research.suite.write_task) records every public column that carries the held-out labels in the
task's truth dir (`truth_copies.json`), and the trusted ingest
(research.truth_copies.declare_task_truth_copies, called from kerneld's ingest) turns each record into
Kernel.declare_truth_copy, so K3's non-interference replay runs on records given that file.

Fail-first reasoning (NOT run: owner rule): at 82719e1 nothing in production called declare_truth_copy (s3-kernel's
docs: "nobody in production declares copies yet"), write_task wrote no declaration, and
research.truth_copies did not exist (ImportError).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_harness.research import suite as S
from custom_harness.research.task import load_task


def _spec(n: int = 40, leak: bool = True) -> S.TaskSpecFiles:
    header = ["a", "leak", "label"] if leak else ["a", "b", "label"]
    rows = [[f"{i * 0.37:.3f}", ("L" + str(i % 2)) if leak else f"{i * 1.3:.2f}", str(i % 2)] for i in range(n)]
    return S.TaskSpecFiles("leaky", "t", "d", "accuracy", "label", header, rows[: n // 2], rows[n // 2:],
                           "print(1)\n", "toy")


def test_exporter_records_a_relabelled_label_column(tmp_path: Path) -> None:
    d = S.write_task(_spec(), tmp_path / "suite")
    rec = json.loads((d / "private" / "truth_copies.json").read_text(encoding="utf-8"))
    assert rec == [{"file": "test_X.csv", "column": "leak"}]
    assert b"leak" not in (d / "task.yaml").read_bytes()  # the declaration never sits in an agent-readable file


def test_exporter_records_nothing_for_an_honest_task(tmp_path: Path) -> None:
    d = S.write_task(_spec(leak=False), tmp_path / "suite")
    assert not (d / "private" / "truth_copies.json").exists()
    d2 = S.write_task(S.synthetic_task("rings", 1, 300, key=b"k" * 32), tmp_path / "suite")
    assert not (d2 / "private" / "truth_copies.json").exists()


def test_detector_regression_and_small_sets() -> None:
    rows = [[f"{i}.5", f"{i * 2}"] for i in range(30)]
    assert S.truth_copy_columns(["x", "y"], rows, [f"{i}.50" for i in range(30)], "rmse") == ["x"]
    assert S.truth_copy_columns(["x", "y"], rows[:5], [r[0] for r in rows[:5]], "rmse") == []


def _registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from custom_harness.kernel import Kernel
    from custom_harness.research.claims import register_task
    from custom_harness.store import EvidenceStore

    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    d = S.write_task(_spec(), tmp_path / "suite")
    (d / "task.yaml").write_text("id: leaky\ntitle: t\ndescription: d\nmetric: accuracy\ntarget_column: label\n",
                                 encoding="utf-8")
    store = EvidenceStore(tmp_path / "store")

    class Ctx:  # register_task ingests through kerneld (s4-kerneld): it names the run and finds the store root
        run_id = "r"

        def kernel(self) -> Kernel:
            return Kernel(store)

    task = load_task(d)
    ids = register_task(Ctx(), task, d)  # type: ignore[arg-type]
    return Kernel(store), task, d, ids


def test_ingest_declares_each_copy_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_harness.research.truth_copies import TRUTH_COPY_DECLARER, declare_task_truth_copies

    kernel, task, d, ids = _registered(tmp_path, monkeypatch)
    declare_task_truth_copies(kernel, task, d, ids)
    declare_task_truth_copies(kernel, task, d, ids)  # re-registration adds no second declaration
    entries = kernel.ledger.entries("truth_copy")
    assert [(e.body["target"], e.body["dataset_id"], e.body["file"], e.body["column"], e.body["declarer"])
            for e in entries] == [(ids["test"], ids["test"], "test_X.csv", "leak", TRUTH_COPY_DECLARER)]


def test_malformed_or_foreign_record_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from custom_harness.research.truth_copies import TruthCopyRecordError, declare_task_truth_copies

    kernel, task, d, ids = _registered(tmp_path, monkeypatch)
    for bad in ("not json", json.dumps([{"file": "other.csv", "column": "leak"}])):
        (d / "private" / "truth_copies.json").write_text(bad, encoding="utf-8")
        with pytest.raises(TruthCopyRecordError):
            declare_task_truth_copies(kernel, task, d, ids)
