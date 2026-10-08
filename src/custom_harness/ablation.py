"""Ablation switches (GOAL §2.1 "appears in an ablation", invariant 12). One mechanism, one closed registry.

A capability claimed in the coverage contract (GOAL §9, `scorecard/capabilities.yaml`) is ablated by re-running the
system with that capability switched off (`scorecard.runner.run_ablation`). This module is that switch mechanism:

- `SWITCHES` is the CLOSED registry: one switch per capability row that has a real, tested switch. A name not in it
  is refused everywhere (`AblationSet.off` raises), so a typo can never silently ablate nothing.
- `AblationSet` is the typed set of switched-off capabilities that the code paths a scorecard `System.run` drives
  receive (`Engine(ablation=...)`, `ModuleContext.ablation`, `Evaluator(ablation=...)`,
  `evolve_generation(ablation=...)` ...). `NO_ABLATION` (empty) is the default everywhere.

Safety design (owner-delegated decision, 2026-10-08): an ablation is NEVER a production backdoor.
- A non-empty AblationSet can only be made by `scorecard.runner.ablation_set` (the scorecard runner's ablation entry
  point): the constructor refuses any other caller module. There is no environment variable, config default or CLI
  flag that makes one (tests/test_ablation_core.py checks the source tree for each).
- An ablated run is marked ABLATED: its run record (`Run.ablated`), every trace step (`TraceStep.ablated`, stamped by
  the store) and its run bundle manifest (`ABLATED`).
- Nothing an ablated run produces is trusted: the Kernel refuses to certify on evidence produced by an ablated run;
  the run-bound Kernel a module gets refuses pre-registration, evidence and certification; the memory store refuses
  every write (memory and skills) from an ablated run; evolution never accepts an edit while ablated or on an
  ablated run, and an ablated generation's records go to separate `<kind>:ABLATED` record kinds; the scorecard lists
  ablated runs apart and never counts them on the production axes. All of these fail closed.
  Not enforced (stated): L8 weights export takes trajectories as inputs and is not wired to runs yet, so it has no
  run marker to check.

Integrity mechanisms (Kernel checks, sandbox isolation, the leak/canary audit) have NO switch here: a production path
that skips a Kernel check is never added. Their contribution is measured inside the evaluation harness only (the
planted-exploit museum compares a check against a simulated check-off arm built in the test layer); their rows in
capabilities.yaml are `not_ablatable` with that reason, and the scorecard keeps them as invariant-12 gaps.

Honest limits: Python cannot make a constructor truly private. The caller-module check stops accidental or casual
construction and keeps every ablation entry point greppable (one function); trusted in-process code could forge it,
as it could forge any store row (runtime/modules.py says the same of modules).

This module is a leaf: it imports nothing from the rest of the package (the memory package and the Kernel may use
the run marker without loading runtime, research or evolve code).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# the ONLY module allowed to construct a non-empty AblationSet (the scorecard runner's ablation entry point)
ENTRY_POINT_MODULE = "custom_harness.scorecard.runner"
MARKER = "ABLATED"


class AblationError(ValueError):
    """An unknown switch, or an AblationSet made outside the scorecard runner's entry point."""


class AblationRefused(RuntimeError):
    """Something an ablated run produced was offered where only production output is accepted (fail closed)."""


@dataclass(frozen=True)
class Switch:
    name: str
    capability: str  # the capabilities.yaml row id it turns off
    turns_off: str  # exactly what is off when the switch is set (the code path)
    test: str  # the test that shows behaviour changes when off and is unchanged when on ("path::function")


def _registry(*switches: Switch) -> Mapping[str, Switch]:
    out: dict[str, Switch] = {}
    for s in switches:
        if s.name in out or any(x.capability == s.capability for x in out.values()):
            raise AblationError(f"duplicate switch or capability in the registry: {s.name} / {s.capability}")
        out[s.name] = s
    return MappingProxyType(out)


# The closed registry, by layer. Add a switch ONLY together with its code path and its test (docs/ablation.md).
_CORE: tuple[Switch, ...] = (
)

_EVOLVE: tuple[Switch, ...] = (  # L6 / L7 (evolve/, control/)
)

_RESEARCH: tuple[Switch, ...] = (  # L3 / L4 / L5 and the rest (research/, memory wiring, verify/, literature/ ...)
)

SWITCHES: Mapping[str, Switch] = _registry(*_CORE, *_EVOLVE, *_RESEARCH)


class AblationSet:
    """An immutable set of switched-off capabilities. Empty = production (`NO_ABLATION`)."""

    __slots__ = ("_off",)
    _off: frozenset[str]

    def __init__(self, off: Iterable[str] = ()) -> None:
        names = frozenset(str(x) for x in off)
        unknown = sorted(names - set(SWITCHES))
        if unknown:
            raise AblationError(f"unknown ablation switch(es) {unknown}: the registry is closed "
                                f"(custom_harness.ablation.SWITCHES)")
        if names:
            caller = sys._getframe(1).f_globals.get("__name__")
            if caller != ENTRY_POINT_MODULE:
                raise AblationError(f"an ablation can only be switched on through the scorecard runner's entry point "
                                    f"({ENTRY_POINT_MODULE}.ablation_set), not from {caller!r}")
        object.__setattr__(self, "_off", names)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("AblationSet is immutable")

    def off(self, switch: str) -> bool:
        """True when `switch` is switched off in this run. An unknown name raises (never a silent no-op)."""
        if switch not in SWITCHES:
            raise AblationError(f"unknown ablation switch {switch!r}")
        return switch in self._off

    @property
    def active(self) -> bool:
        return bool(self._off)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._off))

    def marker(self) -> dict[str, Any]:
        """What an ablated run's records carry (empty for production)."""
        return {MARKER: True, "disabled": list(self.names)} if self._off else {}

    def __iter__(self) -> Iterator[str]:
        return iter(self.names)

    def __len__(self) -> int:
        return len(self._off)

    def __bool__(self) -> bool:
        return bool(self._off)

    def __contains__(self, item: object) -> bool:
        return item in self._off

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AblationSet) and other._off == self._off

    def __hash__(self) -> int:
        return hash(self._off)

    def __repr__(self) -> str:
        return f"AblationSet({list(self.names)})"


NO_ABLATION = AblationSet()


def run_ablated(store: Any, run_id: str | None) -> tuple[str, ...]:
    """The switches an existing run was ablated with; () for a production run or a run id with no record (a run is
    only ever ablated through `Engine(ablation=...)`, which writes the marker into the run record itself)."""
    if not run_id:
        return ()
    try:
        run = store.get_run(run_id)
    except KeyError:
        return ()
    return tuple(getattr(run, "ablated", ()) or ())


def refuse_if_ablated(store: Any, run_id: str | None, what: str) -> None:
    """Raise AblationRefused when `run_id` is an ablated run (fail closed)."""
    off = run_ablated(store, run_id)
    if off:
        raise AblationRefused(f"{what} refused: run {run_id} is {MARKER} (switched off: {', '.join(off)}); nothing an "
                              f"ablated run produces is persisted, certified or accepted")


__all__ = ["ENTRY_POINT_MODULE", "MARKER", "NO_ABLATION", "SWITCHES", "AblationError", "AblationRefused",
           "AblationSet", "Switch", "refuse_if_ablated", "run_ablated"]
