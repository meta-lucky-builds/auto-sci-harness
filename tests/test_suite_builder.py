"""The task-suite builder (GOAL M2). Synthetic families are checked here end to end on the host; the real-data
and repair tasks need the sandbox image (scripts/validate_suite.py checks all 21 tasks there)."""

from __future__ import annotations

import csv
import io
import sys
from collections import Counter
from pathlib import Path

import yaml

from custom_harness.kernel.execution import DEFAULT_ARGV, run_program
from custom_harness.research.suite import REAL, REPAIRS, SYNTHETIC, build_suite, real_task
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


def test_real_task_builder_shapes_and_repair_metadata():
    data = {"columns": ["a", "b"], "X": [[float(i), float(i % 3)] for i in range(50)], "y": [i % 2 for i in range(50)]}
    t = real_task("wine", data, seed=1)
    assert t.metric == "accuracy" and len(t.train) == 35 and len(t.test) == 15 and t.family == "real"
    r = real_task("wine", data, seed=1, task_id="wine_knn_unscaled", defect="x", baseline="code")
    assert r.family == "repair" and r.defect == "x" and r.baseline == "code"
    assert set(REPAIRS) and all(spec[0] in REAL for spec in REPAIRS.values())
    assert sys.version_info >= (3, 12)
