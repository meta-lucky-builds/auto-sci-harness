"""Non-interference replay against the STORED production output (GOAL §16.5.4, invariant 18).

The frozen program is re-run with every DECLARED copy of the truth (held-out labels) replaced by maximally different
worlds: the labels flipped, and one constant label. An honest program never reads those bytes, so its decisions
cannot change; a program whose decisions depend on them is caught whatever world it believes it is in. The
reference is the production output that was stored when the claim was evaluated, never a second replay: a device
that cheats only in production and behaves in every replay is caught only that way.

Decisions, not score bytes, are compared: class labels exactly; regression values within a tolerance (float
accumulation order changes honest bytes).

Exact class: a plain replay with the real copies must reproduce production exactly (run under the entropy pin,
sandbox/hermetic.py). If it does not, the program is nondeterministic and the paired McNemar dependence test is
the fallback: production plus (m-1) real-label replays against m flipped-world replays, counting on rows where the
paired runs disagree which side matches its own labels. It has a stealth margin (a leak too small to reach the
threshold), which is reported with the result. Regression programs have no McNemar fallback: nondeterministic
regression fails closed.

Limits (stated): only DECLARED copies are perturbed (a copy nobody declared, a memorised answer key or an encoded
copy is invisible here); copies are replaced where the runner can place them (the program's input files); copies
baked into the sandbox image are not yet replaceable by this runner. Origins: institutionalist/noninterference.py,
maverick/mcnemar*.py, noiseoptout.py (toy simulations, code_review/upgrade_review_2026-10-07).
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from ..kernel.execution import run_program
from ..sandbox.base import Sandbox
from ..sandbox.hermetic import EntropyPin

# ---------------------------------------------------------------------------------------------------------------
# Parameters (GOAL §13.8). One place.
MCNEMAR_RUNS = 3  # UNCALIBRATED (owner decision, GOAL §13.8): m real + m counterfactual runs (toy: 3 + 3)
MCNEMAR_Z = 3.0  # UNCALIBRATED (owner decision, GOAL §13.8): one-sided z above which label dependence is flagged
REGRESSION_REL_TOL = 1e-9  # UNCALIBRATED (owner decision, GOAL §13.8): regression decisions equal within this
REGRESSION_ABS_TOL = 1e-12  # UNCALIBRATED (owner decision, GOAL §13.8)
# ---------------------------------------------------------------------------------------------------------------


class World(StrEnum):
    FLIPPED = "flipped"
    CONSTANT = "constant"


class Reason(StrEnum):
    """Closed set (no free text can carry data out)."""

    DECISIONS_CHANGED_FLIPPED = "decisions_changed_flipped"
    DECISIONS_CHANGED_CONSTANT = "decisions_changed_constant"
    MCNEMAR_LABEL_DEPENDENCE = "mcnemar_label_dependence"
    NONDETERMINISTIC_REGRESSION = "nondeterministic_regression"
    RUN_FAILED = "run_failed"
    WRONG_PREDICTION_COUNT = "wrong_prediction_count"
    BAD_TRUTH_COPY = "bad_truth_copy"
    SINGLE_CLASS_TRUTH = "single_class_truth"


@dataclass(frozen=True)
class TruthCopy:
    """A declared copy of the held-out truth that the program could reach: a CSV file (by the runner's name for it)
    and the column holding the labels."""

    name: str
    column: str


@dataclass
class NIReport:
    ok: bool
    mode: str  # "exact" | "mcnemar" | "failed"
    reasons: list[Reason] = field(default_factory=list)
    changed: dict[str, int] = field(default_factory=dict)  # world -> rows whose decision changed
    runs: int = 0
    mcnemar_z: float | None = None
    #: McNemar only: the per-run fraction of rows a leak could shift while staying below MCNEMAR_Z, given the
    #: disagreement volume observed (an approximation: Z * sqrt(b + c) / (m * n)). Printed with the result.
    stealth_margin: float | None = None

    def summary(self) -> str:
        s = f"non-interference: {'PASS' if self.ok else 'FAIL'} ({self.mode}; {self.runs} runs)"
        if self.reasons:
            s += " reasons=" + ",".join(r.value for r in self.reasons)
        if self.changed:
            s += " changed=" + ",".join(f"{k}:{v}" for k, v in sorted(self.changed.items()))
        if self.stealth_margin is not None:
            s += f" z={self.mcnemar_z:.2f} stealth_margin={self.stealth_margin:.4f} (fraction of rows per run)"
        return s


# ------------------------------------------------------------------ worlds
def flip_values(values: list[str], *, regression: bool) -> list[str]:
    """Maximally different labels. Classes: every label becomes the next class in sorted order (binary: swapped).
    Regression: reflected around the midrange (y -> min + max - y; every value moves unless it is the midpoint)."""
    if regression:
        xs = [float(v) for v in values]
        lo, hi = min(xs), max(xs)
        return [repr(lo + hi - x) for x in xs]
    classes = sorted(set(values))
    nxt = {c: classes[(i + 1) % len(classes)] for i, c in enumerate(classes)}
    return [nxt[v] for v in values]


def constant_values(values: list[str], *, regression: bool) -> list[str]:
    """One label for every row: the least frequent class (ties: the last in sorted order), or the regression
    minimum. Either way the world is as unlike the real one as a constant can be."""
    if regression:
        lo = min(float(v) for v in values)
        return [repr(lo)] * len(values)
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    rare = min(sorted(counts, reverse=True), key=lambda c: counts[c])
    return [rare] * len(values)


def read_column(data: bytes, column: str) -> list[str]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    if not rows or column not in rows[0]:
        raise ValueError("label column missing")
    j = rows[0].index(column)
    return [r[j] for r in rows[1:]]


def replace_column(data: bytes, column: str, values: list[str]) -> bytes:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    j = rows[0].index(column)
    if len(values) != len(rows) - 1:
        raise ValueError("row count changed")
    for r, v in zip(rows[1:], values, strict=True):
        r[j] = v
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


def world_files(files: dict[str, bytes], copies: list[TruthCopy], world: World, *,
                regression: bool) -> dict[str, bytes]:
    out = dict(files)
    for c in copies:
        vals = read_column(files[c.name], c.column)
        new = (flip_values if world is World.FLIPPED else constant_values)(vals, regression=regression)
        out[c.name] = replace_column(out[c.name], c.column, new)  # RT-S4-1: every column, not the last
    return out


# ------------------------------------------------------------------ decisions
def changed_rows(a: list[str], b: list[str], *, regression: bool) -> int:
    """Rows whose decision differs (a length mismatch counts every row)."""
    if len(a) != len(b):
        return max(len(a), len(b))
    if not regression:
        return sum(x != y for x, y in zip(a, b, strict=True))
    n = 0
    for x, y in zip(a, b, strict=True):
        try:
            fx, fy = float(x), float(y)
        except ValueError:
            n += x != y
            continue
        n += not (math.isfinite(fx) and math.isfinite(fy)
                  and math.isclose(fx, fy, rel_tol=REGRESSION_REL_TOL, abs_tol=REGRESSION_ABS_TOL)) and x != y
    return n


def mcnemar_z(real_runs: list[list[str]], cf_runs: list[list[str]], labels: list[str]) -> float:
    """One-sided z of label dependence: on rows where a paired (real, counterfactual) run disagrees, b = the real run
    matches `labels` and the counterfactual does not, c = the other way round. Honest programs: b ~ c."""
    b = c = 0
    for a_run, c_run in zip(real_runs, cf_runs, strict=True):
        for x, y, lab in zip(a_run, c_run, labels, strict=True):
            if x != y:
                if x == lab:
                    b += 1
                elif y == lab:
                    c += 1
    return (b - c) / math.sqrt(b + c) if b + c else 0.0


def _disagreements(real_runs: list[list[str]], cf_runs: list[list[str]]) -> int:
    return sum(x != y for a, c in zip(real_runs, cf_runs, strict=True) for x, y in zip(a, c, strict=True))


# ------------------------------------------------------------------ the check
Runner = Callable[[dict[str, bytes]], list[str] | None]


def replay_noninterference(run: Runner, files: dict[str, bytes], copies: list[TruthCopy], production: list[str], *,
                           regression: bool, runs: int = MCNEMAR_RUNS) -> NIReport:
    """`run(files)` executes the frozen program (in a fresh, entropy-pinned sandbox) with exactly these files and
    returns its decisions (None = the run failed). `files` must hold every declared copy; `production` is the
    stored production output. Fails closed on every irregularity."""
    rep = NIReport(ok=False, mode="failed")
    truth: list[list[str]] = []
    try:
        for c in copies:
            truth.append(read_column(files[c.name], c.column))
            if regression and not all(math.isfinite(float(v)) for v in truth[-1]):
                raise ValueError("non-finite regression label")
    except (KeyError, ValueError, UnicodeDecodeError, IndexError):
        rep.reasons.append(Reason.BAD_TRUTH_COPY)
        return rep
    if any(len(set(t)) < 2 for t in truth):  # flipping a single-class copy is not a different world
        rep.reasons.append(Reason.SINGLE_CLASS_TRUTH)
        return rep

    def go(fs: dict[str, bytes]) -> list[str] | None:
        rep.runs += 1
        out = run(fs)
        if out is None:
            rep.reasons.append(Reason.RUN_FAILED)
        elif len(out) != len(production):
            rep.reasons.append(Reason.WRONG_PREDICTION_COUNT)
            return None
        return out

    real = go(files)
    if real is None:
        return rep
    worlds = {w: world_files(files, copies, w, regression=regression) for w in World}
    if changed_rows(real, production, regression=regression) == 0:
        rep.mode = "exact"
        for w, fs in worlds.items():
            out = go(fs)
            if out is None:
                return rep
            n = changed_rows(out, production, regression=regression)
            rep.changed[w.value] = n
            if n:
                rep.reasons.append(Reason.DECISIONS_CHANGED_FLIPPED if w is World.FLIPPED
                                   else Reason.DECISIONS_CHANGED_CONSTANT)
        rep.ok = not rep.reasons
        return rep
    # nondeterministic: the McNemar fallback (production counts as the first real run: a replay-aware device
    # that cheats only in production shows up as dependence on the real labels)
    rep.mode = "mcnemar"
    if regression:
        rep.reasons.append(Reason.NONDETERMINISTIC_REGRESSION)
        return rep
    real_runs = [production, real]
    while len(real_runs) < runs:
        out = go(files)
        if out is None:
            return rep
        real_runs.append(out)
    cf_runs = []
    for _ in range(runs):
        out = go(worlds[World.FLIPPED])
        if out is None:
            return rep
        cf_runs.append(out)
    labels = truth[0]
    cf_labels = read_column(worlds[World.FLIPPED][copies[0].name], copies[0].column)
    z = max(mcnemar_z(real_runs, cf_runs, labels), mcnemar_z(cf_runs, real_runs, cf_labels))
    rep.mcnemar_z = z
    rep.stealth_margin = MCNEMAR_Z * math.sqrt(max(_disagreements(real_runs, cf_runs), 1)) / (runs * len(production))
    if z > MCNEMAR_Z:
        rep.reasons.append(Reason.MCNEMAR_LABEL_DEPENDENCE)
    rep.ok = not rep.reasons
    return rep


def sandbox_runner(sandbox_factory: Callable[[str], Sandbox], *, code: bytes, entrypoint: str, argv: tuple[str, ...],
                   inputs: dict[str, tuple[str, bytes]], seed: int, timeout_s: float,
                   pin: EntropyPin | None) -> Runner:
    """A Runner over the program's input files, keyed by input role: `files[role]` replaces that input's bytes.
    Every call runs in a FRESH sandbox with the production run's entropy pin (the replay must reproduce production,
    so production must have run under the same pin: see EntropyPin.for_run)."""
    calls = [0]

    def run(files: dict[str, bytes]) -> list[str] | None:
        calls[0] += 1
        box = sandbox_factory(f"ni-{calls[0]}")
        box.pin_entropy(pin)
        ins = {role: (name, files.get(role, data)) for role, (name, data) in inputs.items()}
        preds, _ = run_program(box, code=code, entrypoint=entrypoint, argv=argv, inputs=ins, seed=seed,
                               timeout_s=timeout_s)
        return preds
    return run
