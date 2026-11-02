"""evolve/export.py and evolve/multiplicity.py ask kerneld for facts about held-out truth (GOAL 16.5.2).

Fail-first reasoning (against dbf473e): that version calls truth_file/truth_files/truth_dir/legacy_truth_dir and
read_bytes on truth paths in both modules (AST test fails); held_out_target_id hashes the labels in-process, so with
KernelD made unavailable it returns normally instead of raising KernelDUnavailable (fail-closed test fails); and
_fingerprints puts truth hashes and contents into fp (fp["hashes"] / fp["contents"] are non-empty, so the no-truth-in-fp
test fails). The target-id equality and planted-copy tests pin the kerneld answers to the in-process truth.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import custom_harness.evolve.export as X
import custom_harness.evolve.multiplicity as M
from custom_harness.evolve.evaluate import SuiteTask
from custom_harness.kerneld import client as kclient
from custom_harness.kerneld.hoststore import open_host_store
from custom_harness.research.task import load_task
from custom_harness.truth_layout import truth_file

ROOT = Path(__file__).resolve().parents[1]
_TRUTH_CALLS = {"truth_file", "truth_files", "truth_dir", "legacy_truth_dir"}


def _make_task(tmp_path: Path) -> Path:
    out = tmp_path / "rings"
    subprocess.run([sys.executable, str(ROOT / "examples" / "tasks" / "make_rings_task.py"), str(out),
                    "--n-train", "60", "--n-test", "40"], check=True, capture_output=True)
    return out


@pytest.mark.parametrize("mod", [X, M])
def test_source_does_not_read_truth(mod) -> None:
    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            assert name not in _TRUTH_CALLS, (node.lineno, name)
        if isinstance(node, ast.Attribute) and node.attr in ("read_bytes", "read_text"):
            assert "truth" not in ast.unparse(node.value), ast.unparse(node.value)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not imported & _TRUTH_CALLS


def test_target_id_matches_vault_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task_dir = _make_task(tmp_path)
    task = load_task(task_dir)
    store = open_host_store(tmp_path / "store")
    with kclient.KernelD(store.root) as kd:
        run = kd.open_run("r1")
        want = run.ingest_research_task(task.id, task_dir)["test"]
        run.close()
    assert M.held_out_target_id(task_dir) == want

    def boom(self, *a, **k):
        raise kclient.KernelDUnavailable("down")

    monkeypatch.setattr(kclient.KernelD, "__init__", boom)
    with pytest.raises(kclient.KernelDUnavailable):
        M.held_out_target_id(task_dir)


def test_export_audit_uses_kerneld_and_fp_holds_no_truth(tmp_path: Path) -> None:
    task_dir = _make_task(tmp_path)
    store = open_host_store(tmp_path / "store")
    fp = X._fingerprints(store, [SuiteTask("rings", task_dir, "evolve", family="rings")], {}, [])
    assert fp["contents"] == [] and fp["hashes"] == {} and fp["windows"] == []
    out = tmp_path / "export"
    out.mkdir()
    (out / "leak.csv").write_bytes(truth_file(task_dir, load_task(task_dir).test_labels_file).read_bytes())
    problems = X._audit(out, fp)
    assert any("is a copy of private file" in p for p in problems), problems
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    assert X._audit(clean, fp) == []
