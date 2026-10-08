"""Task selection by reward variance (GOAL §5 L8; the AWorld idea, which its repo does not implement).

A task whose rollouts always succeed or always fail carries no learning signal (zero reward variance; for 0/1 rewards
the variance is p(1-p), largest at p = 0.5). Tasks are ranked by population variance of their rollout rewards,
descending, ties by task id; tasks with fewer than `config.VARIANCE_MIN_ROLLOUTS` rollouts (`TOO_FEW_ROLLOUTS`) or a
variance below `config.VARIANCE_MIN` (`LOW_VARIANCE`) are excluded with the reason. A non-finite reward is an error.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import config as C


@dataclass(frozen=True)
class TaskVariance:
    task_id: str
    variance: float
    n: int


@dataclass
class VarianceSelection:
    selected: list[TaskVariance] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)


def select_by_reward_variance(rewards: Mapping[str, Sequence[float]], *, k: int | None = None,
                              min_rollouts: int = C.VARIANCE_MIN_ROLLOUTS,
                              min_variance: float = C.VARIANCE_MIN) -> VarianceSelection:
    out = VarianceSelection()
    ranked: list[TaskVariance] = []
    for task, rs in rewards.items():
        vals = [float(r) for r in rs]
        if not all(math.isfinite(v) for v in vals):
            raise ValueError(f"task {task!r} has a non-finite reward")
        if len(vals) < min_rollouts:
            out.excluded[task] = "TOO_FEW_ROLLOUTS"
            continue
        var = statistics.pvariance(vals)
        if var < min_variance:
            out.excluded[task] = "LOW_VARIANCE"
            continue
        ranked.append(TaskVariance(task, var, len(vals)))
    ranked.sort(key=lambda x: (-x.variance, x.task_id))
    out.selected = ranked if k is None else ranked[:k]
    return out
