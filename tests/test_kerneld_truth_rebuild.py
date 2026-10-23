"""research/rebuild.py asks kerneld for facts about held-out truth (GOAL 16.5.2); it never reads truth itself.

Fail-first reasoning (against dbf473e): that version calls truth_file/truth_files and read_bytes/read_text on truth
paths in rebuild.py (AST test fails), and task_digest hashes truth in-process, so with KernelD made unavailable it
returns normally instead of raising KernelDUnavailable (fail-closed test fails). The digest-equality and
label_agreement tests pin the kerneld answers to the in-process truth (they hold before and after; they guard the
change). The label_agreement test uses truth_query directly (prng_regression needs generators, hard to set up).
"""

from __future__ import annotations

import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import custom_harness.research.rebuild as R
from custom_harness.kerneld import client as kclient
from custom_harness.truth_layout import LEGACY_DIRNAME, truth_file, truth_files

ROOT = Path(__file__).resolve().parents[1]


def _make_task(tmp_path: Path) -> tuple[Path, Path]:
    suite = tmp_path / "suite"
    task = suite / "rings"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.run([sys.executable, str(ROOT / "examples" / "tasks" / "make_rings_task.py"), str(task),
                    "--n-train", "60", "--n-test", "30"], check=True, env=env, capture_output=True)
    return suite, task


def test_rebuild_source_does_not_read_truth() -> None:
    tree = ast.parse(Path(R.__file__).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in ("truth_file", "truth_files"):
                bad.append((node.lineno, name))
    assert not bad, bad
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not imported & {"truth_file", "truth_files"}
    # the only truth_dir use is rebuild()'s write target (out_truth, tested with is_dir/iterdir only)
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "truth_dir"]
    assert len(uses) == 1
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in ("read_bytes", "read_text"):
            seg = ast.unparse(n.value)
            assert "truth" not in seg, seg


def test_task_digest_truth_matches_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite, task = _make_task(tmp_path)
    want = {f"{task.name}/{LEGACY_DIRNAME}/{rel}": hashlib.sha256(p.read_bytes()).hexdigest()
            for rel, p in truth_files(task).items()}
    assert want
    got = R.task_digest(suite)
    for k, v in want.items():
        assert got[k] == v

    def boom(self, *a, **k):
        raise kclient.KernelDUnavailable("down")

    monkeypatch.setattr(kclient.KernelD, "__init__", boom)
    with pytest.raises(kclient.KernelDUnavailable):
        R.task_digest(suite)


def test_label_agreement_query(tmp_path: Path) -> None:
    _, task = _make_task(tmp_path)
    labels = [ln for ln in truth_file(task, "test_y.csv").read_text(encoding="utf-8").splitlines()[1:] if ln]
    ok = kclient.truth_query("label_agreement", task_dir=str(task), guess=labels)
    assert ok["present"] and ok["exact"] and ok["agree"] == ok["n"] == len(labels)
    flipped = [labels[0] + "_x"] + labels[1:]
    bad = kclient.truth_query("label_agreement", task_dir=str(task), guess=flipped)
    assert not bad["exact"] and bad["agree"] < bad["n"]
