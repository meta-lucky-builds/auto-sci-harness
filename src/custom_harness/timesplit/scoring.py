"""Per-window scoring and the §6.4 seed-vs-evolved comparison.

Scoring is the Kernel's e-process (kernel.predictions): p0 fixed at registration, q >= p0, LR = q/p0 on a true
outcome else (1-q)/(1-p0), gain = log2 LR. A prediction whose task is unresolved after its window end is an EXPIRED
MISS (outcome False), so not resolving a task never helps. Skill is omitted: there is no pinned reference forecaster
for time-split tasks yet. Contaminated tasks (any probe flag) are excluded from every headline number and listed.

The low-prior-art subset and `weighted_vds` = sum_i w_i * gain_i are GOAL §6.4's "novelty-weighted subset" and
novelty-weighted VDS (w from probes.audit).

compare(): paired per-task differences (evolved minus seed in gain; seed minus evolved in Brier, so positive = evolved
better) with a seeded paired bootstrap CI. The verdict on the LAST window is "evolved better" only if both CI lower
bounds are > 0 and every precondition holds; otherwise "not shown", with the reasons.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Iterable
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..kernel.predictions import PredictionScore, calibration, vds
from . import config
from .benchmark import (
    Benchmark,
    Registration,
    Resolution,
    TimesplitError,
    check_observation_date,
    check_registration,
)
from .probes import Audit
from .rules import rule_hash

EVOLVED_BETTER = "evolved better"
NOT_SHOWN = "not shown"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TaskScore(_Frozen):
    task_id: str
    window: int
    registration: str
    q: float
    p0: float
    outcome: bool | None  # None = pending (unresolved, window not over)
    expired: bool
    gain_bits: float | None
    brier: float | None
    prior_art_weight: float
    low_prior_art: bool
    headline: bool
    excluded_reason: str | None


class SubsetScore(_Frozen):
    n: int
    resolved: int
    expired: int
    pending: int
    vds: float
    calibration: dict[str, Any]


class WindowScore(_Frozen):
    index: int
    start: date
    end: date
    headline: SubsetScore
    low_prior_art: SubsetScore
    weighted_vds: float
    excluded: tuple[str, ...]


class Scorecard(_Frozen):
    benchmark_id: str
    harness_id: str
    as_of_now: date
    resolutions_hash: str
    audit_hash: str
    memorisation_probe: str | None
    low_prior_art_threshold: float
    windows: tuple[WindowScore, ...]
    tasks: tuple[TaskScore, ...]


def _accept_resolutions(benchmark: Benchmark, resolutions: Iterable[Resolution], as_of_now: date
                        ) -> dict[str, Resolution]:
    by_task: dict[str, list[Resolution]] = {}
    for r in resolutions:
        if r.benchmark_id != benchmark.id:
            raise TimesplitError(f"resolution for task {r.task_id!r} is for another benchmark")
        task = benchmark.task(r.task_id)
        if r.rule_hash != rule_hash(task.rule):
            raise TimesplitError(f"resolution for task {r.task_id!r} was made under a different rule")
        check_observation_date(benchmark, task, r.dated)
        if r.dated > as_of_now:
            raise TimesplitError(f"resolution for task {r.task_id!r} is dated {r.dated}, after as_of_now={as_of_now}")
        by_task.setdefault(r.task_id, []).append(r)
    chosen: dict[str, Resolution] = {}
    for tid, rs in by_task.items():
        if len({r.outcome for r in rs}) > 1:
            raise TimesplitError(f"conflicting resolutions for task {tid!r}")
        chosen[tid] = min(rs, key=lambda r: (r.dated, r.record_hash))
    return chosen


def _subset(rows: list[TaskScore]) -> SubsetScore:
    ps = [PredictionScore(claim_hash=r.registration, q=r.q, p0=r.p0, outcome=r.outcome, expired=r.expired,
                          info_gain_bits=r.gain_bits) for r in rows]
    return SubsetScore(n=len(rows), resolved=sum(1 for r in rows if r.outcome is not None and not r.expired),
                       expired=sum(1 for r in rows if r.expired), pending=sum(1 for r in rows if r.outcome is None),
                       vds=vds(ps), calibration=calibration(ps, bins=config.CALIBRATION_BINS))


def score(benchmark: Benchmark, predictions: Iterable[Registration], resolutions: Iterable[Resolution],
          as_of_now: date, audit: Audit) -> Scorecard:
    """Score ONE harness's predictions per window. Pure: same inputs, same Scorecard."""
    if audit.benchmark_id != benchmark.id:
        raise TimesplitError("the audit is for another benchmark")
    preds = list(predictions)
    harnesses = {p.harness_id for p in preds}
    if len(harnesses) > 1:
        raise TimesplitError(f"score() takes one harness's predictions, got {sorted(harnesses)}")
    seen: set[str] = set()
    for p in preds:
        check_registration(benchmark, p)
        if p.task_id in seen:
            raise TimesplitError(f"duplicate registration for task {p.task_id!r}")
        seen.add(p.task_id)
    chosen = _accept_resolutions(benchmark, resolutions, as_of_now)
    resolutions_hash = hashlib.sha256(json.dumps(sorted(r.record_hash for r in chosen.values())).encode()).hexdigest()
    contaminated = audit.contaminated()
    rows: list[TaskScore] = []
    for p in sorted(preds, key=lambda p: p.task_id):
        task = benchmark.task(p.task_id)
        res = chosen.get(task.id)
        expired = res is None and as_of_now > benchmark.window_of(task).end
        outcome: bool | None = res.outcome if res is not None else (False if expired else None)
        gain = brier = None
        if outcome is not None:
            gain = math.log2(p.q / p.p0) if outcome else math.log2((1 - p.q) / (1 - p.p0))
            brier = (p.q - float(outcome)) ** 2
        w = audit.prior_art_weight[task.id]
        rows.append(TaskScore(task_id=task.id, window=task.window, registration=p.hash, q=p.q, p0=p.p0,
                              outcome=outcome, expired=expired, gain_bits=gain, brier=brier, prior_art_weight=w,
                              low_prior_art=w >= config.LOW_PRIOR_ART_THRESHOLD, headline=task.id not in contaminated,
                              excluded_reason=contaminated.get(task.id)))
    windows = []
    for i, win in enumerate(benchmark.windows):
        mine = [r for r in rows if r.window == i]
        head = [r for r in mine if r.headline]
        windows.append(WindowScore(
            index=i, start=win.start, end=win.end, headline=_subset(head),
            low_prior_art=_subset([r for r in head if r.low_prior_art]),
            weighted_vds=sum(r.prior_art_weight * r.gain_bits for r in head if r.gain_bits is not None),
            excluded=tuple(r.task_id for r in mine if not r.headline)))
    return Scorecard(benchmark_id=benchmark.id, harness_id=next(iter(harnesses), ""), as_of_now=as_of_now,
                     resolutions_hash=resolutions_hash, audit_hash=audit.hash,
                     memorisation_probe=audit.memorisation_probe,
                     low_prior_art_threshold=config.LOW_PRIOR_ART_THRESHOLD,
                     windows=tuple(windows), tasks=tuple(rows))


# -- comparison

class Interval(_Frozen):
    mean: float
    lo: float
    hi: float


class WindowComparison(_Frozen):
    index: int
    n_pairs: int
    unpaired: tuple[str, ...]
    pending: int
    gain_diff: Interval | None  # evolved - seed, bits per task
    brier_diff: Interval | None  # seed - evolved (positive = evolved better)


class Comparison(_Frozen):
    seed_harness: str
    evolved_harness: str
    bootstrap_seed: int
    n_boot: int
    ci_level: float
    windows: tuple[WindowComparison, ...]
    verdict: str
    reasons: tuple[str, ...]


def _bootstrap(d_gain: list[float], d_brier: list[float], seed: int) -> tuple[Interval, Interval]:
    n = len(d_gain)
    rng = random.Random(seed)
    mg: list[float] = []
    mb: list[float] = []
    for _ in range(config.BOOTSTRAP_N):
        idx = [rng.randrange(n) for _ in range(n)]
        mg.append(sum(d_gain[i] for i in idx) / n)
        mb.append(sum(d_brier[i] for i in idx) / n)
    a = (1 - config.CI_LEVEL) / 2
    lo_i = math.floor(a * config.BOOTSTRAP_N)
    hi_i = min(config.BOOTSTRAP_N - 1, math.ceil((1 - a) * config.BOOTSTRAP_N) - 1)

    def iv(xs: list[float], ms: list[float]) -> Interval:
        s = sorted(ms)
        return Interval(mean=sum(xs) / n, lo=s[lo_i], hi=s[hi_i])

    return iv(d_gain, mg), iv(d_brier, mb)


def compare(seed_scores: Scorecard, evolved_scores: Scorecard) -> Comparison:
    s, e = seed_scores, evolved_scores
    for name in ("benchmark_id", "as_of_now", "resolutions_hash", "audit_hash"):
        if getattr(s, name) != getattr(e, name):
            raise TimesplitError(f"scorecards differ in {name}: they must share benchmark, resolutions, audit and date")
    out: list[WindowComparison] = []
    for w in s.windows:
        sh = {t.task_id: t for t in s.tasks if t.window == w.index and t.headline}
        eh = {t.task_id: t for t in e.tasks if t.window == w.index and t.headline}
        both = sorted(set(sh) & set(eh))
        unpaired = tuple(sorted(set(sh) ^ set(eh)))
        pairs = [(sh[t], eh[t]) for t in both if sh[t].gain_bits is not None and eh[t].gain_bits is not None]
        pending = len(both) - len(pairs)
        gi = bi = None
        if pairs:
            dg = [float(b.gain_bits) - float(a.gain_bits) for a, b in pairs]  # type: ignore[arg-type]
            db = [float(a.brier) - float(b.brier) for a, b in pairs]  # type: ignore[arg-type]
            gi, bi = _bootstrap(dg, db, config.BOOTSTRAP_SEED + w.index)
        out.append(WindowComparison(index=w.index, n_pairs=len(pairs), unpaired=unpaired, pending=pending,
                                    gain_diff=gi, brier_diff=bi))
    last = out[-1]
    reasons: list[str] = []
    if s.memorisation_probe is None:
        reasons.append("memorisation probe not run (a live probe needs the owner)")
    if s.as_of_now <= s.windows[-1].end:
        reasons.append("the last window has not closed")
    if last.unpaired:
        reasons.append(f"{len(last.unpaired)} headline task(s) predicted by only one harness")
    if last.pending:
        reasons.append(f"{last.pending} paired task(s) still pending")
    if last.n_pairs < config.MIN_PAIRS_FOR_VERDICT:
        reasons.append(f"{last.n_pairs} resolved pairs < {config.MIN_PAIRS_FOR_VERDICT}")
    if last.gain_diff is None or last.gain_diff.lo <= 0:
        reasons.append("gain-difference CI lower bound is not > 0")
    if last.brier_diff is None or last.brier_diff.lo <= 0:
        reasons.append("Brier-difference CI lower bound is not > 0")
    return Comparison(seed_harness=s.harness_id, evolved_harness=e.harness_id, bootstrap_seed=config.BOOTSTRAP_SEED,
                      n_boot=config.BOOTSTRAP_N, ci_level=config.CI_LEVEL, windows=tuple(out),
                      verdict=NOT_SHOWN if reasons else EVOLVED_BETTER, reasons=tuple(reasons))
