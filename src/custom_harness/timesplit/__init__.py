"""Time-split prediction benchmark (GOAL §6.4, §4.5, M7). See docs/timesplit.md."""

from .benchmark import (
    Benchmark,
    DataEntry,
    DataFile,
    Observation,
    Registration,
    Resolution,
    Task,
    TimesplitError,
    Window,
    check_observation_date,
    check_registration,
    freeze_benchmark,
    load_benchmark,
    register_prediction,
    resolve,
)
from .probes import Audit, FakeMemorisationProbe, MemorisationProbe, MemorisationResult, ProbeFlag, audit
from .rules import RuleUndetermined, ThresholdRule, evaluate, rule_hash
from .scoring import EVOLVED_BETTER, NOT_SHOWN, Comparison, Scorecard, compare, score

__all__ = [
    "EVOLVED_BETTER", "NOT_SHOWN", "Audit", "Benchmark", "Comparison", "DataEntry", "DataFile",
    "FakeMemorisationProbe", "MemorisationProbe", "MemorisationResult", "Observation", "ProbeFlag", "Registration",
    "Resolution", "RuleUndetermined", "Scorecard", "Task", "ThresholdRule", "TimesplitError", "Window", "audit",
    "check_observation_date", "check_registration", "compare", "evaluate", "freeze_benchmark", "load_benchmark",
    "register_prediction", "resolve", "rule_hash", "score",
]
