"""The per-completion race (control/allocator.py) over the evolution Evaluator (evolve/loop.py), single writer.

What made the Evaluator unsafe for concurrent use (found 2026-10-08, tests/test_evalrace.py):
- `Evaluator.outcome` checked its cache, ran the harness, then filled the cache: two threads asking for the same unit
  both missed and BOTH ran it (two harness runs, two campaign-budget charges, two sets of Kernel pre-registrations);
- its counters (`calls`, `infra_failures`) were unguarded read-modify-writes;
- the verdict-bit release of an evolve run is a write to the shared `Releases` object (counters, replay pins, the
  record log) and to the ledger, made from inside the run;
- the campaign budget was checked on the running thread, so `slots` runs could all pass the check together.

Design (ScienceFlow single-writer state, not locks): the Evaluator has ONE writer thread (`Evaluator.begin/finish`
refuse a second one). Worker threads only run `Evaluator.attempt` (the harness run; the evidence store is
internally locked). Every completion is committed by `EvaluatorExecutor.pump`, which the allocator calls on ITS
thread: score + release emission, the R2-2 multiplicity check, the budget charge, the infra-retry decision, the
durable unit record and the cache. The budget is checked before every attempt starts (writer thread), so a campaign
overshoots its cap by at most the runs already in flight (`slots`, vs 1 sequentially).

What this does NOT make safe (Kernel / engine side, not changed here; see docs/control_allocation.md):
- unit scores are not a pure function of (spec, unit): K4's transcript bound counts the verdict-bit releases logged
  before a claim's registration, so the ORDER in which runs happen can change later certifications. With slots=1 the
  allocator evaluates in exactly the batch race's order (tests/test_evalrace.py checks the order); with slots > 1 the
  order differs and the decisions are equal to the batch race's only when scores are order-independent;
- concurrent runs over-taint each other's releases (seq windows: conservative, but timing-dependent);
- `Vault.register_dataset` is check-then-append (two first registrations of one dataset at once both append).
Hence `EvolveConfig.race_slots > 1` is refused unless `race_concurrency_ack` is set (an owner decision).
"""

from __future__ import annotations

import queue
from collections.abc import Hashable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ..evolve.selection import RaceResult, Score, Unit
from .allocator import AllocatorError, Fail, Key, RaceAllocator, Report

if TYPE_CHECKING:
    from ..evolve.evaluate import Attempt
    from ..runtime.spec import HarnessSpec


class EvaluatorUnitStore:
    """The allocator's source of truth = the Evaluator's own outcomes (its cache, warm-started from durable unit
    records after a crash). Read and written on the writer thread only."""

    def __init__(self, evaluator: Any, specs: Mapping[Hashable, HarnessSpec], units: list[Unit]):
        self.evaluator, self.units = evaluator, list(units)
        self.hashes = {c: s.spec_hash() for c, s in specs.items()}

    def put(self, candidate: Hashable, unit: Unit, score: Score) -> None:
        got = self.evaluator.cache.get((self.hashes[candidate], unit[0], unit[1]))
        if got is None or got.score != score:
            raise AllocatorError(f"reported {(candidate, unit)} disagrees with the Evaluator's record")

    def finished(self) -> dict[Key, Score]:
        out: dict[Key, Score] = {}
        for c, h in self.hashes.items():
            for u in self.units:
                o = self.evaluator.cache.get((h, u[0], u[1]))
                if o is not None:
                    out[(c, u)] = o.score
        return out


class EvaluatorExecutor:
    """Allocator executor over an Evaluator. submit() and pump() run on the allocator's (= the writer) thread."""

    def __init__(self, evaluator: Any, specs: Mapping[Hashable, HarnessSpec], slots: int, timeout_s: float = 3600.0):
        self.evaluator, self.specs, self.timeout_s = evaluator, specs, timeout_s
        self.pool = ThreadPoolExecutor(max_workers=max(1, slots), thread_name_prefix="chh-race")
        self.done: queue.Queue[tuple[Attempt, Hashable, Report, Future[Any]]] = queue.Queue()
        self.in_flight = 0
        self.started: list[tuple[str, str, int, int]] = []  # (spec hash, task, seed, attempt): observable order

    def _start(self, att: Attempt, candidate: Hashable, report: Report) -> None:
        self.started.append((*att.key, att.n))
        fut = self.pool.submit(self.evaluator.attempt, att)
        self.in_flight += 1
        fut.add_done_callback(lambda f: self.done.put((att, candidate, report, f)))

    def submit(self, candidate: Hashable, unit: Unit, report: Report, fail: Fail) -> None:
        # budget check + bookkeeping on the writer thread; BudgetExceeded propagates out of RaceAllocator.run
        self._start(self.evaluator.begin(self.specs[candidate], unit, 0), candidate, report)

    def pump(self) -> None:
        """Commit completions until one unit is FINAL (reported to the allocator). An infra failure with a retry
        left starts the next attempt instead. Any error propagates unchanged (as in the sequential Evaluator)."""
        while True:
            try:
                att, candidate, report, fut = self.done.get(timeout=self.timeout_s)
            except queue.Empty:
                raise AllocatorError(f"no evaluation finished within {self.timeout_s} s") from None
            self.in_flight -= 1
            err = fut.exception()
            if err is not None:
                self.evaluator.abandon(att)
                raise err
            out = self.evaluator.finish(att, fut.result())
            if out is None:  # infrastructure failure, retry left: a fresh attempt (budget checked again)
                self._start(self.evaluator.begin(att.spec, att.unit, att.n + 1), candidate, report)
                continue
            report(candidate, att.unit, out.score)
            return

    def close(self) -> None:
        """Wait for every run still in flight and COMMIT it (its spend is charged, its outcome kept), starting
        nothing new; errors at this point are secondary (the first error already propagates)."""
        while self.in_flight:
            att, _c, _r, fut = self.done.get()
            self.in_flight -= 1
            try:
                if fut.exception() is not None or self.evaluator.finish(att, fut.result()) is None:
                    self.evaluator.abandon(att)
            except Exception:  # noqa: BLE001 - secondary to the error being propagated
                self.evaluator.abandon(att)
        self.pool.shutdown(wait=True)


def race_evaluator(candidates: Mapping[Hashable, HarnessSpec], units: list[Unit], evaluator: Any,
                   parent_scores: dict[Unit, Score], *, slots: int, rounds: int, keep: int = 1,
                   timeout_s: float = 3600.0) -> tuple[RaceResult, RaceAllocator, EvaluatorExecutor]:
    """evolve/selection.race over the Evaluator, per completion with `slots` concurrent harness runs. The decision is
    race() over the recorded scores (allocator.py); the allocator and executor are returned for inspection."""
    ex = EvaluatorExecutor(evaluator, candidates, slots, timeout_s)
    alloc = RaceAllocator(list(candidates), units, parent_scores,
                          store=EvaluatorUnitStore(evaluator, candidates, units), executor=ex, slots=slots,
                          rounds=rounds, keep=keep)
    try:
        return alloc.run(timeout_s), alloc, ex
    finally:
        ex.close()
