"""ML research task format (PLAN D1: fixed-dataset improvement tasks first).

task_dir/
  task.yaml        TaskSpec
  public/          copied into the agent workspace (train.csv, test_X.csv)
  baseline.py      reference solution; same CLI contract as agent solutions
truth_layout.truth_dir(task_dir)/
                   test_y.csv, canary.txt (and mint.json): NEVER inside any agent-readable root. For a suite task
                   (under <data>/suites) it is <data>/private/suites/<same path>; elsewhere the legacy
                   task_dir/private/, allowed only outside every agent-readable root (truth_layout.py)

Solution CLI contract (agent code and baseline):
  python solution.py --train TRAIN_CSV --test TEST_X_CSV --out PREDS_CSV --seed N
  PREDS_CSV: header `prediction`, one row per test row, same order.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from ..verify.metrics import HIGHER_IS_BETTER


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    metric: str
    target_column: str
    train_file: str = "train.csv"
    test_features_file: str = "test_X.csv"
    test_labels_file: str = "test_y.csv"
    baseline_file: str = "baseline.py"
    solution_timeout_s: float = 900
    allowed_libraries: list[str] = ["numpy", "pandas", "scikit-learn", "scipy", "torch"]
    # Minimum effect (metric units, positive = better) a claim on this task must assert. None = default:
    # 0.02 for accuracy/f1_macro, 2% of the baseline's dev score for error metrics (rmse/mae).
    min_effect: float | None = None
    canary_file: str = "canary.txt"  # under private/, optional
    # Auxiliary agent-visible DEV tables under public/ (plain file names), profiled into the key graph (GOAL L4.2,
    # research/keys.py) and copied into the workspace's data/. Not passed to solution runs (the CLI contract has only
    # TRAIN and TEST): ideation context only.
    aux_files: list[str] = []

    @property
    def higher_is_better(self) -> bool:
        return HIGHER_IS_BETTER[self.metric]

    def effect_floor(self, baseline_dev_score: float) -> float:
        if self.min_effect is not None:
            return self.min_effect
        if self.higher_is_better:
            return 0.02
        return max(1e-9, 0.02 * abs(baseline_dev_score))

    def dataset_ids(self, data_digest: str, task_digest: str) -> dict[str, str]:
        """Vault ids. Datasets are identified by their CONTENT (digest of the data files only), whatever the task:
        tasks sharing data (a repair task re-using a real task's split) name ONE held-out target, so every claim
        on those labels joins one multiplicity family and no 'twin' exists under another id. The baseline is the
        task's own (task id + a digest of all task files): equal names never collide."""
        ns = f"data@{data_digest[:16]}"
        return {"train": f"{ns}/train", "test": f"{ns}/test", "baseline": f"{self.id}@{task_digest[:12]}/baseline"}


def load_task(task_dir: Path) -> TaskSpec:
    task_dir = Path(task_dir)
    spec = TaskSpec.model_validate(yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8")))
    for name in spec.aux_files:
        if not name or name != Path(name).name or name in (".", "..") or name in (
                spec.train_file, spec.test_features_file) or len(set(spec.aux_files)) != len(spec.aux_files):
            raise ValueError(f"task {spec.id}: aux file {name!r} must be a distinct plain file name under public/")
    for rel in (f"public/{spec.train_file}", f"public/{spec.test_features_file}", spec.baseline_file,
                *(f"public/{a}" for a in spec.aux_files)):
        if not (task_dir / rel).exists():
            raise FileNotFoundError(f"task {spec.id}: missing {rel}")
    from ..truth_layout import truth_file  # raises TruthLayoutError for unmigrated or agent-readable truth

    if not truth_file(task_dir, spec.test_labels_file).exists():
        raise FileNotFoundError(f"task {spec.id}: missing private/{spec.test_labels_file} (truth_layout.truth_dir)")
    return spec
