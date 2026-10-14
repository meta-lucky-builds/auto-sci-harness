"""Downgrade-only gate for LLM controllers (GOAL §1.1, §5 L1, §10 invariants 7 and 21, §16.5.8).

"Every gate, admission, budget and kill decision is code. An LLM may only downgrade, never grant." This module is
the one interface through which an LLM-backed controller may act on budgets, admission and runs. The controller
(a gateway Backend, e.g. MockBackend in tests) only ever produces TEXT. `DowngradeGate.submit` parses that text
strictly and applies at most these three action types, each re-checked by code against code-owned state:

- `deny`   a PENDING admission request becomes DENIED (one-way; a denied request can never be admitted);
- `shrink` a named budget's cap for one unit in budget.UNITS is lowered to a finite value >= 0 strictly below the
           current cap (an unlimited cap, None or absent, may be shrunk to any finite value >= 0);
- `kill`   a named, still-killable run is stopped, ONLY with evidence of a kind in EVIDENCE_KINDS whose facts code
           recomputes from the store; the claim must match the recomputation and the condition must hold.

Everything else is rejected, fails closed and is ledgered: unknown action types (grant, admit, approve, revive,
unkill, enlarge, ...), extra or missing fields, wrong types (bools are not numbers here), malformed or non-JSON
text, duplicate JSON keys, NaN / inf / negative / non-decreasing shrinks, unknown budgets, units, requests or runs,
deny of a request that is not pending, kill of a terminal run, kill without evidence, kill whose evidence code
cannot confirm, and any exception raised inside a checker or an effect callback (an exception is a rejection,
never an acceptance).

Batch semantics (chosen and tested): a proposal is the JSON object {"actions": [ ... ]}. If the ENVELOPE is bad
(not JSON, not that exact object shape, too large, too many actions) the whole proposal is rejected and one
`invalid` action record is written. Otherwise every action is decided INDEPENDENTLY, in order, against the state
left by the earlier actions: legal downgrades are applied even when the same proposal also contains illegal ones.
This is safe because every legal action is a downgrade on its own; no action's legality depends on another's.

Ledger (append-only store records; the controller's free text is NEVER stored, §16.5.8 covert channel):
- `controller_proposal`: controller_id, proposal_sha256 (sha256 of the raw text), n_actions, envelope reason code;
- `controller_action`: controller_id, proposal_sha256, index, action_type (deny|shrink|kill|invalid), target_id
  (only when it names an object code knows; otherwise None, with target_sha256 of the claimed id), outcome
  (applied|rejected), reason (a ReasonCode), and for applied actions the code-checked effect (unit/old/new cap,
  evidence kind). The proposal record is written before any effect; each action record right after its decision.

Effects are owned by code through injected objects the controller never sees: a Mapping[str, Budget] of live
budgets (the gate only ever LOWERS `limits[unit]`, re-asserting the shrink rule immediately before the write), an
AdmissionRegister (the gate calls only `state` and `deny`; `admit` exists for CODE callers and the gate never calls
it), and a `kill(run_id, code)` callback. There is no code path in the gate that raises a cap, admits or revives.

Not enforced here (stated, see docs/control_downgrade.md): binding to studio kill (`chh kill` is a human action
with a ledger entry), to the run engine's admission, or to the Evaluator's campaign budget; that is the caller's
wiring. The no_progress / stalled thresholds are UNCALIBRATED defaults.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..budget import UNITS, Budget, _value
from ..gateway.base import Backend, ChatRequest, Message
from ..records import RunStatus
from ..store import EvidenceStore

PROPOSAL_KIND = "controller_proposal"
ACTION_KIND = "controller_action"

ACTION_TYPES = ("deny", "shrink", "kill")
EVIDENCE_KINDS = ("budget_exceeded", "no_progress", "stalled")

MAX_PROPOSAL_CHARS = 65_536
MAX_ACTIONS = 16
# UNCALIBRATED defaults (no measurement behind them yet; see docs/control_downgrade.md)
DEFAULT_NO_PROGRESS_MIN_STEPS = 3
DEFAULT_STALL_S = 600.0


class Outcome(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"


class ReasonCode(StrEnum):
    """Closed set. Every ledgered decision carries exactly one of these, never free text."""

    OK = "ok"
    # envelope
    NOT_JSON = "not_json"
    TOO_LARGE = "too_large"
    BAD_ENVELOPE = "bad_envelope"
    TOO_MANY_ACTIONS = "too_many_actions"
    BACKEND_ERROR = "backend_error"
    # per action, shape
    UNKNOWN_ACTION = "unknown_action"
    SCHEMA_INVALID = "schema_invalid"
    # deny
    UNKNOWN_REQUEST = "unknown_request"
    NOT_PENDING = "not_pending"
    # shrink
    UNKNOWN_BUDGET = "unknown_budget"
    UNKNOWN_UNIT = "unknown_unit"
    NOT_FINITE = "not_finite"
    NEGATIVE = "negative"
    NOT_A_SHRINK = "not_a_shrink"
    # kill
    UNKNOWN_RUN = "unknown_run"
    RUN_TERMINAL = "run_terminal"
    NO_EVIDENCE = "no_evidence"
    UNKNOWN_EVIDENCE_KIND = "unknown_evidence_kind"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    EVIDENCE_NOT_MET = "evidence_not_met"
    CHECKER_ERROR = "checker_error"
    # effect callback raised
    EFFECT_ERROR = "effect_error"


class AdmissionState(StrEnum):
    PENDING = "pending"
    DENIED = "denied"
    ADMITTED = "admitted"


class AdmissionRegister:
    """Admission requests owned by code. Transitions are one-way out of PENDING: to DENIED (code or the gate) or to
    ADMITTED (code only: the gate never calls `admit`). Nothing moves a request out of DENIED or ADMITTED."""

    def __init__(self) -> None:
        self._state: dict[str, AdmissionState] = {}

    def submit(self, request_id: str) -> None:
        if request_id in self._state:
            raise ValueError(f"admission request {request_id!r} already exists")
        self._state[request_id] = AdmissionState.PENDING

    def state(self, request_id: str) -> AdmissionState | None:
        return self._state.get(request_id)

    def deny(self, request_id: str) -> None:
        if self._state.get(request_id) != AdmissionState.PENDING:
            raise ValueError(f"admission request {request_id!r} is not pending")
        self._state[request_id] = AdmissionState.DENIED

    def admit(self, request_id: str) -> None:
        """CODE-ONLY grant. Not reachable from DowngradeGate."""
        if self._state.get(request_id) != AdmissionState.PENDING:
            raise ValueError(f"admission request {request_id!r} is not pending")
        self._state[request_id] = AdmissionState.ADMITTED

    def snapshot(self) -> dict[str, AdmissionState]:
        return dict(self._state)


@dataclass(frozen=True)
class ActionResult:
    index: int
    action_type: str  # deny | shrink | kill | invalid
    target_id: str | None
    outcome: Outcome
    reason: ReasonCode


@dataclass(frozen=True)
class ProposalResult:
    proposal_sha256: str
    envelope: ReasonCode
    actions: tuple[ActionResult, ...]

    @property
    def applied(self) -> tuple[ActionResult, ...]:
        return tuple(a for a in self.actions if a.outcome == Outcome.APPLIED)


class _Reject(Exception):  # internal control flow, never escapes the gate
    def __init__(self, code: ReasonCode):
        super().__init__(code.value)
        self.code = code


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate JSON key")
        out[k] = v
    return out


def _exact_keys(obj: dict[str, Any], required: set[str]) -> None:
    if set(obj) != required:
        raise _Reject(ReasonCode.SCHEMA_INVALID)


def _str_field(obj: dict[str, Any], key: str) -> str:
    v = obj[key]
    if type(v) is not str or not v:
        raise _Reject(ReasonCode.SCHEMA_INVALID)
    return v


def _int_field(obj: dict[str, Any], key: str) -> int:
    v = obj[key]
    if type(v) is not int:  # bool is a subclass of int: rejected by the exact type check
        raise _Reject(ReasonCode.SCHEMA_INVALID)
    return v


def _number(v: Any) -> float:
    if type(v) not in (int, float):  # rejects bool, str, None, containers
        raise _Reject(ReasonCode.SCHEMA_INVALID)
    try:
        f = float(v)
    except OverflowError:  # an int too large for a float
        raise _Reject(ReasonCode.NOT_FINITE) from None
    if not math.isfinite(f):
        raise _Reject(ReasonCode.NOT_FINITE)
    return f


def _killable(status: RunStatus, detail: str) -> bool:
    """Same rule as studio/kill.py (g-studio): a run that can still do something."""
    return (status in (RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.INFRA_FAILED)
            or (status == RunStatus.FAILED and detail.startswith("engine crash")))


class DowngradeGate:
    """Applies LLM-proposed downgrades. Construct it with code-owned state; give the controller nothing but text.

    budgets:      named live budgets the controller may shrink (the gate only ever lowers their caps).
    admissions:   the admission register (the gate only denies pending requests).
    kill:         callback kill(run_id, code) that stops a run; `code` is "controller_<evidence kind>".
    run_budgets:  run_id -> the Budget that run was given (the cap budget_exceeded is checked against).
    clock:        injected for deterministic tests of `stalled`.
    """

    def __init__(self, store: EvidenceStore, *, budgets: Mapping[str, Budget], admissions: AdmissionRegister,
                 kill: Callable[[str, str], None], run_budgets: Mapping[str, Budget] | None = None,
                 no_progress_min_steps: int = DEFAULT_NO_PROGRESS_MIN_STEPS, stall_s: float = DEFAULT_STALL_S,
                 clock: Callable[[], float] = time.time):
        if no_progress_min_steps < 1 or not (math.isfinite(stall_s) and stall_s > 0):
            raise ValueError("no_progress_min_steps must be >= 1 and stall_s finite and > 0")
        self._store = store
        self._budgets = budgets
        self._admissions = admissions
        self._kill = kill
        self._run_budgets: Mapping[str, Budget] = {} if run_budgets is None else run_budgets
        self._min_steps = no_progress_min_steps
        self._stall_s = float(stall_s)
        self._clock = clock
        self._killed: set[str] = set()

    # --- entry points ------------------------------------------------------------------------------------------
    def ask(self, controller_id: str, backend: Backend, observation: str, *, model: str | None = None
            ) -> ProposalResult:
        """One controller turn: show `observation` (built by code) to the backend and submit its reply. A backend
        failure is a ledgered rejection; nothing is applied."""
        req = ChatRequest(model=model, temperature=0.0, messages=[
            Message(role="system", content=CONTROLLER_PROMPT), Message(role="user", content=observation)])
        try:
            text = backend.complete(req).text
        except Exception:  # noqa: BLE001 - any backend failure fails closed and is ledgered
            return self._reject_envelope(controller_id, _sha256(""), ReasonCode.BACKEND_ERROR)
        return self.submit(controller_id, text)

    def submit(self, controller_id: str, raw: str) -> ProposalResult:
        if type(controller_id) is not str or not controller_id.strip():
            raise ValueError("controller_id (code-supplied) must be a non-empty string")
        if type(raw) is not str:
            return self._reject_envelope(controller_id, _sha256(repr(type(raw))), ReasonCode.BAD_ENVELOPE)
        digest = _sha256(raw)
        if len(raw) > MAX_PROPOSAL_CHARS:
            return self._reject_envelope(controller_id, digest, ReasonCode.TOO_LARGE)
        try:
            doc = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
        except (ValueError, RecursionError):
            return self._reject_envelope(controller_id, digest, ReasonCode.NOT_JSON)
        if type(doc) is not dict or set(doc) != {"actions"} or type(doc["actions"]) is not list:
            return self._reject_envelope(controller_id, digest, ReasonCode.BAD_ENVELOPE)
        actions = doc["actions"]
        if len(actions) > MAX_ACTIONS:
            return self._reject_envelope(controller_id, digest, ReasonCode.TOO_MANY_ACTIONS)
        self._store.append_record(PROPOSAL_KIND, {"controller_id": controller_id, "proposal_sha256": digest,
                                                  "n_actions": len(actions), "envelope": ReasonCode.OK.value})
        results = tuple(self._decide(controller_id, digest, i, a) for i, a in enumerate(actions))
        return ProposalResult(digest, ReasonCode.OK, results)

    # --- decisions ---------------------------------------------------------------------------------------------
    def _reject_envelope(self, controller_id: str, digest: str, code: ReasonCode) -> ProposalResult:
        self._store.append_record(PROPOSAL_KIND, {"controller_id": controller_id, "proposal_sha256": digest,
                                                  "n_actions": 0, "envelope": code.value})
        res = ActionResult(-1, "invalid", None, Outcome.REJECTED, code)
        self._ledger(controller_id, digest, res, None, {})
        return ProposalResult(digest, code, (res,))

    def _decide(self, controller_id: str, digest: str, index: int, action: Any) -> ActionResult:
        atype = "invalid"
        target: str | None = None
        claimed: str | None = None
        effect: dict[str, Any] = {}
        try:
            if type(action) is not dict or type(action.get("type")) is not str:
                raise _Reject(ReasonCode.SCHEMA_INVALID)
            if action["type"] not in ACTION_TYPES:
                raise _Reject(ReasonCode.UNKNOWN_ACTION)
            atype = action["type"]
            if type(action.get("target")) is str:
                claimed = action["target"]
                target = self._known_target(atype, claimed)
            if atype == "deny":
                rid = self._check_deny(action)
                self._effect(lambda: self._admissions.deny(rid))
            elif atype == "shrink":
                name, unit, old, new = self._check_shrink(action)
                self._effect(lambda: self._apply_shrink(name, unit, new))
                effect = {"unit": unit, "old_cap": old, "new_cap": new}
            else:
                run_id, kind = self._check_kill(action)
                self._effect(lambda: self._kill(run_id, f"controller_{kind}"))
                self._killed.add(run_id)
                effect = {"evidence_kind": kind}
            res = ActionResult(index, atype, target, Outcome.APPLIED, ReasonCode.OK)
        except _Reject as r:
            res = ActionResult(index, atype, target, Outcome.REJECTED, r.code)
            effect = {}
        except Exception:  # noqa: BLE001 - a checker that raises rejects; it never accepts
            res = ActionResult(index, atype, target, Outcome.REJECTED, ReasonCode.CHECKER_ERROR)
            effect = {}
        self._ledger(controller_id, digest, res, claimed, effect)
        return res

    def _known_target(self, atype: str, claimed: str) -> str | None:
        """The claimed id, if it names an object of the action's kind that code knows; else None (then only its
        sha256 is ledgered)."""
        try:
            if atype == "deny":
                return claimed if self._admissions.state(claimed) is not None else None
            if atype == "shrink":
                return claimed if claimed in self._budgets else None
            self._store.run_status(claimed)
            return claimed
        except Exception:  # noqa: BLE001 - unknown is the safe answer
            return None

    def _effect(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - the effect did not happen as checked: record a rejection
            raise _Reject(ReasonCode.EFFECT_ERROR) from None

    def _ledger(self, controller_id: str, digest: str, res: ActionResult, claimed: str | None,
                effect: dict[str, Any]) -> None:
        body: dict[str, Any] = {"controller_id": controller_id, "proposal_sha256": digest, "index": res.index,
                                "action_type": res.action_type, "target_id": res.target_id,
                                "outcome": res.outcome.value, "reason": res.reason.value}
        if res.target_id is None and claimed is not None:
            body["target_sha256"] = _sha256(claimed)  # an unknown id is never stored verbatim
        body.update(effect)
        self._store.append_record(ACTION_KIND, body)

    def _check_deny(self, a: dict[str, Any]) -> str:
        _exact_keys(a, {"type", "target"})
        rid = _str_field(a, "target")
        state = self._admissions.state(rid)
        if state is None:
            raise _Reject(ReasonCode.UNKNOWN_REQUEST)
        if state != AdmissionState.PENDING:
            raise _Reject(ReasonCode.NOT_PENDING)
        return rid

    def _check_shrink(self, a: dict[str, Any]) -> tuple[str, str, float | None, float]:
        _exact_keys(a, {"type", "target", "unit", "value"})
        name = _str_field(a, "target")
        if name not in self._budgets:
            raise _Reject(ReasonCode.UNKNOWN_BUDGET)
        unit = _str_field(a, "unit")
        if unit not in UNITS:
            raise _Reject(ReasonCode.UNKNOWN_UNIT)
        new = _number(a["value"])
        if new < 0:
            raise _Reject(ReasonCode.NEGATIVE)
        old = self._budgets[name].limits.get(unit)
        if old is not None and not new < old:
            raise _Reject(ReasonCode.NOT_A_SHRINK)
        return name, unit, old, new

    def _apply_shrink(self, name: str, unit: str, new: float) -> None:
        budget = self._budgets[name]
        old = budget.limits.get(unit)
        # re-asserted at the single write site: this line can only ever LOWER a cap to a finite value >= 0
        if not (math.isfinite(new) and new >= 0 and (old is None or new < old)):
            raise RuntimeError("shrink invariant violated")
        budget.limits[unit] = new

    def _check_kill(self, a: dict[str, Any]) -> tuple[str, str]:
        if "evidence" not in a or a.get("evidence") is None:
            raise _Reject(ReasonCode.NO_EVIDENCE)
        _exact_keys(a, {"type", "target", "evidence"})
        rid = _str_field(a, "target")
        try:
            status, detail = self._store.run_status(rid)
        except KeyError:
            raise _Reject(ReasonCode.UNKNOWN_RUN) from None
        if rid in self._killed or not _killable(status, detail):
            raise _Reject(ReasonCode.RUN_TERMINAL)
        ev = a["evidence"]
        if type(ev) is not dict or type(ev.get("kind")) is not str:
            raise _Reject(ReasonCode.SCHEMA_INVALID)
        kind = ev["kind"]
        if kind not in EVIDENCE_KINDS:
            raise _Reject(ReasonCode.UNKNOWN_EVIDENCE_KIND)
        if kind == "budget_exceeded":
            self._check_budget_exceeded(rid, ev)
        elif kind == "no_progress":
            self._check_no_progress(rid, ev)
        else:
            self._check_stalled(rid, status, ev)
        return rid, kind

    # --- evidence checkers: each recomputes from the store; the claim must match, then the condition must hold ----
    def _check_budget_exceeded(self, rid: str, ev: dict[str, Any]) -> None:
        """The run's recorded cost (store.run_cost) in `unit` is >= the cap of the Budget it was given."""
        _exact_keys(ev, {"kind", "unit", "spent", "cap"})
        unit = _str_field(ev, "unit")
        if unit not in UNITS:
            raise _Reject(ReasonCode.UNKNOWN_UNIT)
        spent_claim, cap_claim = _number(ev["spent"]), _number(ev["cap"])
        budget = self._run_budgets.get(rid)
        cap = None if budget is None else budget.limits.get(unit)
        if cap is None:
            raise _Reject(ReasonCode.EVIDENCE_NOT_MET)  # no finite cap was given: it cannot be exceeded
        spent = _value(self._store.run_cost(rid), unit)
        if cap_claim != cap or not math.isclose(spent_claim, spent, rel_tol=1e-9, abs_tol=1e-9):
            raise _Reject(ReasonCode.EVIDENCE_MISMATCH)
        if not spent >= cap:
            raise _Reject(ReasonCode.EVIDENCE_NOT_MET)

    def _check_no_progress(self, rid: str, ev: dict[str, Any]) -> None:
        """The run's last `steps` trace steps (steps >= the code's minimum) all failed with the same error, whose
        sha256 is `error_sha256`."""
        _exact_keys(ev, {"kind", "steps", "error_sha256"})
        k = _int_field(ev, "steps")
        claim = _str_field(ev, "error_sha256")
        if k < self._min_steps:
            raise _Reject(ReasonCode.EVIDENCE_NOT_MET)
        tail = self._store.trace(rid)[-k:]
        if len(tail) < k:
            raise _Reject(ReasonCode.EVIDENCE_MISMATCH)  # the claimed steps do not exist
        sigs = {None if s.error is None else _sha256(s.error) for s in tail}
        if sigs != {claim}:
            raise _Reject(ReasonCode.EVIDENCE_MISMATCH)

    def _check_stalled(self, rid: str, status: RunStatus, ev: dict[str, Any]) -> None:
        """A RUNNING run whose last step (index `last_step_index`, -1 for none) ended >= stall_s ago (measured from
        the run's start when it has no step). A PAUSED run waits for a human by design: never `stalled`."""
        _exact_keys(ev, {"kind", "last_step_index"})
        claim = _int_field(ev, "last_step_index")
        trace = self._store.trace(rid)
        last = trace[-1].index if trace else -1
        if claim != last:
            raise _Reject(ReasonCode.EVIDENCE_MISMATCH)
        if status != RunStatus.RUNNING:
            raise _Reject(ReasonCode.EVIDENCE_NOT_MET)
        since = trace[-1].ended_at if trace else self._store.get_run(rid).started_at
        if not self._clock() - since >= self._stall_s:
            raise _Reject(ReasonCode.EVIDENCE_NOT_MET)


CONTROLLER_PROMPT = (
    "You are a downgrade-only controller. Reply with ONE JSON object and nothing else: "
    f'{{"actions": [...]}} with at most {MAX_ACTIONS} actions. Allowed actions, exactly these fields:\n'
    '{"type": "deny", "target": REQUEST_ID}\n'
    f'{{"type": "shrink", "target": BUDGET_NAME, "unit": one of {list(UNITS)}, "value": NUMBER below the '
    "current cap}\n"
    '{"type": "kill", "target": RUN_ID, "evidence": EVIDENCE} where EVIDENCE is one of\n'
    '  {"kind": "budget_exceeded", "unit": UNIT, "spent": NUMBER, "cap": NUMBER}\n'
    '  {"kind": "no_progress", "steps": INT, "error_sha256": HEX}\n'
    '  {"kind": "stalled", "last_step_index": INT}\n'
    "You cannot grant, admit, raise a cap or revive anything; such proposals are rejected and recorded."
)
