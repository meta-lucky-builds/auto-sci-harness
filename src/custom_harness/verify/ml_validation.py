"""Fault-injection validation of the `ml` adapter (GOAL §5 "Domain adapters", L3 "verifier validation").

Moved verbatim from kernel/adapters.py by s4-kernel (2026-10-15), as the Lean adapter's fixtures already live in
verify/lean_validation.py: each check plants a known fault or a known answer. This module is listed in the adapter's
modules, so it is part of the ML adapter's content hash: changing a fixture needs a new human approval (before the
move the fixtures sat in kernel/adapters.py, which no adapter hash covered).
"""

from __future__ import annotations

from collections.abc import Callable

from ..kernel.execution import ExecutionError, parse_predictions
from . import leak, transductive
from . import metrics as M
from .stats import mean_t_interval, paired_example_interval


def raises(fn: Callable[[], object]) -> bool:
    try:
        fn()
    except (ValueError, ExecutionError):
        return True
    return False


def _effect_interval_ok() -> bool:
    """Planted effect: arm a is right on all 100 examples, arm b on half of them -> effect 0.5 exactly."""
    y = [str(i % 2) for i in range(100)]
    b = [y[i] if i < 50 else ("0" if y[i] == "1" else "1") for i in range(100)]
    iv = paired_example_interval(M.accuracy, y, [y], [b], higher_is_better=True, alpha=0.1, n_boot=1000)
    return abs(iv.estimate - 0.5) < 1e-12 and 0.0 < iv.lo <= 0.5 <= iv.hi


ML_CHECKS: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("accuracy of a known case", lambda: M.accuracy(["a", "b"], ["a", "a"]) == 0.5),
    ("misaligned predictions are refused", lambda: raises(lambda: M.accuracy(["a"], ["a", "b"]))),
    ("empty labels are refused", lambda: raises(lambda: M.accuracy([], []))),
    ("non-finite regression values are refused", lambda: raises(lambda: M.rmse(["1"], ["nan"]))),
    ("macro F1 of a perfect prediction", lambda: M.f1_macro(["0", "1", "1"], ["0", "1", "1"]) == 1.0),
    ("planted paired effect is recovered and bounded", _effect_interval_ok),
    ("Student-t interval covers a known mean", lambda: mean_t_interval([1.0, 2.0, 3.0], alpha=0.05).lo < 2.0
     < mean_t_interval([1.0, 2.0, 3.0], alpha=0.05).hi),
    ("planted canary is detected", lambda: bool(leak.scan_text("x CHH-CANARY-ab12 y", "t", ["CHH-CANARY-ab12"], []))),
    ("base64-encoded planted canary is detected",  # base64 of "x CHH-CANARY-ab12 y"
     lambda: bool(leak.scan_text("eCBDSEgtQ0FOQVJZLWFiMTIgeQ==", "t", ["CHH-CANARY-ab12"], []))),
    ("multi-column predictions are refused", lambda: raises(lambda: parse_predictions(b"prediction\n1,2\n"))),
    *transductive.F4_CHECKS,  # F4 (GOAL §16.5 item 7): a prediction must not depend on the other test rows
)
