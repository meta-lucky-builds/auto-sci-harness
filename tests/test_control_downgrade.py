"""GOAL §1.1 / §5 L1: LLM controllers may only downgrade (deny, shrink, kill-with-code-checked-evidence)."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import random
from types import SimpleNamespace

import pytest

from custom_harness.budget import UNITS, Budget
from custom_harness.control import downgrade as dg
from custom_harness.control.downgrade import (
    ACTION_KIND,
    MAX_ACTIONS,
    PROPOSAL_KIND,
    AdmissionRegister,
    AdmissionState,
    DowngradeGate,
    Outcome,
    ReasonCode,
)
from custom_harness.gateway.base import BackendError
from custom_harness.gateway.mock import MockBackend
from custom_harness.records import Cost, Run, RunStatus, StepKind, TraceStep
from custom_harness.store import EvidenceStore

NOW = 1_000_000.0
ERR = "Traceback: ZeroDivisionError at step"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@pytest.fixture
def store(tmp_path):
    """A real EvidenceStore. fsync is switched off on this throwaway test database only, so the thousands of
    append-only records the property test writes stay CPU/IO-light (each commit otherwise fsyncs on Windows)."""
    s = EvidenceStore(tmp_path / "store")
    s._db.execute("PRAGMA synchronous=OFF")
    s._db.execute("PRAGMA journal_mode=MEMORY")
    yield s
    s.close()


def _run(store, steps=(), status=None, started_at=NOW - 10_000):
    run = Run(harness_id="h", harness_hash="hh", task_id="t", started_at=started_at)
    store.add_run(run)
    for i, (cost, err, ended) in enumerate(steps):
        store.add_step(TraceStep(run_id=run.id, index=i, node_id="n", kind=StepKind.LLM, input={}, output={},
                                 error=err, cost=cost, started_at=ended - 1, ended_at=ended))
    if status is not None:
        store.set_run_status(run.id, status, "done")
    return run.id


@pytest.fixture
def world(store):
    budgets = {"campaign": Budget(limits={"usd": 10.0, "tokens": 1000.0}), "open": Budget(limits={})}
    reg = AdmissionRegister()
    for rid in ("req_pending", "req_pending2", "req_denied", "req_admitted"):
        reg.submit(rid)
    reg.deny("req_denied")
    reg.admit("req_admitted")  # code may admit; the gate never can
    over = _run(store, [(Cost(usd=6.0), None, NOW - 50), (Cost(usd=5.0), None, NOW - 40)])  # 11 usd > cap 5
    under = _run(store, [(Cost(usd=1.0), None, NOW - 50)])
    loop = _run(store, [(Cost(), "other", NOW - 90)] + [(Cost(), ERR, NOW - 80 + i) for i in range(3)])
    idle = _run(store, [(Cost(), None, NOW - 700)])
    fresh = _run(store, [(Cost(), None, NOW - 5)])
    done = _run(store, [(Cost(usd=100.0), None, NOW - 50)], status=RunStatus.COMPLETED)
    paused = _run(store, [(Cost(), None, NOW - 5000)], status=RunStatus.PAUSED)
    run_budgets = {r: Budget(limits={"usd": 5.0}) for r in (over, under, done)}
    kills: list[tuple[str, str]] = []

    def kill(run_id: str, code: str) -> None:
        kills.append((run_id, code))
        store.set_run_status(run_id, RunStatus.FAILED, f"killed by controller ({code})")

    gate = DowngradeGate(store, budgets=budgets, admissions=reg, kill=kill, run_budgets=run_budgets,
                         clock=lambda: NOW)
    return SimpleNamespace(store=store, gate=gate, budgets=budgets, reg=reg, kills=kills, over=over, under=under,
                           loop=loop, idle=idle, fresh=fresh, done=done, paused=paused, run_budgets=run_budgets)


def _state(w):
    runs = [w.over, w.under, w.loop, w.idle, w.fresh, w.done, w.paused]
    return ({n: dict(b.limits) for n, b in w.budgets.items()}, w.reg.snapshot(),
            {r: w.store.run_status(r) for r in runs}, list(w.kills))


def _one(action) -> str:
    return json.dumps({"actions": [action]})


def _last_action(store) -> dict:
    return store.records(ACTION_KIND)[-1]


# --- every illegal proposal is rejected, leaves state unchanged and is ledgered with its code ----------------------
ILLEGAL = [
    ("grant", lambda w: _one({"type": "grant", "target": "campaign", "unit": "usd", "value": 99}),
     ReasonCode.UNKNOWN_ACTION),
    ("admit", lambda w: _one({"type": "admit", "target": "req_pending"}), ReasonCode.UNKNOWN_ACTION),
    ("approve", lambda w: _one({"type": "approve", "target": "req_pending"}), ReasonCode.UNKNOWN_ACTION),
    ("revive", lambda w: _one({"type": "revive", "target": w.done}), ReasonCode.UNKNOWN_ACTION),
    ("unkill", lambda w: _one({"type": "unkill", "target": w.done}), ReasonCode.UNKNOWN_ACTION),
    ("enlarge", lambda w: _one({"type": "enlarge", "target": "campaign", "unit": "usd", "value": 20}),
     ReasonCode.UNKNOWN_ACTION),
    ("DENY_case", lambda w: _one({"type": "DENY", "target": "req_pending"}), ReasonCode.UNKNOWN_ACTION),
    ("shrink_equal", lambda w: _one({"type": "shrink", "target": "campaign", "unit": "usd", "value": 10.0}),
     ReasonCode.NOT_A_SHRINK),
    ("shrink_larger", lambda w: _one({"type": "shrink", "target": "campaign", "unit": "usd", "value": 11}),
     ReasonCode.NOT_A_SHRINK),
    ("shrink_nan", lambda w: '{"actions": [{"type": "shrink", "target": "campaign", "unit": "usd", "value": NaN}]}',
     ReasonCode.NOT_FINITE),
    ("shrink_inf", lambda w: '{"actions": [{"type": "shrink", "target": "open", "unit": "usd", "value": Infinity}]}',
     ReasonCode.NOT_FINITE),
    ("shrink_neg_inf",
     lambda w: '{"actions": [{"type": "shrink", "target": "campaign", "unit": "usd", "value": -Infinity}]}',
     ReasonCode.NOT_FINITE),
    ("shrink_1e999", lambda w: '{"actions": [{"type": "shrink", "target": "open", "unit": "usd", "value": 1e999}]}',
     ReasonCode.NOT_FINITE),
    ("shrink_huge_int", lambda w: _one({"type": "shrink", "target": "open", "unit": "usd", "value": 10 ** 400}),
     ReasonCode.NOT_FINITE),
    ("shrink_negative", lambda w: _one({"type": "shrink", "target": "campaign", "unit": "usd", "value": -1}),
     ReasonCode.NEGATIVE),
    ("shrink_bool", lambda w: _one({"type": "shrink", "target": "campaign", "unit": "usd", "value": True}),
     ReasonCode.SCHEMA_INVALID),
    ("shrink_str", lambda w: _one({"type": "shrink", "target": "campaign", "unit": "usd", "value": "5"}),
     ReasonCode.SCHEMA_INVALID),
    ("shrink_null", lambda w: _one({"type": "shrink", "target": "campaign", "unit": "usd", "value": None}),
     ReasonCode.SCHEMA_INVALID),
    ("shrink_unknown_unit", lambda w: _one({"type": "shrink", "target": "campaign", "unit": "eur", "value": 1}),
     ReasonCode.UNKNOWN_UNIT),
    ("shrink_unknown_budget", lambda w: _one({"type": "shrink", "target": "nope", "unit": "usd", "value": 1}),
     ReasonCode.UNKNOWN_BUDGET),
    ("shrink_extra_field",
     lambda w: _one({"type": "shrink", "target": "campaign", "unit": "usd", "value": 1, "also_raise": "tokens"}),
     ReasonCode.SCHEMA_INVALID),
    ("shrink_missing_field", lambda w: _one({"type": "shrink", "target": "campaign", "value": 1}),
     ReasonCode.SCHEMA_INVALID),
    ("deny_extra_field", lambda w: _one({"type": "deny", "target": "req_pending", "then": "admit"}),
     ReasonCode.SCHEMA_INVALID),
    ("deny_target_int", lambda w: _one({"type": "deny", "target": 3}), ReasonCode.SCHEMA_INVALID),
    ("deny_denied", lambda w: _one({"type": "deny", "target": "req_denied"}), ReasonCode.NOT_PENDING),
    ("deny_admitted", lambda w: _one({"type": "deny", "target": "req_admitted"}), ReasonCode.NOT_PENDING),
    ("deny_unknown", lambda w: _one({"type": "deny", "target": "req_x"}), ReasonCode.UNKNOWN_REQUEST),
    ("kill_no_evidence", lambda w: _one({"type": "kill", "target": w.over}), ReasonCode.NO_EVIDENCE),
    ("kill_null_evidence", lambda w: _one({"type": "kill", "target": w.over, "evidence": None}),
     ReasonCode.NO_EVIDENCE),
    ("kill_evidence_str", lambda w: _one({"type": "kill", "target": w.over, "evidence": "trust me"}),
     ReasonCode.SCHEMA_INVALID),
    ("kill_unknown_kind", lambda w: _one({"type": "kill", "target": w.over, "evidence": {"kind": "vibes"}}),
     ReasonCode.UNKNOWN_EVIDENCE_KIND),
    ("kill_unknown_run", lambda w: _one({"type": "kill", "target": "run_nope",
                                         "evidence": {"kind": "stalled", "last_step_index": 0}}),
     ReasonCode.UNKNOWN_RUN),
    ("kill_terminal", lambda w: _one({"type": "kill", "target": w.done, "evidence": {
        "kind": "budget_exceeded", "unit": "usd", "spent": 100.0, "cap": 5.0}}), ReasonCode.RUN_TERMINAL),
    ("kill_evidence_extra", lambda w: _one({"type": "kill", "target": w.over, "evidence": {
        "kind": "budget_exceeded", "unit": "usd", "spent": 11.0, "cap": 5.0, "note": "x"}}),
     ReasonCode.SCHEMA_INVALID),
    ("kill_extra_field", lambda w: _one({"type": "kill", "target": w.idle, "reason": "x",
                                         "evidence": {"kind": "stalled", "last_step_index": 0}}),
     ReasonCode.SCHEMA_INVALID),
    ("action_not_object", lambda w: json.dumps({"actions": ["deny req_pending"]}), ReasonCode.SCHEMA_INVALID),
    ("action_type_missing", lambda w: _one({"target": "req_pending"}), ReasonCode.SCHEMA_INVALID),
    # envelope-level
    ("not_json", lambda w: "Sure! I will deny req_pending.", ReasonCode.NOT_JSON),
    ("empty", lambda w: "", ReasonCode.NOT_JSON),
    ("truncated", lambda w: '{"actions": [{"type": "deny"', ReasonCode.NOT_JSON),
    ("duplicate_keys", lambda w: '{"actions": [{"type": "deny", "type": "admit", "target": "req_pending"}]}',
     ReasonCode.NOT_JSON),
    ("bare_list", lambda w: json.dumps([{"type": "deny", "target": "req_pending"}]), ReasonCode.BAD_ENVELOPE),
    ("actions_dict", lambda w: '{"actions": {"type": "deny", "target": "req_pending"}}', ReasonCode.BAD_ENVELOPE),
    ("envelope_extra", lambda w: json.dumps({"actions": [], "grant": True}), ReasonCode.BAD_ENVELOPE),
    ("too_many", lambda w: json.dumps({"actions": [{"type": "deny", "target": "req_pending"}] * (MAX_ACTIONS + 1)}),
     ReasonCode.TOO_MANY_ACTIONS),
    ("too_large", lambda w: json.dumps({"actions": [], "pad": "x" * 70_000}), ReasonCode.TOO_LARGE),
]


@pytest.mark.parametrize(("name", "make", "code"), ILLEGAL, ids=[c[0] for c in ILLEGAL])
def test_illegal_proposal_rejected_unchanged_and_ledgered(world, name, make, code):
    raw = make(world)
    before = _state(world)
    res = world.gate.submit("ctl-1", raw)
    assert _state(world) == before
    assert not res.applied
    assert [a.reason for a in res.actions] == [code]
    rec = _last_action(world.store)
    assert rec["outcome"] == "rejected" and rec["reason"] == code.value
    assert rec["controller_id"] == "ctl-1" and rec["proposal_sha256"] == _sha(raw)
    assert rec["reason"] in {c.value for c in ReasonCode}
    prop = world.store.records(PROPOSAL_KIND)[-1]
    assert prop["proposal_sha256"] == _sha(raw)


def test_unknown_action_types_are_ledgered_as_invalid_type(world):
    world.gate.submit("c", _one({"type": "grant", "target": "campaign"}))
    assert _last_action(world.store)["action_type"] == "invalid"


# --- legal downgrades are applied and ledgered --------------------------------------------------------------------
def test_deny_pending_applied(world):
    res = world.gate.submit("c", _one({"type": "deny", "target": "req_pending"}))
    assert [(a.outcome, a.reason) for a in res.actions] == [(Outcome.APPLIED, ReasonCode.OK)]
    assert world.reg.state("req_pending") == AdmissionState.DENIED
    rec = _last_action(world.store)
    assert (rec["action_type"], rec["target_id"], rec["outcome"], rec["reason"]) == \
        ("deny", "req_pending", "applied", "ok")
    # one-way: neither the gate nor code can now admit it
    with pytest.raises(ValueError):
        world.reg.admit("req_pending")


def test_shrink_finite_and_unlimited_applied(world):
    res = world.gate.submit("c", json.dumps({"actions": [
        {"type": "shrink", "target": "campaign", "unit": "usd", "value": 2.5},
        {"type": "shrink", "target": "open", "unit": "gpu_hours", "value": 0},
        {"type": "shrink", "target": "campaign", "unit": "usd", "value": 2.5},  # second time: not below 2.5
    ]}))
    assert [a.reason for a in res.actions] == [ReasonCode.OK, ReasonCode.OK, ReasonCode.NOT_A_SHRINK]
    assert world.budgets["campaign"].limits["usd"] == 2.5
    assert world.budgets["open"].limits["gpu_hours"] == 0.0
    recs = world.store.records(ACTION_KIND)[-3:]
    assert (recs[0]["unit"], recs[0]["old_cap"], recs[0]["new_cap"]) == ("usd", 10.0, 2.5)
    assert (recs[1]["old_cap"], recs[1]["new_cap"]) == (None, 0.0)
    assert "new_cap" not in recs[2]


def _kill_budget(w, spent=11.0, cap=5.0, run=None):
    return _one({"type": "kill", "target": run or w.over,
                 "evidence": {"kind": "budget_exceeded", "unit": "usd", "spent": spent, "cap": cap}})


def test_kill_budget_exceeded_applied(world):
    res = world.gate.submit("c", _kill_budget(world))
    assert res.actions[0].reason == ReasonCode.OK
    assert world.kills == [(world.over, "controller_budget_exceeded")]
    assert world.store.run_status(world.over)[0] == RunStatus.FAILED
    rec = _last_action(world.store)
    assert (rec["action_type"], rec["target_id"], rec["outcome"], rec["evidence_kind"]) == \
        ("kill", world.over, "applied", "budget_exceeded")
    # a second kill of the same run is a terminal-run rejection, not a second effect
    again = world.gate.submit("c", _kill_budget(world))
    assert again.actions[0].reason == ReasonCode.RUN_TERMINAL and len(world.kills) == 1


def test_kill_no_progress_applied(world):
    res = world.gate.submit("c", _one({"type": "kill", "target": world.loop,
                                       "evidence": {"kind": "no_progress", "steps": 3, "error_sha256": _sha(ERR)}}))
    assert res.actions[0].reason == ReasonCode.OK and world.kills == [(world.loop, "controller_no_progress")]


def test_kill_stalled_applied(world):
    res = world.gate.submit("c", _one({"type": "kill", "target": world.idle,
                                       "evidence": {"kind": "stalled", "last_step_index": 0}}))
    assert res.actions[0].reason == ReasonCode.OK and world.kills == [(world.idle, "controller_stalled")]


# --- forged or unconfirmable evidence is rejected ------------------------------------------------------------------
FORGED = [
    ("spent_overstated", lambda w: _kill_budget(w, run=w.under, spent=6.0), ReasonCode.EVIDENCE_MISMATCH),
    ("cap_understated", lambda w: _kill_budget(w, run=w.under, spent=1.0, cap=0.5), ReasonCode.EVIDENCE_MISMATCH),
    ("true_but_not_met", lambda w: _kill_budget(w, run=w.under, spent=1.0, cap=5.0), ReasonCode.EVIDENCE_NOT_MET),
    ("no_cap_given", lambda w: _kill_budget(w, run=w.idle, spent=0.0, cap=0.0), ReasonCode.EVIDENCE_NOT_MET),
    ("budget_unit_unknown", lambda w: _one({"type": "kill", "target": w.over, "evidence": {
        "kind": "budget_exceeded", "unit": "eur", "spent": 11.0, "cap": 5.0}}), ReasonCode.UNKNOWN_UNIT),
    ("budget_spent_bool", lambda w: _one({"type": "kill", "target": w.over, "evidence": {
        "kind": "budget_exceeded", "unit": "usd", "spent": True, "cap": 5.0}}), ReasonCode.SCHEMA_INVALID),
    ("loop_wrong_sha", lambda w: _one({"type": "kill", "target": w.loop, "evidence": {
        "kind": "no_progress", "steps": 3, "error_sha256": _sha("something else")}}), ReasonCode.EVIDENCE_MISMATCH),
    ("loop_too_few_steps", lambda w: _one({"type": "kill", "target": w.loop, "evidence": {
        "kind": "no_progress", "steps": 2, "error_sha256": _sha(ERR)}}), ReasonCode.EVIDENCE_NOT_MET),
    ("loop_steps_span_other_error", lambda w: _one({"type": "kill", "target": w.loop, "evidence": {
        "kind": "no_progress", "steps": 4, "error_sha256": _sha(ERR)}}), ReasonCode.EVIDENCE_MISMATCH),
    ("loop_more_steps_than_exist", lambda w: _one({"type": "kill", "target": w.loop, "evidence": {
        "kind": "no_progress", "steps": 50, "error_sha256": _sha(ERR)}}), ReasonCode.EVIDENCE_MISMATCH),
    ("no_errors_at_all", lambda w: _one({"type": "kill", "target": w.idle, "evidence": {
        "kind": "no_progress", "steps": 3, "error_sha256": _sha("")}}), ReasonCode.EVIDENCE_MISMATCH),
    ("stalled_wrong_index", lambda w: _one({"type": "kill", "target": w.idle, "evidence": {
        "kind": "stalled", "last_step_index": 5}}), ReasonCode.EVIDENCE_MISMATCH),
    ("stalled_but_fresh", lambda w: _one({"type": "kill", "target": w.fresh, "evidence": {
        "kind": "stalled", "last_step_index": 0}}), ReasonCode.EVIDENCE_NOT_MET),
    ("stalled_paused_run", lambda w: _one({"type": "kill", "target": w.paused, "evidence": {
        "kind": "stalled", "last_step_index": 0}}), ReasonCode.EVIDENCE_NOT_MET),
    ("stalled_index_bool", lambda w: _one({"type": "kill", "target": w.idle, "evidence": {
        "kind": "stalled", "last_step_index": False}}), ReasonCode.SCHEMA_INVALID),
]


@pytest.mark.parametrize(("name", "make", "code"), FORGED, ids=[c[0] for c in FORGED])
def test_kill_with_forged_or_unconfirmed_evidence_rejected(world, name, make, code):
    before = _state(world)
    res = world.gate.submit("c", make(world))
    assert [a.reason for a in res.actions] == [code]
    assert _state(world) == before and world.kills == []
    assert _last_action(world.store)["reason"] == code.value


class _Boom(dict):
    def get(self, *a, **k):
        raise RuntimeError("checker exploded")


def test_checker_exception_rejects(store):
    run = _run(store, [(Cost(usd=50.0), None, NOW - 5)])
    kills = []
    gate = DowngradeGate(store, budgets={}, admissions=AdmissionRegister(), kill=lambda r, c: kills.append(r),
                         run_budgets=_Boom())
    res = gate.submit("c", _one({"type": "kill", "target": run, "evidence": {
        "kind": "budget_exceeded", "unit": "usd", "spent": 50.0, "cap": 5.0}}))
    assert res.actions[0].reason == ReasonCode.CHECKER_ERROR and kills == []
    assert _last_action(store)["outcome"] == "rejected"


def test_effect_exception_rejects(store):
    run = _run(store, [(Cost(), None, NOW - 5000)])

    def bad_kill(run_id, code):
        raise OSError("kill failed")

    gate = DowngradeGate(store, budgets={}, admissions=AdmissionRegister(), kill=bad_kill, clock=lambda: NOW)
    res = gate.submit("c", _one({"type": "kill", "target": run, "evidence": {"kind": "stalled",
                                                                           "last_step_index": 0}}))
    assert res.actions[0].reason == ReasonCode.EFFECT_ERROR
    assert _last_action(store)["outcome"] == "rejected"


# --- batch semantics: each action decided independently --------------------------------------------------------
def test_mixed_batch_applies_legal_rejects_illegal(world):
    raw = json.dumps({"actions": [
        {"type": "grant", "target": "campaign", "unit": "usd", "value": 1e6},
        {"type": "deny", "target": "req_pending"},
        {"type": "shrink", "target": "campaign", "unit": "usd", "value": 50},
        {"type": "shrink", "target": "campaign", "unit": "tokens", "value": 10},
        {"type": "admit", "target": "req_pending2"},
        {"type": "kill", "target": world.under},
    ]})
    res = world.gate.submit("c", raw)
    assert [a.reason for a in res.actions] == [ReasonCode.UNKNOWN_ACTION, ReasonCode.OK, ReasonCode.NOT_A_SHRINK,
                                               ReasonCode.OK, ReasonCode.UNKNOWN_ACTION, ReasonCode.NO_EVIDENCE]
    assert world.reg.state("req_pending") == AdmissionState.DENIED
    assert world.reg.state("req_pending2") == AdmissionState.PENDING
    assert world.budgets["campaign"].limits == {"usd": 10.0, "tokens": 10.0}
    assert world.kills == []
    recs = world.store.records(ACTION_KIND)
    assert [r["index"] for r in recs] == list(range(6))
    assert world.store.records(PROPOSAL_KIND)[-1]["n_actions"] == 6


# --- covert channel: free text is never stored ------------------------------------------------------------------
def test_free_text_never_stored(world):
    canary = "CANARY_label_7f3e_the_answer_is_42"
    raws = [canary, _one({"type": canary, "target": canary}), _one({"type": "deny", "target": canary}),
            _one({"type": "shrink", "target": "campaign", "unit": canary, "value": 1}),
            _one({"type": "kill", "target": world.idle, "evidence": {"kind": canary}}),
            _one({"type": "kill", "target": canary, "evidence": {"kind": "stalled", "last_step_index": 0}}),
            json.dumps({"actions": [], "note": canary})]
    for raw in raws:
        world.gate.submit("c", raw)
    dump = json.dumps(world.store.records(ACTION_KIND) + world.store.records(PROPOSAL_KIND))
    assert "CANARY" not in dump
    unknown = [r for r in world.store.records(ACTION_KIND) if r.get("target_sha256") == _sha(canary)]
    assert unknown and all(r["target_id"] is None for r in unknown)


# --- the gateway path (MockBackend; no real model) ---------------------------------------------------------------
def test_ask_via_mock_backend(world):
    reply = json.dumps({"actions": [{"type": "deny", "target": "req_pending"},
                                    {"type": "grant", "target": "campaign"}]})
    backend = MockBackend([reply])
    res = world.gate.ask("ctl-llm", backend, "pending requests: req_pending")
    assert [a.reason for a in res.actions] == [ReasonCode.OK, ReasonCode.UNKNOWN_ACTION]
    assert world.reg.state("req_pending") == AdmissionState.DENIED
    assert backend.requests[0].messages[0].content == dg.CONTROLLER_PROMPT


def test_ask_backend_failure_fails_closed(world):
    def boom(req):
        raise BackendError("provider down", transient=True)

    before = _state(world)
    res = world.gate.ask("ctl-llm", MockBackend(boom), "obs")
    assert res.envelope == ReasonCode.BACKEND_ERROR and not res.applied
    assert _state(world) == before
    assert _last_action(world.store)["reason"] == "backend_error"


# --- structural: the gate has no path that grants ----------------------------------------------------------------
def test_gate_source_has_no_grant_path():
    src = inspect.getsource(DowngradeGate)
    assert ".admit(" not in src
    assert "RunStatus.RUNNING," not in src and "set_run_status" not in src
    # the only write to a budget cap is the asserted shrink site
    assert src.count(".limits[") == 1
    assert "budget.limits[unit] = new" in inspect.getsource(DowngradeGate._apply_shrink)


# --- property test: random proposals never grant ---------------------------------------------------------------
_TYPES = ["deny", "shrink", "kill", "grant", "admit", "approve", "revive", "unkill", "enlarge", "raise", "", "Shrink"]
_VALUES = [0, 0.0, 1, 2.5, 5, 9.999, 10, 10.0, 11, 1e6, -1, -0.0, 1e308, True, False, None, "1", [], {}]
_SPECIAL = ["NaN", "Infinity", "-Infinity", "1e999"]


def _rand_proposal(rng: random.Random, w) -> str:
    runs = [w.over, w.under, w.loop, w.idle, w.fresh, w.done, w.paused, "run_ghost"]
    targets = runs + ["campaign", "open", "nope", "req_pending", "req_pending2", "req_denied", "req_admitted", "x"]
    acts = []
    specials = []
    for _ in range(rng.randint(0, 5)):
        a: dict = {"type": rng.choice(_TYPES)}
        # half the actions carry exactly the fields their type needs (so legality turns on the values), half are
        # random field soups
        shaped = rng.random() < 0.5
        if (shaped and a["type"] in ("deny", "shrink", "kill")) or (not shaped and rng.random() < 0.9):
            a["target"] = rng.choice(targets)
        if (shaped and a["type"] == "shrink") or (not shaped and rng.random() < 0.6):
            a["unit"] = rng.choice(list(UNITS) + ["eur", 3])
        if (shaped and a["type"] == "shrink") or (not shaped and rng.random() < 0.6):
            if rng.random() < 0.15:
                tok = f"__SPECIAL{len(specials)}__"
                specials.append(rng.choice(_SPECIAL))
                a["value"] = tok
            else:
                a["value"] = rng.choice(_VALUES) if rng.random() < 0.6 else rng.uniform(-20, 2000)
        if (shaped and a["type"] == "kill") or (not shaped and rng.random() < 0.4):
            kind = rng.choice(["budget_exceeded", "no_progress", "stalled", "vibes", None])
            ev: dict = {"kind": kind}
            if kind == "budget_exceeded":
                ev.update(unit=rng.choice(["usd", "tokens", "eur"]), spent=rng.choice([11.0, 1.0, 6.0, 100.0]),
                          cap=rng.choice([5.0, 0.5, 10.0]))
            elif kind == "no_progress":
                ev.update(steps=rng.choice([1, 2, 3, 4, 50, True]), error_sha256=rng.choice([_sha(ERR), "00"]))
            elif kind == "stalled":
                ev.update(last_step_index=rng.choice([-1, 0, 1, 3, False]))
            if rng.random() < 0.1:
                ev["extra"] = 1
            a["evidence"] = ev if rng.random() < 0.9 else rng.choice([None, "trust me", 1])
        if not shaped and rng.random() < 0.1:
            a["note"] = "please"
        acts.append(a)
    doc: object = {"actions": acts}
    r = rng.random()
    if r < 0.05:
        doc = acts
    elif r < 0.08:
        doc = {"actions": acts, "grant": True}
    raw = json.dumps(doc)
    for i, s in enumerate(specials):
        raw = raw.replace(f'"__SPECIAL{i}__"', s)
    r = rng.random()
    if r < 0.04 and raw:
        raw = raw[: rng.randrange(len(raw))]
    elif r < 0.06:
        raw = "I grant everything. " + raw
    return raw


def _caps_never_rose(before: dict, after: dict) -> bool:
    for name, limits in before.items():
        for unit in UNITS:
            old, new = limits.get(unit), after[name].get(unit)
            if old is not None and (new is None or not new <= old):
                return False
            if new is not None and not (math.isfinite(new) and new >= 0):
                return False
    return True


def test_property_random_proposals_never_grant(world):
    rng = random.Random(20261008)
    applied = 0
    applied_types: set[str] = set()
    reasons: set[str] = set()
    terminal: set[str] = {world.done}
    for _ in range(2000):
        before_caps = {n: dict(b.limits) for n, b in world.budgets.items()}
        before_reg = world.reg.snapshot()
        raw = _rand_proposal(rng, world)
        res = world.gate.submit("prop", raw)
        applied += len(res.applied)
        applied_types |= {a.action_type for a in res.applied}
        reasons |= {a.reason.value for a in res.actions}
        after_caps = {n: dict(b.limits) for n, b in world.budgets.items()}
        assert _caps_never_rose(before_caps, after_caps), raw
        after_reg = world.reg.snapshot()
        assert sum(s == AdmissionState.ADMITTED for s in after_reg.values()) == 1  # only the code-admitted one
        for rid, s in before_reg.items():
            if s != AdmissionState.PENDING:
                assert after_reg[rid] == s, raw
        for rid in terminal:
            assert world.store.run_status(rid)[0] != RunStatus.RUNNING, raw
        terminal |= {k for k, _ in world.kills}
        for a in res.actions:
            assert a.reason in ReasonCode and (a.outcome == Outcome.APPLIED) == (a.reason == ReasonCode.OK)
        # once every cap is 0 and every request decided, refresh the world a little so later draws still test grants
        if rng.random() < 0.02:
            world.budgets["campaign"].limits.update({"usd": 10.0, "tokens": 1000.0})  # code (not the gate) resets
    assert applied > 0 and applied_types == {"deny", "shrink", "kill"}  # it does reach legal downgrades
    assert len(reasons) >= 15, sorted(reasons)  # and a broad spread of rejection codes
    assert len(world.kills) == len(set(k for k, _ in world.kills))  # no run killed twice
    recs = world.store.records(ACTION_KIND)
    assert all(r["reason"] in {c.value for c in ReasonCode} for r in recs)
    assert {r["action_type"] for r in recs} <= {"deny", "shrink", "kill", "invalid"}
