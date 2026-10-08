"""Run a solution program as an F4 predictor (GOAL §16.5 item 7; the pure check is `verify.transductive`).

The program follows the ML solution CLI contract (research/task.py):
  python solution.py --train TRAIN_CSV --test TEST_X_CSV --out PREDS_CSV --seed N
Each `predict(rows)` writes a test_X.csv holding exactly those rows (the given header), runs the program in a FRESH
sandbox from `sandbox_factory` (via `kernel.execution.run_program`) with the same code, train file and seed, and
returns its predictions. A failed run (nonzero exit, timeout, missing or malformed predictions) raises
`PredictorFailed`, which the check records as the closed code `predictor_failed`: never silently ok. The run's
diagnostic text stays on the predictor object (`diagnostics`) for the operator; it never enters the F4 report.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence

from ..kernel.execution import DEFAULT_ARGV, SandboxFactory, run_program
from .transductive import F4Report, PredictorFailed, Rows, check_transductive


def _csv(header: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _read_csv(data: bytes) -> tuple[list[str], Rows]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    if not rows:
        raise ValueError("empty CSV")
    return rows[0], rows[1:]


class ProgramPredictor:
    """`predict(rows) -> predictions` backed by one sandboxed program run per call."""

    def __init__(self, sandbox_factory: SandboxFactory, *, code: bytes, entrypoint: str, train_csv: bytes,
                 test_header: Sequence[str], seed: int, timeout_s: float, argv: tuple[str, ...] = DEFAULT_ARGV,
                 train_name: str = "train.csv", test_name: str = "test_X.csv", label: str = "f4"):
        self.sandbox_factory, self.code, self.entrypoint, self.argv = sandbox_factory, code, entrypoint, argv
        self.train = (train_name, train_csv)
        self.test_header, self.test_name = list(test_header), test_name
        self.seed, self.timeout_s, self.label = seed, timeout_s, label
        self.runs = 0
        self.diagnostics: list[str] = []

    def __call__(self, rows: Rows) -> list[str]:
        self.runs += 1
        sandbox = self.sandbox_factory(f"{self.label}-{self.runs}")
        preds, diag = run_program(sandbox, code=self.code, entrypoint=self.entrypoint, argv=self.argv,
                                  inputs={"train": self.train,
                                          "test": (self.test_name, _csv(self.test_header, rows))},
                                  seed=self.seed, timeout_s=self.timeout_s)
        self.diagnostics.append(diag)
        if preds is None:
            raise PredictorFailed
        return preds


def check_program(sandbox_factory: SandboxFactory, *, code: bytes, entrypoint: str, train_csv: bytes,
                  test_x_csv: bytes, seed: int, timeout_s: float, regression: bool = False,
                  target_column: str | None = None, argv: tuple[str, ...] = DEFAULT_ARGV) -> F4Report:
    """F4 check of one program on one test_X file. With `target_column`, the mixing probe uses the train rows'
    features (target dropped, columns in test_X order) as the other rows; otherwise duplicated test rows."""
    header, rows = _read_csv(test_x_csv)
    extra: Rows | None = None
    if target_column is not None:
        t_header, t_rows = _read_csv(train_csv)
        missing = [c for c in header if c not in t_header]
        if target_column not in t_header or missing:
            raise ValueError(f"train CSV lacks the target column or test features: {target_column!r}, {missing}")
        cols = [t_header.index(c) for c in header]
        extra = [[r[i] for i in cols] for r in t_rows]
    predictor = ProgramPredictor(sandbox_factory, code=code, entrypoint=entrypoint, train_csv=train_csv,
                                 test_header=header, seed=seed, timeout_s=timeout_s, argv=argv)
    return check_transductive(predictor, rows, seed=seed, regression=regression, extra_rows=extra)
