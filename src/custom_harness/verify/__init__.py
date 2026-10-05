"""Verifier ladder (PLAN L3). Grows: stats (T2), metrics (T1 scoring), judges (T5), quarantine."""

from .metrics import HIGHER_IS_BETTER, score
from .stats import DiffCI, T2Verdict, bootstrap_diff_ci, t2_verdict

__all__ = ["HIGHER_IS_BETTER", "score", "DiffCI", "T2Verdict", "bootstrap_diff_ci", "t2_verdict"]
