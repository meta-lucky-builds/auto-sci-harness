"""fork(step, state_patch) (GOAL §5 L0 Runtime): a new run from a checkpoint with a patch; the patch may not touch
reserved or `_private` keys; forks are ledgered; budgets carry over without double counting."""

import pytest

from custom_harness.budget import Budget
from custom_harness.gateway import ChatRequest, Message, MockBackend
from custom_harness.kernel.ledger import KINDS, Ledger
from custom_harness.records import Cost, RunStatus
from custom_harness.runtime import Edge, Engine, ForkRefused, HarnessSpec, LLMNode, ModuleNode, register


def _tokens(c: Cost) -> int:
    return c.prompt_tokens + c.completion_tokens


@register("test.fork_secret")
def _secret(state, params, ctx):
    ctx.llm("executor", ChatRequest(messages=[Message(role="user", content="pick a topic please")]))
    return {"_private_answer": "the hidden label is XYZZY-PLUGH-42", "topic": "cats"}


def _spec():
    return HarnessSpec(id="fk", entry="s", nodes={
        "s": ModuleNode(module="test.fork_secret"),
        "a": LLMNode(prompt="write about {{topic}}", output_key="essay")},
        edges=[Edge(source="s", target="a")])


def _echo():
    return {"executor": MockBackend(lambda req: "ESSAY " + req.messages[-1].content)}


def _fork_records(store):
    if "fork" in KINDS:
        return [e.body for e in Ledger(store).entries("fork")]
    return store.records("fork")


def test_fork_applies_the_patch_and_is_ledgered(store):
    eng = Engine(_spec(), backends=_echo(), store=store)
    parent = eng.run("t", {})
    child = Engine(_spec(), backends=_echo(), store=store).fork(parent.run_id, 0, state_patch={"topic": "dogs"})
    assert child.status == RunStatus.COMPLETED
    assert child.state["essay"] == "ESSAY write about dogs" and parent.state["essay"] == "ESSAY write about cats"
    [rec] = _fork_records(store)
    assert rec["parent_run_id"] == parent.run_id and rec["child_run_id"] == child.run_id and rec["step"] == 0
    assert rec["patch_keys"] == ["topic"] and len(rec["patch_sha256"]) == 64
    assert child.state["_forked_from"]["patch_sha256"] == rec["patch_sha256"]
    assert store.get_run(child.run_id).forked_from == (parent.run_id, 0)


@pytest.mark.parametrize("patch,why", [
    ({"_private_answer": "x"}, "private"), ({"_last_error": None}, "private"), ({"_seed": 7}, "private"),
    ({"review_approval": {"decision": "approved"}}, "approval"), ({"gate_decision": "approved"}, "approval"),
    ({"input": {}}, "engine"), ({"model_policy": {}}, "engine"),
    ({"claim": {}}, "reserved"), ({"certificate": {}}, "reserved"), ({"audit": {"ok": True}}, "reserved"),
    ({"publishable": True}, "reserved"),
    ({"topic.sub": 1}, "top-level"), ({"topic": object()}, "not JSON"),
    ({"topic": "leak: the hidden label is XYZZY-PLUGH-42"}, "private value"),
    ({"topic": {"nested": ["the hidden label is XYZZY-PLUGH-42"]}}, "private value")])
def test_fork_patch_touching_reserved_or_private_state_is_refused(store, patch, why):
    eng = Engine(_spec(), backends=_echo(), store=store)
    parent = eng.run("t", {})
    n_runs = len(store.list_runs(100))
    with pytest.raises(ForkRefused):
        Engine(_spec(), backends=_echo(), store=store).fork(parent.run_id, 0, state_patch=patch)
    assert len(store.list_runs(100)) == n_runs and _fork_records(store) == []  # nothing written


def test_fork_budget_carries_over_without_double_counting(store):
    eng = Engine(_spec(), backends=_echo(), store=store)
    parent = eng.run("t", {})
    prefix = sum((s.cost for s in store.trace(parent.run_id) if s.index <= 0), Cost())
    # engine-owned budget: starts at the parent's spend up to the step (cannot be reset by forking)
    e2 = Engine(_spec(), backends=_echo(), store=store)
    c1 = e2.fork(parent.run_id, 0, state_patch={"topic": "dogs"})
    own = store.trace(c1.run_id)
    assert _tokens(e2.budget.spent) == _tokens(prefix) + _tokens(sum((s.cost for s in own), Cost()))
    assert [s.node_id for s in own] == ["a"]  # the child's trace holds only its own steps (prefix not re-recorded)
    [rec] = _fork_records(store)
    assert _tokens(Cost.model_validate(rec["carried_spent"])) == _tokens(prefix)
    # a shared (campaign) budget that already holds the parent's spend: the prefix is not added a second time
    shared = Budget(limits={"tokens": 10_000})
    shared.charge(parent.cost)
    before = _tokens(shared.spent)
    e3 = Engine(_spec(), backends=_echo(), store=store, budget=shared)
    c2 = e3.fork(parent.run_id, 0, state_patch={"topic": "owls"})
    assert _tokens(shared.spent) == before + _tokens(c2.cost)
    # a fresh shared budget is raised to the carried spend (a fork never starts below it)
    fresh = Budget(limits={"tokens": 10_000})
    c3 = Engine(_spec(), backends=_echo(), store=store, budget=fresh).fork(parent.run_id, 0)
    assert _tokens(fresh.spent) == _tokens(prefix) + _tokens(c3.cost)


def test_fork_budget_cap_counts_the_carried_spend(store):
    eng = Engine(_spec(), backends=_echo(), store=store)
    parent = eng.run("t", {})
    prefix = _tokens(store.trace(parent.run_id)[0].cost)
    assert prefix > 0
    tight = Budget(limits={"tokens": prefix})  # the carried spend alone reaches the cap
    res = Engine(_spec(), backends=_echo(), store=store, budget=tight).fork(parent.run_id, 0,
                                                                         state_patch={"topic": "dogs"})
    assert res.status == RunStatus.BUDGET_EXCEEDED and store.trace(res.run_id) == []
