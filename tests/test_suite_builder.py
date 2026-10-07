"""The task-suite builder (GOAL M2). Synthetic families are checked here end to end on the host; the real-data
and repair tasks need the sandbox image (scripts/validate_suite.py checks all 21 tasks there)."""

from __future__ import annotations

import csv
import dataclasses
import io
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from custom_harness.evolve.driver import load_suite
from custom_harness.kernel.execution import DEFAULT_ARGV, run_program
from custom_harness.research import suite as suite_mod
from custom_harness.research.suite import REAL, REPAIRS, SYNTHETIC, build_suite, real_task, rings, write_task
from custom_harness.research.task import load_task
from custom_harness.sandbox import LocalSandbox
from custom_harness.verify import metrics as M


def _labels(p: Path) -> list[str]:
    return [r[0] for r in list(csv.reader(io.StringIO(p.read_text(encoding="utf-8"))))[1:]]


def test_synthetic_suite_is_well_formed_and_baselines_run(tmp_path):
    suite = yaml.safe_load(build_suite(tmp_path / "s", real_data=None).read_text(encoding="utf-8"))
    tasks = suite["tasks"]
    assert len(tasks) == len(SYNTHETIC) == 12 and len({t["id"] for t in tasks}) == 12
    assert set(Counter(t["split"] for t in tasks)) == {"evolve", "gate", "sealed"}
    for i, t in enumerate(tasks):
        d = Path(t["dir"])
        task = load_task(d)
        test_header = (d / "public" / task.test_features_file).read_text(encoding="utf-8").splitlines()[0]
        assert task.target_column not in test_header.split(",")
        assert (d / "private" / "canary.txt").read_text(encoding="utf-8").startswith("CHH-CANARY-")
        y = _labels(d / "private" / task.test_labels_file)
        sb = LocalSandbox(tmp_path / "sb" / str(i), i_understand_no_isolation=True)  # stdlib baselines: trusted
        preds, diag = run_program(sb, code=(d / task.baseline_file).read_bytes(), entrypoint="solution.py",
                                  argv=DEFAULT_ARGV, inputs={
                                      "train": (task.train_file, (d / "public" / task.train_file).read_bytes()),
                                      "test": (task.test_features_file,
                                               (d / "public" / task.test_features_file).read_bytes())},
                                  seed=0, timeout_s=120)
        assert preds is not None and len(preds) == len(y), (t["id"], diag)
        M.score(task.metric, y, preds)  # scorable


def _fake_real_data() -> dict[str, dict]:
    """Host-side stand-ins for the sandbox export: distinct rows per dataset (offset k), so datasets never collide."""
    out = {}
    for k, name in enumerate(sorted(suite_mod.REAL)):
        out[name] = {"columns": ["a", "b"], "X": [[float(k * 1000 + i), float(i % 7)] for i in range(60)],
                     "y": [float(i % 3) for i in range(60)]}
    return out


def _base_dataset(task_id: str) -> str:
    """Derived independently of the builder: a repair task re-splits REPAIRS[id][0]; '<name>-real' splits <name>."""
    if task_id in REPAIRS:
        return REPAIRS[task_id][0]
    return task_id.removesuffix("-real")


@pytest.mark.parametrize("drop", [None, "iris"])
def test_tasks_from_one_dataset_share_a_split(tmp_path, monkeypatch, drop):
    """Live finding 2026-10-06: every held-out example of a real-data task is public in its repair variant (same
    base dataset, different split). The pairs shared a split only by luck of the cyclic assignment; removing one
    real dataset (here iris, which has no repair variant) shifts the pattern and used to split every pair."""
    if drop:
        monkeypatch.setattr(suite_mod, "REAL", {k: v for k, v in suite_mod.REAL.items() if k != drop})
    path = build_suite(tmp_path / "s", real_data=_fake_real_data())
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))["tasks"]
    splits: dict[str, set[str]] = {}
    for t in tasks:
        splits.setdefault(_base_dataset(t["id"]), set()).add(t["split"])
    assert {k: v for k, v in splits.items() if len(v) > 1} == {}
    assert set(Counter(t["split"] for t in tasks)) == {"evolve", "gate", "sealed"}
    assert len(load_suite(path)) == len(tasks)  # the evolution entry point accepts it


def test_repair_tasks_share_their_real_tasks_data_and_tests_are_deduplicated(tmp_path):
    """A repair task and its real-data task now use the SAME split (and canary): no held-out label of one is public
    in the other, and both name one held-out target in the vault (one multiplicity family). Test rows whose
    features also occur in the training file are dropped (benchmark de-duplication; the live iris task had one)."""
    data = _fake_real_data()
    data["wine"]["X"][1] = list(data["wine"]["X"][0])  # a duplicate example, so de-duplication has work to do
    build_suite(tmp_path / "s", real_data=data)
    for tid, (name, _model, _defect) in REPAIRS.items():
        real, rep = tmp_path / "s" / f"{name}-real", tmp_path / "s" / tid
        for rel in ("public/train.csv", "public/test_X.csv", "private/test_y.csv", "private/canary.txt"):
            assert (real / rel).read_bytes() == (rep / rel).read_bytes(), (tid, rel)
    for d in (tmp_path / "s").iterdir():
        if (d / "task.yaml").exists():
            task = load_task(d)
            lines = (d / "public" / task.train_file).read_text(encoding="utf-8").splitlines()
            ti = lines[0].split(",").index(task.target_column)
            feats = {",".join(v for j, v in enumerate(r.split(",")) if j != ti) for r in lines[1:]}
            test = (d / "public" / task.test_features_file).read_text(encoding="utf-8").splitlines()[1:]
            assert not feats & set(test), d.name
            assert len(test) == len(_labels(d / "private" / task.test_labels_file))


def test_write_task_drops_test_rows_duplicating_a_training_example(tmp_path):
    spec = suite_mod.TaskSpecFiles("t", "t", "d", "accuracy", "y", ["a", "b", "y"],
                                   train=[["1", "2", "0"], ["3", "4", "1"]],
                                   test=[["1", "2", "1"], ["5", "6", "0"]], baseline="x", family="f")
    d = write_task(spec, tmp_path)
    assert (d / "public" / "test_X.csv").read_text().splitlines() == ["a,b", "5,6"]
    assert (d / "private" / "test_y.csv").read_text().splitlines() == ["y", "0"]


def _two_task_suite(tmp_path, *, share_rows: bool, sources: tuple[str | None, str | None]) -> Path:
    a = rings(1, 120)
    b = dataclasses.replace(a, task_id="rings-copy") if share_rows else rings(2, 120)
    entries = []
    for spec, split, source in ((a, "evolve", sources[0]), (b, "gate", sources[1])):
        d = write_task(spec, tmp_path)
        entries.append({"id": spec.task_id, "dir": str(d), "split": split, **({"source": source} if source else {})})
    p = tmp_path / "suite.yaml"
    p.write_text(yaml.safe_dump({"tasks": entries}), encoding="utf-8")
    return p


def test_suite_loading_refuses_examples_shared_across_splits(tmp_path):
    """Content-based, whatever the declared sources say: the same rows under two ids in two splits."""
    with pytest.raises(ValueError, match="share 120 example row"):
        load_suite(_two_task_suite(tmp_path, share_rows=True, sources=("a", "b")))


def test_suite_loading_refuses_a_task_without_source(tmp_path):
    with pytest.raises(ValueError, match="no `source`"):
        load_suite(_two_task_suite(tmp_path, share_rows=False, sources=("rings-s1", None)))


def test_suite_loading_accepts_isolated_splits(tmp_path):
    assert len(load_suite(_two_task_suite(tmp_path, share_rows=False, sources=("rings-s1", "rings-s2")))) == 2


def test_real_task_builder_shapes_and_repair_metadata():
    data = {"columns": ["a", "b"], "X": [[float(i), float(i % 3)] for i in range(50)], "y": [i % 2 for i in range(50)]}
    t = real_task("wine", data, seed=1)
    assert t.metric == "accuracy" and len(t.train) == 35 and len(t.test) == 15 and t.family == "real"
    r = real_task("wine", data, seed=1, task_id="wine_knn_unscaled", defect="x", baseline="code")
    assert r.family == "repair" and r.defect == "x" and r.baseline == "code"
    assert set(REPAIRS) and all(spec[0] in REAL for spec in REPAIRS.values())
    assert sys.version_info >= (3, 12)
