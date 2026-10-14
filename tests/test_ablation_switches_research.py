"""Ablation switches of the research / L3 / L4 / L5 capability rows (custom_harness/ablation.py `_RESEARCH`; GOAL §2.1,
invariant 12). Each test shows that the behaviour CHANGES when its switch is off, and is UNCHANGED when it is not:
the same call with NO_ABLATION (production) and with an unrelated switch off gives the production result.

Offline and model-free: scripted MockBackends, the literature fixture world, trusted stdlib solution code in a
LocalSandbox (CHH_ALLOW_UNISOLATED is set by conftest for tests)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import custom_harness.research  # noqa: F401  registers the research modules
from custom_harness.ablation import _RESEARCH, NO_ABLATION, AblationError, AblationSet
from custom_harness.budget import Budget
from custom_harness.literature.client import LiteratureClient
from custom_harness.memory import Kind, MemoryStore
from custom_harness.research import modules as M
from custom_harness.research import tree as T
from custom_harness.research.data import profile
from custom_harness.research.memory_wiring import EMPTY, recall_memory
from custom_harness.research.task import load_task
from custom_harness.research.tournament import tournament_module
from custom_harness.runtime import Engine
from custom_harness.runtime.modules import ModuleContext
from custom_harness.sandbox import LocalSandbox
from custom_harness.sandbox.snapshot import take_snapshot
from custom_harness.scorecard.registry import load_capabilities, switch_fact
from custom_harness.scorecard.runner import ablation_set
from custom_harness.weights.selection import select_by_reward_variance
from lit_world import OA, r, world
from test_memory import MW, P, honest_skills
from test_recall import _handle, _llm, _spec
from test_research import BROKEN, KNN_RADIUS, rings_task  # noqa: F401  (rings_task: fixture re-export)
from test_tournament import POOL, _Ctx, _ids_in
from test_tournament import _state as _tournament_state

UNRELATED = "reward_variance_selection"  # a switch that none of the research-loop code paths below reads


def _arms(switch: str) -> dict[str, AblationSet]:
    """production, an unrelated switch off (must behave like production), and this switch off"""
    other = UNRELATED if switch != UNRELATED else "btl_tournament"
    return {"prod": NO_ABLATION, "other": ablation_set({other}), "off": ablation_set({switch})}


# ================================================================ the registry and the coverage contract agree
RESEARCH_SWITCHES = {"cross_task_memory": "evoscientist", "skills_paged_recall": "evomaster",
                     "playbook_dead_ends": "auto-recsys", "method_library": "mm-agent",
                     "data_profile_kway_rank": "scider", "btl_tournament": "robin",
                     "experiment_tree_repair_improve": "ai-scientist-v1-v2", "multi_source_retrieval": "feynman",
                     "reward_variance_selection": "aworld-rl"}
NOT_ABLATABLE = ("scientistone", "parness", "matagent", "autoopt", "ahois", "scienceide", "prime-agent",
                 "researchbench-sde", "langgraph", "vg-audit-2608-05179")


def test_every_research_switch_is_named_by_its_row_and_has_its_test_here():
    assert {s.name: s.capability for s in _RESEARCH} == RESEARCH_SWITCHES
    rows = {c.id: c for c in load_capabilities()}
    for cap in NOT_ABLATABLE:  # recorded with the exact reason, reported as a gap (never as a switch)
        rec = rows[cap].ablation
        assert rec is not None and rec.not_ablatable, cap
        assert switch_fact(cap, rec.model_dump())["has_switch"] is False
    here = Path(__file__).name
    for sw in _RESEARCH:
        rec = rows[sw.capability].ablation
        assert rec is not None and rec.switch == sw.name, (sw.capability, rec)
        path, fn = sw.test.split("::")
        assert Path(path).name == here and callable(globals().get(fn)), sw.test
        fact = switch_fact(sw.capability, rec.model_dump())
        assert fact["switch"] == sw.name and fact.get("test") == sw.test
        # a partial switch is reported as a partial switch (still an invariant-12 gap), never as a full one
        assert fact["has_switch"] is (not rec.not_covered), fact
    with pytest.raises(AblationError):
        NO_ABLATION.off("cross_task_memroy")  # a typo is refused, never a silent no-op


# ================================================================ L5 memory recall (memory_wiring.recall_memory)
NS = "abl"


@pytest.fixture(scope="module")
def mem(tmp_path_factory):
    """One readable entry of each kind the switches touch, in namespace NS: positive, dead-end, playbook, method."""
    m = MW(tmp_path_factory.mktemp("abl-memory"))
    s = MemoryStore(m.kernel, namespace=NS, exploit_check=honest_skills("lp playbook", "lp method"))
    ws = tmp_path_factory.mktemp("abl-dead-end-ws")
    (ws / "solution.py").write_text("print('dead end')\n")
    ref = take_snapshot(ws, m.kernel.store.blobs)
    ids: dict[Kind, str] = {}
    for kind, title, evidence, kw in (
            (Kind.POSITIVE, "lp optimum certified", (m.L1,), {}),
            (Kind.DEAD_END, "kNN with k=60 on rings", (m.N,), {"snapshot_ref": ref}),
            (Kind.PLAYBOOK, "lp playbook", (m.L1,), {}),
            (Kind.METHOD, "lp method", (m.L1,), {"data": {"category": "optimisation/lp"}})):
        res = s.submit(P(kind, title, evidence, **kw), writer="research-loop")
        assert res.passed, (kind, res.reasons)
        ids[kind] = res.entry_id
    yield m, ids
    m.w.close()


def _recall(m: MW, ablation: AblationSet) -> dict:
    ctx = ModuleContext(run_id="abl-memory-run", store=m.kernel.store, backends={}, budget=Budget(), sandbox=None,
                        ablation=ablation)
    out = recall_memory({}, {"namespace": NS}, ctx)["memory"]
    assert out["error"] is None, out
    return out


def _comparable(out: dict) -> dict:
    """The recall result without the rendered text (compared separately: the wrapper carries a nonce)."""
    return {k: v for k, v in out.items() if k != "context"}


def _check_withholds(mem, switch: str, withheld: set[Kind]) -> None:
    m, ids = mem
    arms = {name: _recall(m, a) for name, a in _arms(switch).items()}
    prod, off = arms["prod"], arms["off"]
    assert set(prod["entries"]) == set(ids.values()) and "lp playbook" in prod["context"]  # all four, rendered
    assert _comparable(arms["other"]) == _comparable(prod)
    kept = {k for k in ids if k not in withheld}
    assert set(off["entries"]) == {ids[k] for k in kept}
    assert set(off["skills"]) == {ids[k] for k in kept if k in (Kind.PLAYBOOK, Kind.METHOD)}
    assert off["ablated_kinds"] == sorted(str(k) for k in withheld)
    titles = {Kind.POSITIVE: "lp optimum certified", Kind.DEAD_END: "kNN with k=60", Kind.PLAYBOOK: "lp playbook",
              Kind.METHOD: "lp method"}
    for k, t in titles.items():
        assert (t in off["context"]) is (k in kept), (k, switch)


def test_cross_task_memory_switch_renders_no_memory(mem):
    m, ids = mem
    arms = {name: _recall(m, a) for name, a in _arms("cross_task_memory").items()}
    assert set(arms["prod"]["entries"]) == set(ids.values()) and arms["prod"]["context"] != EMPTY
    assert _comparable(arms["other"]) == _comparable(arms["prod"])
    assert "lp optimum certified" in arms["other"]["context"]
    off = arms["off"]
    assert off["context"] == EMPTY and off["entries"] == [] and off["skills"] == []
    assert off["ablated"] == ["cross_task_memory"]


def test_skills_paged_recall_switch_withholds_skills_and_refuses_pages(mem, store):
    _check_withholds(mem, "skills_paged_recall", {Kind.PLAYBOOK, Kind.METHOD})
    # paged recall (research.recall), through the Engine: the switch reaches the module via ModuleContext.ablation
    results = {}
    for name, ablation in _arms("skills_paged_recall").items():
        res = Engine(_spec(), backends={"executor": _llm(lambda t: {"handle": _handle(t), "page": 1})}, store=store,
                     ablation=ablation).run("t", {})
        results[name] = res
        assert store.run_ablated(res.run_id) == ablation.names  # an ablated run is marked in its record
    for name in ("prod", "other"):
        rec = results[name].state["recalled"]
        assert rec.get("refused") is None and rec["page"] == 1 and "UNTRUSTED-RECALLED-DATA" in rec["text"], name
    assert results["off"].state["recalled"]["refused"] == "ablated"
    step = next(s for s in store.trace(results["off"].run_id) if s.node_id == "recall")
    assert step.output["recall_log"] == {"served": False, "code": "ablated", "agent_step": None}


def test_playbook_dead_ends_switch_withholds_playbooks_and_dead_ends(mem):
    _check_withholds(mem, "playbook_dead_ends", {Kind.PLAYBOOK, Kind.DEAD_END})


def test_method_library_switch_withholds_methods(mem):
    _check_withholds(mem, "method_library", {Kind.METHOD})


# ================================================================ L4.2 profiling and L4.5 tournament
class _ACtx(_Ctx):
    """test_tournament's scripted judge context, carrying an AblationSet"""

    def __init__(self, responder, ablation: AblationSet):
        super().__init__(responder)
        self.ablation = ablation


def _judge(req):
    return '{"ranking": ["d", "c", "b", "a"]}'


def _first_shown_wins(req):
    return json.dumps({"winner": _ids_in(req.messages[0].content)[0]})


def _tournament(ablation: AblationSet, params: dict | None = None, judge=_judge) -> tuple[dict, int]:
    ctx = _ACtx(judge, ablation)
    out = tournament_module(_tournament_state(), params or {"rounds": 2}, ctx)
    return out, len(ctx.backend.requests)


def _check_tournament_off(switch: str) -> None:
    arms = {name: _tournament(a) for name, a in _arms(switch).items()}
    (prod, prod_calls), (other, other_calls), (off, off_calls) = arms["prod"], arms["other"], arms["off"]
    assert prod["ranking"]["order"] == ["d", "c", "b", "a"] and prod_calls == 2  # the judge decided the order
    assert other == prod and other_calls == prod_calls
    assert off_calls == 0  # no judge call at all
    assert off["ranking"]["order"] == [h["id"] for h in POOL] and off["queue"] == POOL  # the proposer's order
    assert off["ranking"]["mode"] == "ablated" and switch in off["ranking"]["method"]
    with pytest.raises(ValueError, match="unknown tournament operators"):  # parameter checks still run first
        _tournament(ablation_set({switch}), {"operators": ["mutate"]})


def test_data_profile_kway_rank_switch_skips_profile_and_ranking(store, tmp_path, rings_task):  # noqa: F811
    task = load_task(rings_task)
    sb = LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)
    (sb.workspace / "data").mkdir(parents=True)
    shutil.copy2(rings_task / "public" / task.train_file, sb.workspace / "data" / task.train_file)
    state = {"task": task.model_dump()}
    out = {}
    for name, ablation in _arms("data_profile_kway_rank").items():
        ctx = ModuleContext(run_id=f"profile-{name}", store=store, backends={}, budget=Budget(), sandbox=sb,
                            ablation=ablation)
        out[name] = M.profile_data(state, {}, ctx)["profile"]
    expected = profile(sb.workspace / "data" / task.train_file, task.target_column)
    assert out["prod"] == out["other"] == expected and "ablated" not in expected
    assert set(out["off"]) == {"ablated"}
    _check_tournament_off("data_profile_kway_rank")


def test_btl_tournament_switch_keeps_the_proposer_order():
    _check_tournament_off("btl_tournament")
    # pairwise (both-order BT) mode is switched off too
    prod, calls = _tournament(NO_ABLATION, {"mode": "pairwise"}, _first_shown_wins)
    assert prod["ranking"]["mode"] == "pairwise" and calls > 0
    off, calls = _tournament(ablation_set({"btl_tournament"}), {"mode": "pairwise"}, _first_shown_wins)
    assert calls == 0 and off["ranking"]["order"] == [h["id"] for h in POOL]


# ================================================================ L4.7 experiment tree: repairs and improve rounds
def _rctx(store, sb, run_id: str, ablation: AblationSet) -> ModuleContext:
    return ModuleContext(run_id=run_id, store=store, backends={}, budget=Budget(), sandbox=sb, ablation=ablation)


def test_experiment_tree_switch_removes_repairs_and_improve_rounds(store, tmp_path, rings_task):  # noqa: F811
    switch = "experiment_tree_repair_improve"  # (= research.modules.TREE_SWITCH)
    sb = LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)
    base = _rctx(store, sb, "tree-base", NO_ABLATION)
    st: dict = {"input": {"task_dir": str(rings_task)}}
    st = {**st, **M.setup(st, {}, base)}
    st = {**st, **M.baseline_dev(st, {}, base)}
    params = {"max_attempts": 8, "max_fixes": 2, "max_improves": 2}
    arms = _arms(switch)

    # (1) repairs: a crashing candidate gets a `fix` in production; with the switch its node is abandoned
    broken = {**st, "impl": {"code": BROKEN, "notes": ""}, "current": {"id": "h_broken"}}
    seen = {}
    for name, ablation in arms.items():
        out = M.run_candidate(broken, params, _rctx(store, sb, f"tree-fix-{name}", ablation))
        node = out["attempts"][-1]["tree_node"]
        seen[name] = (out["route"], T.ExperimentTree.load(store, out["tree_id"]).nodes[node].status)
        assert out["attempts"][-1]["error"], out["attempts"][-1]
    assert seen["prod"] == seen["other"] == ("fix", "provisional")
    assert seen["off"] == ("give_up", "abandoned")

    # (2) improve rounds: an incumbent that beats the baseline is refined in production; with the switch it is not
    good = {**st, "impl": {"code": KNN_RADIUS, "notes": ""}, "current": {"id": "h_radius"}}
    ran = {**good, **M.run_candidate(good, params, _rctx(store, sb, "tree-improve", NO_ABLATION))}
    assert ran["route"] == "next" and ran["best"]["dev_score"] > ran["baseline_dev"]
    off = M.next_hypothesis(ran, params, _rctx(store, sb, "tree-improve-off", arms["off"]))
    assert off["route"] == "verify" and off["current"] is None
    assert off["skipped_calls"] == [{"node": "improve", "incumbent": "h_radius",
                                     "reason": f"ablation: {switch} is switched off"}]
    assert T.ExperimentTree.load(store, ran["tree_id"]).seq == ran["tree_seq"]  # nothing ran, nothing was logged
    state = ran
    for name in ("prod", "other"):
        on = M.next_hypothesis(state, params, _rctx(store, sb, f"tree-improve-{name}", arms[name]))
        assert on["route"] == "improve" and on["current"]["id"] == "improve-1", (name, on.get("skipped_calls"))
        assert not on.get("skipped_calls")
        state = {**state, "tree_seq": on["tree_seq"]}  # the first call confirmed the incumbent (logged)


# ================================================================ L4.3 literature: multi-source retrieval
Q = "sparse gradient routing tabular"


def _search(ablation: AblationSet, *, openalex_down: bool):
    tr = world()
    if openalex_down:
        tr.set(OA, {"search": Q, "per-page": "5"}, r(500, "down"))
    res = LiteratureClient(tr, sleep=lambda _s: None, max_attempts=2, ablation=ablation).search(Q, limit=5)
    return res, tr


def test_multi_source_retrieval_switch_disables_source_fallback():
    arms = _arms("multi_source_retrieval")
    prod, _ = _search(arms["prod"], openalex_down=True)
    assert prod.complete and prod.records[0].source == "crossref"  # fell back from openalex to crossref
    other, _ = _search(arms["other"], openalex_down=True)
    assert other.model_dump() == prod.model_dump()
    off, tr = _search(arms["off"], openalex_down=True)
    assert not off.complete and off.records == [] and [e["source"] for e in off.errors] == ["openalex"]
    assert {url for url, _ in tr.calls} == {OA}  # no other source was asked
    # with the first source up, nothing else changes: the switch removes the fallback only
    up_prod, _ = _search(arms["prod"], openalex_down=False)
    up_off, _ = _search(arms["off"], openalex_down=False)
    assert up_off.model_dump() == up_prod.model_dump() and up_off.complete


# ================================================================ L8: task selection by reward variance
REWARDS = {"solved": [1, 1, 1, 1, 1, 1], "hopeless": [0, 0, 0, 0, 0, 0], "boundary": [1, 0, 1, 0, 1, 0],
           "mostly": [1, 1, 1, 1, 1, 0], "few": [1, 0]}


def test_reward_variance_selection_switch_selects_uniformly():
    arms = _arms("reward_variance_selection")
    prod = select_by_reward_variance(REWARDS, k=2, ablation=arms["prod"])
    assert [s.task_id for s in prod.selected] == ["boundary", "mostly"] and prod.excluded
    other = select_by_reward_variance(REWARDS, k=2, ablation=arms["other"])
    assert other == prod
    off = select_by_reward_variance(REWARDS, k=2, ablation=arms["off"])
    assert [s.task_id for s in off.selected] == ["boundary", "few"] and off.excluded == {}  # task-id order
    everything = select_by_reward_variance(REWARDS, ablation=arms["off"])
    assert [s.task_id for s in everything.selected] == sorted(REWARDS)
    with pytest.raises(ValueError):  # input checks are not switched off
        select_by_reward_variance({"x": [1, 0, float("inf"), 0]}, ablation=arms["off"])
