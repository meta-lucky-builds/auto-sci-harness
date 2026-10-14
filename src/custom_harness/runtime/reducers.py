"""Declared reducers for MapNode fan-out (GOAL §5 L0 Runtime, LangGraph-style "declared reducers").

A MapNode names the reducer that combines its branch outputs (in item order) into one value. Only reducers declared
here (or with `declare_reducer` by trusted code) may be named: a spec naming anything else is refused when the spec is
validated and again when an Engine is built, never resolved at run time to some default. Reducers are pure and fail
loudly (TypeError / ValueError) on outputs they cannot combine; the engine turns that into a policy failure of the
map step.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Reducer = Callable[[list[Any]], Any]

_REDUCERS: dict[str, Reducer] = {}


class UndeclaredReducer(KeyError):
    pass


def declare_reducer(name: str) -> Callable[[Reducer], Reducer]:
    def deco(fn: Reducer) -> Reducer:
        if name in _REDUCERS and _REDUCERS[name] is not fn:
            raise ValueError(f"reducer '{name}' already declared")
        _REDUCERS[name] = fn
        return fn
    return deco


def declared() -> frozenset[str]:
    return frozenset(_REDUCERS)


def get_reducer(name: str) -> Reducer:
    try:
        return _REDUCERS[name]
    except KeyError:
        raise UndeclaredReducer(f"reducer '{name}' is not declared (declared: {sorted(_REDUCERS)})") from None


@declare_reducer("collect")
def _collect(values: list[Any]) -> list[Any]:
    """Branch outputs as a list, in item order."""
    return list(values)


@declare_reducer("concat")
def _concat(values: list[Any]) -> list[Any]:
    """Concatenate list outputs, in item order."""
    out: list[Any] = []
    for i, v in enumerate(values):
        if not isinstance(v, list):
            raise TypeError(f"concat: branch {i} output is {type(v).__name__}, not a list")
        out.extend(v)
    return out


@declare_reducer("sum")
def _sum(values: list[Any]) -> float | int:
    """Numeric sum (booleans are refused: True + True is not a count anyone declared)."""
    total: float | int = 0
    for i, v in enumerate(values):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TypeError(f"sum: branch {i} output is {type(v).__name__}, not a number")
        total += v
    return total


@declare_reducer("merge")
def _merge(values: list[Any]) -> dict[str, Any]:
    """Union of dict outputs; two branches giving one key different values is refused (no silent last-wins)."""
    out: dict[str, Any] = {}
    for i, v in enumerate(values):
        if not isinstance(v, dict):
            raise TypeError(f"merge: branch {i} output is {type(v).__name__}, not an object")
        for k, x in v.items():
            if k in out and out[k] != x:
                raise ValueError(f"merge: key '{k}' conflicts between branches")
            out[k] = x
    return out
