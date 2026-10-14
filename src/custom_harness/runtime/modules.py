"""Registry of code modules referenced by ModuleNode.module (PLAN B2)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..budget import Budget
    from ..gateway import Backend, ChatRequest, ChatResponse
    from ..kernel import Kernel
    from ..kernel.checks import Replayer as _Replayer
    from ..records import Cost
    from ..sandbox import Sandbox
    from ..store import EvidenceStore


# The context surfaces touched by the module step executing in this context (engine-owned, runtime/taint.py): set by
# the engine around a module call. A store facade or a ModuleContext a module CACHED from an earlier step still
# records into the step that uses it.
STEP_TOUCHED: ContextVar[set[str] | None] = ContextVar("chh_step_touched", default=None)


def _note_touch(name: str) -> None:
    cur = STEP_TOUCHED.get()
    if cur is not None:
        cur.add(name)


class ModuleStore:
    """What a module may do with the evidence store directly (STATUS §D "trusted modules have store access"): read
    runs, traces and evidence, and use the content-addressed blob store. It has NO method that writes ledger entries,
    evidence, claims, records, runs or trace steps: those go through the Kernel API (ModuleContext.kernel(), which
    runs the K-checks) or the engine. `Ledger(ctx.store)`, `Kernel(ctx.store)` and `ctx.store.ledger_append(...)`
    therefore fail.

    Not a security boundary (stated): modules are in-process Python, and the Kernel object returned by
    ctx.kernel() holds the real store (kernel.store / kernel.ledger). It stops accidental or casual bypasses of the
    Kernel and makes every write path greppable; a real boundary needs the Kernel in a separate process (or a signed
    ledger) with modules holding only an RPC handle."""

    _READS = ("get_run", "run_status", "list_runs", "trace", "run_cost", "evidence_for_run", "get_evidence",
              "get_claim", "claim_tier", "claim_status", "harness_spec", "records_where", "records", "ledger_rows")

    def __init__(self, store: EvidenceStore):
        object.__setattr__(self, "_ModuleStore__store", store)
        object.__setattr__(self, "blobs", store.blobs)

    def __getattribute__(self, name: str) -> Any:
        if not name.startswith("__"):
            _note_touch("store")
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        if name in ModuleStore._READS:
            return getattr(self.__store, name)
        raise AttributeError(f"modules have no direct '{name}' on the evidence store: write through the Kernel "
                             f"(ctx.kernel()) or return state updates")

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("the module store facade is read-only")


@dataclass
class ModuleContext:
    run_id: str
    store: EvidenceStore | ModuleStore
    backends: dict[str, Backend]
    budget: Budget
    sandbox: Sandbox | None
    spent: Cost = field(default_factory=lambda: _cost())
    # the run's `_private*` values and their encodings (set by the engine): for content screens only (memory)
    secrets: tuple[str, ...] = ()
    # provenance of every handle the engine archived in a prompt of this run (set by the engine; read-only view):
    # handle digest -> held-out targets of that prompt (empty = not target-derived). A missing handle has UNKNOWN
    # provenance (research/evidence_refs.py refuses it). In-memory per engine: empty after a resume.
    archive_taint: Mapping[str, frozenset[str]] = field(default_factory=dict)
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    _store: Any = field(init=False, repr=False, default=None)
    # engine-owned access record (runtime/taint.py): which context surfaces the module touched. The engine sets it to
    # a fresh set after construction; a module never reports its own reads.
    _touched: set[str] | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        if not isinstance(self.store, ModuleStore):
            self._store = self.store
            self.store = ModuleStore(self.store)
        else:
            self._store = self.store._ModuleStore__store

    def __getattribute__(self, name: str) -> Any:
        if name in _RECORDED:
            touched = object.__getattribute__(self, "_touched")
            if touched is not None:
                touched.add(name)
            _note_touch(name)
        return object.__getattribute__(self, name)

    def kernel(self, replayer: _Replayer | None = None) -> Kernel:
        """The Evidence Kernel on the run's store: the only module path to ledger, vault and evidence writes. It is
        bound to the run: once a human kills the run (`chh kill`), it pre-registers, records and certifies nothing
        (studio/kill.py)."""
        from ..studio.kill import RunGuardedKernel
        return RunGuardedKernel(self._store, run_id=self.run_id, replayer=replayer)

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


# ModuleContext surfaces whose use the engine records for the step's taint label (runtime/taint.py). `_store` and
# `kernel` both reach the whole store; `sandbox` the workspace; `secrets` the run's private values; `archive_taint`
# the digests of values archived from (possibly target-derived) prompts.
_RECORDED = frozenset({"store", "_store", "kernel", "sandbox", "secrets", "archive_taint"})


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
