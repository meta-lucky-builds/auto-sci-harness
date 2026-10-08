"""Harness interpreter (PLAN L0 + L1 hooks).

Executes a HarnessSpec as a graph. Guarantees:
- every step is written to the Evidence store as a TraceStep (full traces, PLAN P1);
- every step is checkpointed (state + next node + workspace snapshot) so any run can be
  forked from any step (exact backtracking, ScienceFlow). The workspace is snapshotted after every
  module/tool step (unchanged files are reused by fingerprint); LLM steps cannot touch it and reuse
  the previous snapshot;
- budgets are enforced by deterministic code before and after every step (PLAN P7);
- LLM contracts (output JSON schemas) are validated in code, with bounded retries;
- approval nodes (GOAL §5 L0, invariant 15) pause the run (PAUSED) until a human decision for exactly
  (run, node, item hash) is in the ledger;
- a human kill (`chh kill`, studio/kill.py) stops the run before its next step and ends it FAILED; a killed run is
  never resumed;
- an optional campaign budget (budget.Campaign) is checked before every step, across all the campaign's runs.

Approval trust boundary (stated honestly). The engine reads decisions ONLY from ledger entries of kind "approval",
which only `chh approve` writes; values in run state are discarded and overwritten. To stop code that runs before
the pause from pre-planting a decision, a decision counts only if its ledger seq is later than an
"approval_request" entry the engine appended when it paused at that node for that exact item (and recorded in the
pause's trace step). Module nodes, however, are TRUSTED Python with full store access: a malicious module could
forge trace rows and ledger entries alike. That is not prevented in code; it is prevented by modules being
human-written, registry-only code that no evolved edit can add (evolve/edits.py). LLM output and evolved spec edits
cannot approve.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import socket
import sys
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import jsonschema

from ..budget import Budget, BudgetExceeded, Campaign
from ..errors import InfraError, infra_cause, is_transient
from ..gateway import Backend, BackendError, ChatRequest, ChatResponse, Message
from ..kernel.ledger import Ledger
from ..policy import model_policy
from ..records import Cost, Run, RunStatus, StepKind, TraceStep
from ..sandbox import Sandbox, restore_snapshot, take_snapshot
from ..store import EvidenceStore
from .modules import ModuleContext, get_module
from .spec import (
    END,
    ApprovalNode,
    Condition,
    HarnessSpec,
    LLMNode,
    MapNode,
    ModuleNode,
    ToolNode,
    approval_state_keys,
    engine_reserved,
)
from .template import lookup, references, render

APPROVAL_DECISIONS = ("approved", "rejected")
# (module id, output key) -> the reduced release alphabet that module may DECLARE for that key (`_release_alphabets`,
# 2f1d54a). Owner decision 2026-10-08: kept, and pinned here (tests/test_release_embargo.py); a declaration from any
# other module or for any other key is ignored (the key stays `continuous`, fail closed). Every module listed here
# writes reserved state, so no evolve edit can add, rebind or change the node that runs it.
RELEASE_ALPHABET_ALLOWLIST: Mapping[tuple[str, str], str] = MappingProxyType({
    ("research.certify", "replication_brief"): "verdict_bit"})


def approval_item_hash(item: Any) -> str:
    """sha256 of the canonical JSON of the item an approval binds to (raises TypeError if not JSON)."""
    return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def approval_decision(store: EvidenceStore, run_id: str, node_id: str, item_hash: str) -> dict[str, Any] | None:
    """Latest intact human decision for exactly (run_id, node_id, item_hash), appended to the ledger after the
    engine paused this run at this node for this item (so nothing that ran before the pause can pre-plant it).
    Module-level so trusted host commands (e.g. `chh return-data`) apply exactly the engine's rule."""
    ledger = Ledger(store)
    want = {"run_id": run_id, "node_id": node_id, "item_hash": item_hash}
    requests = {e.seq for e in ledger.entries("approval_request") if e.intact() and e.body == want}
    paused_at = [s.output.get("request_seq") for s in store.trace(run_id)
                 if s.node_id == node_id and s.output.get("awaiting_approval") is True
                 and s.output.get("item_hash") == item_hash]
    valid = [q for q in paused_at if isinstance(q, int) and q in requests]
    if not valid:
        return None
    first = min(valid)
    for e in reversed(ledger.entries("approval")):
        b = e.body
        if (e.seq > first and e.intact() and all(b.get(k) == v for k, v in want.items())
                and b.get("decision") in APPROVAL_DECISIONS
                and isinstance(b.get("approver"), str) and b["approver"].strip()):
            note = b.get("note")
            return {"decision": b["decision"], "item_hash": item_hash, "approver": b["approver"],
                    "note": note if isinstance(note, str) else "", "ledger_seq": e.seq}
    return None


def paused_approval(store: EvidenceStore, run_id: str) -> dict[str, Any] | None:
    """The pause a PAUSED run is waiting on: its last trace step's output plus node id and checkpoint state
    (None if the last step is not an approval pause)."""
    steps = store.trace(run_id)
    if not steps:
        return None
    last = steps[-1]
    if last.checkpoint_ref is None or last.output.get("awaiting_approval") is not True:
        return None
    cp = store.blobs.get_json(last.checkpoint_ref)
    if cp.get("next") != last.node_id:
        return None
    return {**last.output, "node_id": last.node_id, "state": cp["state"]}


class ContractError(ValueError):
    def __init__(self, msg: str, cost: Cost, attempts: list[str], messages: list[dict] | None = None):
        super().__init__(msg)
        self.cost = cost
        self.messages = messages or []
        self.attempts = attempts


class ModuleError(RuntimeError):
    def __init__(self, msg: str, cost: Cost, llm_calls: list):
        super().__init__(msg)
        self.cost = cost
        self.llm_calls = llm_calls


class _JournalingBackend(Backend):
    """Transparent proxy: after every completed model call, append its cost to the run's cost journal
    (kind "cost_journal", keyed by run and step) BEFORE control returns to the step. A hard kill can then
    lose the step's outputs, but never the record of what it spent."""

    def __init__(self, inner: Backend, engine: Engine):
        self._inner, self._engine = inner, engine
        self.name, self.provider, self.mode = inner.name, inner.provider, inner.mode
        self.training_on_outputs = inner.training_on_outputs

    def complete(self, req: ChatRequest) -> ChatResponse:
        resp = self._inner.complete(req)
        cursor = self._engine._cursor
        if cursor is not None:
            entry: dict[str, Any] = {"run_id": cursor[0], "step": cursor[1], "cost": resp.cost.model_dump()}
            if len(cursor) == 4:  # inside a fan-out branch: (run, step, branch id, execution nonce)
                entry.update(branch=cursor[2], nonce=cursor[3])
            self._engine.store.append_record("cost_journal", entry)
        return resp

    def __getattr__(self, item: str) -> Any:  # e.g. MockBackend.requests in tests
        return getattr(self._inner, item)


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
    op = cond.op
    if op == "eq":
        return bool(value == cond.value)
    if op == "ne":
        return bool(value != cond.value)
    if op == "truthy":
        return bool(value)
    if op == "falsy":
        return not value
    if value is None:
        return False
    try:
        if op == "contains":
            return cond.value in value
        if op == "lt":
            return bool(value < cond.value)
        if op == "gt":
            return bool(value > cond.value)
    except TypeError:  # incomparable types: the condition does not hold (fail closed, never crash)
        return False
    raise ValueError(op)


class Engine:
    def __init__(self, spec: HarnessSpec, *, backends: dict[str, Backend], store: EvidenceStore,
                 budget: Budget | None = None, sandbox: Sandbox | None = None,
                 retry: RetryPolicy | None = None, sleep=time.sleep, allow_same_family: bool | None = None,
                 campaign: Campaign | None = None):
        # invariant 5, checked before any model call; an override (argument or CHH_ALLOW_SAME_FAMILY=1) is
        # recorded in every run's state as NOT MET and reported in every certificate checklist
        allowed = allow_same_family if allow_same_family is not None else (
            os.environ.get("CHH_ALLOW_SAME_FAMILY") == "1")
        self.model_policy = model_policy(backends, allow_same_family=allowed)
        # declared reducers only (also checked by spec validation; repeated here because a spec can be built with
        # model_construct, which skips validation)
        from .reducers import get_reducer
        for node in spec.nodes.values():
            if isinstance(node, MapNode):
                get_reducer(node.reducer)
        self.spec = spec
        self._budget_owned = budget is None
        # fan-out bookkeeping: a resume of an infra-failed map step hands it the instance and the nonces already
        # charged (from that step's checkpoint); `_map_pending` is what the current map step adds to its checkpoint
        self._map_resume: dict[str, dict[str, Any]] = {}
        self._map_pending: dict[str, Any] | None = None
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        # every model call is journalled the moment it returns (write-ahead), so the spend of a step that is
        # killed mid-flight can be recovered by resume instead of being lost (M0 review)
        self._cursor: tuple[Any, ...] | None = None
        self.backends: dict[str, Backend] = {role: _JournalingBackend(b, self) for role, b in backends.items()}
        self.store = store
        self.sandbox = sandbox
        if budget is None:
            limits = {k: v for k, v in spec.budget.model_dump().items() if v is not None}
            budget = Budget(limits=limits)
        self.budget = budget
        self.campaign = campaign  # GOAL L1: one budget across every run of a campaign (derived from the store)
        self.spec_hash = store.add_harness_version(spec.id, spec.model_dump(mode="json"), parent_hash=None)
        self._ws_ref: str | None = None  # latest workspace snapshot of the current run
        # release emission (GOAL §16.5.3): state keys written by a step during which private files were read are
        # TAINTED with those targets; rendering a tainted key into a prompt or tool command is a ledgered release.
        # In-memory per engine (a resumed run re-taints only from steps it runs itself: stated limit).
        self.taint: dict[str, set[str]] = {}
        # a TRUSTED module may declare that a tainted output key carries at most the verdict bit or a count (e.g. the
        # replication brief: built from non-target data, its existence is the verdict). Only module outputs can
        # declare (`_release_alphabets`, never stored in state); any other write of the key clears the declaration.
        self.release_alphabet: dict[str, str] = {}
        self.releases: Any = None  # None = live releases on the store's ledger
        self._prompt_taint: dict[int, set[str]] = {}
        # provenance of archived handles (paged recall / critic citations refuse unknown or target-derived ones): a
        # handle archived while rendering a prompt that rendered any tainted key carries all of that prompt's targets
        # (conservative). In-memory per engine, like `taint`.
        self.archive_taint: dict[str, frozenset[str]] = {}

    # ------------------------------------------------------------------ public
    def run(self, task_id: str, inputs: dict[str, Any], seed: int = 0) -> RunResult:
        state = {"input": inputs, "_seed": seed, "model_policy": self.model_policy}
        run = Run(harness_id=self.spec.id, harness_hash=self.spec_hash, task_id=task_id, seed=seed,
                  inputs_ref=self.store.blobs.put_json(state),
                  workspace=str(self.sandbox.workspace) if self.sandbox is not None else None)
        self.store.add_run(run)
        if self.campaign is not None:
            self.campaign.join(run.id)
        self._ws_ref = None
        self._map_resume = {}
        self._started(run.id)
        return self._loop(run.id, state, self.spec.entry, start_index=0)

    def fork(self, run_id: str, step_index: int, state_patch: dict[str, Any] | None = None,
             task_id: str | None = None) -> RunResult:
        """`fork(step, state_patch)` (GOAL §5 L0): start a new run from the checkpoint written after `step_index` of
        `run_id`, with `state_patch` (top-level key -> JSON value) applied to the checkpoint state.

        Refused (ForkRefused, before anything is written): a patch key that is private or engine state (`_*`),
        an approval outcome, run inputs, the model policy, or a reserved key (evolve/edits.RESERVED_OUTPUT_KEYS:
        claims, certificates, audit results, task setup ...); a dotted key; a non-JSON value; a value that carries a
        private value of the checkpoint state. Every fork is ledgered (kind `fork`; an append-only store record of
        that kind until the Kernel allows the ledger kind). A killed run (studio kill switch) is never forked
        (ValueError, also before anything is written): it would continue under a new id.

        Budgets carry over without double counting: an engine-owned (per-run) budget starts at the parent's
        cumulative spend up to that step, so forking can never reset it (GOAL L1); a caller-supplied (e.g. campaign)
        budget is raised per unit to at least that spend and never has it added a second time. A fork made under a
        campaign joins that campaign like any new run."""
        from ..studio.kill import kill_entry, killed_detail
        kill = kill_entry(self.store, run_id)
        if kill is not None:
            raise ValueError(f"run {run_id} was {killed_detail(kill)}; a killed run is not forked (it would continue "
                             f"under a new id)")
        steps = self.store.trace(run_id)
        step = next((s for s in steps if s.index == step_index), None)
        if step is None or step.checkpoint_ref is None:
            raise KeyError(f"no checkpoint at step {step_index} of {run_id}")
        cp = self.store.blobs.get_json(step.checkpoint_ref)
        patch = dict(state_patch or {})
        patch_sha = _check_fork_patch(patch, cp["state"])
        if cp.get("workspace") and self.sandbox is not None:
            restore_snapshot(cp["workspace"], self.sandbox.workspace, self.store.blobs)
        self._ws_ref = cp.get("workspace")
        self._map_resume = {}  # a fork re-runs a pending fan-out in full under its own run id
        carried = _checkpoint_spent(cp, self.store, run_id, step_index)
        self.budget.spent = carried if self._budget_owned else _unit_max(self.budget.spent, carried)
        parent = self.store.get_run(run_id)
        state = dict(cp["state"])
        state.update(copy.deepcopy(patch))
        run = Run(harness_id=self.spec.id, harness_hash=self.spec_hash,
                  task_id=task_id or parent.task_id, seed=parent.seed, inputs_ref=parent.inputs_ref,
                  workspace=str(self.sandbox.workspace) if self.sandbox is not None else None,
                  forked_from=(run_id, step_index))
        self.store.add_run(run)
        body = {"parent_run_id": run_id, "step": step_index, "child_run_id": run.id, "patch_keys": sorted(patch),
                "patch_sha256": patch_sha, "carried_spent": carried.model_dump()}
        from ..kernel.ledger import KINDS
        if "fork" in KINDS:
            seq: int | None = Ledger(self.store).append("fork", body).seq
        else:  # until the Kernel allows ledger kind `fork` (requested from kstats): append-only, not hash-chained
            self.store.append_record("fork", body)
            seq = None
        state["_forked_from"] = {"run_id": run_id, "step": step_index, "patch_keys": sorted(patch),
                                 "patch_sha256": patch_sha, "ledger_seq": seq}
        if self.campaign is not None:
            self.campaign.join(run.id)
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

        Resumable: RUNNING (the process died, e.g. kill -9), FAILED by an engine crash, INFRA_FAILED, PAUSED (at an
        approval node: it is re-entered and pauses again unless a human decision for its item is in the ledger).
        Never resumable: COMPLETED, and genuine harness failures (FAILED by the harness, STEP_LIMIT,
        BUDGET_EXCEEDED) -- resuming those would let a harness retry its own failure.

        Exactness: execution continues from the last recorded checkpoint with its state, its workspace
        snapshot and its cumulative spend, so the trace prefix is untouched and no recorded cost is
        charged twice. Honest limits: a step killed mid-flight left no trace row, so its partial spend
        (e.g. an LLM call that finished before the kill) is not recorded; and evidence rows it wrote
        stay in the store unreferenced."""
        status, detail = self.store.run_status(run_id)
        run = self.store.get_run(run_id)
        from ..studio.kill import kill_entry, killed_detail
        kill = kill_entry(self.store, run_id)
        if kill is not None:
            raise ValueError(f"run {run_id} was {killed_detail(kill)}; a killed run is not resumable")
        if run.harness_hash != self.spec_hash:
            raise ValueError("resume needs the exact harness spec the run started with")
        resumable = (status in (RunStatus.RUNNING, RunStatus.INFRA_FAILED, RunStatus.PAUSED)
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
            if last.checkpoint_ref is None:
                raise ValueError(f"run {run_id}: last step has no checkpoint; cannot resume")
            cp = self.store.blobs.get_json(last.checkpoint_ref)
            state, next_node, start = dict(cp["state"]), cp["next"], last.index + 1
            spent = _checkpoint_spent(cp, self.store, run_id, last.index)
            ws_ref = cp.get("workspace")
            # an infra-failed map step left its fan-out instance and the branch spend it already charged
            pending = cp.get("map_instance") and last.node_id == next_node
            self._map_resume = ({next_node: {"instance": cp["map_instance"],
                                             "charged": list(cp.get("map_charged", []))}} if pending else {})
        else:  # died before the first step finished
            if run.inputs_ref is None:
                raise ValueError(f"run {run_id} predates resumable runs (no stored inputs)")
            self._map_resume = {}
            state, next_node, start, spent, ws_ref = (self.store.blobs.get_json(run.inputs_ref), self.spec.entry,
                                                      0, Cost(), None)
        if self.sandbox is not None and ws_ref:
            restore_snapshot(ws_ref, self.sandbox.workspace, self.store.blobs)
        self._ws_ref = ws_ref
        self.budget.spent = spent
        if self.campaign is not None:
            self.campaign.join(run_id)
        if next_node == END:
            self.store.set_run_status(run_id, RunStatus.COMPLETED, "resumed: terminal checkpoint was already reached")
            return RunResult(run_id, RunStatus.COMPLETED, state, cost=Cost())
        if next_node is None:
            self.store.set_run_status(run_id, RunStatus.FAILED, "resumed: last checkpoint was a failed terminal")
            return RunResult(run_id, RunStatus.FAILED, state, cost=Cost())
        start = self._record_interrupted_step(run_id, start, next_node, state, ws_ref)
        self._started(run_id, note=f"resumed at step {start}")
        return self._loop(run_id, state, next_node, start_index=start)

    def _record_interrupted_step(self, run_id: str, index: int, node_id: str, state: dict[str, Any],
                                 ws_ref: str | None) -> int:
        """If the process died inside step `index`, its model calls are in the cost journal but the step
        has no trace row. Record that spend as an explicit 'interrupted' row (same checkpoint, so the node
        is retried next) and charge it: resume never double-charges and never loses recorded spend."""
        if isinstance(self.spec.nodes[node_id], MapNode):
            return index  # a fan-out step recovers its own journalled spend, per branch (see _map)
        entries = [e for e in self.store.records_where("cost_journal", "run_id", run_id) if e.get("step") == index]
        if not entries:
            return index
        spent = Cost()
        for e in entries:
            spent = spent + Cost.model_validate(e["cost"])
        cp_ref = self.store.blobs.put_json({"state": state, "next": node_id, "workspace": ws_ref,
                                            "spent": (self.budget.spent + spent).model_dump()})
        node = self.spec.nodes[node_id]
        now = time.time()
        self.store.add_step(TraceStep(
            run_id=run_id, index=index, node_id=node_id, kind=_kind(node), input={"node": node.model_dump(mode="json")},
            output={"interrupted": True, "journalled_model_calls": len(entries)},
            error="interrupted: the process died during this step; its model-call spend was recovered from the "
                  "cost journal and the step is re-executed",
            cost=spent, started_at=now, ended_at=now, checkpoint_ref=cp_ref))
        self.budget.spent = self.budget.spent + spent
        return index + 1

    def _started(self, run_id: str, note: str = "") -> None:
        owner = {"pid": os.getpid(), "host": socket.gethostname()}
        self.store.append_record("run_owner", {"run_id": run_id, **owner})
        self.store.set_run_status(run_id, RunStatus.RUNNING, json.dumps({**owner, "note": note}))

    def _owner(self, run_id: str) -> dict | None:
        rows = [r for r in self.store.records("run_owner") if r.get("run_id") == run_id]
        return rows[-1] if rows else None

    # ----------------------------------------------------------------- internals
    def _loop(self, run_id: str, state: dict[str, Any], node_id: str, start_index: int) -> RunResult:
        """Never leaves a run in RUNNING: any escaping exception marks it FAILED, then re-raises. Every run, whatever
        its outcome, ends with a Reproducibility Bundle (invariant 9)."""
        try:
            res = self._loop_inner(run_id, state, node_id, start_index)
        except BaseException as e:
            from ..studio.kill import kill_entry, killed_detail
            kill = kill_entry(self.store, run_id)
            self.store.set_run_status(run_id, RunStatus.FAILED, killed_detail(kill) if kill is not None
                                      else f"engine crash: {type(e).__name__}: {e}")
            self._write_run_bundle(run_id)
            raise
        self._write_run_bundle(run_id)
        return res

    def _write_run_bundle(self, run_id: str) -> None:
        from ..config import data_dir
        from ..store.run_bundle import build_run_bundle
        path = data_dir() / "bundles" / "runs" / f"{run_id}.zip"
        try:
            env = self.sandbox.environment() if self.sandbox is not None else None
        except Exception as e:  # noqa: BLE001  (e.g. Docker gone at the very end): recorded, bundle still written
            env = {"environment_unavailable": f"{type(e).__name__}: {e}"}
        try:
            digest = build_run_bundle(self.store, run_id, path, environment=env, workspace_ref=self._ws_ref)
            self.store.append_record("run_bundle", {"run_id": run_id, "path": str(path), "manifest_sha256": digest})
        except (OSError, ValueError, KeyError) as e:  # a missing bundle is recorded, never silent
            self.store.append_record("run_bundle", {"run_id": run_id, "error": f"{type(e).__name__}: {e}"})

    def _loop_inner(self, run_id: str, state: dict[str, Any], node_id: str, start_index: int) -> RunResult:
        from ..studio.kill import kill_entry, killed_detail
        index = start_index
        total = Cost()
        status, detail = RunStatus.COMPLETED, ""
        while True:
            kill = kill_entry(self.store, run_id)
            if kill is not None:  # a human stopped the run: no further step runs
                status, detail = RunStatus.FAILED, killed_detail(kill)
                break
            if index >= self.spec.max_steps:
                status, detail = RunStatus.STEP_LIMIT, f"max_steps={self.spec.max_steps}"
                break
            try:
                self.budget.check()
                if self.campaign is not None:
                    self.campaign.check()
            except BudgetExceeded as e:
                status, detail = RunStatus.BUDGET_EXCEEDED, str(e)
                break

            node = self.spec.nodes[node_id]
            t0 = time.time()
            paused = False
            if isinstance(node, ApprovalNode):
                out, error, paused = self._approval(node_id, node, state, run_id)
                cost, prov, infra = Cost(), None, None
                state["_last_error"] = error
            else:
                self._cursor = (run_id, index)
                rows = self.store.ledger_rows("data_access")
                seq_before = rows[-1][0] if rows else 0
                self._run_id = run_id
                self._map_pending = None
                if isinstance(node, MapNode):
                    out, cost, prov, error, infra = self._map(node_id, node, state, run_id, index)
                else:
                    out, cost, prov, error, infra = self._attempt(node, state, run_id)
                self._cursor = None
                if infra is None:
                    state["_last_error"] = error
                    if error is None:
                        from ..kerneld.releases import private_reads
                        read = set(private_reads(self.store, after_seq=seq_before))
                        key = getattr(node, "output_key", None)
                        if key is not None:
                            self.release_alphabet.pop(key, None)
                            state[key] = out.get("value")
                            carried = read | self._prompt_taint.get(id(node), set())
                            if carried:
                                self.taint[key] = carried
                        else:
                            declared = out.pop("_release_alphabets", None)
                            for k in out:
                                self.release_alphabet.pop(k, None)
                            if isinstance(declared, dict):
                                module = str(getattr(node, "module", ""))
                                self.release_alphabet |= {k: a for k, a in declared.items() if k in out and a in (
                                    "verdict_bit", "count") and RELEASE_ALPHABET_ALLOWLIST.get((module, k)) == a}
                            state.update(out)  # module nodes return a dict of state updates
                            if read:
                                for k in out:
                                    self.taint[k] = self.taint.get(k, set()) | read

            # An infra failure is not the harness's doing: never route it through the harness's error
            # edges. Stop, and checkpoint so that a resume retries this same node. A pause checkpoints the same
            # way (resume re-enters the approval node); an approval node that cannot read its item fails the run
            # (fail closed: error edges cannot route around a missing approval item).
            if infra is not None or paused:
                next_node: str | None = node_id
            elif isinstance(node, ApprovalNode) and error is not None:
                next_node = None
            else:
                next_node = self._next(node_id, state)
            workspace_ref = None
            if self.sandbox is not None:
                if isinstance(node, (LLMNode, ApprovalNode)):
                    workspace_ref = self._ws_ref  # LLM and approval steps cannot touch the workspace
                elif not (isinstance(node, ToolNode) and not node.snapshot_after):
                    workspace_ref = take_snapshot(self.sandbox.workspace, self.store.blobs, previous=self._ws_ref)
                self._ws_ref = workspace_ref
            # next=None marks a failed terminal (distinct from END), so resume/fork never mistake a
            # failure for completion; spent = cumulative cost after this step (persisted budget)
            cp = {"state": state, "next": next_node, "workspace": workspace_ref,
                  "spent": (self.budget.spent + cost).model_dump()}
            if infra is not None and self._map_pending is not None:  # a resume continues this fan-out instance
                cp.update(self._map_pending)
            cp_ref = self.store.blobs.put_json(cp)
            # the trace is agent-visible (the harness proposer reads it): private values never enter it
            trace_out, trace_err = _scrub_step(out, error, _private_values(state))
            self.store.add_step(TraceStep(
                run_id=run_id, index=index, node_id=node_id, kind=_kind(node),
                input={"node": node.model_dump(mode="json")}, output=trace_out, error=trace_err,
                provenance=prov, cost=cost, started_at=t0, ended_at=time.time(), checkpoint_ref=cp_ref))
            total = total + cost
            index += 1
            try:
                self.budget.charge(cost)
            except BudgetExceeded as e:
                status, detail = RunStatus.BUDGET_EXCEEDED, str(e)
                break

            if paused:
                status, detail = RunStatus.PAUSED, f"awaiting approval of {node_id} item {out['item_hash'][:12]}"
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

        kill = kill_entry(self.store, run_id)
        if kill is not None:  # killed during the last step: it ends FAILED, never COMPLETED (or PAUSED)
            status, detail = RunStatus.FAILED, killed_detail(kill)
        self.store.set_run_status(run_id, status, detail)
        kill = kill_entry(self.store, run_id)
        if kill is not None and status != RunStatus.FAILED:  # a kill that landed between the check and the write
            status, detail = RunStatus.FAILED, killed_detail(kill)
            self.store.set_run_status(run_id, status, detail)
        return RunResult(run_id, status, state, detail, steps=index - start_index, cost=total)

    def _approval(self, node_id: str, node: ApprovalNode, state: dict[str, Any],
                  run_id: str) -> tuple[dict[str, Any], str | None, bool]:
        """Apply the human decision for this exact item, or pause. Returns (trace output, error, paused).
        Whatever is in state under the node's outcome keys is discarded first: nothing in state can approve."""
        for k in approval_state_keys(node_id):
            state.pop(k, None)
        try:
            item_hash = approval_item_hash(lookup(state, node.item_key))
        except (KeyError, TypeError, ValueError) as e:
            return {}, f"approval item '{node.item_key}' unavailable: {type(e).__name__}: {e}", False
        decision = self._approval_decision(run_id, node_id, item_hash)
        if decision is None:
            req = Ledger(self.store).append("approval_request",
                                            {"run_id": run_id, "node_id": node_id, "item_hash": item_hash})
            return ({"awaiting_approval": True, "item_hash": item_hash, "request_seq": req.seq,
                     "item_key": node.item_key, "prompt": node.prompt}, None, True)
        outcome_key, decision_key = approval_state_keys(node_id)
        state[outcome_key] = decision
        state[decision_key] = decision["decision"]
        return {"approval": decision}, None, False

    def _approval_decision(self, run_id: str, node_id: str, item_hash: str) -> dict[str, Any] | None:
        return approval_decision(self.store, run_id, node_id, item_hash)

    def _map(self, node_id: str, node: MapNode, state: dict[str, Any], run_id: str, index: int):
        """Fan-out step (GOAL §5 L0). Returns what `_attempt` returns: (output, cost, provenance, error, infra).

        Branch k of a fan-out instance has the deterministic id sha256(instance, node, k, item hash); the instance is
        "<run_id>:<step index>" of the step that started it (a resume after a kill re-runs the same index; a resume
        after an infra failure takes the instance from that step's checkpoint). Each finished branch is persisted as
        a `map_branch` record (output blob, cost, nonce, workspace snapshot). A re-execution of the same instance
        reuses the contiguous prefix of finished branches and runs only the rest.

        Charging, exactly once: every model call inside a branch is journalled with the branch id and a nonce unique
        to that execution of the branch. The step's cost is the cost of every branch execution not yet charged
        (reused branches from a killed attempt included: a kill charged nothing) plus the journalled calls of
        branch executions that died unfinished (spent, so charged, once). The nonces charged so far go into the
        checkpoint of an infra-failed map step, so its resume never charges them again."""
        from .reducers import get_reducer
        body_id = node.body
        body = self.spec.nodes[body_id]
        res = self._map_resume.pop(node_id, None)
        instance = res["instance"] if res else f"{run_id}:{index}"
        charged: set[str] = set(res["charged"]) if res else set()
        self._map_pending = {"map_instance": instance, "map_charged": sorted(charged)}
        try:
            items = lookup(state, node.items_key)
        except KeyError:
            return {}, Cost(), None, f"MapError: items '{node.items_key}' not in state", None
        if not isinstance(items, list):
            return {}, Cost(), None, f"MapError: items '{node.items_key}' is {type(items).__name__}, not a list", None
        if len(items) > node.max_items:
            return ({}, Cost(), None, f"MapError: {len(items)} items exceed max_items={node.max_items} (never "
                    "truncated)", None)
        try:
            hashes = [approval_item_hash(it) for it in items]
        except (TypeError, ValueError) as e:
            return {}, Cost(), None, f"MapError: items are not JSON: {e}", None
        ids = [_branch_id(instance, node_id, k, h) for k, h in enumerate(hashes)]
        records = self.store.records_where("map_branch", "instance", instance)
        done = {r["branch_id"]: r for r in records if r.get("node_id") == node_id and r.get("error") is None}
        known_nonces = {r["nonce"] for r in records}

        items_root = node.items_key.split(".")[0]
        items_taint = set(self.taint.get(items_root, set()))
        prompt_taint = set(items_taint)
        acc = Cost()
        values: list[Any] = []
        branches: list[dict[str, Any]] = []
        replayed = False
        prov_out = None
        error: str | None = None
        infra = None
        reused = 0
        # 1. reuse the finished prefix (persisted by an earlier execution of this instance)
        for k, bid in enumerate(ids):
            r = done.get(bid)
            if r is None:
                break
            blob = self.store.blobs.get_json(r["output_ref"])
            values.append(blob["value"])
            c = Cost.model_validate(r["cost"])
            if r["nonce"] not in charged:
                acc = acc + c
                charged.add(r["nonce"])
            replayed = replayed or bool(r.get("replayed"))
            prompt_taint |= set(r.get("taint", []))
            branches.append({"index": k, "branch_id": bid, "item_hash": hashes[k], "reused": True,
                             "output_ref": r["output_ref"], "cost": c.model_dump(), "out": blob["trace"]})
            if r.get("workspace") and self.sandbox is not None:
                restore_snapshot(r["workspace"], self.sandbox.workspace, self.store.blobs)
                self._ws_ref = r["workspace"]
            reused += 1
        # 2. spend of earlier executions of this instance that is not reused and was never charged: persisted
        # branches past the reused prefix (e.g. a policy-failed branch whose step row the kill prevented), then the
        # journalled model calls of branch executions that died unfinished
        lost = Cost()
        lost_calls = 0
        for r in records:
            if r.get("node_id") == node_id and r["nonce"] not in charged:
                lost = lost + Cost.model_validate(r["cost"])
                charged.add(r["nonce"])
        id_set = set(ids)
        for j in self.store.records_where("cost_journal", "run_id", run_id):
            n = j.get("nonce")
            if j.get("branch") in id_set and n is not None and n not in known_nonces and n not in charged:
                lost = lost + Cost.model_validate(j["cost"])
                lost_calls += 1
                charged.add(n)
        acc = acc + lost
        # 3. run the remaining branches in item order
        saved_taint = self.taint
        for k in range(reused, len(items)):
            over = self._would_exceed(acc)
            if over is not None:
                error = f"BudgetExceeded: during fan-out before branch {k}: {over}"
                break
            bid, nonce = ids[k], uuid.uuid4().hex
            bstate = copy.deepcopy(state)
            bstate[node.item_key] = copy.deepcopy(items[k])
            bstate["_map"] = {"node": node_id, "index": k, "branch_id": bid, "n": len(items)}
            bstate["_last_error"] = None
            self.taint = {kk: set(v) for kk, v in saved_taint.items()}
            if items_taint:
                self.taint[node.item_key] = set(items_taint)
            else:
                self.taint.pop(node.item_key, None)
            self._prompt_taint.pop(id(body), None)
            self._cursor = (run_id, index, bid, nonce)
            rows = self.store.ledger_rows("data_access")
            seq_b = rows[-1][0] if rows else 0
            try:
                out, cost, prov, berr, binfra = self._attempt(body, bstate, run_id)
            finally:
                self._cursor = (run_id, index)
                self.taint = saved_taint
            from ..kerneld.releases import private_reads
            btaint = set(self._prompt_taint.pop(id(body), set())) | set(private_reads(self.store, after_seq=seq_b))
            prompt_taint |= btaint
            acc = acc + cost
            charged.add(nonce)
            value: Any = None
            if berr is None:
                if getattr(body, "output_key", None) is not None:
                    value = out.get("value")
                else:
                    value = {kk: v for kk, v in out.items() if kk not in ("_module_llm_calls", "_infra_retries")}
                    bad = sorted(kk for kk in value if engine_reserved(kk))
                    if bad:
                        berr = f"MapError: branch {k} wrote reserved state key(s) {bad}"
            b_replayed = bool(prov is not None and prov.replayed) or any(
                bool((c.get("provenance") or {}).get("replayed")) for c in out.get("_module_llm_calls", []))
            replayed = replayed or b_replayed
            if prov is not None and prov_out is None:
                prov_out = prov
            ws_ref = self._ws_ref
            if self.sandbox is not None and binfra is None and not isinstance(body, LLMNode) and not (
                    isinstance(body, ToolNode) and not body.snapshot_after):
                ws_ref = take_snapshot(self.sandbox.workspace, self.store.blobs, previous=self._ws_ref)
                self._ws_ref = ws_ref
            summary: dict[str, Any] = {"index": k, "branch_id": bid, "item_hash": hashes[k], "reused": False,
                                       "cost": cost.model_dump(), "out": out}
            if prov is not None:
                summary["provenance"] = prov.model_dump(mode="json")
            if binfra is None:
                ref = self.store.blobs.put_json({"value": value, "trace": out})
                # reduce what was persisted (canonical JSON: sorted keys), so a fresh branch and the same branch
                # reused after a crash give byte-identical reducer input
                value = self.store.blobs.get_json(ref)["value"]
                summary["output_ref"] = ref
                self.store.append_record("map_branch", {
                    "instance": instance, "run_id": run_id, "step": index, "node_id": node_id, "index": k,
                    "branch_id": bid, "item_hash": hashes[k], "nonce": nonce, "output_ref": ref,
                    "cost": cost.model_dump(), "error": berr, "workspace": ws_ref, "replayed": b_replayed,
                    "taint": sorted(btaint)})
            if berr is not None:
                summary["error"] = berr
            branches.append(summary)
            if berr is not None:
                error, infra = (f"branch {k} ({bid[:12]}): {berr}", binfra)
                break
            values.append(value)
        self._map_pending = {"map_instance": instance, "map_charged": sorted(charged)}
        self._prompt_taint[id(node)] = prompt_taint
        out_doc: dict[str, Any] = {"reducer": node.reducer, "n_items": len(items), "instance": instance,
                                   "branches": branches, "reused_branches": reused,
                                   "recovered_lost_calls": lost_calls}
        if replayed:
            out_doc["replayed"] = True  # K0 refuses evidence from replayed output (invariant 21)
            if prov_out is not None:
                prov_out = prov_out.model_copy(update={"replayed": True})
        if error is None:
            try:
                out_doc["value"] = get_reducer(node.reducer)(values)
            except (TypeError, ValueError) as e:
                error = f"ReducerError: {node.reducer}: {e}"
        return out_doc, acc, prov_out, error, infra

    def _would_exceed(self, extra: Cost) -> str | None:
        """Would spend `extra` on top of the budget's current spend already be at a cap? (reason, or None)"""
        try:
            Budget(limits=dict(self.budget.limits), spent=self.budget.spent + extra).check()
        except BudgetExceeded as e:
            return str(e)
        return None

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
                return ({"attempts": e.attempts, "messages": e.messages},
                        failed + _walled(e.cost, time.monotonic() - t0), None, f"ContractError: {e}", None)
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
            node_id = next((k for k, v in self.spec.nodes.items() if v is node), "tool")
            self._prompt_taint[id(node)] = self._release_rendered(node_id, list(node.command), state)
            cmd = [render(c, state, ctx.max_value_chars, ctx.head_fraction) for c in node.command]
            res = self.sandbox.exec(cmd, timeout_s=node.timeout_s)
            if res.limit_exceeded:  # the command's own failure (policy): route through the harness's error edges
                raise ValueError(f"tool command exceeded a sandbox limit: {res.limit_exceeded}")
            return {"value": res.model_dump()}, Cost(wall_s=res.wall_s), None
        if isinstance(node, ModuleNode):
            fn = get_module(node.module)
            # the run's private values (and their encodings) go to modules as `secrets`, so content screens that
            # modules apply (e.g. the L5 memory screen) refuse them; they are never module-visible state otherwise
            mctx = ModuleContext(run_id=run_id, store=self.store, backends=self.backends,
                                 budget=self.budget, sandbox=self.sandbox, secrets=tuple(_private_values(state)),
                                 archive_taint=MappingProxyType(self.archive_taint))
            try:
                updates = fn(state, node.params, mctx)
            except Exception as e:
                raise ModuleError(f"{type(e).__name__}: {e}", mctx.spent, mctx.llm_calls) from e
            if mctx.llm_calls:
                updates = {**updates, "_module_llm_calls": mctx.llm_calls}
            return updates, mctx.spent, None
        raise ValueError(f"unknown node type {type(node)}")

    def _release_rendered(self, node_id: str, templates: list[str], state: dict[str, Any]) -> set[str]:
        """Emit a `prompt` release for every tainted state key the templates render; returns the targets."""
        from ..kerneld.releases import Releases, alphabet_of, note_code
        targets: set[str] = set()
        for t in templates:
            for path in references(t):
                tk = self.taint.get(path.split(".")[0])
                if not tk:
                    continue
                targets |= tk
                rel = self.releases or Releases.live(self.store)
                alphabet = self.release_alphabet.get(path.split(".")[0]) or alphabet_of(lookup(state, path, None))
                rel.emit(lookup(state, path, None), targets=tk, channel="prompt",
                         alphabet=alphabet, note=note_code("prompt", node_id, path),
                         key=getattr(self, "_run_id", ""))
        return targets

    def _execute_llm(self, node: LLMNode, state, max_chars, head_fraction):
        backend = self.backends.get(node.role)
        if backend is None:
            raise ValueError(f"no backend bound to role '{node.role}'")
        node_id = next((k for k, v in self.spec.nodes.items() if v is node), "llm")
        targets = self._release_rendered(node_id, [node.system or "", node.prompt], state)
        self._prompt_taint[id(node)] = targets
        archived: list[str] = []  # handles of values truncated in the prompt (full text kept as blobs)

        def archive(text: str) -> str:
            digest = self.store.blobs.put(text.encode())
            archived.append(digest)
            self.archive_taint[digest] = self.archive_taint.get(digest, frozenset()) | frozenset(targets)
            return digest

        messages = []
        if node.system:
            messages.append(Message(role="system", content=render(node.system, state, max_chars, head_fraction,
                                                                  archive)))
        messages.append(Message(role="user", content=render(node.prompt, state, max_chars, head_fraction, archive)))
        total = Cost()
        attempts: list[str] = []
        for _ in range(node.retries + 1):
            resp = backend.complete(ChatRequest(messages=messages, json_schema=node.output_schema,
                                                temperature=node.temperature, max_tokens=node.max_tokens))
            total = total + resp.cost
            if node.output_schema is None:
                return ({"value": resp.text, "attempts": attempts + [resp.text], "messages": _dump(messages),
                         "archived": archived}, total, resp.provenance)
            try:
                value = parse_json_output(resp.text)
                jsonschema.validate(value, node.output_schema)
                return ({"value": value, "attempts": attempts + [resp.text], "messages": _dump(messages),
                         "archived": archived}, total, resp.provenance)
            except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                msg = e.msg if isinstance(e, json.JSONDecodeError) else e.message
                attempts.append(resp.text)
                messages = messages + [
                    Message(role="assistant", content=resp.text),
                    Message(role="user", content=f"Your output violated the required JSON contract: {msg}. "
                                                 f"Reply with only valid JSON matching the schema."),
                ]
        raise ContractError(f"output contract failed after {node.retries + 1} attempts", total, attempts,
                            _dump(messages))


def _dump(messages: list[Message]) -> list[dict]:
    """Rendered prompts as the model saw them: recorded so the leak audit can check them."""
    return [m.model_dump() for m in messages]


def _private_values(state: dict[str, Any]) -> list[str]:
    """Every literal string that means a private value reached the trace: the value itself (>= 8 characters) and,
    for long values, its common encodings and long fragments (verify.leak.secret_patterns, thresholds there)."""
    from ..verify.leak import secret_patterns
    vals = [str(v) for k, v in state.items() if k.startswith("_private") and v is not None]
    return sorted({p for v in vals for p in secret_patterns(v)}, key=lambda p: (-len(p), p))


def _scrub(obj: Any, secrets: list[str], hits: list[int] | None = None) -> Any:
    """Drop `_private*` keys and replace private values inside strings (e.g. a private path quoted in
    an exception message) before anything is written to the trace. `hits` counts string replacements:
    a private VALUE inside a non-private field means it reached agent-visible output, and the step is
    marked so the leak audit fails closed (scrubbing hides it from readers, never from the audit)."""
    hits = hits if hits is not None else [0]
    if isinstance(obj, dict):
        return {k: ("<private>" if str(k).startswith("_private") else _scrub(v, secrets, hits))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, secrets, hits) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            if s in obj:
                hits[0] += obj.count(s)
                obj = obj.replace(s, "<private>")
        return obj
    return obj


def _scrub_step(out: dict[str, Any], error: str | None, secrets: list[str]) -> tuple[dict[str, Any], str | None]:
    hits = [0]
    clean_out = _scrub(out, secrets, hits)
    clean_err = _scrub(error, secrets, hits) if error is not None else None
    if hits[0]:
        clean_out = {**clean_out, "_redacted_private_values": hits[0]}
    return clean_out, clean_err


class ForkRefused(ValueError):
    """A fork's state patch would touch reserved, private or engine state (nothing was written)."""


def _branch_id(instance: str, node_id: str, k: int, item_hash: str) -> str:
    """Deterministic fan-out branch id: same instance, node, position and item -> same id."""
    doc = json.dumps({"instance": instance, "node": node_id, "index": k, "item": item_hash}, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()[:32]


def _check_fork_patch(patch: dict[str, Any], state: dict[str, Any]) -> str:
    """Refuse a fork patch that touches reserved or private state; return the patch's sha256 ("" if empty)."""
    from ..evolve.edits import RESERVED_OUTPUT_KEYS
    for key in patch:
        if not isinstance(key, str) or not key or "." in key:
            raise ForkRefused(f"fork patch key {key!r}: only plain top-level keys may be patched")
        if engine_reserved(key):
            raise ForkRefused(f"fork patch key '{key}' is private, engine or approval state")
        if key in RESERVED_OUTPUT_KEYS:
            raise ForkRefused(f"fork patch key '{key}' is reserved (only trusted code writes it)")
    if not patch:
        return ""
    try:
        canon = json.dumps(patch, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise ForkRefused(f"fork patch is not JSON: {e}") from e
    texts = [canon]
    stack: list[Any] = [patch]
    while stack:  # string leaves and keys too: JSON escaping must not hide a private value
        cur = stack.pop()
        if isinstance(cur, str):
            texts.append(cur)
        elif isinstance(cur, dict):
            texts.extend(str(k) for k in cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    if any(p in t for p in _private_values(state) for t in texts):
        raise ForkRefused("fork patch carries a private value of the checkpoint state")
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _unit_max(a: Cost, b: Cost) -> Cost:
    """Per-unit maximum of two cumulative spends."""
    return Cost(prompt_tokens=max(a.prompt_tokens, b.prompt_tokens),
                completion_tokens=max(a.completion_tokens, b.completion_tokens), usd=max(a.usd, b.usd),
                gpu_hours=max(a.gpu_hours, b.gpu_hours), quota_units=max(a.quota_units, b.quota_units),
                wall_s=max(a.wall_s, b.wall_s), estimated=a.estimated or b.estimated)


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
    return {"llm": StepKind.LLM, "tool": StepKind.TOOL, "module": StepKind.MODULE,
            "approval": StepKind.APPROVAL, "map": StepKind.CONTROL}[node.kind]
