"""Harness interpreter (PLAN L0 + L1 hooks).

Executes a HarnessSpec as a graph. Guarantees:
- every step is written to the Evidence store as a TraceStep (full traces, PLAN P1);
- every step is checkpointed (state + next node + workspace snapshot) so any run can be
  forked from any step (exact backtracking, ScienceFlow). The workspace is snapshotted after every
  module/tool step (unchanged files are reused by fingerprint); LLM steps cannot touch it and reuse
  the previous snapshot;
- budgets are enforced by deterministic code before and after every step (PLAN P7);
- LLM contracts (output JSON schemas) are validated in code, with bounded retries.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from ..budget import Budget, BudgetExceeded
from ..errors import InfraError, infra_cause, is_transient
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


@dataclass(frozen=True)
class RetryPolicy:
    """Retries TRANSIENT infrastructure failures only (timeouts, 429/5xx, sandbox daemon hiccups).
    Policy failures (contract violations, crashing agent code) are never retried here: that is the
    harness's own business (its fix loops and error edges). Every attempt's cost is charged."""

    max_attempts: int = 3
    backoff_s: float = 2.0
    backoff_factor: float = 2.0

    def delay(self, attempt: int) -> float:
        return self.backoff_s * self.backoff_factor ** (attempt - 1)


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
                 budget: Budget | None = None, sandbox: Sandbox | None = None,
                 retry: RetryPolicy = RetryPolicy(), sleep=time.sleep):
        self.spec = spec
        self.retry = retry
        self._sleep = sleep
        self.backends = backends
        self.store = store
        self.sandbox = sandbox
        if budget is None:
            limits = {k: v for k, v in spec.budget.model_dump().items() if v is not None}
            budget = Budget(limits=limits)
        self.budget = budget
        self.spec_hash = store.add_harness_version(spec.id, spec.model_dump(mode="json"), parent_hash=None)
        self._ws_ref: str | None = None  # latest workspace snapshot of the current run

    # ------------------------------------------------------------------ public
    def run(self, task_id: str, inputs: dict[str, Any], seed: int = 0) -> RunResult:
        state = {"input": inputs, "_seed": seed}
        run = Run(harness_id=self.spec.id, harness_hash=self.spec_hash, task_id=task_id, seed=seed,
                  inputs_ref=self.store.blobs.put_json(state),
                  workspace=str(self.sandbox.workspace) if self.sandbox is not None else None)
        self.store.add_run(run)
        self._ws_ref = None
        self._started(run.id)
        return self._loop(run.id, state, self.spec.entry, start_index=0)

    def fork(self, run_id: str, step_index: int, task_id: str | None = None) -> RunResult:
        """Start a new run from the checkpoint written after `step_index` of `run_id`.

        The new run's budget starts at the parent's cumulative spend up to that step: forking can
        never be used to reset a budget (GOAL L1)."""
        steps = self.store.trace(run_id)
        step = next((s for s in steps if s.index == step_index), None)
        if step is None or step.checkpoint_ref is None:
            raise KeyError(f"no checkpoint at step {step_index} of {run_id}")
        cp = self.store.blobs.get_json(step.checkpoint_ref)
        if cp.get("workspace") and self.sandbox is not None:
            restore_snapshot(cp["workspace"], self.sandbox.workspace, self.store.blobs)
        self._ws_ref = cp.get("workspace")
        self.budget.spent = _checkpoint_spent(cp, self.store, run_id, step_index)
        parent = self.store.get_run(run_id)
        state = dict(cp["state"])
        state["_forked_from"] = {"run_id": run_id, "step": step_index}
        run = Run(harness_id=self.spec.id, harness_hash=self.spec_hash,
                  task_id=task_id or parent.task_id, seed=parent.seed, inputs_ref=parent.inputs_ref,
                  workspace=str(self.sandbox.workspace) if self.sandbox is not None else None,
                  forked_from=(run_id, step_index))
        self.store.add_run(run)
        if cp["next"] == END:
            self.store.set_run_status(run.id, RunStatus.COMPLETED, "forked at terminal checkpoint")
            return RunResult(run.id, RunStatus.COMPLETED, state)
        if cp["next"] is None:
            self.store.set_run_status(run.id, RunStatus.FAILED, "forked at a failed terminal checkpoint")
            return RunResult(run.id, RunStatus.FAILED, state)
        self._started(run.id)
        return self._loop(run.id, state, cp["next"], start_index=0)

    def resume(self, run_id: str, *, force: bool = False) -> RunResult:
        """Continue a run that was interrupted, in this process (GOAL L0, CR P0-4).

        Resumable: RUNNING (the process died, e.g. kill -9), FAILED by an engine crash, INFRA_FAILED.
        Never resumable: COMPLETED, and genuine harness failures (FAILED by the harness, STEP_LIMIT,
        BUDGET_EXCEEDED) -- resuming those would let a harness retry its own failure.

        Exactness: execution continues from the last recorded checkpoint with its state, its workspace
        snapshot and its cumulative spend, so the trace prefix is untouched and no recorded cost is
        charged twice. Honest limits: a step killed mid-flight left no trace row, so its partial spend
        (e.g. an LLM call that finished before the kill) is not recorded; and evidence rows it wrote
        stay in the store unreferenced."""
        status, detail = self.store.run_status(run_id)
        run = self.store.get_run(run_id)
        if run.harness_hash != self.spec_hash:
            raise ValueError("resume needs the exact harness spec the run started with")
        resumable = (status == RunStatus.RUNNING or status == RunStatus.INFRA_FAILED
                     or (status == RunStatus.FAILED and detail.startswith("engine crash")))
        if not resumable:
            raise ValueError(f"run {run_id} is {status.value} ({detail[:120]}); not resumable")
        if status == RunStatus.RUNNING and not force:
            owner = self._owner(run_id)
            if owner is not None and owner["host"] == socket.gethostname() and _pid_alive(owner["pid"]):
                raise RuntimeError(f"run {run_id} may still be running (pid {owner['pid']} is alive); "
                                   f"stop it first or pass force=True")
        steps = self.store.trace(run_id)
        if steps:
            last = steps[-1]
            cp = self.store.blobs.get_json(last.checkpoint_ref)
            state, next_node, start = dict(cp["state"]), cp["next"], last.index + 1
            spent = _checkpoint_spent(cp, self.store, run_id, last.index)
            ws_ref = cp.get("workspace")
        else:  # died before the first step finished
            if run.inputs_ref is None:
                raise ValueError(f"run {run_id} predates resumable runs (no stored inputs)")
            state, next_node, start, spent, ws_ref = (self.store.blobs.get_json(run.inputs_ref), self.spec.entry,
                                                      0, Cost(), None)
        if self.sandbox is not None and ws_ref:
            restore_snapshot(ws_ref, self.sandbox.workspace, self.store.blobs)
        self._ws_ref = ws_ref
        self.budget.spent = spent
        if next_node == END:
            self.store.set_run_status(run_id, RunStatus.COMPLETED, "resumed: terminal checkpoint was already reached")
            return RunResult(run_id, RunStatus.COMPLETED, state, cost=Cost())
        if next_node is None:
            self.store.set_run_status(run_id, RunStatus.FAILED, "resumed: last checkpoint was a failed terminal")
            return RunResult(run_id, RunStatus.FAILED, state, cost=Cost())
        self._started(run_id, note=f"resumed at step {start}")
        return self._loop(run_id, state, next_node, start_index=start)

    def _started(self, run_id: str, note: str = "") -> None:
        owner = {"pid": os.getpid(), "host": socket.gethostname()}
        self.store.append_record("run_owner", {"run_id": run_id, **owner})
        self.store.set_run_status(run_id, RunStatus.RUNNING, json.dumps({**owner, "note": note}))

    def _owner(self, run_id: str) -> dict | None:
        rows = [r for r in self.store.records("run_owner") if r.get("run_id") == run_id]
        return rows[-1] if rows else None

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
            out, cost, prov, error, infra = self._attempt(node, state, run_id)
            if infra is None:
                state["_last_error"] = error
                if error is None:
                    key = getattr(node, "output_key", None)
                    if key is not None:
                        state[key] = out.get("value")
                    else:
                        state.update(out)  # module nodes return a dict of state updates

            # An infra failure is not the harness's doing: never route it through the harness's error
            # edges. Stop, and checkpoint so that a resume retries this same node.
            next_node = node_id if infra is not None else self._next(node_id, state)
            workspace_ref = None
            if self.sandbox is not None:
                if isinstance(node, LLMNode):
                    workspace_ref = self._ws_ref  # LLM steps cannot touch the workspace
                elif not (isinstance(node, ToolNode) and not node.snapshot_after):
                    workspace_ref = take_snapshot(self.sandbox.workspace, self.store.blobs, previous=self._ws_ref)
                self._ws_ref = workspace_ref
            # next=None marks a failed terminal (distinct from END), so resume/fork never mistake a
            # failure for completion; spent = cumulative cost after this step (persisted budget)
            cp_ref = self.store.blobs.put_json({"state": state, "next": next_node, "workspace": workspace_ref,
                                                "spent": (self.budget.spent + cost).model_dump()})
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

            if infra is not None:
                status, detail = RunStatus.INFRA_FAILED, f"infrastructure failure at '{node_id}': {error}"
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

    def _attempt(self, node, state: dict[str, Any], run_id: str):
        """Execute a node with the transient-infra retry policy.
        Returns (output, cost incl. failed attempts, provenance, error string | None, infra cause | None)."""
        failed = Cost()  # tokens/$/quota of failed infra attempts are charged; their wall time is not
        notes: list[str] = []
        for attempt in range(1, self.retry.max_attempts + 1):
            t0 = time.monotonic()
            try:
                out, cost, prov = self._execute(node, state, run_id)
                if notes:
                    out = {**out, "_infra_retries": notes}
                return out, failed + _walled(cost, time.monotonic() - t0), prov, None, None
            except ContractError as e:
                # failed attempts still cost money and must appear in the trace
                return ({"attempts": e.attempts}, failed + _walled(e.cost, time.monotonic() - t0), None,
                        f"ContractError: {e}", None)
            except (ModuleError, BackendError, InfraError, KeyError, ValueError, OSError) as e:
                elapsed = time.monotonic() - t0
                partial = e.cost if isinstance(e, ModuleError) else Cost()
                detail: dict[str, Any] = {"llm_calls": e.llm_calls} if isinstance(e, ModuleError) else {}
                cause = infra_cause(e)
                if cause is not None and is_transient(e) and attempt < self.retry.max_attempts:
                    failed = failed + _walled(partial, 0.0)
                    notes.append(f"attempt {attempt}: {type(cause).__name__}: {cause}"[:500])
                    self._sleep(self.retry.delay(attempt))
                    continue
                if notes:
                    detail["_infra_retries"] = notes
                # an outage's wall time is not the harness's spend; a policy failure's is
                final = _walled(partial, 0.0 if cause is not None else elapsed)
                return detail, failed + final, None, f"{type(e).__name__}: {e}", cause
        raise AssertionError("unreachable")

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


def _walled(cost: Cost, wall_s: float) -> Cost:
    """Wall-clock budget unit: a step costs its measured elapsed time (replaces backend-reported wall)."""
    return cost.model_copy(update={"wall_s": wall_s})


def _checkpoint_spent(cp: dict, store: EvidenceStore, run_id: str, step_index: int) -> Cost:
    """Cumulative spend at a checkpoint. Checkpoints written before M0 lack it: rebuild from the trace."""
    if cp.get("spent") is not None:
        return Cost.model_validate(cp["spent"])
    total = Cost()
    for s in store.trace(run_id):
        if s.index <= step_index:
            total = total + s.cost
    return total


def _pid_alive(pid: int) -> bool:
    """Is a process with this pid alive on this host? Never signals or kills it (on Windows os.kill
    with any signal other than CTRL events TERMINATES the process, so it must not be used here).
    PID reuse can make a dead run look alive; that only ever makes resume refuse (safe direction)."""
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kind(node) -> StepKind:
    return {"llm": StepKind.LLM, "tool": StepKind.TOOL, "module": StepKind.MODULE}[node.kind]
