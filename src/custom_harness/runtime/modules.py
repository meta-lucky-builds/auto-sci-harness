"""Registry of code modules referenced by ModuleNode.module (PLAN B2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..budget import Budget
    from ..gateway import Backend
    from ..sandbox import Sandbox
    from ..store import EvidenceStore


@dataclass
class ModuleContext:
    run_id: str
    store: "EvidenceStore"
    backends: dict[str, "Backend"]
    budget: "Budget"
    sandbox: "Sandbox | None"


ModuleFn = Callable[[dict[str, Any], dict[str, Any], ModuleContext], dict[str, Any]]

_REGISTRY: dict[str, ModuleFn] = {}


def register(module_id: str) -> Callable[[ModuleFn], ModuleFn]:
    def deco(fn: ModuleFn) -> ModuleFn:
        if module_id in _REGISTRY and _REGISTRY[module_id] is not fn:
            raise ValueError(f"module '{module_id}' already registered")
        _REGISTRY[module_id] = fn
        return fn
    return deco


def get_module(module_id: str) -> ModuleFn:
    try:
        return _REGISTRY[module_id]
    except KeyError:
        raise KeyError(f"unknown module '{module_id}'") from None


def registered() -> list[str]:
    return sorted(_REGISTRY)
