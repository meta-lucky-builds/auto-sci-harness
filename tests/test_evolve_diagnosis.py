"""Structured diagnosis, remaining parts (GOAL §5 L6.3, AutoSaddler + Self-Harness):

- content-level first divergence (outputs, not only node/route/error), compared on embargoed content;
- verifier-evidence linkage: every signature cites its runs' verdicts and failed Kernel checks, and every reference
  it carries resolves (in the trace, and in the export the proposer reads); unlinkable signatures are dropped;
- citation enforcement: an edit citing anything that resolves to nothing the proposer was shown is rejected
  unevaluated (`rejected_claims`), and honest citations pass;
- the claimed-diff check: the claimed change must equal the real diff of the typed ops (hidden change, false claim,
  missing claim all rejected; honest claims pass);
- embargo: gate runs and held-out values never reach the diagnosis.

Deterministic: hand-built traces and table-driven evaluators, no model call.
"""

from __future__ import annotations

import json
import re

from custom_harness.evolve import EvolveConfig, RunOutcome, evolve_generation
from custom_harness.evolve.claims import (
    canonical_claim,
    check_claimed_diff,
    check_claims,
    digest_refs,
    export_refs,
    spec_diff,
)
from custom_harness.evolve.diagnosis import first_divergence, link_digest
from custom_harness.evolve.edits import Edit, EditOp, apply_edit
from custom_harness.evolve.export import export_evidence
from custom_harness.evolve.propose import build_prompt, diagnose
from custom_harness.evolve.proposer_agent import ProposerAgent
from custom_harness.gateway import MockBackend
from custom_harness.kernel.ledger import Ledger
from custom_harness.records import Cost, RunStatus, StepKind, TraceStep
from custom_harness.sandbox import LocalSandbox
from test_evolve import PROPOSALS, seed_spec
from test_evolve_evidence import GOOD_STEPS, CostTable, _add_run, _gain, _outcome, _task_dir, _tasks, _tokens

HELD_OUT = 0.873141592653  # a continuous held-out statistic (certificate subject mean): must never reach a digest
HELD_TEXT = "held-out accuracy 0.8731 on 412 hidden rows"  # report text quoting held-out values


def _trace(store, spec, steps, task="evo0", status=RunStatus.COMPLETED):
    rid = _add_run(store, spec, task, steps, status=status)
    return rid, store.trace(rid)


def _code(code: str, **extra) -> tuple:
    return ("implement", {"code": code, **extra}, None)


# ================================================================ content-level divergence (c)
def test_content_divergence_when_node_route_and_error_agree(store):
    spec = seed_spec()
    ok_id, ok = _trace(store, spec, [GOOD_STEPS[0], _code("model = fit(X, y)"), *GOOD_STEPS[2:]])
    bad_id, bad = _trace(store, spec, [GOOD_STEPS[0], _code("model = None"), *GOOD_STEPS[2:]])
    sig = first_divergence(bad, ok, failing_score=0.0)
    assert sig is not None and sig["mechanism"] == "content" and sig["step"] == 1 and sig["node"] == "implement"
    assert sig["content"]["keys"] == ["code"] and "code" in sig["cause"]
    assert f"{bad_id}#1" in sig["evidence"] and f"{ok_id}#1" in sig["evidence"]
    # names only: the content itself is read in the audited export, never copied into the signature
    assert "model = None" not in json.dumps(sig) and "fit(X, y)" not in json.dumps(sig)


def test_run_specific_ids_are_not_a_content_divergence(store):
    spec = seed_spec()
    _, ok = _trace(store, spec, [GOOD_STEPS[0], _code("x", claim_hash="a" * 64, report_ref="b" * 64,
                                                       evidence_ids=["ev_" + "1" * 16]), *GOOD_STEPS[2:]])
    _, bad = _trace(store, spec, [GOOD_STEPS[0], _code("x", claim_hash="c" * 64, report_ref="d" * 64,
                                                        evidence_ids=["ev_" + "2" * 16]), *GOOD_STEPS[2:]])
    assert first_divergence(bad, ok, failing_score=0.0) is None
    # ...while a real content change next to the same id noise is still found
    _, other = _trace(store, spec, [GOOD_STEPS[0], _code("y", claim_hash="e" * 64), *GOOD_STEPS[2:]])
    sig = first_divergence(other, ok, failing_score=0.0)
    assert sig is not None and sig["content"]["keys"] == ["code"]


def test_decision_divergence_carries_the_first_content_difference_before_it(store):
    spec = seed_spec()
    _, ok = _trace(store, spec, [GOOD_STEPS[0], _code("good"), ("run", {"route": "claim"}, None), ("claim", {}, None)])
    _, bad = _trace(store, spec, [GOOD_STEPS[0], _code("bad"), ("run", {"route": "give_up"}, None),
                                  ("report", {}, None)])
    sig = first_divergence(bad, ok, failing_score=0.2)
    assert sig["mechanism"] == "route" and sig["step"] == 2
    assert sig["content"] == {"step": 1, "node": "implement", "keys": ["code"]}


def test_embargoed_content_is_not_compared_and_never_reaches_the_signature(store):
    """Two runs whose certificates differ only in held-out statistics and whose report text quotes them: the
    diagnosis sees what the export may show (labels), so the held-out values are neither a divergence nor copied."""
    spec = seed_spec()

    def cert(mean):
        return ("certify", {"certificate": {"hash": "e" * 64, "label": "REJECTED", "subject_mean": mean,
                                            "ci": [mean - 0.1, mean + 0.1]},
                            "report": f"{HELD_TEXT} ({mean})"}, None)

    _, ok = _trace(store, spec, [GOOD_STEPS[0], _code("good"), *GOOD_STEPS[2:3], cert(0.5)])
    _, bad = _trace(store, spec, [GOOD_STEPS[0], _code("bad"), *GOOD_STEPS[2:3], cert(HELD_OUT)])
    sig = first_divergence(bad, ok, failing_score=0.0)
    assert sig is not None and sig["mechanism"] == "content" and sig["content"]["keys"] == ["code"]
    blob = json.dumps(sig)
    assert str(HELD_OUT) not in blob and "0.8731" not in blob and "hidden rows" not in blob


# ================================================================ references resolve (bug fixes found here)
def test_truncated_signature_cites_only_steps_that_exist(store):
    """Before: a truncated failing run was cited at '<run>#<len>' - a step it never had."""
    spec = seed_spec()
    ok_id, ok = _trace(store, spec, GOOD_STEPS)
    bad_id, bad = _trace(store, spec, GOOD_STEPS[:2], status=RunStatus.FAILED)
    sig = first_divergence(bad, ok, failing_score=0.0)
    steps = {f"{bad_id}#{st.index}" for st in bad} | {f"{ok_id}#{st.index}" for st in ok}
    assert sig["mechanism"] == "truncated" and set(sig["evidence"]) <= steps, sig["evidence"]
    assert f"{bad_id}#1" in sig["evidence"]


def test_references_use_the_steps_own_index_when_a_trace_has_a_gap():
    def st(run, i, node, out=None):
        return TraceStep(run_id=run, index=i, node_id=node, kind=StepKind.MODULE, input={}, output=out or {},
                         cost=Cost(), started_at=0.0, ended_at=1.0)

    ok = [st("run_ok", 0, "setup"), st("run_ok", 1, "implement"), st("run_ok", 2, "run", {"route": "claim"})]
    bad = [st("run_bad", 0, "setup"), st("run_bad", 5, "implement"), st("run_bad", 9, "run", {"route": "x"})]
    sig = first_divergence(bad, ok, failing_score=0.0)
    assert sig["step"] == 2 and "run_bad#9" in sig["evidence"] and "run_bad#2" not in sig["evidence"]


# ================================================================ verifier-evidence linkage (a)
def _cert_world(store, tmp_path):
    spec = seed_spec()
    tasks = _tasks(tmp_path)
    blob = store.blobs.put_json({"label": "REJECTED", "checks": {
        "K7": {"status": "fail", "reasons": [f"subject mean {HELD_OUT} too close to baseline"]},
        "K0": {"status": "pass", "reasons": []}}})
    Ledger(store).append("certificate", {"cert_hash": "c" * 64, "claim_hash": "d" * 64, "label": "REJECTED",
                                         "blob": blob, "resolution": {}})
    ok_id, _ = _trace(store, spec, [GOOD_STEPS[0], _code("good"), *GOOD_STEPS[2:]], task="e0")
    bad_id, _ = _trace(store, spec, [GOOD_STEPS[0], _code("bad"), ("run", {"route": "claim"}, None),
                                     ("certify", {"certificate": {"hash": "c" * 64, "label": "REJECTED",
                                                                  "subject_mean": HELD_OUT}}, None)], task="e0")
    gate_id, _ = _trace(store, spec, GOOD_STEPS, task="g0")
    return spec, tasks, ok_id, bad_id, gate_id


def test_signatures_cite_verifier_evidence_that_resolves_in_the_export(store, tmp_path):
    _, tasks, ok_id, bad_id, _ = _cert_world(store, tmp_path)
    failing, ref = _outcome(bad_id, "e0", 0.0), _outcome(ok_id, "e0", 1.0, seed=1, verified=True)
    digest = diagnose([failing], store, references=[ref], evolve_ids={"e0", "e1", "e2"})
    (d,) = digest["divergences"]
    v = d["verifier"]
    assert v["failing"]["ref"] == f"{bad_id}#verdict" and v["failing"]["score"] == 0.0
    assert v["failing"]["failed_checks"] == [f"{bad_id}#3/K7"]
    assert v["reference"]["ref"] == f"{ok_id}#verdict" and v["reference"]["verified"] is True
    assert {f"{bad_id}#verdict", f"{ok_id}#verdict", f"{bad_id}#3/K7"} <= set(d["evidence"])
    assert digest["unlinked"] == 0
    # every reference the proposer is shown resolves in the export written from the same runs
    exp = export_evidence(store, tmp_path / "export", runs=[failing, ref], tasks=tasks)
    refs = export_refs(exp.path)
    assert set(d["evidence"]) <= refs, set(d["evidence"]) - refs
    # the failed check's reason quotes a held-out statistic: only the check NAME reaches the digest
    assert str(HELD_OUT) not in json.dumps(digest)


def test_an_unresolvable_signature_is_dropped_not_shown(store, tmp_path):
    _, _, ok_id, bad_id, _ = _cert_world(store, tmp_path)
    digest = diagnose([_outcome(bad_id, "e0", 0.0)], store, references=[_outcome(ok_id, "e0", 1.0, seed=1)])
    assert len(digest["divergences"]) == 1
    # against a mount that lacks the reference run's trace (e.g. a partial export), the signature does not link
    partial = {r for r in digest_refs(digest) if not r.startswith(ok_id)}
    linked = link_digest(digest, partial)
    assert linked["divergences"] == [] and linked["unlinked"] == 1
    # a forged reference inside a signature is caught the same way
    forged = json.loads(json.dumps(digest))
    forged["divergences"][0]["evidence"].append("run_" + "0" * 16 + "#2")
    assert link_digest(forged, digest_refs(digest))["divergences"] == []


# ================================================================ embargo: held-out runs never reach the diagnosis
def test_gate_runs_never_reach_the_diagnosis(store, tmp_path):
    _, tasks, _, bad_id, gate_id = _cert_world(store, tmp_path)
    # a failing gate-task run (its failure signature would become a cluster) and a better-scoring gate run (a
    # reference): both are refused before anything is read
    gate_ref = _outcome(gate_id, "g0", 1.0)
    planted = RunOutcome("g0", gate_id, 3, 0.0, False, "completed", Cost(), ["run: held-out crash g0"])
    digest = diagnose([_outcome(bad_id, "e0", 0.0), planted], store, references=[gate_ref],
                      evolve_ids={t.task_id for t in tasks if t.split == "evolve"})
    blob = json.dumps(digest)
    assert gate_id not in blob and "g0" not in blob and "held-out crash" not in blob
    assert digest["n_excluded_split"] == 2 and digest["no_reference"] == ["e0"]


# ================================================================ citation enforcement (a, cluster ids)
def _edit(**kw) -> Edit:
    item = PROPOSALS["edits"][0]
    return Edit.model_validate({"type": item["type"], "rationale": "r", "ops": [
        {"op": "replace", "path": "/nodes/run/params/max_fixes", "value": 2}],
        "claimed_change": kw.pop("claimed", ["/nodes/run/params/max_fixes"]), **kw})


def test_citations_must_resolve_in_what_the_proposer_saw():
    digest = {"clusters": [{"id": "C0"}], "divergences": [{"id": "D0", "evidence": ["run_a#2", "run_a#verdict"]}]}
    ok = check_claims(_edit(evidence=["C0", "D0", "run_a#2", "run_a#verdict"],
                            predicted={"failure_clusters_fixed": ["D0"]}), digest)
    assert ok["passed"] and ok["unsupported"] == []
    for bad in ("C3", "D9", "run_a#7", "run_b#2", "run_a#2/K7", "see the crash in run_a", "traces/run_a.json"):
        r = check_claims(_edit(evidence=["C0", bad]), digest)
        assert not r["passed"] and r["unsupported"] == [bad], bad
    # citing nothing claims nothing
    assert check_claims(_edit(), digest)["passed"]


def _gen(store, tmp_path, items, *, evaluator=None):
    parent = seed_spec()
    tasks = _tasks(tmp_path)
    ev = evaluator or CostTable(store, tasks, parent.spec_hash(), _gain, lambda a, t, s: _tokens(100))
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=MockBackend([json.dumps({"edits": items})]),
                            store=store, cfg=EvolveConfig(seeds=(0, 1), n_edits=len(items) or 1, race_rounds=1))
    return res, ev


def _item(path="/nodes/run/params/max_fixes", value=2, **kw) -> dict:
    return {"type": "control_flow", "ops": [{"op": "replace", "path": path, "value_json": json.dumps(value)}],
            "rationale": kw.pop("rationale", "allow fix attempts"), "evidence": kw.pop("evidence", []),
            "failure_clusters_fixed": kw.pop("clusters", []), "expected_delta": 1.0, "tasks_at_risk": [],
            "claimed_change": kw.pop("claimed", [path]), **kw}


def test_loop_rejects_unresolvable_citations_unevaluated_and_honest_ones_pass(store, tmp_path):
    forged = [_item(evidence=["run_" + "0" * 16 + "#3"]), _item(clusters=["C7"]),
              _item(evidence=["the crash in e0"])]
    res, ev = _gen(store, tmp_path, forged)
    st = [r["status"] for r in store.records("lineage")]
    assert st == ["rejected_claims"] * 3, st
    assert res.counters["rejected_claims"] == 3 and res.counters["raced"] == 0 and not res.accepted
    assert {k[0] for k in ev.cache} == {seed_spec().spec_hash()}  # no candidate was ever run
    assert all("resolve to nothing" in r["reason"] for r in store.records("lineage"))
    # the same ops, honestly cited (C0 is the diagnosis's cluster), are a new proposal: not novelty-blocked
    res2, _ = _gen(store, tmp_path, [_item(evidence=["C0"], clusters=["C0"])], evaluator=ev)
    assert res2.accepted, res2.decision
    assert store.records("harness_edit")[-1]["claims_check"]["passed"] is True


# ================================================================ claimed-diff check (b)
def test_spec_diff_and_claim_canonicalisation():
    a = {"nodes": {"run": {"params": {"max_fixes": 0, "x": 1}, "prompt": "p"}}, "edges": [{"t": 1}, {"t": 2}]}
    b = {"nodes": {"run": {"params": {"max_fixes": 2, "x": 1}, "prompt": "q"}, "lint": {}}, "edges": [{"t": 3}]}
    assert spec_diff(a, b) == ["/edges", "/nodes/lint", "/nodes/run/params/max_fixes", "/nodes/run/prompt"]
    assert spec_diff(a, a) == []
    assert canonical_claim("/edges/0/t", a, b) == "/edges"  # a list is one value
    assert canonical_claim("/nodes/run/prompt", a, b) == "/nodes/run/prompt"


def test_claimed_diff_mismatch_is_rejected_fail_closed():
    parent = seed_spec().model_dump(mode="json")
    after = json.loads(json.dumps(parent))
    after["nodes"]["run"]["params"]["max_fixes"] = 2
    honest = check_claimed_diff(_edit(), parent, after)
    assert honest["passed"] and honest["real"] == ["/nodes/run/params/max_fixes"]
    hidden = json.loads(json.dumps(after))
    hidden["nodes"]["fix"]["prompt"] = "something else"
    r = check_claimed_diff(_edit(), parent, hidden)  # claims max_fixes only; also changed the fix prompt
    assert not r["passed"] and r["unclaimed"] == ["/nodes/fix/prompt"]
    r = check_claimed_diff(_edit(claimed=["/nodes/run/params/max_fixes", "/nodes/audit/prompt"]), parent, after)
    assert not r["passed"] and r["n_false_claims"] == 1 and "/nodes/audit/prompt" not in json.dumps(r)
    r = check_claimed_diff(_edit(claimed=None), parent, after)
    assert not r["passed"] and "no claimed_change" in r["reason"]


def test_loop_rejects_a_hidden_change_a_false_claim_and_a_missing_claim(store, tmp_path):
    run_node = seed_spec().model_dump(mode="json")["nodes"]["run"]
    smuggled = json.loads(json.dumps(run_node))
    smuggled["params"]["max_fixes"] = 2
    smuggled["params"]["max_improves"] = 9  # the second change the claim does not mention
    items = [
        _item("/nodes/run", smuggled, claimed=["/nodes/run/params/max_fixes"]),       # hidden change
        _item(claimed=["/nodes/run/params/max_fixes", "/nodes/next/prompt"]),         # claims a change not made
        {k: v for k, v in _item().items() if k != "claimed_change"},                   # no claim at all
    ]
    res, ev = _gen(store, tmp_path, items)
    lin = store.records("lineage")
    assert [r["status"] for r in lin] == ["rejected_claims"] * 3, [r["status"] for r in lin]
    assert lin[0]["claims_check"]["claimed_diff"]["unclaimed"] == ["/nodes/run/params/max_improves"]
    assert lin[1]["claims_check"]["claimed_diff"]["n_false_claims"] == 1
    assert "no claimed_change" in lin[2]["reason"]
    assert res.counters["raced"] == 0 and {k[0] for k in ev.cache} == {seed_spec().spec_hash()}


def test_honest_claims_pass_including_whole_node_and_list_edits(store, tmp_path):
    spec = seed_spec()
    run_node = spec.model_dump(mode="json")["nodes"]["run"]
    run_node["params"]["max_fixes"] = 2
    idx = next(i for i, e in enumerate(spec.edges) if e.source == "fix" and e.when is None)
    items = [_item("/nodes/run", run_node, claimed=["/nodes/run/params/max_fixes"])]
    res, _ = _gen(store, tmp_path, items)
    assert res.accepted, (res.decision, [r.get("reason") for r in store.records("lineage")])
    # an edge re-route claimed at its op path: a list is one value, so the claim is read as "/edges"
    edge = _edit(claimed=[f"/edges/{idx}/target"]).model_copy(update={"ops": [
        EditOp(op="replace", path=f"/edges/{idx}/target", value="next")]})
    after = apply_edit(spec, edge)  # a valid edit (statically accepted), so the check below is not vacuous
    r = check_claimed_diff(edge, spec.model_dump(mode="json"), after.model_dump(mode="json"))
    assert r["passed"] and r["real"] == ["/edges"], r


def test_prompt_states_the_citation_forms_and_the_claimed_change_rule():
    p = build_prompt(seed_spec(), {"clusters": [], "divergences": []}, [], 1, ())
    assert "<run_id>#verdict" in p and "claimed_change" in p and "rejected unevaluated" in p


# ================================================================ agentic proposer: citations of the export it read
class TraceTable(CostTable):
    """CostTable whose runs exist in the store (so the export has traces to cite)."""

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            o = super().outcome(spec, unit)
            rid = _add_run(self.store, spec, unit[0], GOOD_STEPS, seed=unit[1])
            self.cache[key] = RunOutcome(o.task_id, rid, o.seed, o.score, o.verified, o.status, o.cost,
                                         o.failure_signatures)
        return self.cache[key]


def test_agent_citing_its_export_passes_and_a_forged_reference_is_rejected(store, tmp_path):
    def responder(req):
        if len(req.messages) == 1:
            return json.dumps({"tool": "read", "path": "scores.json"})
        found = re.search(r"run_[0-9a-f]{16}", req.messages[-1].content)  # a run id the agent read in scores.json
        assert found, req.messages[-1].content[:300]
        rid = found.group(0)
        real = _item(evidence=[f"{rid}#1", f"{rid}#verdict", "C0"], clusters=["C0"])
        fake = _item("/nodes/run/params/max_improves", 3, evidence=["run_" + "f" * 16 + "#1"])
        return json.dumps({"final": {"edits": [real, fake]}})

    boxes = tmp_path / "boxes"
    agent = ProposerAgent(MockBackend(responder), export_root=tmp_path / "exports",
                          sandbox_factory=lambda k: LocalSandbox(boxes / k, i_understand_no_isolation=True))
    parent = seed_spec()
    tasks = _tasks(tmp_path)
    ev = TraceTable(store, tasks, parent.spec_hash(), _gain, lambda a, t, s: _tokens(100))
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=agent, store=store,
                            cfg=EvolveConfig(seeds=(0, 1), n_edits=2, race_rounds=1))
    st = {r["edit"]["ops"][0]["path"]: r["status"] for r in store.records("lineage") if "edit" in r}
    assert st["/nodes/run/params/max_improves"] == "rejected_claims", st
    assert st["/nodes/run/params/max_fixes"] in ("raced", "accepted"), st
    assert res.counters["rejected_claims"] == 1


def test_export_refs_cover_steps_verdicts_and_failed_checks(store, tmp_path):
    spec = seed_spec()
    task = _task_dir(tmp_path, "evo0")
    from custom_harness.evolve import SuiteTask
    blob = store.blobs.put_json({"label": "REJECTED", "checks": {"K7": {"status": "fail", "reasons": ["x"]}}})
    Ledger(store).append("certificate", {"cert_hash": "c" * 64, "claim_hash": "d" * 64, "label": "REJECTED",
                                         "blob": blob, "resolution": {}})
    rid = _add_run(store, spec, "evo0", [*GOOD_STEPS[:3], ("certify", {"certificate": {"hash": "c" * 64,
                                                                                       "label": "REJECTED"}}, None)])
    exp = export_evidence(store, tmp_path / "x", runs=[_outcome(rid, "evo0", 0.5)],
                          tasks=[SuiteTask("evo0", task, "evolve", family="f")])
    assert export_refs(exp.path) == {f"{rid}#0", f"{rid}#1", f"{rid}#2", f"{rid}#3", f"{rid}#verdict",
                                     f"{rid}#3/K7"}
