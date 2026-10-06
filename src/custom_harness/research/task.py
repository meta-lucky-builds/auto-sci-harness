"""ML research task format (PLAN D1: fixed-dataset improvement tasks first).

task_dir/
  task.yaml        TaskSpec
  public/          copied into the agent workspace (train.csv, test_X.csv)
  private/         NEVER copied into any agent-visible place (test_y.csv)
  baseline.py      reference solution; same CLI contract as agent solutions

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
    dev_fraction: float = 0.2
    solution_timeout_s: float = 900
    allowed_libraries: list[str] = ["numpy", "pandas", "scikit-learn", "scipy", "torch"]
    # Minimum effect (metric units, positive = better) a claim on this task must assert. None = default:
    # 0.02 for accuracy/f1_macro, 2% of the baseline's dev score for error metrics (rmse/mae).
    min_effect: float | None = None
    canary_file: str = "canary.txt"  # under private/, optional

    @property
    def higher_is_better(self) -> bool:
        return HIGHER_IS_BETTER[self.metric]

    def effect_floor(self, baseline_dev_score: float) -> float:
        if self.min_effect is not None:
            return self.min_effect
        if self.higher_is_better:
            return 0.02
        return max(1e-9, 0.02 * abs(baseline_dev_score))

    def dataset_ids(self, digest: str) -> dict[str, str]:
        """Vault ids, namespaced by a digest of the task's files: two tasks may share a name, never an id
        (same bytes -> same ids, so repeated claims on the same data share one multiplicity family)."""
        ns = f"{self.id}@{digest[:12]}"
        return {"train": f"{ns}/train", "test": f"{ns}/test", "baseline": f"{ns}/baseline"}


def load_task(task_dir: Path) -> TaskSpec:
    task_dir = Path(task_dir)
    spec = TaskSpec.model_validate(yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8")))
    for rel in (f"public/{spec.train_file}", f"public/{spec.test_features_file}",
                f"private/{spec.test_labels_file}", spec.baseline_file):
        if not (task_dir / rel).exists():
            raise FileNotFoundError(f"task {spec.id}: missing {rel}")
    return spec
