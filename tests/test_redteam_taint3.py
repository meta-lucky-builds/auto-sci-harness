"""Red team round 3 (w2-taint, against a8dd56f): more ways to launder a target-derived value past the labels.

Same conventions as test_redteam_taint.py: handled attacks are plain tests; an attack that gets through is a strict
xfail whose reason names the mechanism (Hn), and asserts first that the attack is LIVE (the value, or the
target-dependent difference, really reached the surface), so it cannot pass vacuously.

The held-out target is `tf-held` (private labels 1,0,1,1, mean 0.75); export tests use `tfx-held` (mean 4/7).
"""

from __future__ import annotations

import copy
import hashlib
import json
import string
import sys

import pytest

from custom_harness.errors import InfraError
from custom_harness.gateway import ChatRequest, Message, MockBackend
from custom_harness.kerneld.releases import released
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec, register
from custom_harness.runtime.engine import RetryPolicy
from custom_harness.runtime.taint import step_labels
from custom_harness.sandbox import LocalSandbox
from test_redteam_taint import CAT, COPY, PUB, _graph, _mock, _mod, _prompts, _run_sb
from test_taint_flow import EXPORT_LABELS, HELD, _ask, _evolve_run, _held_mean, _prompt_releases, _run, _spec

EXPORT_MEAN = sum(map(float, EXPORT_LABELS.decode().split()[1:])) / len(EXPORT_LABELS.decode().split()[1:])


def _say(ctx, text: str) -> None:
    ctx.llm("executor", ChatRequest(messages=[Message(role="user", content=text)]))


@register("rt3.held_only")
def _held_only(state, params, ctx):
    """Like tf.held, but writes ONLY `raw` (so it is the one labelled key once a clean step resets `_last_error`)."""
    return {"raw": {"mean": _held_mean(ctx)}}


HELD_ONLY = _mod("rt3.held_only")


# ===================================================================================================== HANDLED
# ----------------------------------------------------------------------------------------- more indirect reads
_IDIOMS = {
    "eval_formula": lambda s: str(eval("raw['mean'] * 1", s)),
    "eval_lambda": lambda s: str(eval("(lambda: raw['mean'])()", s)),
    "eval_locals": lambda s: str(eval("raw['mean']", {}, s)),
    "string_template": lambda s: string.Template("${route}").substitute(s),
    "nested_json": lambda s: json.dumps({"wrap": [s]}, default=str),
    "nested_deepcopy": lambda s: str(copy.deepcopy({"wrap": s})["wrap"]["raw"]),
    "percent_mapping": lambda s: "%(raw)s" % s,  # noqa: UP031  (old-style mapping format)
}


@register("rt3.idiom")
def _idiom(state, params, ctx):
    return {"derived": _IDIOMS[params["idiom"]](state)}


@pytest.mark.parametrize("idiom", sorted(_IDIOMS))
def test_more_indirect_state_reads_are_released(store, idiom):
    _res, llm = _run(store, {"held": HELD, "d": _mod("rt3.idiom", idiom=idiom), "ask": _ask("D: {{derived}}")},
                     ["held", "d", "ask"])
    assert "0.75" in _prompts(llm)[0] or "high" in _prompts(llm)[0]  # live
    assert _prompt_releases(store) == [("continuous", "prompt:ask:derived")]


# ----------------------------------------------------------------------------------------- routing operators
@pytest.mark.parametrize("cond", [{"key": "raw.mean", "op": "gt", "value": 0.5},
                                  {"key": "raw.mean", "op": "lt", "value": 0.9},
                                  {"key": "route", "op": "contains", "value": "hig"}], ids=["gt", "lt", "contains"])
def test_routing_with_comparison_operators_on_a_tainted_subpath_is_released(store, cond):
    spec = _graph({"held": HELD, "hi": _ask("A"), "lo": _ask("B")}, [("held", "hi", cond), ("held", "lo")], "held")
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert _prompts(llm) == ["A"]
    assert _prompt_releases(store) == [("verdict_bit", "control:held:hi")]


# ----------------------------------------------------------------------------------------- _infra_retries in state
_att = {"n": 0}


@register("rt3.transient_quoting")
def _transient_quoting(state, params, ctx):
    """Attempt 1 quotes the value in a TRANSIENT infra error (retried); attempt 2 reads nothing. The retry note lands
    in state under `_infra_retries`."""
    _att["n"] += 1
    if _att["n"] == 1:
        raise InfraError(f"upload of {state['raw']['mean']} timed out", transient=True)
    return {}


def test_infra_retry_notes_written_into_state_are_released_when_rendered(store):
    _att["n"] = 0
    spec = _spec({"held": HELD, "w": _mod("rt3.transient_quoting"), "ask": _ask("R: {{_infra_retries}}")},
                 ["held", "w", "ask"])
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store,
                 retry=RetryPolicy(max_attempts=2, backoff_s=0.0)).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store) == [("continuous", "prompt:ask:_infra_retries")]


# ----------------------------------------------------------------------------------------- LLM contract failure
def test_llm_contract_failure_attempts_are_labelled_and_its_error_is_released(store):
    nodes = {"held": HELD, "copy": COPY,
             "c": {"kind": "llm", "prompt": "C: {{copied}}", "output_key": "out", "output_schema": {"type": "object"}},
             "fix": _ask("F: {{_last_error}}")}
    spec = _graph(nodes, [("held", "copy"), ("copy", "c"), ("c", "fix", {"key": "_last_error", "op": "truthy"})],
                  "held")
    llm = MockBackend(lambda req: "not json")
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    c_step = next(s for s in store.trace(res.run_id) if s.node_id == "c")
    assert c_step.error and "ContractError" in c_step.error
    assert "0.75" in json.dumps(c_step.output)  # the attempts and messages carry the value
    assert step_labels(store, res.run_id)[c_step.index].tainted  # so the export / failure signatures withhold it
    assert ("continuous", "prompt:fix:_last_error") in _prompt_releases(store)


# ----------------------------------------------------------------------------------------- ModuleError + llm_calls
@register("rt3.llm_then_raise")
def _llm_then_raise(state, params, ctx):
    _say(ctx, f"v={state['copied']}")
    raise RuntimeError("gave up")


def test_module_error_with_partial_llm_calls_is_released_and_labelled(store):
    spec = _spec({"held": HELD, "copy": COPY, "m": _mod("rt3.llm_then_raise")}, ["held", "copy", "m"])
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.FAILED
    m_step = next(s for s in store.trace(res.run_id) if s.node_id == "m")
    assert "v=0.75" in json.dumps(m_step.output)  # live: the partial llm_calls are in the trace
    assert step_labels(store, res.run_id)[m_step.index].tainted
    assert ("continuous", "module_llm:m:copied") in _prompt_releases(store)


# ----------------------------------------------------------------------------------------- tool, no snapshot
def test_tool_writing_a_file_without_snapshot_then_catted_is_released(store, tmp_path):
    write = {"kind": "tool", "command": [sys.executable, "-c", "import sys; open('f.txt','w').write(sys.argv[1])",
                                         "{{copied}}"], "output_key": "w", "snapshot_after": False}
    _res, llm = _run_sb(store, tmp_path, {"held": HELD, "copy": COPY, "write": write, "cat": CAT,
                                         "ask": _ask("T: {{tout.stdout}}")}, ["held", "copy", "write", "cat", "ask"])
    assert "0.75" in _prompts(llm)[0]
    assert ("continuous", "prompt:ask:tout.stdout") in _prompt_releases(store)


# ----------------------------------------------------------------------------------------- map: infra + resume
_mb = {"n": 0}


@register("rt3.public_items")
def _public_items(state, params, ctx):
    return {"listing": ["a", "b"]}


@register("rt3.branch_flaky")
def _branch_flaky(state, params, ctx):
    """Branch `a` mixes in the value; branch `b` reads nothing labelled and infra-fails on its first execution."""
    if state["item"] == "a":
        return {"v": f"a:{state['raw']['mean']}"}
    if _mb["n"] == 0:
        _mb["n"] = 1
        raise InfraError("sandbox daemon went away", transient=False)
    return {"v": "b"}


def test_infra_failure_inside_a_map_branch_then_fresh_engine_resume_keeps_the_reused_branch_label(store):
    _mb["n"] = 0
    nodes = {"held": HELD, "items": _mod("rt3.public_items"),
             "fan": {"kind": "map", "items_key": "listing", "body": "body", "output_key": "fanned",
                     "reducer": "collect"}, "body": _mod("rt3.branch_flaky"), "ask": _ask("R: {{fanned}}")}
    spec = _spec(nodes, ["held", "items", "fan", "ask"])
    first = Engine(spec, backends={"executor": _mock()}, store=store,
                   retry=RetryPolicy(max_attempts=1, backoff_s=0.0)).run("t", {})
    assert first.status == RunStatus.INFRA_FAILED, first.detail
    llm = _mock()
    done = Engine(spec, backends={"executor": llm}, store=store).resume(first.run_id)
    assert done.status == RunStatus.COMPLETED, done.detail
    fan = [s for s in store.trace(first.run_id) if s.node_id == "fan"][-1]
    assert fan.output["reused_branches"] == 1  # branch a was reused, not re-run
    assert "0.75" in _prompts(llm)[0]
    assert _prompt_releases(store) == [("continuous", "prompt:ask:fanned")]


# ===================================================================================================== GET THROUGH
# ----------------------------------------------------------------------------------------- H1 pop + bulk read
@register("rt3.pop_then_bulk")
def _pop_then_bulk(state, params, ctx):
    """Consume a work item, then look at what is left (an ordinary idiom)."""
    raw = state.pop("raw")
    left = len(state) if params["how"] == "len" else sorted(k for k, _v in state.items())
    if params.get("llm"):
        _say(ctx, f"consumed {raw['mean']}; {left} keys left")
        return {}
    return {"derived": f"{raw['mean']}", "left": str(left)}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H1: Engine._module_reads (engine.py:1131) takes the keys of the view AFTER the module ran when it did a bulk read
# (`set(dict.keys(tracked)) if tracked.bulk`), so a key the module popped (or a popitem()/clear()) BEFORE the bulk
# read is dropped from the step's label although `tracked.reads` recorded it
@pytest.mark.parametrize("how", ["len", "items"])
def test_H1_popped_key_then_bulk_read_loses_its_label_in_a_later_prompt(store, how):
    _res, llm = _run(store, {"held": HELD_ONLY, "pub": PUB, "pop": _mod("rt3.pop_then_bulk", how=how),
                             "ask": _ask("D: {{derived}}")}, ["held", "pub", "pop", "ask"])
    assert "D: 0.75" in _prompts(llm)[0]  # live
    assert _prompt_releases(store) == [("continuous", "prompt:ask:derived")]


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H1 (module model call): _before_module_model_call uses the same _module_reads (engine.py:1131/1174): after
# `state.pop(k)` and a bulk read, the module's own prompt carrying the popped value is released on nothing
def test_H1_popped_key_then_bulk_read_in_the_modules_own_model_call(store):
    _res, llm = _run(store, {"held": HELD_ONLY, "pub": PUB, "pop": _mod("rt3.pop_then_bulk", how="len", llm=True)},
                     ["held", "pub", "pop"])
    assert "consumed 0.75" in _prompts(llm)[0]  # live
    assert _prompt_releases(store), "a target-derived value reached a module-built prompt with no release"


# ----------------------------------------------------------------------------------------- H2 tuple holding lists
@register("rt3.mk_pair")
def _mk_pair(state, params, ctx):
    return {"pair": ([], [])}  # e.g. (train_ids, test_ids)


@register("rt3.fill_pair")
def _fill_pair(state, params, ctx):
    state["pair"][0].append(state["raw"]["mean"])
    return {}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H2: taint.fingerprint (taint.py:38) treats a tuple as immutable (returns None), so a top-level TUPLE value is never
# fingerprinted; an in-place change of a list INSIDE it (no write to the key) is not seen by changed_in_place and the
# key keeps its clean label
def test_H2_in_place_change_inside_a_tuple_valued_key_is_not_seen(store):
    _res, llm = _run(store, {"mk": _mod("rt3.mk_pair"), "held": HELD, "fill": _mod("rt3.fill_pair"),
                             "ask": _ask("P: {{pair}}")}, ["mk", "held", "fill", "ask"])
    assert "0.75" in _prompts(llm)[0]  # live
    assert ("continuous", "prompt:ask:pair") in _prompt_releases(store)


# ----------------------------------------------------------------------------------------- H3 _last_error substring
# FIXED (w2-taint; was a strict-xfail red-team finding):
# H3: Engine._next (engine.py:1031) lets an errored step follow any edge whose key CONTAINS the substring
# '_last_error' (e.g. `retry_last_error`), but _route (engine.py:1053) counts `_last_error` as consulted only when an
# edge's ROOT key is `_last_error`; so a step that errors iff a target condition holds picks the successor with no
# tainted decision (any spec author, incl. an evolved edit, can write such an edge
def test_H3_error_routing_through_an_edge_key_containing_last_error_is_unreleased(store):
    # rt.raise_if_low raises iff raw.mean < cut: with cut=0.9 the target (0.75) makes it raise
    nodes = {"held": HELD, "chk": _mod("rt.raise_if_low", cut=0.9), "ok": _ask("fine"), "err": _ask("it failed")}
    spec = _graph(nodes, [("held", "chk"), ("chk", "ok"),
                          ("chk", "err", {"key": "retry_last_error", "op": "falsy"})], "held")
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert _prompts(llm) == ["it failed"]  # live: the target chose this branch (cut=0.5 gives "fine")
    assert _prompt_releases(store), "a target-dependent routing decision reached a prompt with no release"


# ----------------------------------------------------------------------------------------- H4 MapError early return
@register("rt3.items7")
def _items7(state, params, ctx):
    return {"listing": ["x"] * round(state["raw"]["mean"] * 7)}  # the item COUNT is target-derived


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H4: Engine._map returns its MapError early (engine.py:822-832: not a list / exceeds max_items / not JSON) BEFORE it
# records the items' label (`self._step_label[id(node)] = prompt_taint`, engine.py:969), so `_last_error` ('3 items
# exceed max_items=2') is unlabelled and a prompt rendering it releases nothing
def test_H4_map_error_quoting_the_tainted_item_count_is_unreleased(store):
    nodes = {"held": HELD, "items": _mod("rt.tainted_items"),
             "fan": {"kind": "map", "items_key": "listing", "body": "body", "output_key": "fanned",
                     "reducer": "collect", "max_items": 2}, "body": _ask("I: {{item}}"),
             "report": _ask("E: {{_last_error}}")}
    spec = _graph(nodes, [("held", "items"), ("items", "fan"),
                          ("fan", "report", {"key": "_last_error", "op": "truthy"})], "held")
    llm = _mock()
    res = Engine(spec, backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert "3 items exceed" in _prompts(llm)[0]  # live: 3 = 0.75 * 4
    assert ("continuous", "prompt:report:_last_error") in _prompt_releases(store)


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H4 (export): the same early MapError leaves the map step's checkpoint label empty (engine.py:822-832), so
# evolve/export.py exports the step's error text (the target-derived item count) un-withheld
def test_H4_export_shows_the_map_error_quoting_the_tainted_item_count(store, tmp_path):
    from custom_harness.evolve.export import export_evidence
    nodes = {"held": {"kind": "module", "module": "tf.export_held"}, "items": _mod("rt3.items7"),
             "fan": {"kind": "map", "items_key": "listing", "body": "body", "output_key": "fanned",
                     "reducer": "collect", "max_items": 2}, "body": PUB}
    out, task = _evolve_run(store, tmp_path, nodes, ["held", "items", "fan"])
    fan = next(s for s in store.trace(out.run_id) if s.node_id == "fan")
    assert "4 items exceed" in (fan.error or "")  # live: 4 = round(4/7 * 7)
    res = export_evidence(store, tmp_path / "exp", runs=[out], tasks=[task])
    text = "".join(p.read_text(encoding="utf-8") for p in res.path.rglob("*.json"))
    assert "traces/" in " ".join(res.files)
    assert "4 items exceed" not in text, "a target-derived count reached the evolver's export"


# ----------------------------------------------------------------------------------------- H5 alphabet at call time
@register("rt3.consume_then_llm")
def _consume_then_llm(state, params, ctx):
    x = state["raw"]["mean"]
    state["raw"] = None  # consumed
    _say(ctx, f"v={x}")
    return {}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H5: _before_module_model_call (engine.py:1175-1176) picks the release alphabet from the key's value AT CALL TIME
# (`alphabet_of(dict.get(f.tracked, src))`), not the value the module read: a module that reads a continuous value,
# then sets the key to None/True/a small int, gets its prompt released as `verdict_bit`/`count` (K4 under-counts
def test_H5_module_llm_release_alphabet_comes_from_the_overwritten_value(store):
    _res, llm = _run(store, {"held": HELD, "m": _mod("rt3.consume_then_llm")}, ["held", "m"])
    assert "v=0.75" in _prompts(llm)[0]  # live: a continuous value
    rel = _prompt_releases(store)
    assert [n for _a, n in rel] == ["module_llm:m:raw"]  # released, but on which alphabet?
    assert rel == [("continuous", "module_llm:m:raw")]


# ----------------------------------------------------------------------------------------- H6 evolution score dev term
@register("rt3.best_from_held")
def _best_from_held(state, params, ctx):
    """Records the candidate's score under `best.dev_score` (here the held-out mean: a mislabelled field, or a "best"
    chosen by a held-out evaluation) next to a passed audit."""
    return {"best": {"dev_score": state["raw"]["mean"]}, "baseline_dev": 0.5, "audit": {"passed": True}}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H6: the evolution score's dev term (evolve/evaluate.py:209 dev_term, used by finish_run/score_state) is read from
# state keys `best.dev_score`/`baseline_dev` trusted BY NAME as public; their taint labels are ignored, so a
# target-derived `best` reaches the proposer as a CONTINUOUS score (scores.json, selection) with only a `verdict_bit`
# release on the bits
def test_H6_evolution_score_dev_term_carries_a_labelled_value(store, tmp_path):
    out, _task = _evolve_run(store, tmp_path, {"held": {"kind": "module", "module": "tf.export_held"},
                                               "best": _mod("rt3.best_from_held")}, ["held", "best"])
    assert out.status == "completed"
    expected = 0.5 * (EXPORT_MEAN - 0.5) / 0.5
    last = store.trace(out.run_id)[-1]
    cp = store.blobs.get_json(last.checkpoint_ref)
    assert cp["state"]["best"]["dev_score"] == EXPORT_MEAN  # live: the scored state carries the held-out value
    assert "best" in cp["taint"]["labels"]  # ... and the engine knows it is target-derived
    continuous = [r for r in released(store, "tfx-held")
                  if r["channel"] == "evolution_score" and r["alphabet"] == "continuous"]
    assert out.score != pytest.approx(expected) or continuous, (
        f"score {out.score} is a continuous function of the held-out value, released only as verdict bits")


# ----------------------------------------------------------------------------------------- H7 export output keys
@register("rt3.path_hi")
def _path_hi(state, params, ctx):
    return {"path_hi": "done"}


@register("rt3.path_lo")
def _path_lo(state, params, ctx):
    return {"path_lo": "done"}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H7: evolve/export.py embargo_labelled (export.py:174-182) withholds a control-tainted step's node id and input but
# keeps its output KEYS (and kind, provenance, cost): the key names (`path_hi` vs `path_lo`) tell the evolver which
# branch a held-out-dependent routing decision took
def test_H7_export_output_keys_reveal_the_branch_a_tainted_decision_took(store, tmp_path):
    from custom_harness.evolve.evaluate import SuiteTask, run_harness
    from custom_harness.evolve.export import EMBARGOED, export_evidence
    nodes = {"held": {"kind": "module", "module": "tf.export_held"}, "hi": _mod("rt3.path_hi"),
             "lo": _mod("rt3.path_lo")}
    spec = _graph(nodes, [("held", "hi", {"key": "raw.mean", "op": "gt", "value": 0.5}), ("held", "lo")], "held")
    task = SuiteTask("tfx", tmp_path / "taskdir", "evolve", family="A")
    task.task_dir.mkdir(parents=True, exist_ok=True)
    out = run_harness(spec, task, 0, backends={}, store=store,
                      sandbox_factory=lambda k: LocalSandbox(tmp_path / k, i_understand_no_isolation=True))
    assert out.status == "completed"
    res = export_evidence(store, tmp_path / "exp", runs=[out], tasks=[task])
    doc = json.loads((res.path / "traces" / f"{out.run_id}.json").read_text(encoding="utf-8"))
    step = doc["steps"][1]
    assert step["node_id"] == EMBARGOED  # live: the export knows this step ran because of a tainted decision
    assert "path_hi" not in json.dumps(step), f"the exported step reveals the branch: {step['output']}"


# ----------------------------------------------------------------------------------------- H8 ctx.archive_taint
@register("rt3.long_value")
def _long_value(state, params, ctx):
    return {"long": f"value {state['raw']['mean']} " + "z" * 300}


@register("rt3.list_handles")
def _list_handles(state, params, ctx):
    """Offers the agent the archived handles it may page in (an ordinary recall helper)."""
    return {"handles": sorted(ctx.archive_taint)}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H8: ModuleContext.archive_taint (modules.py:76, engine.py:1100) is not a recorded context surface (_RECORDED,
# modules.py:116): its keys are sha256 digests of the FULL target-derived values truncated in earlier prompts, so a
# module listing them hands a later prompt a target-derived digest with no label
def test_H8_archive_handle_digests_listed_from_ctx_archive_taint_are_unreleased(store):
    nodes = {"held": HELD, "long": _mod("rt3.long_value"), "ask1": _ask("L: {{long}}"),
             "list": _mod("rt3.list_handles"), "ask2": {"kind": "llm", "prompt": "H: {{handles}}",
                                                        "output_key": "answer2"}}
    _res, llm = _run(store, nodes, ["held", "long", "ask1", "list", "ask2"], context={"max_value_chars": 120})
    digest = hashlib.sha256(("value 0.75 " + "z" * 300).encode()).hexdigest()
    assert digest in _prompts(llm)[1]  # live: the digest of the held-out-derived text reached the second prompt
    assert ("continuous", "prompt:ask2:handles") in _prompt_releases(store)


# ----------------------------------------------------------------------------------------- H9 nested engine inputs
_inner: dict[str, MockBackend] = {}


@register("rt3.nested_own_backend")
def _nested_own_backend(state, params, ctx):
    sub = HarnessSpec.model_validate({"id": "rt3-sub", "entry": "a", "edges": [],
                                      "nodes": {"a": {"kind": "llm", "prompt": "X={{input.x}}", "output_key": "o"}}})
    inner = _inner["llm"] = MockBackend(lambda req: "ok")
    res = Engine(sub, backends={"executor": inner}, store=ctx._store).run("sub", {"x": state["copied"]})
    return {"sub_status": res.status.value}


# FIXED (w2-taint; was a strict-xfail red-team finding):
# H9 (low realism: the module builds its own backend instead of using ctx.backends): Engine.run (engine.py:358) starts
# a NESTED run (inside a module step, _ACTIVE_RUN set) with its inputs unlabelled; its LLM node renders the outer
# target-derived value and neither engine releases it
def test_H9_nested_engine_on_its_own_backend_renders_an_unlabelled_input(store):
    _run(store, {"held": HELD, "copy": COPY, "nest": _mod("rt3.nested_own_backend")}, ["held", "copy", "nest"])
    assert "X=0.75" in _prompts(_inner["llm"])[0]  # live
    assert _prompt_releases(store), "a target-derived value reached a nested LLM prompt with no release"
