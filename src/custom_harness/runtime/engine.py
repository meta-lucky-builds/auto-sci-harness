"""Harness interpreter (PLAN L0 + L1 hooks).

Executes a HarnessSpec as a graph. Guarantees:
- every step is written to the Evidence store as a TraceStep (full traces, PLAN P1);
- every step is checkpointed (state + next node + workspace snapshot) so any run can be
  forked from any step (exact backtracking, ScienceFlow);
- budgets are enforced by deterministic code before and after every step (PLAN P7);
- LLM contracts (output JSON schemas) are validated in code, with bounded retries.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from ..budget import Budget, BudgetExceeded
from ..gateway import Backend, BackendError, ChatRequest, Message
from ..records import Cost, Run, RunStatus, StepKind, TraceStep
from ..sandbox import Sandbox, restore_snapshot, take_snapshot
from ..store import EvidenceStore
from .modules import ModuleContext, get_module
from .spec import END, Condition, HarnessSpec, LLMNode, ModuleNode, ToolNode
from .template import lookup, render


class ContractError(ValueError):
    def __init__(self, msg: str, cost: Cost, attempts: list[str]):
        super().__init__(msg)
        self.cost = cost
        self.attempts = attempts


class ModuleError(RuntimeError):
    def __init__(self, msg: str, cost: Cost, llm_calls: list):
        super().__init__(msg)
        self.cost = cost
        self.llm_calls = llm_calls


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    state: dict[str, Any]
    detail: str = ""
    steps: int = 0
    cost: Cost = field(default_factory=Cost)


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_json_output(text: str) -> Any:
    text = text.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    return json.loads(text)


def _holds(cond: Condition, state: dict[str, Any]) -> bool:
    value = lookup(state, cond.key, default=None)
    match cond.op:
        case "eq": return value == cond.value
        case "ne": return value != cond.value
        case "truthy": return bool(value)
        case "falsy": return not value
        case "contains": return value is not None and cond.value in value
        case "lt": return value is not None and value < cond.value
        case "gt": return value is not None and value > cond.value
    raise ValueError(cond.op)


class Engine:
    def __init__(self, spec: HarnessSpec, *, backends: dict[str, Backend], store: EvidenceStore,
                 budget: Budget | None = None, sandbox: Sandbox | None = None):
        self.spec = spec
        self.backends = backends
        self.store = store
        self.sandbox = sandbox
        if budget is None:
            limits = {k: v for k, v in spec.budget.model_dump().items() if v is not None}
            budget = Budget(limits=limits)
        self.budget = budget
        self.spec_hash = store.add_harness_version(spec.id, spec.model_dump(mode="json"), parent_hash=None)

    # ------------------------------------------------------------------ public
    def run(self, task_id: str, inputs: dict[str, Any], seed: int = 0) -> RunResult:
        run = Run(harness_id=self.spec.id, harness_hash=self.spec_hash, task_id=task_id, seed=seed)
        self.store.add_run(run)
        state = {"input": inputs, "_seed": seed}
        return self._loop(run.id, state, self.spec.entry, start_index=0)

    def fork(self, run_id: str, step_index: int, task_id: str | None = None) -> RunResult:
        """Start a new run from the checkpoint written after `step_index` of `run_id`."""
        steps = self.store.trace(run_id)
        step = next((s for s in steps if s.index == step_index), None)
        if step is None or step.checkpoint_ref is None:
            raise KeyError(f"no checkpoint at step {step_index} of {run_id}")
        cp = self.store.blobs.get_json(step.checkpoint_ref)
        if cp.get("workspace") and self.sandbox is not None:
            restore_snapshot(cp["workspace"], self.sandbox.workspace, self.store.blobs)
        parent = self.store.get_run(run_id)
        run = Run(harness_id=self.spec.id, harness_hash=self.spec_hash,
                  task_id=task_id or parent.task_id, seed=parent.seed)
        self.store.add_run(run)
        state = dict(cp["state"])
        state["_forked_from"] = {"run_id": run_id, "step": step_index}
        if cp["next"] == END:
            self.store.set_run_status(run.id, RunStatus.COMPLETED, "forked at terminal checkpoint")
            return RunResult(run.id, RunStatus.COMPLETED, state)
        return self._loop(run.id, state, cp["next"], start_index=0)

    # ----------------------------------------------------------------- internals
    def _loop(self, run_id: str, state: dict[str, Any], node_id: str, start_index: int) -> RunResult:
        """Never leaves a run in RUNNING: any escaping exception marks it FAILED, then re-raises."""
        try:
            return self._loop_inner(run_id, state, node_id, start_index)
        except BaseException as e:
            self.store.set_run_status(run_id, RunStatus.FAILED, f"engine crash: {type(e).__name__}: {e}")
            raise

    def _loop_inner(self, run_id: str, state: dict[str, Any], node_id: str, start_index: int) -> RunResult:
        index = start_index
        total = Cost()
        status, detail = RunStatus.COMPLETED, ""
        while True:
            if index >= self.spec.max_steps:
                status, detail = RunStatus.STEP_LIMIT, f"max_steps={self.spec.max_steps}"
                break
            try:
                self.budget.check()
            except BudgetExceeded as e:
                status, detail = RunStatus.BUDGET_EXCEEDED, str(e)
                break

            node = self.spec.nodes[node_id]
            t0 = time.time()
            error = None
            out: dict[str, Any] = {}
            cost = Cost()
            prov = None
            try:
                out, cost, prov = self._execute(node, state, run_id)
            except ContractError as e:
                error = f"ContractError: {e}"
                cost = e.cost  # failed attempts still cost money and must appear in the trace
                out = {"attempts": e.attempts}
            except ModuleError as e:
                error = f"ModuleError: {e}"
                cost = e.cost
                out = {"llm_calls": e.llm_calls}
            except (BackendError, KeyError, ValueError, OSError) as e:
                error = f"{type(e).__name__}: {e}"
            state["_last_error"] = error
            if error is None:
                key = getattr(node, "output_key", None)
                if key is not None:
                    state[key] = out.get("value")
                else:
                    state.update(out)  # module nodes return a dict of state updates

            next_node = self._next(node_id, state)
            workspace_ref = None
            if self.sandbox is not None and isinstance(node, ToolNode) and node.snapshot_after:
                workspace_ref = take_snapshot(self.sandbox.workspace, self.store.blobs)
            cp_ref = self.store.blobs.put_json({"state": state, "next": next_node or END,
                                                "workspace": workspace_ref})
            self.store.add_step(TraceStep(
                run_id=run_id, index=index, node_id=node_id, kind=_kind(node),
                input={"node": node.model_dump(mode="json")}, output=out, error=error,
                provenance=prov, cost=cost, started_at=t0, ended_at=time.time(), checkpoint_ref=cp_ref))
            total = total + cost
            index += 1
            try:
                self.budget.charge(cost)
            except BudgetExceeded as e:
                status, detail = RunStatus.BUDGET_EXCEEDED, str(e)
                break

            if next_node is None:
                if error is not None:
                    status, detail = RunStatus.FAILED, f"unhandled error at '{node_id}': {error}"
                else:  # outgoing edges exist but no condition matched: a harness bug, never success
                    status, detail = RunStatus.FAILED, f"no outgoing edge matched at '{node_id}'"
                break
            if next_node == END:
                break
            node_id = next_node

        self.store.set_run_status(run_id, status, detail)
        return RunResult(run_id, status, state, detail, steps=index - start_index, cost=total)

    def _next(self, node_id: str, state: dict[str, Any]) -> str | None:
        """First matching outgoing edge. Errors only follow edges that explicitly test _last_error."""
        errored = state.get("_last_error") is not None
        for edge in self.spec.outgoing(node_id):
            if errored and (edge.when is None or "_last_error" not in edge.when.key):
                continue
            if edge.when is None or _holds(edge.when, state):
                return edge.target
        if errored:
            return None
        return END if not self.spec.outgoing(node_id) else None

    def _execute(self, node, state: dict[str, Any], run_id: str):
        ctx = self.spec.context
        if isinstance(node, LLMNode):
            return self._execute_llm(node, state, ctx.max_value_chars, ctx.head_fraction)
        if isinstance(node, ToolNode):
            if self.sandbox is None:
                raise ValueError("tool node requires a sandbox")
            cmd = [render(c, state, ctx.max_value_chars, ctx.head_fraction) for c in node.command]
            res = self.sandbox.exec(cmd, timeout_s=node.timeout_s)
            return {"value": res.model_dump()}, Cost(wall_s=res.wall_s), None
        if isinstance(node, ModuleNode):
            fn = get_module(node.module)
            mctx = ModuleContext(run_id=run_id, store=self.store, backends=self.backends,
                                 budget=self.budget, sandbox=self.sandbox)
            try:
                updates = fn(state, node.params, mctx)
            except Exception as e:
                raise ModuleError(f"{type(e).__name__}: {e}", mctx.spent, mctx.llm_calls) from e
            if mctx.llm_calls:
                updates = {**updates, "_module_llm_calls": mctx.llm_calls}
            return updates, mctx.spent, None
        raise ValueError(f"unknown node type {type(node)}")

    def _execute_llm(self, node: LLMNode, state, max_chars, head_fraction):
        backend = self.backends.get(node.role)
        if backend is None:
            raise ValueError(f"no backend bound to role '{node.role}'")
        messages = []
        if node.system:
            messages.append(Message(role="system", content=render(node.system, state, max_chars, head_fraction)))
        messages.append(Message(role="user", content=render(node.prompt, state, max_chars, head_fraction)))
        total = Cost()
        attempts = []
        for attempt in range(node.retries + 1):
            resp = backend.complete(ChatRequest(messages=messages, json_schema=node.output_schema,
                                                temperature=node.temperature, max_tokens=node.max_tokens))
            total = total + resp.cost
            if node.output_schema is None:
                return {"value": resp.text, "attempts": attempts + [resp.text]}, total, resp.provenance
            try:
                value = parse_json_output(resp.text)
                jsonschema.validate(value, node.output_schema)
                return {"value": value, "attempts": attempts + [resp.text]}, total, resp.provenance
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                msg = e.msg if isinstance(e, json.JSONDecodeError) else e.message
                attempts.append(resp.text)
                messages = messages + [
                    Message(role="assistant", content=resp.text),
                    Message(role="user", content=f"Your output violated the required JSON contract: {msg}. "
                                                 f"Reply with only valid JSON matching the schema."),
                ]
        raise ContractError(f"output contract failed after {node.retries + 1} attempts", total, attempts)


def _kind(node) -> StepKind:
    return {"llm": StepKind.LLM, "tool": StepKind.TOOL, "module": StepKind.MODULE}[node.kind]
