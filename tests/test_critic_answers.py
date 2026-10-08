"""Socratic critic, second half (GOAL L4.8, AHOIS): answers to critic questions must cite evidence that exists in the
run's evidence store, is visible to the answering agent and is not hidden (research/critic_answers.py).

Mini harness (no sandbox): facts (module) -> critic (judge LLM) -> socratic -> critic_evidence -> defend (proposer
LLM) -> check_answers -> gate (test module: research.critic_answers.critic_gate, as research.certify calls it).
"""

from __future__ import annotations

import json
import re
from itertools import pairwise

from custom_harness.gateway import MockBackend
from custom_harness.kernel.vault import DatasetRole
from custom_harness.records import StepKind
from custom_harness.research import critic_answers as CA
from custom_harness.research import evidence_refs as R
from custom_harness.runtime import Engine, HarnessSpec, register

QUESTIONS = ["What would falsify the radius hypothesis?", "Is the color column leaking the label?"]
LONG = "fact " * 200  # longer than the context budget below: rendered as head + tail + an archived handle


@register("test_critic.facts")
def _facts(state, params, ctx):
    out = {"profile": {"rows": 400, "note": "computed by code"}, "notes": LONG}
    if params.get("canary_in_notes"):  # a module bug: a private value of the run inside a public key
        out["notes"] = "x" * 300 + state["_private_canary"] + "y" * 300
    if params.get("mention"):
        out["mention"] = params["mention"]
    return out


@register("test_critic.private")
def _private(state, params, ctx):
    return {"_private_canary": "CHH-CANARY-" + "a1b2c3d4" * 4}


@register("test_critic.held_out_read")
def _held_out_read(state, params, ctx):
    vault = ctx.kernel().vault
    vault.register_dataset("crit-held", DatasetRole.TEST, public={"x.csv": b"x\n1\n"}, private={"y.csv": b"y\n1\n"})
    vault.read("crit-held", "y.csv", purpose="leakscan", run_id=ctx.run_id)  # a private read during this step
    return {"held_summary": "derived " * 100}


@register("test_critic.gate")
def _gate(state, params, ctx):
    ok, why = CA.critic_gate(ctx)
    return {"gate": {"ok": ok, "why": why}}


@register("test_critic.forge_state")
def _forge_state(state, params, ctx):  # what an evolved node could write into state
    return {"critic_answers": {"status": "pass", "questions": [{"id": "q1", "status": "cited", "codes": []}]}}


def _spec(*, pre: list[str] | None = None, facts_params: dict | None = None, with_check: bool = True,
          forge: bool = False, defend_prompt_extra: str = "") -> HarnessSpec:
    nodes: dict = {
        "private": {"kind": "module", "module": "test_critic.private"},
        "facts": {"kind": "module", "module": "test_critic.facts", "params": facts_params or {}},
        "held": {"kind": "module", "module": "test_critic.held_out_read"},
        "critic": {"kind": "llm", "role": "judge", "prompt": "Profile: {{profile}}. Ask at most 5 short questions.",
                   "output_key": "critique_raw", "output_schema": {"type": "object"}},
        "socratic": {"kind": "module", "module": "research.socratic_filter", "params": {"max_questions": 5}},
        "critic_evidence": {"kind": "module", "module": "research.critic_evidence"},
        "defend": {"kind": "llm", "role": "proposer", "output_key": "critic_answers_raw", "output_schema": {},
                   "prompt": "ANSWER THE CRITIC. Notes: {{notes}} Questions: {{critic_evidence.questions}} "
                             "Catalog: {{critic_evidence.catalog}}" + defend_prompt_extra},
        "check_answers": {"kind": "module", "module": "research.check_critic_answers",
                          "params": {"answerer": "defend"}},
        "forge": {"kind": "module", "module": "test_critic.forge_state"},
        "gate": {"kind": "module", "module": "test_critic.gate"},
    }
    order = [*(pre or []), "facts", "critic", "socratic", "critic_evidence", "defend"]
    order += ["check_answers"] if with_check else []
    order += ["forge"] if forge else []
    order += ["gate"]
    used = {k: nodes[k] for k in order}
    edges = []
    for a, b in pairwise(order):
        if a == "defend":  # a failed answer call goes on to the check (fail closed there), as in research_v1/v2
            edges.append({"source": a, "target": b, "when": {"key": "_last_error", "op": "truthy"}})
        edges.append({"source": a, "target": b})
    return HarnessSpec.model_validate({"id": "critic-mini", "entry": order[0], "max_steps": 20, "nodes": used,
                                       "edges": edges, "context": {"max_value_chars": 200}})


def _llm(answers):
    """judge: the fixed questions; proposer: `answers(prompt_text)` (a dict, or a raw string)."""
    def respond(req):
        text = req.messages[-1].content
        if "Ask at most 5 short questions" in text:
            return json.dumps({"questions": QUESTIONS})
        if "ANSWER THE CRITIC" in text:
            out = answers(text)
            return out if isinstance(out, str) else json.dumps(out)
        raise AssertionError(text[:200])
    return MockBackend(respond)


def _catalog_step(text: str, node: str = "facts") -> str:
    """The catalog id of `node` as shown in the prompt (the catalog is rendered as JSON)."""
    m = re.search(r'"id": "(step:\d+)",\s*"node": "' + node + '"', text)
    assert m, text[:600]
    return m.group(1)


def _run(store, spec, answers):
    llm = _llm(answers)
    res = Engine(spec, backends={"judge": llm, "proposer": llm}, store=store).run("t", {})
    return res, llm


def _by_q(res) -> dict[str, dict]:
    return {q["id"]: q for q in res.state["critic_answers"]["questions"]}


def _both(evidence_for):
    return lambda t: {"answers": [{"question": f"q{i}", "answer": "Because the profile says so.",
                                   "evidence": evidence_for(t)} for i in (1, 2)]}


# ---------------------------------------------------------------- the honest path
def test_answers_citing_visible_code_evidence_pass_the_check_and_the_gate(store):
    res, _ = _run(store, _spec(), _both(lambda t: [_catalog_step(t)]))
    assert res.state["critic_answers"]["status"] == "pass", res.state["critic_answers"]
    assert all(q["status"] == "cited" for q in _by_q(res).values())
    assert res.state["gate"] == {"ok": True, "why": ""}


def test_a_visible_archived_handle_is_citable(store):
    def answers(t):
        handle = re.search(r"archived as ([0-9a-f]{16})", t).group(1)
        return {"answers": [{"question": "q1", "answer": "See the notes.", "evidence": [f"blob:{handle}"]},
                            {"question": "q2", "answer": "See the profile.", "evidence": [_catalog_step(t)]}]}
    res, _ = _run(store, _spec(), answers)
    assert res.state["critic_answers"]["status"] == "pass", res.state["critic_answers"]


# ---------------------------------------------------------------- fails closed
def test_an_uncited_answer_is_rejected_and_blocks_the_gate(store):
    res, _ = _run(store, _spec(), lambda t: {"answers": [
        {"question": "q1", "answer": "Trust me.", "evidence": []},
        {"question": "q2", "answer": "Fine.", "evidence": [_catalog_step(t)]}]})
    q = _by_q(res)
    assert q["q1"]["status"] == "rejected" and q["q1"]["codes"] == ["uncited"]
    assert q["q2"]["status"] == "cited"
    assert res.state["gate"]["ok"] is False and "1 of 2 question(s)" in res.state["gate"]["why"]


def test_unresolvable_and_unlisted_citations_are_rejected(store):
    # a step id not in the catalog (never shown), a Kernel evidence id that does not exist although it was shown
    # (the facts module mentions it), and a handle that was never archived
    spec = _spec(facts_params={"mention": "see ev_0123456789abcdef and 0123456789abcdef0123"},
                 defend_prompt_extra=" Mention: {{mention}}")
    res, _ = _run(store, spec, lambda t: {"answers": [
        {"question": "q1", "answer": "Step 99.", "evidence": ["step:99"]},
        {"question": "q2", "answer": "Evidence.", "evidence": ["ev:ev_0123456789abcdef"]}]})
    q = _by_q(res)
    assert q["q1"]["codes"] == ["not_visible"] and q["q2"]["codes"] == ["unresolvable"]
    res2, _ = _run(store, spec, _both(lambda t: ["blob:0123456789abcdef0123"]))
    assert {c for q in _by_q(res2).values() for c in q["codes"]} == {"not_visible"}  # shown, but not a handle
    assert res.state["gate"]["ok"] is False and res2.state["gate"]["ok"] is False


def test_kernel_evidence_is_target_derived_and_never_citable(store):
    """`ev:` ids resolve to Kernel Evidence, produced only from held-out runs / certificates: always refused."""
    from custom_harness.records import Evidence, EvidenceKind
    from custom_harness.tiers import Tier
    ev = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T1_EXECUTABLE, verifier_id="metric:accuracy",
                  produced_by_run=None, payload_ref=store.blobs.put(b"{}"))
    store.add_evidence(ev)
    spec = _spec(facts_params={"mention": f"evidence {ev.id}"}, defend_prompt_extra=" Mention: {{mention}}")
    res, _ = _run(store, spec, _both(lambda t: [f"ev:{ev.id}"]))
    assert {c for q in _by_q(res).values() for c in q["codes"]} == {"target_derived"}


def test_hidden_steps_are_not_in_the_catalog_and_not_citable(store):
    """A step that wrote `_private*` state, or read a private vault file while it ran, is hidden (invariant 2)."""
    spec = _spec(pre=["private", "held"])
    seen = {}

    def answers(t):
        seen["prompt"] = t
        return {"answers": [{"question": "q1", "answer": "a", "evidence": ["step:0"]},   # the _private writer
                            {"question": "q2", "answer": "b", "evidence": ["step:1"]}]}  # the private reader
    res, _ = _run(store, spec, answers)
    assert '"node": "private"' not in seen["prompt"] and '"node": "held"' not in seen["prompt"]
    # facts ran AFTER this run's private read: possibly target-derived (run_private_reads), so hidden too
    assert '"node": "facts"' not in seen["prompt"] and "Catalog: []" in seen["prompt"]
    assert {c for q in _by_q(res).values() for c in q["codes"]} == {"not_visible"}
    trace = store.trace(res.run_id)
    reads = R.private_read_windows(store)
    assert R.vet_step(trace[0], before_index=99, private_reads=reads).code == "hidden"
    assert R.vet_step(trace[1], before_index=99, private_reads=reads).code == "hidden"
    assert R.vet_step(trace[2], before_index=99, private_reads=reads).ok  # facts: no read while it ran...
    run_reads = R.run_private_reads(store, trace, res.run_id)
    assert R.vet_step(trace[2], before_index=99, private_reads=reads, run_reads=run_reads).code == "hidden"
    critic_step = next(s for s in trace if s.node_id == "critic")
    assert R.vet_step(critic_step, before_index=99, private_reads=reads).code == "not_evidence"  # model output
    assert R.vet_step(trace[2], before_index=2, private_reads=reads).code == "unresolvable"  # not before the answer


def test_a_handle_whose_content_carries_a_private_value_is_rejected(store):
    spec = _spec(pre=["private"], facts_params={"canary_in_notes": True})

    def answers(t):
        handle = re.search(r"archived as ([0-9a-f]{16})", t).group(1)
        return _both(lambda _t: [f"blob:{handle}"])(t)
    res, _ = _run(store, spec, answers)
    assert {c for q in _by_q(res).values() for c in q["codes"]} == {"private"}


def test_a_target_derived_handle_is_rejected(store):
    """A prompt value derived from a step that read held-out data (engine taint, invariant 18) cannot be cited."""
    spec = _spec(pre=["held"], defend_prompt_extra=" Held: {{held_summary}}")

    def answers(t):
        handles = re.findall(r"archived as ([0-9a-f]{16})", t)
        return _both(lambda _t: [f"blob:{handles[-1]}"])(t)  # the held_summary handle (rendered last)
    res, _ = _run(store, spec, answers)
    assert {c for q in _by_q(res).values() for c in q["codes"]} == {"target_derived"}


def test_malformed_answers_are_rejected_and_a_model_cannot_set_a_status(store):
    def answers(t):
        s = _catalog_step(t)
        return {"answers": [
            {"question": "q1", "answer": "ok", "evidence": [s], "status": "cited"},  # model-set status: malformed
            {"question": "q2", "answer": "", "evidence": [s]}]}  # empty answer
    res, _ = _run(store, _spec(), answers)
    assert {c for q in _by_q(res).values() for c in q["codes"]} == {"malformed"}
    for bad in ({"answers": [], "verdict": "pass"}, {"answers": "all good"}, ["not", "an", "object"]):
        res, _ = _run(store, _spec(), lambda t, bad=bad: bad)
        assert [q["status"] for q in res.state["critic_answers"]["questions"]] == ["malformed", "malformed"], bad
    res, _ = _run(store, _spec(), _both(lambda t: [_catalog_step(t), 7]))  # a non-string id
    assert {c for q in _by_q(res).values() for c in q["codes"]} == {"malformed"}
    res, _ = _run(store, _spec(), _both(lambda t: ["../../evidence.sqlite"]))  # path-shaped
    assert {c for q in _by_q(res).values() for c in q["codes"]} == {"malformed"}
    res, _ = _run(store, _spec(), lambda t: {"answers": [{"question": "q1", "answer": "a",
                                                          "evidence": [_catalog_step(t)] * 9}]})  # too many ids
    assert _by_q(res)["q1"]["codes"] == ["malformed"] and _by_q(res)["q2"]["status"] == "missing"


def test_duplicate_and_missing_answers_do_not_count(store):
    def answers(t):
        a = {"question": "q1", "answer": "ok", "evidence": [_catalog_step(t)]}
        return {"answers": [a, a, {"question": "q9", "answer": "stray", "evidence": [_catalog_step(t)]}]}
    res, _ = _run(store, _spec(), answers)
    q = _by_q(res)
    assert q["q1"]["status"] == "duplicate" and q["q2"]["status"] == "missing"
    assert res.state["critic_answers"]["stray"] == 1 and res.state["gate"]["ok"] is False


def test_an_unparseable_answer_call_leaves_every_question_open(store):
    res, _ = _run(store, _spec(), lambda t: "I refuse to answer in JSON")
    defend = next(s for s in store.trace(res.run_id) if s.node_id == "defend")
    assert defend.kind == StepKind.LLM
    assert res.state["critic_answers"]["status"] == "fail"
    assert res.state["gate"]["ok"] is False


def test_the_gate_fails_closed_without_a_check_step_and_ignores_run_state(store):
    res, _ = _run(store, _spec(with_check=False), _both(lambda t: [_catalog_step(t)]))
    assert res.state["gate"]["ok"] is False and "never checked" in res.state["gate"]["why"]
    # an evolved node overwrites `critic_answers` in state with a pass: the gate recomputes from the trace
    res, _ = _run(store, _spec(forge=True), _both(lambda t: []))
    assert res.state["critic_answers"]["status"] == "pass"  # the forged state
    assert res.state["gate"]["ok"] is False and "uncited" in res.state["gate"]["why"]


def test_ids_quoted_only_in_retry_turns_are_not_visible(store):
    """Visibility is the prompt the model was SHOWN (system + first user message), not retry turns quoting the
    model's own earlier output."""
    from custom_harness.records import TraceStep
    st = TraceStep(run_id="r", index=5, node_id="defend", kind=StepKind.LLM, input={}, started_at=0, ended_at=0,
                   output={"messages": [{"role": "user", "content": "shown step:1"},
                                        {"role": "assistant", "content": "step:2 blob:abcdefabcdefabcd"},
                                        {"role": "user", "content": "contract violated: 'step:3'"}]})
    assert R.rendered_prompt(st) == "shown step:1"
    assert CA._visible("step:1", "shown step:1") and not CA._visible("step:1", "shown step:12")


def test_no_kept_question_means_nothing_to_answer(store):
    def respond(req):
        return json.dumps({"questions": ["Use a random forest."]})  # a directive: dropped by the socratic filter
    llm = MockBackend(respond)
    spec = _spec(with_check=False)
    res = Engine(spec, backends={"judge": llm, "proposer": MockBackend(["{}"])}, store=store).run("t", {})
    assert res.state["critique"]["questions"] == [] and res.state["gate"] == {"ok": True, "why": ""}


# ---------------------------------------------------------------- wired into research_v1 / research_v2
from pathlib import Path  # noqa: E402

import yaml  # noqa: E402

from custom_harness.sandbox import LocalSandbox  # noqa: E402
from test_research import rings_task, scripted_llm  # noqa: E402, F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[1]


def test_research_v1_claim_with_uncited_critic_answers_is_certified_but_not_publishable(store, tmp_path, rings_task):  # noqa: F811
    """Deny-only: the Kernel certificate is unchanged; only publication is blocked (research.certify recomputes the
    check from the trace)."""
    def llm_fn(req):
        if "Answer every critic question" in req.messages[-1].content:
            return json.dumps({"answers": [{"question": "q1", "answer": "Obviously fine.", "evidence": []}]})
        return scripted_llm(req)
    llm = MockBackend(llm_fn)
    spec = HarnessSpec.model_validate(yaml.safe_load((ROOT / "examples/research_v1.yaml").read_text()))
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    s = res.state
    assert s["certificate"]["label"] == "CERTIFIED-INTERNAL", s["certificate"]
    assert s["critic_answers"]["status"] == "fail"
    assert [q["status"] for q in s["critic_answers"]["questions"]] == ["rejected", "missing"]
    assert s["publishable"] is False
    assert any(b.startswith("critic (GOAL L4.8): round 1: 2 of 2 question(s)") for b in s["publish_blockers"]), \
        s["publish_blockers"]
    assert s["audit"]["passed"], s["audit"]


def test_research_v2_wires_the_critic_answer_check(store, tmp_path, rings_task):  # noqa: F811
    doc = yaml.safe_load((ROOT / "examples/research_v2.yaml").read_text())
    nodes, edges = doc["nodes"], doc["edges"]
    assert nodes["critic_evidence"]["module"] == "research.critic_evidence"
    assert nodes["defend"]["role"] == "proposer" and nodes["check_answers"]["params"]["answerer"] == "defend"
    pairs = {(e["source"], e["target"]) for e in edges}
    assert {("socratic", "critic_evidence"), ("critic_evidence", "defend"), ("defend", "check_answers"),
            ("check_answers", "next")} <= pairs
    assert ("socratic", "next") not in pairs  # the check cannot be skipped by the default route
