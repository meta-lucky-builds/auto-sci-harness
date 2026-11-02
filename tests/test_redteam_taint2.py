"""Red team round 2 (w2-taint, after the F1-F11 fixes): more ways to launder a target-derived value past the labels.

Same conventions as test_redteam_taint.py: handled attacks are plain tests; an attack that gets through is a strict
xfail whose reason names the mechanism, and asserts first that the attack is LIVE.
"""

from __future__ import annotations

import collections
from concurrent.futures import ThreadPoolExecutor

import pytest

from custom_harness.errors import InfraError
from custom_harness.records import Run, RunStatus
from custom_harness.runtime import Engine, register
from custom_harness.runtime.engine import RetryPolicy
from test_redteam_taint import COPY, _mock, _mod, _prompts
from test_taint_flow import HELD, _ask, _held_mean, _prompt_releases, _run, _spec


# ----------------------------------------------------------------------------------------- more bulk idioms
@register("rt2.counter")
def _counter(state, params, ctx):
    return {"derived": str(collections.Counter(state)["raw"])}


@register("rt2.update_from")
def _update_from(state, params, ctx):
    d: dict = {}
    d.update(state)
    return {"derived": str(d["raw"])}


@pytest.mark.parametrize("mod", ["rt2.counter", "rt2.update_from"])
def test_counter_and_dict_update_from_the_state_view_are_bulk_reads(store, mod):
    _res, llm = _run(store, {"held": HELD, "d": _mod(mod), "ask": _ask("D: {{derived}}")}, ["held", "d", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store) == [("continuous", "prompt:ask:derived")]


# ----------------------------------------------------------------------------------------- retried module
_attempts = {"n": 0}


@register("rt2.write_then_transient")
def _write_then_transient(state, params, ctx):
    """Attempt 1 writes the value directly, then hits a TRANSIENT infra error (retried); attempt 2 writes nothing."""
    _attempts["n"] += 1
    if _attempts["n"] == 1:
        state["leak"] = f"{state['raw']['mean']}"
        raise InfraError("sandbox daemon hiccup", transient=True)
    return {}


def test_a_direct_write_of_a_retried_attempt_is_released(store):
    _attempts["n"] = 0
    spec = _spec({"held": HELD, "w": _mod("rt2.write_then_transient"), "ask": _ask("L: {{leak}}")},
                 ["held", "w", "ask"])
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store,
                 retry=RetryPolicy(max_attempts=2, backoff_s=0.0)).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store) == [("continuous", "prompt:ask:leak")]


# ----------------------------------------------------------------------------------------- tombstones are not sticky
@register("rt2.delete_copied")
def _delete_copied(state, params, ctx):
    if state["raw"]["mean"] > 0.5:
        del state["copied"]
    return {}


@register("rt2.public_copied")
def _public_copied(state, params, ctx):
    return {"copied": "public"}


def test_a_tombstone_is_replaced_by_a_clean_rewrite(store):
    """Precision: a key deleted on a target condition is labelled while absent; rewritten later by a step that read
    nothing labelled, it is clean again (no release)."""
    _run(store, {"held": HELD, "copy": COPY, "del": _mod("rt2.delete_copied"), "pub": _mod("rt2.public_copied"),
                 "ask": _ask("C: {{copied}}")}, ["held", "copy", "del", "pub", "ask"])
    assert _prompt_releases(store) == []


# ----------------------------------------------------------------------------------------- worker threads
@register("rt2.threadpool_other_run")
def _threadpool_other_run(state, params, ctx):
    """A private read made on a WORKER thread (the context, so the engine's capture, is not copied) naming another,
    known run."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        mean = ex.submit(_held_mean, ctx, run_id=params["other"]).result()
    return {"note": f"{mean}"}


@pytest.mark.xfail(strict=True, reason=(
    "G1 (stated limit, docs/taint.md): a private read made on a worker thread that does not copy the context, naming "
    "another KNOWN run, is attributed by the run id it names (the engine's ledger observer captures only reads made "
    "in the step's own context); same class as kerneld reads and nested runs on another thread"))
def test_G1_worker_thread_read_naming_another_known_run(store):
    other = Run(harness_id="h", harness_hash="x", task_id="t")
    store.add_run(other)
    _res, llm = _run(store, {"r": _mod("rt2.threadpool_other_run", other=other.id), "ask": _ask("N: {{note}}")},
                     ["r", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store), "a target-derived value reached a prompt with no release"


@register("rt2.threadpool_self")
def _threadpool_self(state, params, ctx):
    with ThreadPoolExecutor(max_workers=1) as ex:
        mean = ex.submit(_held_mean, ctx).result()
    return {"note": f"{mean}"}


def test_a_worker_thread_read_naming_this_run_is_released(store):
    _res, llm = _run(store, {"r": _mod("rt2.threadpool_self"), "ask": _ask("N: {{note}}")}, ["r", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store) == [("continuous", "prompt:ask:note")]
