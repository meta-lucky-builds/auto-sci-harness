"""Validate a task suite in the Docker sandbox: every task loads, its baseline produces scorable predictions,
labels never appear in public files, and a generic reference model shows there is headroom.

    python scripts/validate_suite.py SUITE_YAML
Prints one JSON line per task and exits non-zero if any task is unusable.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
from pathlib import Path

import yaml

from custom_harness.kernel.execution import DEFAULT_ARGV, run_program
from custom_harness.research.task import load_task
from custom_harness.sandbox.docker import DockerSandbox
from custom_harness.verify import metrics as M

REFERENCE = '''import argparse
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
p = argparse.ArgumentParser()
for k in ("--train", "--test", "--out"): p.add_argument(k)
p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
tr = pd.read_csv(a.train); te = pd.read_csv(a.test)
y = tr.pop(TARGET)
X = pd.get_dummies(tr); Xt = pd.get_dummies(te).reindex(columns=X.columns, fill_value=0)
M = RandomForestRegressor if REGRESSION else RandomForestClassifier
model = M(n_estimators=300, random_state=a.seed, n_jobs=2).fit(X, y)
pd.DataFrame({"prediction": model.predict(Xt)}).to_csv(a.out, index=False)
'''


def labels(path: Path) -> list[str]:
    return [r[0] for r in list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))[1:] if r]


def main() -> int:
    suite = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, t in enumerate(suite["tasks"]):
            d = Path(t["dir"])
            task = load_task(d)
            train = (d / "public" / task.train_file).read_bytes()
            test = (d / "public" / task.test_features_file).read_bytes()
            y = labels(d / "private" / task.test_labels_file)
            header = test.decode("utf-8").splitlines()[0].split(",")
            problems = [] if task.target_column not in header else ["target column present in public test file"]
            scores = {}
            for name, code in (("baseline", (d / task.baseline_file).read_bytes()),
                               ("reference", REFERENCE.replace("TARGET", repr(task.target_column))
                                .replace("REGRESSION", str(task.metric in ("rmse", "mae"))).encode())):
                sb = DockerSandbox(Path(tmp) / f"{i}-{name}")
                preds, diag = run_program(sb, code=code, entrypoint="solution.py", argv=DEFAULT_ARGV,
                                          inputs={"train": (task.train_file, train),
                                                  "test": (task.test_features_file, test)},
                                          seed=0, timeout_s=600)
                if preds is None:
                    problems.append(f"{name} failed: {diag[-300:]}")
                    continue
                scores[name] = M.score(task.metric, y, preds)
            if "baseline" in scores and "reference" in scores:
                gain = scores["reference"] - scores["baseline"]
                gain = gain if task.higher_is_better else -gain
                if gain <= 0:
                    problems.append(f"no headroom: reference does not beat the baseline ({gain:+.4f})")
            bad += bool(problems)
            print(json.dumps({"task": t["id"], "split": t["split"], "metric": task.metric, "n_test": len(y),
                              **{k: round(v, 4) for k, v in scores.items()}, "problems": problems}), flush=True)
    print(json.dumps({"tasks": len(suite["tasks"]), "unusable": bad}))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
