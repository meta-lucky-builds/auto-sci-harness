"""Red team: try to launder a target-derived value past the engine's taint labels (runtime/taint.py, docs/taint.md)
to an agent-visible surface (an LLM node prompt, a tool command, a module's own model call, the evidence export)
WITHOUT a release (or, for the export, without being withheld).

Handled attacks are plain tests. Attacks that get through are `xfail(strict=True)` with the assertion that holds once
fixed; each reason names the mechanism. Every xfail test first asserts the attack is LIVE (the value or the
target-dependent difference really reached the surface), so it cannot pass vacuously.

The held-out target is `tf-held` (test_taint_flow.py): private labels 1,0,1,1, mean 0.75.
"""

from __future__ import annotations

import collections
import copy
import json
import operator
import pickle
import pprint
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from custom_harness.errors import InfraError
from custom_harness.gateway import MockBackend
from custom_harness.kernel.ledger import Ledger
from custom_harness.records import Run, RunStatus
from custom_harness.runtime import Engine, HarnessSpec, register
from custom_harness.runtime.engine import RetryPolicy, approval_item_hash
from custom_harness.sandbox import LocalSandbox
from test_taint_flow import HELD, _ask, _evolve_run, _held_mean, _prompt_releases, _run, _spec

COPY = {"kind": "module", "module": "tf.copy"}  # copied = raw.mean (0.75), tainted
PUB = {"kind": "module", "module": "tf.public"}  # box = {"note": "public"}, untainted


def _mod(name: str, **params) -> dict:
    return {"kind": "module", "module": name, "params": params}


def _graph(nodes: dict, edges: list[tuple], entry: str) -> HarnessSpec:
    es = []
    for e in edges:
        d = {"source": e[0], "target": e[1]}
        if len(e) > 2:
            d["when"] = e[2]
        es.append(d)
    return HarnessSpec.model_validate({"id": "rt", "entry": entry, "nodes": nodes, "edges": es})


def _mock() -> MockBackend:
    return MockBackend(lambda req: "ok")


def _prompts(llm) -> list[str]:
    return [r.messages[-1].content for r in llm.requests]


# ===================================================================================================== HANDLED
# ----------------------------------------------------------------------------------------- bulk/idiomatic reads
_IDIOMS = {
    "dict": lambda s: str(dict(s)),
    "splat": lambda s: str({**s}),
    "kwargs": lambda s: (lambda **kw: str(kw["raw"]["mean"]))(**s),
    "json_dumps": lambda s: json.dumps(s, sort_keys=True, default=str),
    "deepcopy": lambda s: str(copy.deepcopy(s)["raw"]),
    "copy_copy": lambda s: str(copy.copy(s)["raw"]),
    "items_loop": lambda s: ";".join(f"{k}={v}" for k, v in s.items()),
    "values": lambda s: str(list(s.values())),
    "str": lambda s: str(s),
    "fstring": lambda s: f"{s}",
    "pformat": lambda s: pprint.pformat(s),
    "pickle": lambda s: str(pickle.loads(pickle.dumps(s))["raw"]),
    "format_map": lambda s: "{raw[mean]}".format_map(s),
    "get_default": lambda s: str(s.get("raw", {"mean": -1})["mean"]),
    "chainmap": lambda s: str(collections.ChainMap(s)["raw"]),
    "proxy": lambda s: str(MappingProxyType(s)["raw"]),
    "itemgetter": lambda s: str(operator.itemgetter("raw")(s)),
    "helper": lambda s: _helper_reads_raw(s),
    "sorted_keys": lambda s: str([s[k] for k in sorted(s)]),
    "or_merge": lambda s: str(s | {}),
}


def _helper_reads_raw(st) -> str:
    return f"{st['raw']['mean']:.2f}"


@register("rt.idiom")
def _idiom(state, params, ctx):
    return {"derived": _IDIOMS[params["idiom"]](state)}


@pytest.mark.parametrize("idiom", sorted(_IDIOMS))
def test_bulk_and_indirect_state_reads_are_released(store, idiom):
    _res, llm = _run(store, {"held": HELD, "d": _mod("rt.idiom", idiom=idiom), "ask": _ask("D: {{derived}}")},
                    ["held", "d", "ask"])
    assert "0.75" in _prompts(llm)[0]  # live: the target-derived value reached the prompt
    assert _prompt_releases(store) == [("continuous", "prompt:ask:derived")]


@register("rt.nested_copy")
def _nested_copy(state, params, ctx):
    return {"wrap": {"deep": [{"x": state["raw"]["mean"]}], "public": "p"}}


def test_copy_into_nested_structure_rendered_by_subpath_is_released(store):
    _run(store, {"held": HELD, "n": _mod("rt.nested_copy"), "ask": _ask("{{wrap.deep.0.x}} / {{wrap.public}}")},
         ["held", "n", "ask"])
    assert _prompt_releases(store) == [("continuous", "prompt:ask:wrap.deep.0.x"),
                                       ("continuous", "prompt:ask:wrap.public")]


# ----------------------------------------------------------------------------------------- store / blobs
@register("rt.blob_put")
def _blob_put(state, params, ctx):
    return {"ref": ctx.store.blobs.put(str(state["raw"]["mean"]).encode())}


@register("rt.blob_get")
def _blob_get(state, params, ctx):
    """Reads only the (hash) ref key... and the store."""
    return {"back": ctx.store.blobs.get(state["ref"]).decode()}


def test_blob_round_trip_through_the_store_is_released(store):
    _run(store, {"held": HELD, "put": _mod("rt.blob_put"), "pub": PUB, "get": _mod("rt.blob_get"),
                 "ask": _ask("B: {{back}}")}, ["held", "put", "pub", "get", "ask"])
    assert ("continuous", "prompt:ask:back") in _prompt_releases(store)


# ----------------------------------------------------------------------------------------- _last_error
@register("rt.raise_value")
def _raise_value(state, params, ctx):
    raise ValueError(f"bad mean {state['raw']['mean']}")


def test_exception_message_carrying_the_value_rendered_as_last_error_is_released(store):
    spec = _graph({"held": HELD, "boom": _mod("rt.raise_value"), "ask": _ask("E: {{_last_error}}")},
                  [("held", "boom"), ("boom", "ask", {"key": "_last_error", "op": "truthy"})], "held")
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert "0.75" in _prompts(llm)[0]
    assert ("continuous", "prompt:ask:_last_error") in _prompt_releases(store)


# ----------------------------------------------------------------------------------------- module model calls
@register("rt.nested_engine")
def _nested_engine(state, params, ctx):
    """Starts a nested run (same thread) on the module's own backends, passing the value as the nested run's INPUT
    (unlabelled inside the nested engine)."""
    sub = HarnessSpec.model_validate({"id": "rt-sub", "entry": "a", "edges": [],
                                      "nodes": {"a": {"kind": "llm", "prompt": "X={{input.x}}", "output_key": "o"}}})
    res = Engine(sub, backends=ctx.backends, store=ctx._store).run("sub", {"x": state["copied"]})
    return {"sub_status": res.status.value}


def test_nested_engine_using_ctx_backends_is_released_by_the_outer_frame(store):
    _res, llm = _run(store, {"held": HELD, "copy": COPY, "nest": _mod("rt.nested_engine")}, ["held", "copy", "nest"])
    assert "X=0.75" in _prompts(llm)[0]
    # the copied key it read, plus the store it touched (ctx._store, to start the nested engine); and (since the H9
    # fix) the nested run's own prompt, whose input carries the outer step's label
    assert sorted(n for _a, n in _prompt_releases(store)) == ["module_llm:nest:_store", "module_llm:nest:copied",
                                                              "prompt:a:input.x"]


# ----------------------------------------------------------------------------------------- tool node / sandbox
def _sandbox(tmp_path: Path) -> LocalSandbox:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    return LocalSandbox(ws, i_understand_no_isolation=True)


CAT = {"kind": "tool", "command": [sys.executable, "-c", "print(open('f.txt').read())"], "output_key": "tout"}


@register("rt.ws_write_ctx")
def _ws_write_ctx(state, params, ctx):
    (Path(ctx.sandbox.workspace) / "f.txt").write_text(f"{state['raw']['mean']}", encoding="utf-8")
    return {}


def _run_sb(store, tmp_path, nodes, order):
    llm = _mock()
    res = Engine(_spec(nodes, order), backends={"executor": llm}, store=store, sandbox=_sandbox(tmp_path)).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    return res, llm


def test_workspace_file_written_through_ctx_sandbox_then_catted_is_released(store, tmp_path):
    _res, llm = _run_sb(store, tmp_path, {"held": HELD, "w": _mod("rt.ws_write_ctx"), "cat": CAT,
                                         "ask": _ask("T: {{tout.stdout}}")}, ["held", "w", "cat", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert ("continuous", "prompt:ask:tout.stdout") in _prompt_releases(store)


def test_tool_command_rendering_a_tainted_key_is_released(store, tmp_path):
    echo = {"kind": "tool", "command": [sys.executable, "-c", "import sys; print(sys.argv[1])", "{{copied}}"],
            "output_key": "tout"}
    _run_sb(store, tmp_path, {"held": HELD, "copy": COPY, "echo": echo}, ["held", "copy", "echo"])
    assert _prompt_releases(store) == [("continuous", "prompt:echo:copied")]


# ----------------------------------------------------------------------------------------- resume / fork
_infra = {"n": 0}


@register("rt.write_then_infra")
def _write_then_infra(state, params, ctx):
    """A DIRECT state write of the value, then an infrastructure failure (the step's outputs are not applied)."""
    _infra["n"] += 1
    if _infra["n"] == 1:
        state["leak"] = f"{state['raw']['mean']}"
        raise InfraError("sandbox daemon went away", transient=False)
    return {"after": True}


def test_direct_write_then_infra_failure_then_fresh_engine_resume_is_released(store):
    _infra["n"] = 0
    spec = _spec({"held": HELD, "w": _mod("rt.write_then_infra"), "ask": _ask("L: {{leak}}")}, ["held", "w", "ask"])
    first = Engine(spec, backends={"executor": _mock()}, store=store,
                   retry=RetryPolicy(max_attempts=1, backoff_s=0.0)).run("t", {})
    assert first.status == RunStatus.INFRA_FAILED
    llm = _mock()
    done = Engine(spec, backends={"executor": llm}, store=store).resume(first.run_id)
    assert done.status == RunStatus.COMPLETED, done.detail
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store) == [("continuous", "prompt:ask:leak")]


def test_fork_with_an_unrelated_state_patch_keeps_the_copy_labelled(store):
    spec = _spec({"held": HELD, "copy": COPY, "pub": PUB, "ask": _ask("S: {{copied}}")}, ["held", "copy", "pub", "ask"])
    first = Engine(spec, backends={"executor": _mock()}, store=store).run("t", {})
    assert first.status == RunStatus.COMPLETED
    before = len(_prompt_releases(store))
    child = Engine(spec, backends={"executor": _mock()}, store=store).fork(first.run_id, 1, {"extra": "x"})
    assert child.status == RunStatus.COMPLETED, child.detail
    assert len(_prompt_releases(store)) == before + 1


# ----------------------------------------------------------------------------------------- map / fan-out
@register("rt.tainted_items")
def _tainted_items(state, params, ctx):
    return {"listing": ["x"] * int(state["raw"]["mean"] * 4)}  # 3 items: the COUNT is target-derived


def test_map_over_tainted_items_rendering_the_item_is_released(store):
    nodes = {"held": HELD, "items": _mod("rt.tainted_items"),
             "fan": {"kind": "map", "items_key": "listing", "body": "body", "output_key": "fanned",
                     "reducer": "collect"}, "body": _ask("I: {{item}}")}
    _run(store, nodes, ["held", "items", "fan"])
    assert [n for _a, n in _prompt_releases(store)] == ["prompt:body:item"] * 3


# ----------------------------------------------------------------------------------------- approval
def test_routing_on_an_approval_of_a_tainted_item_is_released(store):
    nodes = {"held": HELD, "copy": COPY,
             "review": {"kind": "approval", "item_key": "copied", "prompt": "ok?"},
             "hi": _ask("approved path"), "lo": _ask("rejected path")}
    spec = _graph(nodes, [("held", "copy"), ("copy", "review"),
                          ("review", "hi", {"key": "review_decision", "op": "eq", "value": "approved"}),
                          ("review", "lo")], "held")
    first = Engine(spec, backends={"executor": _mock()}, store=store).run("t", {})
    assert first.status == RunStatus.PAUSED, first.detail
    Ledger(store).append("approval", {"run_id": first.run_id, "node_id": "review", "decision": "approved",
                                      "item_hash": approval_item_hash(first.state["copied"]), "approver": "alice"})
    llm = _mock()
    done = Engine(spec, backends={"executor": llm}, store=store).resume(first.run_id)
    assert done.status == RunStatus.COMPLETED, done.detail
    assert _prompts(llm) == ["approved path"]
    assert ("verdict_bit", "control:review:hi") in _prompt_releases(store)


# ----------------------------------------------------------------------------------------- error edges (good order)
@register("rt.raise_if_low")
def _raise_if_low(state, params, ctx):
    if state["raw"]["mean"] < params.get("cut", 0.5):
        raise ValueError("low")
    return {}


def test_error_edge_listed_first_is_a_released_control_decision(store):
    """Edge order [error edge, unconditional]: both outcomes consult `_last_error`, so the decision is released."""
    nodes = {"held": HELD, "chk": _mod("rt.raise_if_low"), "ok": _ask("fine"), "err": _ask("it failed")}
    spec = _graph(nodes, [("held", "chk"), ("chk", "err", {"key": "_last_error", "op": "truthy"}), ("chk", "ok")],
                  "held")
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert _prompts(llm) == ["fine"]
    assert _prompt_releases(store) == [("verdict_bit", "control:chk:ok")]


# ----------------------------------------------------------------------------------------- reduced alphabets
@register("rt.declare")
def _declare(state, params, ctx):
    _ = state["raw"]
    return {"brief": {"text": "replicate on dataset B"}, "_release_alphabets": {"brief": "verdict_bit"}}


@register("rt.copy_brief")
def _copy_brief(state, params, ctx):
    return {"brief2": state["brief"]}


def test_a_copy_of_an_allowlisted_key_does_not_inherit_its_reduced_alphabet(store, monkeypatch):
    import custom_harness.runtime.engine as E
    monkeypatch.setattr(E, "RELEASE_ALPHABET_ALLOWLIST", {**E.RELEASE_ALPHABET_ALLOWLIST,
                                                          ("rt.declare", "brief"): "verdict_bit"})
    _run(store, {"held": HELD, "dec": _mod("rt.declare"), "cp": _mod("rt.copy_brief"),
                 "ask": _ask("{{brief}} {{brief2}}")}, ["held", "dec", "cp", "ask"])
    assert _prompt_releases(store) == [("verdict_bit", "prompt:ask:brief"), ("continuous", "prompt:ask:brief2")]


# ===================================================================================================== FINDINGS
# ----------------------------------------------------------------------------------------- F1: returning `state`
@register("rt.passthrough")
def _passthrough(state, params, ctx):
    """The pass-through idiom: stamp the state and hand it back (no key is ever READ through the TrackedState
    before the engine computes the step label)."""
    state["stamp"] = "seen"
    if params.get("wrap"):
        return {"snap": state}
    return state


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F1: a module that RETURNS its TrackedState (`return state`, or `{'snap': state}`) without reading a key: the engine
# computes the step label in _execute's finally (_finish_module) BEFORE it iterates/deep-copies the returned object,
# so the bulk read that copy triggers is never seen; every returned key is then relabelled with the empty step label
# (`return state` CLEARS the label of every tainted key) and the next prompt renders it unreleased
@pytest.mark.parametrize("wrap,prompt", [(False, "S: {{copied}}"), (True, "S: {{snap.copied}}")])
def test_F1_returning_the_tracked_state_launders_every_label(store, wrap, prompt):
    _res, llm = _run(store, {"held": HELD, "copy": COPY, "pass": _mod("rt.passthrough", wrap=wrap),
                            "ask": _ask(prompt)}, ["held", "copy", "pass", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store), "a target-derived value reached a prompt with no release"


@register("tf_rt.export_passthrough")
def _export_passthrough(state, params, ctx):
    return state


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F1 (export): the pass-through step's trace output is the whole state (incl. the held-out mean) but its step label is
# empty, so export_evidence exports it un-withheld
def test_F1_export_leaks_the_passthrough_step_output(store, tmp_path):
    from custom_harness.evolve.export import export_evidence
    out, task = _evolve_run(store, tmp_path, {"held": {"kind": "module", "module": "tf.export_held"},
                                               "pass": {"kind": "module", "module": "tf_rt.export_passthrough"}},
                            ["held", "pass"])
    assert out.status == "completed"
    res = export_evidence(store, tmp_path / "exp", runs=[out], tasks=[task])
    text = "".join(p.read_text(encoding="utf-8") for p in res.path.rglob("*.json"))
    assert "traces/" in " ".join(res.files)
    assert "0.5714285714" not in text, "a continuous held-out value reached the evolver's export"


# ----------------------------------------------------------------------------------------- F2/F3: aliasing
@register("rt.alias_out")
def _alias_out(state, params, ctx):
    shared: list = []
    return {"hist": shared, "hist_view": shared}  # copy.deepcopy(out) keeps the two keys ONE object


@register("rt.alias_direct")
def _alias_direct(state, params, ctx):
    state["hist_view"] = state["hist"]  # a direct write stores the very object (no copy)
    return {}


@register("rt.hist_init")
def _hist_init(state, params, ctx):
    return {"hist": []}


@register("rt.append_hist")
def _append_hist(state, params, ctx):
    state["hist"].append(state["raw"]["mean"])  # in place: `hist` is detected and labelled ...
    return {}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F2/F3: two state keys can alias ONE object: engine.py `state.update(copy.deepcopy(out))` preserves aliasing between
# returned keys (deepcopy's memo; the comment 'no two keys share an object' is false), and a direct write `state['b']
# = state['a']` (_finish_module stores the object, uncopied) shares it with the real state. A later in-place mutation
# through one key is fingerprinted and labelled only for THAT key; the alias changes too and stays unlabelled
@pytest.mark.parametrize("setup", [["mk"], ["init", "mk"]], ids=["returned_alias", "direct_write_alias"])
def test_F2_aliased_keys_launder_an_in_place_mutation(store, setup):
    mk = "rt.alias_out" if setup == ["mk"] else "rt.alias_direct"
    nodes = {"init": _mod("rt.hist_init"), "mk": _mod(mk), "held": HELD, "app": _mod("rt.append_hist"),
             "ask": _ask("H: {{hist_view}}")}
    _res, llm = _run(store, nodes, [*setup, "held", "app", "ask"])
    # fixed by breaking the alias (each returned value and each direct write is copied on its own): the in-place
    # append through `hist` no longer reaches `hist_view` (at 2d661e6 the prompt was "H: [0.75]", unreleased)
    assert _prompts(llm)[0] == "H: []"
    assert _prompt_releases(store) == []


@register("rt.hist_pair")
def _hist_pair(state, params, ctx):
    return {"hist": [], "hist_view": []}


def test_F2_fork_patch_values_are_not_aliased(store):
    """Generalised from F2: `fork` applied the patch with ONE deepcopy of the patch dict, so a caller passing the same
    object under two keys created an alias in the child's state."""
    spec = _spec({"pair": _mod("rt.hist_pair"), "held": HELD, "app": _mod("rt.append_hist"),
                  "ask": _ask("H: {{hist_view}}")}, ["pair", "held", "app", "ask"])
    first = Engine(spec, backends={"executor": _mock()}, store=store).run("t", {})
    assert first.status == RunStatus.COMPLETED, first.detail
    shared: list = []
    llm = _mock()
    child = Engine(spec, backends={"executor": llm}, store=store).fork(first.run_id, 0,
                                                                       {"hist": shared, "hist_view": shared})
    assert child.status == RunStatus.COMPLETED, child.detail
    assert _prompts(llm)[0] == "H: []"


@register("rt.alias_brief")
def _alias_brief(state, params, ctx):
    state["brief_alias"] = state["brief"]
    return {}


@register("rt.poison_alias")
def _poison_alias(state, params, ctx):
    state["brief_alias"]["text"] = f"mean={state['raw']['mean']}"
    return {}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F2 + allowlist: an alias of an allowlisted reduced-alphabet key (direct write) mutated in place with a continuous
# value: the allowlisted key keeps its `verdict_bit` declaration while its content now carries it
def test_F2_alias_keeps_a_reduced_alphabet_on_continuous_content(store, monkeypatch):
    import custom_harness.runtime.engine as E
    monkeypatch.setattr(E, "RELEASE_ALPHABET_ALLOWLIST", {**E.RELEASE_ALPHABET_ALLOWLIST,
                                                          ("rt.declare", "brief"): "verdict_bit"})
    _res, llm = _run(store, {"held": HELD, "dec": _mod("rt.declare"), "al": _mod("rt.alias_brief"),
                            "poison": _mod("rt.poison_alias"), "ask": _ask("{{brief}}")},
                    ["held", "dec", "al", "poison", "ask"])
    # fixed by breaking the alias (a direct write is copied): poisoning `brief_alias` no longer changes `brief`,
    # whose allowlisted declaration then still describes its content (at 2d661e6: "mean=0.75" released as a bit)
    assert "mean=0.75" not in _prompts(llm)[0]
    assert _prompt_releases(store) == [("verdict_bit", "prompt:ask:brief")]


# ----------------------------------------------------------------------------------------- F4: module-global refs
_GLOBAL: dict = {}


@register("rt.grab")
def _grab(state, params, ctx):
    _GLOBAL["box"] = state["box"]  # e.g. a memo/cache holding a reference into run state
    _GLOBAL["store"] = ctx.store  # e.g. a cached store handle
    return {}


@register("rt.poke")
def _poke(state, params, ctx):
    _GLOBAL["box"]["note"] = f"{state['raw']['mean']}"  # mutates `box` without reading it in THIS step
    return {}


@register("rt.cached_store_read")
def _cached_store_read(state, params, ctx):
    steps = _GLOBAL["store"].trace(ctx.run_id)  # the store, through a handle cached in an earlier step
    return {"from_trace": str([s.output.get("raw") for s in steps])}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F4: in-place mutation through a reference a module kept from an EARLIER step (module global/cache): fingerprints
# cover only keys read in the current step, so `box` changes without being labelled
def test_F4_mutation_through_a_reference_kept_from_an_earlier_step(store):
    _GLOBAL.clear()
    _res, llm = _run(store, {"pub": PUB, "grab": _mod("rt.grab"), "held": HELD, "poke": _mod("rt.poke"),
                            "ask": _ask("N: {{box.note}}")}, ["pub", "grab", "held", "poke", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store), "a target-derived value reached a prompt with no release"


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F4b: a store handle cached from an earlier step (when the run label was still empty) is used later without touching
# ctx.store, so the store surface (run label) is not recorded
def test_F4b_store_handle_cached_from_an_earlier_step(store):
    _GLOBAL.clear()
    _res, llm = _run(store, {"pub": PUB, "grab": _mod("rt.grab"), "held": HELD, "tr": _mod("rt.cached_store_read"),
                            "ask": _ask("T: {{from_trace}}")}, ["pub", "grab", "held", "tr", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store), "a target-derived value reached a prompt with no release"


# ----------------------------------------------------------------------------------------- F5: absence channel
@register("rt.del_if_high")
def _del_if_high(state, params, ctx):
    if state["raw"]["mean"] > 0.5:
        del state["box"]
    return {}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F5: deleting a key conditionally on a target (absence channel): a delete CLEARS the key's label (set_label(k, ())),
# so routing on (or a module testing) the key's presence is an unlabelled, unreleased flow
def test_F5_conditional_delete_is_an_unreleased_flow(store):
    nodes = {"pub": PUB, "held": HELD, "del": _mod("rt.del_if_high"), "hi": _ask("box present"),
             "lo": _ask("box gone")}
    spec = _graph(nodes, [("pub", "held"), ("held", "del"), ("del", "hi", {"key": "box", "op": "truthy"}),
                          ("del", "lo")], "pub")
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert _prompts(llm) == ["box gone"]  # live: which prompt runs depends on the target (mean 0.75 > 0.5)
    assert _prompt_releases(store), "a target-dependent routing decision reached a prompt with no release"


# ----------------------------------------------------------------------------------------- F6: error-edge order
# FIXED (w2-taint; was a strict-xfail red-team finding):
# F6: Engine._next skips non-`_last_error` edges when the step errored, and returns at the first unconditional edge
# otherwise: with edge order [unconditional, error edge] the no-error outcome consults nothing, so a step that raises
# iff a target condition holds routes on the target with no decision recorded (no control label, no release) whenever
# it did NOT raise
def test_F6_error_edge_after_an_unconditional_edge_is_an_unreleased_flow(store):
    nodes = {"held": HELD, "chk": _mod("rt.raise_if_low"), "ok": _ask("fine"), "err": _ask("it failed")}
    spec = _graph(nodes, [("held", "chk"), ("chk", "ok"), ("chk", "err", {"key": "_last_error", "op": "truthy"})],
                  "held")
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert _prompts(llm) == ["fine"]  # with mean < 0.5 the run would go to `err`
    assert _prompt_releases(store), "a target-dependent routing decision reached a prompt with no release"


# ----------------------------------------------------------------------------------------- F7: map metadata
# FIXED (w2-taint; was a strict-xfail red-team finding):
# F7: fan-out metadata `_map` (n = len(items), index, branch_id = sha256 over the item hash) is unlabelled in each
# branch (bts.set_label('_map', ())) although the items list is labelled: a body prompt rendering {{_map.n}} or
# {{_map.branch_id}} carries the target-derived count / item hash with no release
def test_F7_map_metadata_of_tainted_items_is_unlabelled(store):
    nodes = {"held": HELD, "items": _mod("rt.tainted_items"),
             "fan": {"kind": "map", "items_key": "listing", "body": "body", "output_key": "fanned",
                     "reducer": "collect"}, "body": _ask("branch {{_map.index}} of {{_map.n}}")}
    _res, llm = _run(store, nodes, ["held", "items", "fan"])
    assert _prompts(llm)[0] == "branch 0 of 3"  # live: 3 = int(0.75 * 4), a target-derived count
    assert _prompt_releases(store), "a target-derived count reached a prompt with no release"


# ----------------------------------------------------------------------------------------- F8: foreign run id
@register("rt.read_as_other_run")
def _read_as_other_run(state, params, ctx):
    """E.g. a re-check of an earlier run's claim: the private read is logged naming THAT run."""
    return {"note": f"{_held_mean(ctx, run_id=params['other'])}"}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F8: attribution trusts the run id a private read NAMES: kerneld/releases.attributed_read_log drops a read that names
# another KNOWN run, even when it was made inside this run's step (same thread, _ACTIVE_RUN = this run), so the value
# the step derives from it is unlabelled
def test_F8_private_read_naming_another_known_run_is_not_attributed(store):
    other = Run(harness_id="h", harness_hash="x", task_id="t")
    store.add_run(other)
    _res, llm = _run(store, {"r": _mod("rt.read_as_other_run", other=other.id), "ask": _ask("N: {{note}}")},
                    ["r", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store), "a target-derived value reached a prompt with no release"


# ----------------------------------------------------------------------------------------- F9: key order
@register("rt.box2")
def _box2(state, params, ctx):
    return {"box": {"a": 1, "b": 2}}


@register("rt.reorder_if_high")
def _reorder_if_high(state, params, ctx):
    box = state["box"]
    if state["raw"]["mean"] > 0.5:
        items = list(box.items())
        box.clear()
        box.update(reversed(items))  # same content, different key ORDER
    return {}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F9: taint.fingerprint uses json.dumps(sort_keys=True), so an in-place REORDERING of a dict's keys is not seen as a
# mutation, but template.render uses json.dumps without sort_keys: the key order (log2(n!) bits for n keys) reaches
# the prompt unlabelled
def test_F9_in_place_key_reordering_is_not_a_detected_mutation(store):
    _res, llm = _run(store, {"box": _mod("rt.box2"), "held": HELD, "re": _mod("rt.reorder_if_high"),
                            "ask": _ask("{{box}}")}, ["box", "held", "re", "ask"])
    assert _prompts(llm)[0].index('"b"') < _prompts(llm)[0].index('"a"')  # live: order flipped by the target
    assert _prompt_releases(store), "a target-dependent key order reached a prompt with no release"


# ----------------------------------------------------------------------------------------- F10: int alphabet
@register("rt.int_code")
def _int_code(state, params, ctx):
    return {"code": int(state["raw"]["mean"] * 10**6)}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F10: kerneld/releases.alphabet_of types ANY int as `count` (KernelConfig.count_alphabet = 16 transcripts), so a
# continuous value encoded as a large int (750000) is released as `count`: K4 under-counts and the target is not
# marked adaptively exposed
def test_F10_a_large_int_is_released_as_a_count(store):
    _run(store, {"held": HELD, "c": _mod("rt.int_code"), "ask": _ask("C: {{code}}")}, ["held", "c", "ask"])
    assert _prompt_releases(store) == [("continuous", "prompt:ask:code")]


# ----------------------------------------------------------------------------------------- F11: workspace by path
@register("rt.ws_write_path")
def _ws_write_path(state, params, ctx):
    """Writes into a directory named in its params (here: the workspace) without touching ctx.sandbox."""
    (Path(params["dir"]) / "f.txt").write_text(f"{state['raw']['mean']}", encoding="utf-8")
    return {}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# F11: the workspace label grows only for tool nodes and modules that touch ctx.sandbox; a module writing a file into
# the workspace by PATH (params, run record, cwd) leaves it unlabelled, so a later tool node's output (stdout of
# `cat`) is unlabelled and reaches a prompt (and the export) unreleased
def test_F11_workspace_file_written_by_path_then_catted(store, tmp_path):
    ws = tmp_path / "ws"
    _res, llm = _run_sb(store, tmp_path, {"held": HELD, "w": _mod("rt.ws_write_path", dir=str(ws)), "cat": CAT,
                                         "ask": _ask("T: {{tout.stdout}}")}, ["held", "w", "cat", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store), "a target-derived value reached a prompt with no release"
