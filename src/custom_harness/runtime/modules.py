"""Registry of code modules referenced by ModuleNode.module (PLAN B2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..budget import Budget
    from ..gateway import Backend, ChatRequest, ChatResponse
    from ..records import Cost
    from ..sandbox import Sandbox
    from ..store import EvidenceStore


@dataclass
class ModuleContext:
    run_id: str
    store: EvidenceStore
    backends: dict[str, Backend]
    budget: Budget
    sandbox: Sandbox | None
    spent: Cost = field(default_factory=lambda: _cost())
    llm_calls: list[dict[str, Any]] = field(default_factory=list)

    def llm(self, role: str, request: ChatRequest, fallback_role: str | None = "executor") -> ChatResponse:
        """LLM call from inside a module: budget-checked, cost-accounted, logged into the step trace."""
        backend = self.backends.get(role) or (self.backends.get(fallback_role) if fallback_role else None)
        if backend is None:
            raise ValueError(f"no backend bound to role '{role}'")
        self.budget.check()
        resp = backend.complete(request)
        self.spent = self.spent + resp.cost
        self.llm_calls.append({"role": role, "provenance": resp.provenance.model_dump(mode="json"),
                               "request": [m.model_dump() for m in request.messages], "response": resp.text})
        return resp


def _cost():
    from ..records import Cost
    return Cost()


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
