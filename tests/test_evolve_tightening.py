"""Tightenings requested after the diagnosis work (2026-10-08):

- the critic chain cannot be switched off by an edit: the socratic node (and, on b-research, `critic_evidence` and
  `check_answers`) keeps its definition (e.g. `max_questions` cannot become 0) and cannot be removed, while its wiring
  stays evolvable (honest edits around it still pass);
- the critic-answer / recall state keys and modules (b-research, by id only) are reserved;
- anchored ids are matched with \\Z, not $: `$` also matches before a trailing newline, so "C0\\n" passed as a
  diagnosis id and a hex hash plus "\\n" passed as a file-name-safe content hash.
"""

from __future__ import annotations

import json

import pytest

from custom_harness.evolve.claims import check_citations
from custom_harness.evolve.diagnosis import first_divergence
from custom_harness.evolve.edits import Edit, EditRejected, apply_edit, screen_prediction
from custom_harness.evolve.export import export_evidence
from test_evolve import seed_spec
from test_evolve_evidence import GOOD_STEPS, _add_run, _outcome, _task_dir


def _edit(*ops: dict) -> Edit:
    return Edit.model_validate({"type": "params", "rationale": "r", "ops": list(ops)})


# ================================================================ critic chain pinned
@pytest.mark.parametrize("op", [
    {"op": "replace", "path": "/nodes/socratic/params/max_questions", "value": 0},
    {"op": "replace", "path": "/nodes/socratic/params", "value": {}},
    {"op": "replace", "path": "/nodes/socratic/module", "value": "research.next_hypothesis"},
])
def test_an_edit_cannot_switch_the_socratic_filter_off(op):
    spec = seed_spec()
    assert spec.nodes["socratic"].params.get("max_questions", 0) >= 1  # the attack target exists
    with pytest.raises(EditRejected, match="critic-chain node 'socratic'"):
        apply_edit(spec, _edit(op))


def test_an_edit_cannot_remove_the_socratic_node():
    spec = seed_spec()
    doc = spec.model_dump(mode="json")
    # bypass: drop the node and route its predecessor straight on (the wiring itself would be a valid edit)
    edges = [e for e in doc["edges"] if e["target"] != "socratic" and e["source"] != "socratic"]
    edges.append({"source": "critic", "target": "next", "when": None})
    with pytest.raises(EditRejected):
        apply_edit(spec, _edit({"op": "remove", "path": "/nodes/socratic", "value": None},
                               {"op": "replace", "path": "/edges", "value": edges}))


def test_honest_edits_around_the_critic_chain_still_pass():
    spec = seed_spec()
    # the critic's prompt is an evolvable surface
    critic_prompt = apply_edit(spec, _edit({"op": "replace", "path": "/nodes/critic/prompt", "value": "Ask more."}))
    assert critic_prompt.nodes["critic"].prompt == "Ask more."
    # a step inserted after the socratic filter (its wiring is evolvable)
    idx = next(i for i, e in enumerate(spec.edges) if e.source == "socratic")
    new = apply_edit(spec, _edit(
        {"op": "add", "path": "/nodes/lint", "value": {"kind": "llm", "role": "executor", "prompt": "p",
                                                        "output_key": "lint_notes"}},
        {"op": "replace", "path": f"/edges/{idx}", "value": {"source": "socratic", "target": "lint", "when": None}},
        {"op": "add", "path": "/edges/-", "value": {"source": "lint", "target": "next", "when": None}}))
    assert new.nodes["socratic"] == spec.nodes["socratic"] and "lint" in new.nodes


@pytest.mark.parametrize("key", ["critic_answers", "critic_evidence", "recalled", "recall_log"])
def test_an_added_node_cannot_write_critic_or_recall_state(key):
    spec = seed_spec()
    idx = next(i for i, e in enumerate(spec.edges) if e.source == "socratic")
    with pytest.raises(EditRejected, match="reserved state key"):
        apply_edit(spec, _edit(
            {"op": "add", "path": "/nodes/forge", "value": {"kind": "llm", "role": "executor", "prompt": "p",
                                                             "output_key": key}},
            {"op": "replace", "path": f"/edges/{idx}", "value": {"source": "socratic", "target": "forge",
                                                                 "when": None}},
            {"op": "add", "path": "/edges/-", "value": {"source": "forge", "target": "next", "when": None}}))


@pytest.mark.parametrize("module", ["research.critic_evidence", "research.check_critic_answers", "research.recall"])
def test_an_added_node_cannot_run_a_critic_or_recall_module(module):
    from custom_harness.evolve.edits import RESERVED_WRITING_MODULES
    assert module in RESERVED_WRITING_MODULES


# ================================================================ \Z, not $
def test_a_cluster_id_with_a_trailing_newline_is_not_a_diagnosis_id():
    e = Edit.model_validate({"type": "params", "rationale": "r", "ops": [],
                             "predicted": {"failure_clusters_fixed": ["C0\n", "C1"]}})
    clean, notes = screen_prediction(e, task_ids=[], deny=[], max_text_chars=100, max_delta=2.0, delta_decimals=3)
    assert clean.predicted.failure_clusters_fixed == ["C1"] and any("dropped" in n for n in notes)


def test_citation_forms_do_not_accept_a_trailing_newline():
    for bad in ("C0\n", "run_a#2\n", "run_a#verdict\n"):
        e = Edit.model_validate({"type": "params", "rationale": "r", "ops": [], "evidence": [bad]})
        r = check_citations(e, {bad, bad.strip()})  # even a poisoned known set does not make it a citation
        assert r["unsupported"] == [bad], bad


def test_a_hash_with_a_trailing_newline_is_never_an_export_file_name(store, tmp_path):
    from custom_harness.evolve import SuiteTask
    spec = seed_spec()
    rid = _add_run(store, spec, "evo0", GOOD_STEPS)
    store.append_record("lineage", {"parent": spec.spec_hash(), "status": "raced", "candidate": "a" * 64 + "\n"})
    exp = export_evidence(store, tmp_path / "x", runs=[_outcome(rid, "evo0", 0.5)],
                          tasks=[SuiteTask("evo0", _task_dir(tmp_path, "evo0"), "evolve", family="f")])
    assert not [f for f in exp.files if "\n" in f or f.startswith("candidates/" + "a" * 64)], exp.files


def test_an_id_with_a_trailing_newline_is_content_not_noise(store):
    spec = seed_spec()
    ok = store.trace(_add_run(store, spec, "evo0", [GOOD_STEPS[0], ("implement", {"code": "a" * 64 + "\n"}, None)]))
    bad = store.trace(_add_run(store, spec, "evo0", [GOOD_STEPS[0], ("implement", {"code": "b" * 64 + "\n"}, None)]))
    sig = first_divergence(bad, ok, failing_score=0.0)
    assert sig is not None and sig["content"]["keys"] == ["code"], json.dumps(sig)


def test_honest_ids_still_match():
    e = Edit.model_validate({"type": "params", "rationale": "r", "ops": [], "evidence": ["C0", "run_a#2"],
                             "predicted": {"failure_clusters_fixed": ["C0"]}})
    assert check_citations(e, {"C0", "run_a#2"})["passed"]
    clean, _ = screen_prediction(e, task_ids=[], deny=[], max_text_chars=100, max_delta=2.0, delta_decimals=3)
    assert clean.predicted.failure_clusters_fixed == ["C0"]
