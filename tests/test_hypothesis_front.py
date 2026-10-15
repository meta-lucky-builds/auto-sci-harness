"""The hypothesis front of research_v2 (research/front.py; docs/hypothesis_front.md): the Problem Formulator as an
optional first stage (GOAL L4.1, §8.17), diversity by measured error correlation in selection (§8.11), the forced
distance-3 inspiration / literature-mined mechanisms / topic quotas enforced in code (L4.4), and the tournament as an
explicit opt-in that execution outranks (L4.5, §16.8)."""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.records import RunStatus, StepKind, TraceStep
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_research import KNN_RADIUS, rings_task, scripted_llm  # noqa: F401  (fixture re-export)
from test_research_yield import packed_llm

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "examples/research_v1.yaml"
V2 = ROOT / "examples/research_v2.yaml"
FIX = ROOT / "tests/fixtures/literature"
OA = "https://api.openalex.org/works"
Q = "radial features ring classes"
RETRIEVED_A = "doi:10.5555/front.2022.001"  # failed replication + open problem + a mechanism sentence (data class)
RETRIEVED_B = "doi:10.5555/front.2023.002"  # an open problem about a conjecture (formal class)


# ---------------------------------------------------------------- helpers
def _records():
    return [
        {"ids": [RETRIEVED_A], "title": "Radial Features for Ring-Shaped Classes",
         "abstract": "We propose radial features because ring-shaped classes are separable by their distance from the "
                     "origin. A reported gain of 4 points failed to replicate on data with noise columns. Why noise "
                     "columns hurt nearest neighbours remains open."},
        {"ids": [RETRIEVED_B], "title": "A Conjecture on Margin Bounds for Rings",
         "abstract": "We state a conjecture on margin bounds for concentric classes whose proof remains open."},
    ]


def _ledger(tmp_path, *adapters):
    store = EvidenceStore(tmp_path / "s")
    k = Kernel(store)
    for a in adapters:
        k.approve_adapter(a, approver="pytest")
    return k.ledger


def _snapshot(router_text: str | None = None) -> str:
    """Freeze a literature snapshot holding ONE recorded openalex search (the query Q). The search here is not in any
    run's retrieval log: only the run's own (offline) search is."""
    from custom_harness.config import data_dir
    from custom_harness.literature.client import LiteratureClient
    from custom_harness.literature.http import HttpResponse
    from custom_harness.literature.snapshot import RecordingTransport, SnapshotStore
    from lit_world import Router, key
    text = router_text or (FIX / "openalex_front_search.json").read_text(encoding="utf-8")
    router = Router({key(OA, {"search": Q, "per-page": "5"}): HttpResponse(status=200, text=text, headers={})})
    store = SnapshotStore(data_dir() / "literature", clock=lambda: datetime(2026, 10, 7, tzinfo=UTC))
    LiteratureClient(RecordingTransport(router, store), sleep=lambda s: None, max_attempts=1).search(
        Q, 5, ["openalex"])
    return store.freeze(date(2026, 1, 1))


def _spec(path: Path, **node_params) -> HarnessSpec:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for node, params in node_params.items():
        doc["nodes"][node].setdefault("params", {}).update(params)
    return HarnessSpec.model_validate(doc)


def _engine(spec, store, tmp_path, llm):
    return Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                  sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True), allow_same_family=True)


FRONT_ON = {"enabled": True, "literature_queries": [Q], "literature_sources": ["openalex"]}


# ---------------------------------------------------------------- 1. Problem Formulator ranking
def test_problem_ranking_is_deterministic(tmp_path):
    from custom_harness.research.formulator import mine_literature, rank_problems
    ledger = _ledger(tmp_path, "ml")
    retrieved = {RETRIEVED_A, RETRIEVED_B}
    a = rank_problems(ledger, mine_literature(_records(), retrieved), [], data_metric="accuracy")
    recs = _records()[::-1]
    b = rank_problems(ledger, list(reversed(mine_literature(recs, retrieved))), [], data_metric="accuracy")
    assert [p.model_dump() for p in a] == [p.model_dump() for p in b]
    assert [p.kind for p in a] == ["failed_replication", "open_problem", "open_problem"]
    assert len(a) == 3 and len({p.id for p in a}) == 3


def test_tier_comes_from_registered_adapters_never_from_the_proposal(tmp_path):
    from custom_harness.research.formulator import ProblemProposal, mine_literature, rank_problems
    props = mine_literature(_records(), {RETRIEVED_A})
    # no approved adapter: every problem is unverifiable (T6, priority 0)
    none = rank_problems(_ledger(tmp_path / "a"), props, [], data_metric="accuracy")
    assert {p.tier for p in none} == {6} and {p.priority for p in none} == {0.0}
    # the human-approved ML adapter serves data-class claims: T2
    ml = rank_problems(_ledger(tmp_path / "b", "ml"), props, [], data_metric="accuracy")
    assert {p.tier for p in ml} == {2} and all(p.priority > 0 for p in ml)
    # a proposal cannot carry a tier (a model or a mined text never supplies it)
    with pytest.raises(ValidationError):
        ProblemProposal.model_validate({**props[0].model_dump(), "tier": 0})


def test_an_unverifiable_problem_ranks_below_a_verifiable_one(tmp_path):
    from custom_harness.research.formulator import ProblemProposal, rank_problems
    ledger = _ledger(tmp_path, "ml")  # data verifiable; formal (Lean) not approved
    formal = ProblemProposal(id="lp-formal", source="literature", kind="failed_replication",
                             description="a high-value formal problem", evidence_class="formal", value=1.0,
                             feasibility=1.0)
    data = ProblemProposal(id="lp-data", source="literature", kind="open_problem", description="a modest data problem",
                           evidence_class="data", value=0.05, feasibility=0.1)
    ranked = rank_problems(ledger, [formal, data], [], data_metric="accuracy")
    assert [p.id for p in ranked] == ["lp-data", "lp-formal"]
    assert ranked[1].tier == 6 and ranked[1].priority == 0.0 < ranked[0].priority


def test_literature_problems_need_retrieval_provenance():
    from custom_harness.research.formulator import mine_literature
    from custom_harness.research.hypotheses import mine_mechanisms
    assert mine_literature(_records(), set()) == []
    assert mine_mechanisms(_records(), {"doi:10.5555/other"}) == []
    mech = mine_mechanisms(_records(), {RETRIEVED_A})
    assert [m.cites for m in mech] == [(RETRIEVED_A,)] and mech[0].quote.startswith("We propose radial features")


# ---------------------------------------------------------------- 2. diversity by measured error correlation
def _div_state(store, metric="accuracy"):
    n = 100
    labels = ["0"] * n

    def preds(wrong):
        return ["1" if i in wrong else "0" for i in range(n)]

    a = set(range(20))
    b = (a - {19}) | {20}  # near-duplicate of a: error correlation ~0.94
    c = set(range(50, 70))  # different mistakes
    attempts = [{"n": i + 1, "hypothesis_id": h, "dev_score": 0.8, "dev_preds_ref": store.blobs.put_json(preds(w)),
                 "code_ref": store.blobs.put(b"x"), "wall_s": 1.0}
                for i, (h, w) in enumerate([("h_a", a), ("h_b", b), ("h_c", c)])]
    task = {"id": "t", "title": "t", "description": "d", "metric": metric, "target_column": "y"}
    return {"task": task, "attempts": attempts, "best": attempts[0],
            "_private_dev_labels_ref": store.blobs.put_json({"private_dev_labels": labels})}


def test_near_duplicate_hypotheses_are_collapsed(tmp_path):
    from custom_harness.research.front import diversity_select
    store = EvidenceStore(tmp_path / "s")
    ctx = SimpleNamespace(store=store)
    d = diversity_select(_div_state(store), {}, ctx)["diversity"]
    assert d["kept"] == [1, 3]
    assert [(x["n"], x["duplicate_of"]) for x in d["dropped"]] == [(2, 1)] and d["dropped"][0]["r"] > 0.8
    assert d["threshold"] == 0.8 and "error correlation" in d["measure"]
    # the threshold is the selection rule: above r, nothing is collapsed
    assert diversity_select(_div_state(store), {"threshold": 0.99}, ctx)["diversity"]["kept"] == [1, 2, 3]


def test_the_ensemble_draws_members_only_from_the_decorrelated_set(tmp_path):
    from custom_harness.research.front import diversity_select
    from custom_harness.research.modules import ensemble
    store = EvidenceStore(tmp_path / "s")
    ctx = SimpleNamespace(store=store)
    st = _div_state(store)
    st |= diversity_select(st, {}, ctx)
    out = ensemble(st, {"members": "diversity"}, ctx)["ensemble"]
    assert out["diversity_excluded"] == [2] and out["members"] == [1, 3] and out["used"] is False
    # asked for diversity members but no record: fail closed (no ensemble)
    st.pop("diversity")
    assert "no diversity record" in ensemble(st, {"members": "diversity"}, ctx)["ensemble"]["reason"]


# ---------------------------------------------------------------- 3. quotas, inspiration, mechanisms: admission
FRONT = {"enabled": True, "quota": {"topics": ["model family", "feature engineering"], "cap": 1, "caps": {}},
         "inspiration": {"source_area": "ecology"}, "min_inspired": 1, "min_mechanisms": 1,
         "mechanisms": [{"id": "mech-a", "cites": [RETRIEVED_A], "quote": "q", "title": "t"}]}


def test_a_quota_violation_is_rejected_not_logged():
    from custom_harness.research.hypotheses import admit
    hyps = [{"id": "h1", "topic": "model family", "inspired_by": "Ecology", "mechanism_cites": [RETRIEVED_A]},
            {"id": "h2", "topic": "model family"},  # over the cap
            {"id": "h3", "topic": "astrology"},  # undeclared
            {"id": "h4", "topic": "feature engineering", "mechanism_cites": ["doi:10.5555/never.retrieved"]},
            {"id": "h5", "topic": "feature engineering", "mechanism_cites": "doi:x"},  # not a list
            {"id": "h6"}]  # no topic
    kept, rec = admit(hyps, FRONT, {RETRIEVED_A})
    assert [h["id"] for h in kept] == ["h1"] and rec["admitted"] == ["h1"]
    reasons = {r["id"]: r["reason"] for r in rec["rejected"]}
    assert set(reasons) == {"h2", "h3", "h4", "h5", "h6"}
    assert "cap" in reasons["h2"] and "undeclared" in reasons["h3"] and "retrieval log" in reasons["h4"]


def test_a_set_without_forced_inspiration_or_cited_mechanism_is_rejected():
    from custom_harness.research.hypotheses import AdmissionRefused, admit
    no_insp = [{"id": "h1", "topic": "model family", "mechanism_cites": [RETRIEVED_A]}]
    with pytest.raises(AdmissionRefused, match="inspired_by"):
        admit(no_insp, FRONT, {RETRIEVED_A})
    no_mech = [{"id": "h1", "topic": "model family", "inspired_by": "ecology"}]
    with pytest.raises(AdmissionRefused, match="mechanism"):
        admit(no_mech, FRONT, {RETRIEVED_A})
    kept, rec = admit(no_mech, {**FRONT, "min_mechanisms": 0, "mechanisms_waived": True}, set())
    assert [h["id"] for h in kept] == ["h1"] and rec["mechanisms_waived"] is True
    with pytest.raises(AdmissionRefused, match="no hypothesis"):
        admit([{"id": "h9", "topic": "nope"}], {**FRONT, "min_inspired": 0, "min_mechanisms": 0}, set())


def test_only_a_shown_mechanism_satisfies_the_mechanism_quota():
    """Review M2: citing a retrieved paper that yielded no shown mechanism is admissible but is not using one."""
    from custom_harness.research.hypotheses import AdmissionRefused, admit
    other = [{"id": "h1", "topic": "model family", "inspired_by": "ecology", "mechanism_cites": [RETRIEVED_B]}]
    with pytest.raises(AdmissionRefused, match="mechanism"):
        admit(other, FRONT, {RETRIEVED_A, RETRIEVED_B})
    kept, rec = admit(other + [{"id": "h2", "topic": "feature engineering", "mechanism_cites": [RETRIEVED_A]}],
                      FRONT, {RETRIEVED_A, RETRIEVED_B})
    assert rec["mechanism_citing"] == 1 and [h["id"] for h in kept] == ["h1", "h2"]


def test_a_node_with_other_queries_does_not_reuse_the_cached_pool():
    """Review M1: the pool cache is keyed by the declared search, not only by snapshot and session."""
    from custom_harness.research.front import literature_pool
    cached = {"snapshot": None, "session": "r1", "queries": [], "limit": 5, "sources": None,
              "records": [{"ids": ["doi:10.5555/stale"], "title": "stale", "abstract": "stale"}], "errors": []}
    ctx = SimpleNamespace(run_id="r1")
    assert literature_pool({"input": {}, "literature_pool": cached}, {}, ctx) is cached
    fresh = literature_pool({"input": {}, "literature_pool": cached}, {"literature_queries": [Q]}, ctx)
    assert fresh is not cached and fresh["queries"] == [Q] and fresh["records"] == []


def test_baseline_predictions_are_mapped_from_fold_order_to_file_order(tmp_path):
    """Review L2: the scan pairs each file row with ITS baseline prediction (checked through the folds' valid_x
    bytes, a channel independent of the folds' row indices), and a label mismatch refuses the mapping."""
    import csv
    import io

    from custom_harness.research.data import cv_folds
    from custom_harness.research.formulator import FormulatorError
    from custom_harness.research.front import baseline_in_file_order
    n = 37
    body = "x,y\n" + "".join(f"{i},{'a' if (i * 7) % 3 else 'b'}\n" for i in range(n))
    p = tmp_path / "train.csv"
    p.write_text(body, encoding="utf-8")
    folds = cv_folds(p, "y", 5, 3, True)
    oof = [row[0] for f in folds for row in list(csv.reader(io.StringIO(f["valid_x"].decode())))[1:]]
    store = EvidenceStore(tmp_path / "s")
    state = {"task": {"id": "t", "title": "t", "description": "d", "metric": "accuracy", "target_column": "y"},
             "dev_scheme": {"k": 5, "seed": 3, "stratified": True},
             "_private_dev_labels_ref": store.blobs.put_json(
                 {"private_dev_labels": [y for f in folds for y in f["labels"]]}),
             "baseline_dev_preds_ref": store.blobs.put_json(oof)}
    ctx = SimpleNamespace(store=store)
    assert oof != [str(i) for i in range(n)]  # fold order really differs from file order
    assert baseline_in_file_order(state, ctx, body.encode()) == [str(i) for i in range(n)]
    bad = dict(state, _private_dev_labels_ref=store.blobs.put_json({"private_dev_labels": ["a"] * n}))
    with pytest.raises(FormulatorError, match="mapping refused"):
        baseline_in_file_order(bad, ctx, body.encode())


def test_the_inspiration_distance_is_forced_to_three():
    from custom_harness.research.front import hypothesis_front
    from custom_harness.research.hypotheses import domain_distance
    ctx = SimpleNamespace(run_id="r1")
    out = hypothesis_front({"input": {}, "_seed": 3}, {"enabled": True, "distance": 1}, ctx)["hypothesis_front"]
    insp = out["inspiration"]
    assert insp["distance"] == 3 and domain_distance("tabular learning", insp["source_area"]) == 3
    assert out["min_inspired"] >= 1 and out["mechanisms_waived"] is True and out["min_mechanisms"] == 0
    assert f'"inspired_by": "{insp["source_area"]}"' in out["prompt"]
    off = hypothesis_front({"input": {}}, {}, ctx)["hypothesis_front"]
    assert off == {"enabled": False, "prompt": ""}


def _front_llm(bad_set=False):
    def llm(req):
        text = req.messages[-1].content
        if "Propose 5 DISTINCT hypotheses" in text:
            area = re.search(r'"inspired_by": "([^"]+)"', text).group(1)
            return json.dumps({"hypotheses": [
                {"id": "h_verbose", "idea": "kNN on radius, verbose", "rationale": "rings", "topic": "model family",
                 **({} if bad_set else {"inspired_by": area})},
                {"id": "h_radius", "idea": "kNN on radial feature", "rationale": "classes are rings",
                 "topic": "feature engineering", "mechanism_cites": [RETRIEVED_A]},
                {"id": "h_fake", "idea": "cite something never retrieved", "rationale": "x", "topic": "data handling",
                 "mechanism_cites": ["doi:10.5555/never.retrieved"]},
                {"id": "h_noise", "idea": "drop noise columns", "rationale": "n1..n4", "topic": "astrology"}]})
        return packed_llm(req)
    return llm


def test_v2_front_end_to_end(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    snap = _snapshot()
    spec = _spec(V2, formulate={**FRONT_ON, "permutations": 39}, front=FRONT_ON)
    llm = MockBackend(_front_llm())
    res = _engine(spec, store, tmp_path, llm).run("rings", {"task_dir": str(rings_task), "literature_snapshot": snap})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    prompts = [r.messages[-1].content for r in llm.requests]
    hyp_prompt = next(p for p in prompts if "Propose 5 DISTINCT hypotheses" in p)
    # the front reached the prompt: mechanism (verbatim, with its retrieval-log id), inspiration, topics, problems
    assert "We propose radial features because" in hyp_prompt and RETRIEVED_A in hyp_prompt
    assert "Optional inspiration (NOT evidence" in hyp_prompt and "Declared topics" in hyp_prompt
    assert "failed to replicate" in hyp_prompt
    # admission rejected the fabricated citation and the undeclared topic: they never ran
    adm = s["hypothesis_admission"]
    assert adm["admitted"] == ["h_verbose", "h_radius"]
    assert {r["id"] for r in adm["rejected"]} == {"h_fake", "h_noise"}
    assert not {"h_fake", "h_noise"} & {a["hypothesis_id"] for a in s["attempts"]}
    # problems: ranked by code; the formal open problem has no approved adapter -> T6, priority 0, ranked last
    ranked = s["problems"]["ranked"]
    lit = [p for p in ranked if p["source"] == "literature"]
    assert lit and lit[-1]["evidence_class"] == "formal" and lit[-1]["tier"] == 6 and lit[-1]["priority"] == 0
    assert all(p["tier"] == 2 for p in lit if p["evidence_class"] == "data")
    assert [p["priority"] for p in ranked] == sorted((p["priority"] for p in ranked), reverse=True)
    # the dev scan ran through the ledger-committed grammar (or says why not)
    scan = s["problems"]["scan"]
    if scan["run"]:
        arts = [e for e in Kernel(store).ledger.entries("register_artifact")
                if e.body.get("kind") == "formulator-grammar"]
        assert arts and arts[-1].seq == scan["grammar_seq"]
    else:
        assert s["problems"]["notes"], s["problems"]
    # no tournament call by default; the diversity record fed the ensemble
    assert not any("Rank ALL of these hypotheses" in p for p in prompts)
    assert s["diversity"]["enabled"] and s["diversity"]["kept"][0] == s["best"]["n"]
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL", s["certificate"]


def test_v2_rejected_hypothesis_set_ends_the_run_honestly(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    snap = _snapshot()
    llm = MockBackend(_front_llm(bad_set=True))
    res = _engine(_spec(V2, front=FRONT_ON), store, tmp_path, llm).run(
        "rings", {"task_dir": str(rings_task), "literature_snapshot": snap})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert "claim" not in s and "certificate" not in s and not s.get("attempts")
    assert not any("Implement" in r.messages[-1].content for r in llm.requests)
    rank = [st for st in store.trace(res.run_id) if st.node_id == "rank"]
    assert rank and "AdmissionRefused" in rank[0].error and "inspired_by" in rank[0].error
    assert s.get("report")


def test_v2_defaults_are_off_and_cost_no_call():
    doc = yaml.safe_load(V2.read_text(encoding="utf-8"))
    n = doc["nodes"]
    assert n["formulate"]["params"]["enabled"] is False and n["front"]["params"]["enabled"] is False
    assert n["rank"]["module"] == "research.admit_rank" and n["rank"]["params"]["tournament"] is None
    assert n["diversify"]["params"]["enabled"] is True and n["ensemble"]["params"]["members"] == "diversity"
    for k in ("formulate", "front", "rank", "diversify"):
        assert n[k]["kind"] == "module"  # no LLM node was added


# ---------------------------------------------------------------- 4. tournament: v1 wiring, v2 opt-in, execution wins
def test_research_v1_wires_the_tournament():
    doc = yaml.safe_load(V1.read_text(encoding="utf-8"))
    assert doc["nodes"]["tournament"]["module"] == "research.tournament"
    edges = [(e["source"], e["target"]) for e in doc["edges"]]
    assert ("hypothesize", "tournament") in edges and ("tournament", "critic") in edges
    assert not any(n.get("module") == "research.tournament" for n in yaml.safe_load(
        V2.read_text(encoding="utf-8"))["nodes"].values())


def test_constrain_to_execution_lets_the_llm_only_downgrade():
    from custom_harness.research.tournament import constrain_to_execution
    rng = random.Random(0)
    for trial in range(300):
        ids = [f"h{i}" for i in range(rng.randint(1, 7))]
        llm = ids[:]
        rng.shuffle(llm)
        scores = {i: rng.choice([None, 0.1, 0.5, 0.5, 0.9]) for i in ids}
        hib = trial % 2 == 0
        out = constrain_to_execution(llm, scores, higher_is_better=hib)
        assert sorted(out) == sorted(ids)
        for x in range(len(out)):  # no executed pair is inverted against execution
            for y in range(x + 1, len(out)):
                a, b = scores[out[x]], scores[out[y]]
                if a is not None and b is not None:
                    assert (a >= b) if hib else (a <= b), (llm, scores, out)
        # unexecuted candidates keep the LLM's positions
        assert [k for k, i in enumerate(out) if scores[i] is None] == [k for k, i in enumerate(llm)
                                                                         if scores[i] is None]
    assert constrain_to_execution(["b", "c", "a"], {"a": 0.9, "b": 0.5}, higher_is_better=True) == ["a", "c", "b"]


def test_admit_rank_tournament_cannot_lift_a_worse_executed_hypothesis():
    from custom_harness.research.front import admit_rank
    calls = []

    def llm(role, req):
        calls.append(role)
        return SimpleNamespace(text=json.dumps({"ranking": ["h_b", "h_c", "h_a"]}))  # judge prefers the worse h_b

    state = {"task": {"id": "t", "title": "t", "description": "d", "metric": "accuracy", "target_column": "y"},
             "profile": {}, "hypotheses": {"hypotheses": [{"id": h, "idea": h} for h in ("h_a", "h_b", "h_c")]},
             "attempts": [{"hypothesis_id": "h_a", "dev_score": 0.9}, {"hypothesis_id": "h_b", "dev_score": 0.5}]}
    ctx = SimpleNamespace(llm=llm)
    out = admit_rank(state, {"tournament": {"rounds": 1}}, ctx)
    assert calls == ["judge"]
    assert out["ranking"]["llm_order"] == ["h_b", "h_c", "h_a"]
    assert out["ranking"]["order"] == ["h_a", "h_c", "h_b"] == [h["id"] for h in out["queue"]]
    # default: no tournament, no call, proposer order
    calls.clear()
    assert admit_rank(state, {}, ctx)["ranking"]["order"] == ["h_a", "h_b", "h_c"] and calls == []
    # review M3: tournament parameters that would add (operators) or delete (max_pool) hypotheses are refused
    for bad in ({"operators": ["combine"]}, {"generations": 2}, {"max_pool": 2}):
        with pytest.raises(ValueError, match="may only reorder"):
            admit_rank(state, {"tournament": {"rounds": 1, **bad}}, ctx)
    assert calls == []


def test_v2_opt_in_tournament_orders_the_queue_but_execution_picks_the_incumbent(store, tmp_path, rings_task,  # noqa: F811
                                                                                 monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))

    def llm(req):
        if "Rank ALL of these hypotheses" in req.messages[-1].content:
            return json.dumps({"ranking": ["h_noise", "h_verbose", "h_radius"]})  # the worst idea first
        return packed_llm(req)

    backend = MockBackend(llm)
    res = _engine(_spec(V2, rank={"tournament": {"rounds": 1}}), store, tmp_path, backend).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert s["ranking"]["order"][0] == "h_noise" and "opt-in tournament" in s["ranking"]["method"]
    assert sum("Rank ALL of these hypotheses" in r.messages[-1].content for r in backend.requests) == 1
    # the judge's favourite ran first and failed; dev evidence chose the incumbent
    assert s["best"]["hypothesis_id"] in ("h_radius", "improve-1", "improve-2")
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL", s["certificate"]


# ---------------------------------------------------------------- relays are not evidence (bug class generalised)
@pytest.mark.parametrize("module", ["research.execution_rank", "research.admit_rank", "research.hypothesis_front",
                                    "research.formulate"])
def test_ranking_and_front_steps_are_relays_not_citable_evidence(module):
    from custom_harness.research.evidence_refs import NOT_EVIDENCE, vet_step
    step = TraceStep(run_id="r", index=3, node_id="rank", kind=StepKind.MODULE,
                     input={"node": {"kind": "module", "module": module}},
                     output={"queue": [{"id": "h1", "idea": "model-written text"}]}, started_at=1.0, ended_at=2.0)
    v = vet_step(step, before_index=9, private_reads=[])
    assert not v.ok and v.code == NOT_EVIDENCE


def test_the_front_nodes_are_pinned_against_evolution():
    """Review M5: an edit cannot switch the front, the Formulator or the tournament opt-in by a parameter."""
    from custom_harness.evolve import Edit, EditOp, EditRejected, EditType, apply_edit
    spec = HarnessSpec.model_validate(yaml.safe_load(V2.read_text(encoding="utf-8")))
    for path, value in (("/nodes/front/params/enabled", True), ("/nodes/front/params/cap", 9),
                        ("/nodes/formulate/params/enabled", True), ("/nodes/rank/params/tournament", {"rounds": 3})):
        with pytest.raises(EditRejected, match="human-only"):
            apply_edit(spec, Edit(type=EditType.PARAMS, ops=[EditOp(op="replace", path=path, value=value)],
                                  rationale="weaken the front"))


def test_front_state_is_reserved_for_trusted_modules():
    from custom_harness.evolve import Edit, EditOp, EditRejected, EditType, apply_edit
    spec = HarnessSpec.model_validate(yaml.safe_load(V1.read_text(encoding="utf-8")))
    ops = [EditOp(op="add", path="/nodes/fake", value={"kind": "llm", "prompt": "p", "output_key":
                                                      "hypothesis_front"})]
    with pytest.raises(EditRejected, match="reserved"):
        apply_edit(spec, Edit(type=EditType.SUBAGENT_ROLE, ops=ops, rationale="fake an empty front"))
