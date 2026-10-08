"""Per-completion evaluation allocator for racing (GOAL §5 L1, OR).

The batch race (evolve/selection.race) evaluates every (candidate, unit) of a round in a loop, then decides. This
allocator runs the same race on a pool of `slots` concurrent evaluations, driven by completions:

- it WAKES on the first finished unit (a completion signal), never on a timer or a batch;
- it RE-READS THE STORE as the source of truth on every wake (results arriving while it was analysing, results left
  by an earlier, interrupted allocator, and coalesced wake-ups are all picked up there; the signal carries no data);
- it ACTS ON EVERY NEWLY FINISHED UNIT: when the last unit a round's decision needs is in, it applies the round's
  elimination rule and queues the next round's units;
- it REFILLS every free slot at once with any unit already known to be needed (no batch barrier: a slot never waits
  for other slots). The only waits are for units the unchanged decision rule itself needs before it can say which
  candidates continue;
- it TERMINATES when drained: the last round is decided and nothing is in flight.

Statistics are unchanged BY CONSTRUCTION: the result returned is `race()` itself, re-run over the store's recorded
scores (each looked up, none evaluated), so every accept/reject statistic and decision is the batch race's. The
scheduler's own round logic (which only chooses what to evaluate next) is cross-checked against it: a unit the race
needs that was never scheduled, or a scheduled unit the race never needed, raises AllocatorError (fail closed).
"""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from ..evolve.selection import RaceResult, Score, Unit, paired_ci, race

Key = tuple[Hashable, Unit]  # (candidate, unit)
Report = Callable[[Hashable, Unit, Score], None]
Fail = Callable[[Hashable, Unit, BaseException], None]


class AllocatorError(RuntimeError):
    """The allocator's schedule disagrees with the batch race, a worker failed, or it would wait forever."""


class UnitStore(Protocol):
    """Where finished evaluations live (the source of truth). Write-once per key."""

    def put(self, candidate: Hashable, unit: Unit, score: Score) -> None: ...

    def finished(self) -> dict[Key, Score]: ...


class MemoryUnitStore:
    """Thread-safe, write-once in-memory UnitStore."""

    def __init__(self, initial: dict[Key, Score] | None = None):
        self._lock = threading.Lock()
        self._data: dict[Key, Score] = dict(initial or {})

    def put(self, candidate: Hashable, unit: Unit, score: Score) -> None:
        key = (candidate, unit)
        with self._lock:
            if key in self._data and self._data[key] != score:
                raise AllocatorError(f"conflicting results for {key}: {self._data[key]} vs {score}")
            self._data[key] = score

    def finished(self) -> dict[Key, Score]:
        with self._lock:
            return dict(self._data)


class Executor(Protocol):
    def submit(self, candidate: Hashable, unit: Unit, report: Report, fail: Fail) -> None:
        """Start evaluating; on completion call report(...) (which writes the store, then signals), or fail(...)."""


class ThreadExecutor:
    """Runs `evaluate(candidate, unit)` on a thread pool of `slots` workers."""

    def __init__(self, evaluate: Callable[[Hashable, Unit], Score], slots: int):
        self.evaluate = evaluate
        self.pool = ThreadPoolExecutor(max_workers=max(1, slots), thread_name_prefix="chh-alloc")

    def submit(self, candidate: Hashable, unit: Unit, report: Report, fail: Fail) -> None:
        def work() -> None:
            try:
                score = self.evaluate(candidate, unit)
            except BaseException as e:  # noqa: BLE001 - handed to the allocator, which re-raises it
                fail(candidate, unit, e)
                return
            report(candidate, unit, score)
        self.pool.submit(work)

    def close(self) -> None:
        self.pool.shutdown(wait=True)


class RaceAllocator:
    def __init__(self, candidates: list[Hashable], units: list[Unit], parent_scores: dict[Unit, Score], *,
                 store: UnitStore, executor: Executor, slots: int, rounds: int = 3, keep: int = 1,
                 n_boot: int = 4000, on_analysis: Callable[[RaceAllocator], None] | None = None):
        if slots < 1:
            raise ValueError("slots must be >= 1")
        self.candidates, self.units, self.parent_scores = list(candidates), list(units), parent_scores
        self.store, self.executor, self.slots = store, executor, slots
        self.rounds, self.keep, self.n_boot = rounds, keep, n_boot
        self.on_analysis = on_analysis  # test hook: runs after each store read, before acting on it
        self._wake = threading.Event()
        self._errors: list[tuple[Key, BaseException]] = []
        self._err_lock = threading.Lock()
        # observable schedule (tests, reports)
        self.submitted: list[Key] = []
        self.wakes = 0
        self.idle_log: list[tuple[int, int]] = []  # at each wait: (in flight, needed units not yet submitted)
        self.in_flight: set[Key] = set()

    # ---------------------------------------------------------------- completion side (any thread)
    def _report(self, candidate: Hashable, unit: Unit, score: Score) -> None:
        try:
            self.store.put(candidate, unit, score)  # the store first: the signal carries no data
        except BaseException as e:  # noqa: BLE001 - a failed write must not leave the allocator waiting
            self._fail(candidate, unit, e)
            return
        self._wake.set()

    def _fail(self, candidate: Hashable, unit: Unit, err: BaseException) -> None:
        with self._err_lock:
            self._errors.append(((candidate, unit), err))
        self._wake.set()

    # ---------------------------------------------------------------- the race's round rule (scheduling only)
    def _decide(self, r: int, is_last: bool, alive: list[Hashable], fin: dict[Key, Score]) -> list[Hashable]:
        """Survivors of round r, by the same rule as evolve/selection.race, from the store's scores in the race's
        canonical order. Only chooses what to evaluate next; the returned decision is race() itself."""
        stats: dict[Hashable, dict[str, Any]] = {}
        ranked = []
        for c in alive:
            diffs = []
            for chunk in self._chunks[:r + 1]:
                for u in chunk:
                    if (c, u) not in fin:
                        continue
                    score, base = fin[(c, u)], self.parent_scores[u]
                    if score is not None and base is not None:
                        diffs.append(score - base)
            mean, _lo, hi = paired_ci(diffs, seed=r, n_boot=self.n_boot)
            stats[c] = {"mean": mean, "hi": hi}
            ranked.append((mean, c))
        ranked.sort(key=lambda t: -t[0])
        survivors = [c for _, c in ranked if stats[c]["hi"] >= 0 or is_last]
        if not is_last:
            survivors = survivors[:max(self.keep, math.ceil(len(survivors) / 2))]
        return survivors

    def _next_round(self, r: int) -> int | None:
        for i in range(r, len(self._chunks)):
            if self._chunks[i]:
                return i
        return None

    # ---------------------------------------------------------------- the loop
    def run(self, timeout_s: float = 3600.0) -> RaceResult:
        if set(self.units) - set(self.parent_scores):
            raise ValueError("parent was not evaluated on every race unit")
        self._chunks = [self.units[i::self.rounds] for i in range(self.rounds)]
        alive = list(self.candidates)
        r = self._next_round(0)
        needed: set[Key] = set()
        queue: deque[Key] = deque()

        def open_round(rr: int) -> None:
            for c in alive:
                for u in self._chunks[rr]:
                    needed.add((c, u))
                    queue.append((c, u))

        if r is not None and alive:
            open_round(r)
        done = r is None or not alive
        while True:
            self._wake.clear()  # BEFORE reading the store: a completion after the read sets it again
            fin = self.store.finished()
            self.wakes += 1
            self.in_flight -= fin.keys()
            if self.on_analysis is not None:
                self.on_analysis(self)
            with self._err_lock:
                if self._errors:
                    key, err = self._errors[0]
                    raise AllocatorError(f"evaluation of {key} failed: {type(err).__name__}: {err}") from err
            # act on every newly finished unit: decide every round whose units are all in
            while not done and r is not None and all((c, u) in fin for c in alive for u in self._chunks[r]):
                nr = self._next_round(r + 1)
                alive = self._decide(r, r == len(self._chunks) - 1, alive, fin)  # race's is_last, exactly
                if nr is None or not alive:
                    done = True
                else:
                    r = nr
                    open_round(r)
            # refill every free slot (no batch barrier)
            while queue and len(self.in_flight) < self.slots:
                key = queue.popleft()
                if key in fin or key in self.in_flight:
                    continue
                self.in_flight.add(key)
                self.submitted.append(key)
                self.executor.submit(key[0], key[1], self._report, self._fail)
            if done and not self.in_flight:
                break
            if not self.in_flight:
                raise AllocatorError("nothing in flight and the race is not decided (would wait forever)")
            self.idle_log.append((len(self.in_flight), sum(1 for k in queue if k not in fin)))
            pump = getattr(self.executor, "pump", None)
            if pump is not None:  # deterministic simulators complete work only when pumped
                pump()
            if not self._wake.wait(timeout_s):
                raise AllocatorError(f"no unit finished within {timeout_s} s ({len(self.in_flight)} in flight)")
        return self._result(needed)

    def _result(self, needed: set[Key]) -> RaceResult:
        """race() over the recorded scores: the batch decision, by construction. Cross-checks the schedule."""
        fin = self.store.finished()
        asked: set[Key] = set()

        def lookup(c: Hashable, u: Unit) -> Score:
            asked.add((c, u))
            if (c, u) not in fin:
                raise AllocatorError(f"the race needs {(c, u)}, which the allocator never evaluated")
            return fin[(c, u)]

        res = race(self.candidates, self.units, lookup, self.parent_scores, rounds=self.rounds, keep=self.keep,
                   n_boot=self.n_boot)
        if asked != needed:
            raise AllocatorError(f"schedule differs from the batch race: extra {sorted(map(str, needed - asked))[:5]}, "
                                 f"missing {sorted(map(str, asked - needed))[:5]}")
        return res


def race_per_completion(candidates: list[Hashable], units: list[Unit], evaluate: Callable[[Hashable, Unit], Score],
                        parent_scores: dict[Unit, Score], *, slots: int, rounds: int = 3, keep: int = 1,
                        n_boot: int = 4000, store: UnitStore | None = None,
                        timeout_s: float = 3600.0) -> RaceResult:
    """Drop-in for evolve/selection.race with `slots` concurrent evaluations. `evaluate` must be thread-safe."""
    ex = ThreadExecutor(evaluate, slots)
    try:
        return RaceAllocator(candidates, units, parent_scores, store=store or MemoryUnitStore(), executor=ex,
                             slots=slots, rounds=rounds, keep=keep, n_boot=n_boot).run(timeout_s)
    finally:
        ex.close()
