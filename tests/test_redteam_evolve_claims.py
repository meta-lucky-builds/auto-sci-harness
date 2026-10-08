"""Red-team attacks on the evolve claim checks (claims.py), the diagnosis (diagnosis.py) and the digest screen
(export.screen_digest).

Each test carries an attack out (and asserts it reached the code path under attack), then asserts it is refused or
harmless. Attacks that succeed on code outside claims.py / diagnosis.py / export.screen_digest are marked
xfail(strict=True) with the reason, so the suite stays green and turns red the day they are fixed (to remove the mark).

Deterministic: hand-built traces and table-driven evaluators, no model call.
"""

from __future__ import annotations

import json

import pytest

from custom_harness.evolve import RunOutcome, SuiteTask, evolve_generation
from custom_harness.evolve.claims import (
    canonical_claim,
    check_claimed_diff,
    check_claims,
    digest_refs,
    export_refs,
    spec_diff,
)
from custom_harness.evolve.diagnosis import first_divergence, link_digest
from custom_harness.evolve.edits import EditOp, apply_edit
from custom_harness.evolve.export import export_evidence, screen_digest
from custom_harness.evolve.loop import EvolveConfig
from custom_harness.evolve.propose import diagnose
from custom_harness.gateway import MockBackend
from custom_harness.records import Cost
from custom_harness.verify.leak import CANARY_PREFIX
from test_evolve import seed_spec
from test_evolve_diagnosis import (
    HELD_OUT,
    LeakyTable,
    TraceTable,
    _cert_world,
    _code,
    _edit,
    _gen,
    _item,
    _trace,
)
from test_evolve_evidence import GOOD_STEPS, _add_run, _gain, _outcome, _task_dir, _tasks, _tokens

DIGEST = {"clusters": [{"id": "C0"}, {"id": "C1"}],
          "divergences": [{"id": "D0", "evidence": ["run_a#2", "run_a#verdict", "run_a#3/K7"]}]}


def _lineage(store):
    return [r for r in store.records("lineage") if "edit" in r]


# ================================================================ 1. citations
@pytest.mark.parametrize("bad", [
    "C０",          # fullwidth zero: \d matches it, the known set does not
    "C٠",          # arabic-indic zero
    "c0", "C0 ", " C0", "C0\n", "C00", "C+0",
    "D1",               # an id the current digest does not have (e.g. from an earlier generation's digest)
    "RUN_A#2", "run_a#02", "run_a#２", "run_a#2 ", "run_a #2",
    "run_a#99",         # a step past the end
    "run_a#3/K0",       # a check that PASSED (only failed checks are citable)
    "run_a#3/k7", "run_a#3/K7 ", "run_a#3/K7/x", "run_a#verdict/K7", "run_a#VERDICT",
    "run_b#verdict",    # a forged run id
    "", "None", "#2", "run_a#",
])
def test_citation_variants_of_real_refs_are_unsupported(bad):
    ok = check_claims(_edit(evidence=["C0", "D0", "run_a#2", "run_a#3/K7"]), DIGEST)
    assert ok["passed"], ok
    r = check_claims(_edit(evidence=["C0", bad]), DIGEST)
    assert not r["passed"] and r["unsupported"] == [bad], (bad, r)
    r = check_claims(_edit(predicted={"failure_clusters_fixed": [bad]}), DIGEST)
    assert not r["passed"], bad


def test_a_known_set_poisoned_with_a_non_citation_string_still_refuses_it():
    """digest_refs takes every string a signature carries: a malformed one is in the known set but still fails the
    form check (both are required), so a digest cannot launder free text into a citation."""
    poisoned = json.loads(json.dumps(DIGEST))
    poisoned["divergences"][0]["evidence"].append("see traces/run_a.json")
    poisoned["clusters"].append({"signature": "no id"})  # id None -> "None" in the known set
    known = digest_refs(poisoned)
    assert "see traces/run_a.json" in known and "None" in known  # the attack reached the known set
    for bad in ("see traces/run_a.json", "None"):
        assert not check_claims(_edit(evidence=[bad]), poisoned)["passed"]


def test_duplicated_citations_are_one_citation_and_a_forged_one_among_them_still_rejects():
    r = check_claims(_edit(evidence=["C0"] * 50, predicted={"failure_clusters_fixed": ["C0"] * 50}), DIGEST)
    assert r["passed"] and r["supported"] == ["C0"]
    r = check_claims(_edit(evidence=["C0"] * 50 + ["C9"]), DIGEST)
    assert not r["passed"] and r["unsupported"] == ["C9"]


def test_gate_run_refs_and_passed_checks_do_not_resolve_in_the_export(store, tmp_path):
    """The gate run exists in the store (and is passed to the export) but is never written: none of its refs
    resolve. The K0 check passed in the evolve certificate: not citable. Steps past the end: not citable."""
    _, tasks, ok_id, bad_id, gate_id = _cert_world(store, tmp_path)
    assert store.trace(gate_id)  # the gate run really is in the store
    runs = [_outcome(bad_id, "e0", 0.0), _outcome(ok_id, "e0", 1.0, seed=1), _outcome(gate_id, "g0", 1.0)]
    exp = export_evidence(store, tmp_path / "export", runs=runs, tasks=tasks)
    refs = export_refs(exp.path)
    assert f"{bad_id}#3/K7" in refs and f"{bad_id}#verdict" in refs  # the honest refs are there
    assert not any(r.startswith(gate_id) for r in refs)
    for bad in (f"{gate_id}#0", f"{gate_id}#verdict", f"{bad_id}#3/K0", f"{bad_id}#4", f"{bad_id}#2/K7"):
        r = check_claims(_edit(evidence=[bad]), refs=refs)
        assert not r["passed"] and r["unsupported"] == [bad], bad


def test_a_divergence_dropped_by_link_digest_cannot_be_cited(store, tmp_path):
    _, _, ok_id, bad_id, _ = _cert_world(store, tmp_path)
    digest = diagnose([_outcome(bad_id, "e0", 0.0)], store, references=[_outcome(ok_id, "e0", 1.0, seed=1)])
    assert [d["id"] for d in digest["divergences"]] == ["D0"]
    mount = {r for r in digest_refs(digest) if not r.startswith(ok_id) and r != "D0"}  # a mount without ok_id
    linked = link_digest(digest, mount)
    assert linked["divergences"] == []
    known = mount | digest_refs(linked)  # exactly what proposer_agent builds as evidence_refs
    assert not check_claims(_edit(evidence=["D0"]), refs=known)["passed"]
    assert not check_claims(_edit(evidence=[f"{ok_id}#1"]), refs=known)["passed"]
    # the dropped signature's refs that ARE in the mount stay citable (they are in the export the agent read)
    assert check_claims(_edit(evidence=[f"{bad_id}#1"]), refs=known)["passed"]


def test_an_id_from_an_earlier_generation_resolves_only_if_this_digest_has_it(store, tmp_path):
    """Ids are positional per digest (C0 = this generation's most common cluster). Citing D3 from an earlier
    generation that this one does not have is rejected; citing C0 resolves to THIS digest's C0 (stated limit: the
    check cannot tell which generation the proposer meant)."""
    res, _ = _gen(store, tmp_path, [_item(evidence=["D3"], clusters=["C0"])])
    lin = _lineage(store)
    assert [r["status"] for r in lin] == ["rejected_claims"] and "D3" in lin[0]["claims_check"]["unsupported"]
    assert res.counters["raced"] == 0


# --- screen truncation interplay: a citation the screen DROPS is never checked
_FORGED_LONG = "run_" + "f" * 60 + "#1"  # a REF-shaped forgery longer than the screen's 64-char evidence limit


@pytest.mark.xfail(strict=True, reason=(
    "FINDING (loop.py/edits.py, outside the red-team's files): edits.screen_prediction silently drops evidence items "
    "> 64 chars, items after the 20th and failure_clusters_fixed items that are not C<n>/D<n>; the loop runs "
    "check_claims on the SCREENED edit, so a forged citation the screen drops never rejects the edit"))
@pytest.mark.parametrize("attack", ["long", "21st", "cluster_text"])
def test_a_forged_citation_dropped_by_the_screen_still_rejects_the_edit(store, tmp_path, attack):
    item = {"long": _item(evidence=["C0", _FORGED_LONG]),
            "21st": _item(evidence=["C0"] * 20 + ["run_" + "0" * 16 + "#3"]),
            "cluster_text": _item(evidence=["C0"], clusters=["C0", "C7 (the crash)"])}[attack]
    _gen(store, tmp_path, [item])
    (rec, *_) = _lineage(store)
    assert any("dropped" in n for n in rec.get("screen", [])), rec  # the attack reached the screen
    assert rec["status"] == "rejected_claims", (rec["status"], rec["claims_check"])


@pytest.mark.xfail(strict=True, reason=(
    "FINDING (edits.py, outside the red-team's files): screen_prediction drops claimed_change paths > 200 chars and "
    "after the 32nd; a dropped FALSE claim then passes the claimed-diff check (the edits.py comment says a dropped "
    "path 'fails the claimed-diff check, never passes it': true only for dropped REAL paths)"))
@pytest.mark.parametrize("attack", ["long", "33rd"])
def test_a_false_claimed_path_dropped_by_the_screen_still_rejects_the_edit(store, tmp_path, attack):
    real = "/nodes/run/params/max_fixes"
    false = "/nodes/next/prompt"
    claimed = {"long": [real, "/nodes/next/" + "p" * 200], "33rd": [real] * 32 + [false]}[attack]
    _gen(store, tmp_path, [_item(evidence=["C0"], clusters=["C0"], claimed=claimed)])
    (rec, *_) = _lineage(store)
    assert any("claimed_change" in n for n in rec.get("screen", [])), rec  # the attack reached the screen
    assert rec["status"] == "rejected_claims", (rec["status"], rec["claims_check"])


def test_a_dropped_real_claimed_path_does_fail_the_check(store, tmp_path):
    """The other half of the screen interplay: a REAL path the screen drops fails the check (fail closed)."""
    run_node = _spec_json()["nodes"]["run"]
    run_node["params"]["max_fixes"] = 2
    run_node["params"]["max_improves"] = 9
    claimed = ["/nodes/run/params/max_fixes"] * 32 + ["/nodes/run/params/max_improves"]  # the 33rd is dropped
    _gen(store, tmp_path, [_item("/nodes/run", run_node, evidence=["C0"], clusters=["C0"], claimed=claimed)])
    (rec, *_) = _lineage(store)
    assert rec["status"] == "rejected_claims" and rec["claims_check"]["claimed_diff"]["unclaimed"] == [
        "/nodes/run/params/max_improves"]


# ================================================================ 2. claimed diff
def _spec_json():
    return seed_spec().model_dump(mode="json")


def _edit_ops(ops, claimed):
    return _edit(claimed=claimed).model_copy(update={"ops": [EditOp(**o) for o in ops]})


def _check(ops, claimed):
    spec = seed_spec()
    e = _edit_ops(ops, claimed)
    after = apply_edit(spec, e)  # every attack here is a statically valid edit: the check is not vacuous
    return check_claimed_diff(e, spec.model_dump(mode="json"), after.model_dump(mode="json"))


def test_replacing_a_parent_object_and_claiming_the_parent_is_refused():
    run_node = _spec_json()["nodes"]["run"]
    run_node["params"]["max_fixes"] = 2
    run_node["params"]["max_improves"] = 9
    r = _check([{"op": "replace", "path": "/nodes/run", "value": run_node}], ["/nodes/run"])
    assert not r["passed"] and r["n_false_claims"] == 1
    assert set(r["unclaimed"]) == {"/nodes/run/params/max_fixes", "/nodes/run/params/max_improves"}
    r = _check([{"op": "replace", "path": "/nodes/run", "value": run_node}],
               ["/nodes/run/params/max_fixes", "/nodes/run/params/max_improves"])
    assert r["passed"], r


def test_add_over_an_existing_key_is_a_change_that_must_be_claimed():
    run_node = _spec_json()["nodes"]["run"]
    run_node["params"]["max_improves"] = 9
    r = _check([{"op": "add", "path": "/nodes/run", "value": run_node}], [])
    assert r["real"] == ["/nodes/run/params/max_improves"] and r["unclaimed"] == r["real"] and not r["passed"]
    r = _check([{"op": "add", "path": "/nodes/run/params/max_fixes", "value": 2}],
               ["/nodes/run/params/max_fixes"])
    assert r["passed"], r


def test_a_list_edit_beside_an_unclaimed_dict_change_is_refused():
    spec = seed_spec()
    idx = next(i for i, e in enumerate(spec.edges) if e.source == "fix" and e.when is None)
    ops = [{"op": "replace", "path": f"/edges/{idx}/target", "value": "next"},
           {"op": "replace", "path": "/nodes/run/params/max_fixes", "value": 2}]
    r = _check(ops, [f"/edges/{idx}/target"])
    assert not r["passed"] and r["unclaimed"] == ["/nodes/run/params/max_fixes"]


def test_a_claim_at_a_nonexistent_list_index_is_read_as_the_list_documented_granularity():
    """Documented: a list is ONE value, so a claim at any index (even one that does not exist) names the whole list.
    Judged harmless: the claimed diff still names the list that changed (/edges) and nothing else; it cannot hide a
    change OUTSIDE the list. It does mean the stored claim may point at the wrong element of that list."""
    spec = seed_spec()
    idx = next(i for i, e in enumerate(spec.edges) if e.source == "fix" and e.when is None)
    ops = [{"op": "replace", "path": f"/edges/{idx}/target", "value": "next"}]
    assert canonical_claim("/edges/999/target", spec.model_dump(mode="json"), spec.model_dump(mode="json")) == "/edges"
    r = _check(ops, ["/edges/999/target"])
    assert r["passed"] and r["real"] == ["/edges"]


@pytest.mark.parametrize("claim", ["/", "", "nodes/run/params/max_fixes", "/nodes//run/params/max_fixes",
                                   "/nodes/run/params", "/nodes/run", "/nodes", "/nodes/run/params/max_fixes/",
                                   "/nodes/run/params/max_fixes/0", "/nodes/run/params/max~1fixes",
                                   "/nodes/run/params/MAX_FIXES", "/nodes/run/params/max_fixes "])
def test_root_empty_parent_and_mangled_claims_are_refused(claim):
    r = _check([{"op": "replace", "path": "/nodes/run/params/max_fixes", "value": 2}], [claim])
    assert not r["passed"], (claim, r)


def test_json_pointer_escaping_of_keys_with_slash_and_tilde():
    """A key containing '/' or '~' is its own path in escaped form only; the unescaped spelling walks elsewhere and is
    a false claim (fail closed), never a match."""
    a = {"params": {"a/b": 1, "c~d": 1, "a": {"b": 1}, "~1": 1}}
    b = {"params": {"a/b": 2, "c~d": 2, "a": {"b": 1}, "~1": 2}}
    real = spec_diff(a, b)
    assert real == ["/params/a~1b", "/params/c~0d", "/params/~01"]
    e = _edit(claimed=real)
    assert check_claimed_diff(e, a, b)["passed"]
    for wrong in (["/params/a/b", "/params/c~0d", "/params/~01"], ["/params/a~1b", "/params/c~0d", "/params/~1"]):
        assert not check_claimed_diff(_edit(claimed=wrong), a, b)["passed"], wrong
    # lenient, judged harmless: an unescaped '~' that is not a valid escape ("c~d") can only mean the literal key, and
    # that key is the one that changed, so the claim is accepted (it names the real change)
    assert check_claimed_diff(_edit(claimed=["/params/a~1b", "/params/c~d", "/params/~01"]), a, b)["passed"]
    # a claim of the nested /params/a/b when only the key "a/b" changed: hidden change + false claim
    r = check_claimed_diff(_edit(claimed=["/params/a/b", "/params/c~0d", "/params/~01"]), a, b)
    assert r["unclaimed"] == ["/params/a~1b"] and r["n_false_claims"] == 1


@pytest.mark.parametrize("before,after", [(0, False), (1, True), (1, 1.0), (0.0, -0.0), ("1", 1), (None, "null"),
                                          ([1], [1.0]), ({"k": 0}, {"k": False})])
def test_type_changes_are_real_changes_never_hidden(before, after):
    """Canonical JSON keeps 0/false, 1/1.0, 0.0/-0.0 apart: a type change is a real diff that must be claimed."""
    a, b = {"params": {"x": before}}, {"params": {"x": after}}
    assert spec_diff(a, b) != [], (before, after)
    assert not check_claimed_diff(_edit(claimed=[]), a, b)["passed"]


def test_type_changes_through_the_real_spec_are_seen():
    for v in (False, 2.0):
        r = _check([{"op": "replace", "path": "/nodes/run/params/max_fixes", "value": v}], [])
        assert r["real"] == ["/nodes/run/params/max_fixes"] and not r["passed"], (v, r)


def test_no_op_ops_beside_a_real_one_must_not_be_claimed():
    cur = _spec_json()["nodes"]["run"]["params"]["max_improves"]
    ops = [{"op": "replace", "path": "/nodes/run/params/max_improves", "value": cur},
           {"op": "replace", "path": "/nodes/run/params/max_fixes", "value": 2}]
    assert _check(ops, ["/nodes/run/params/max_fixes"])["passed"]
    r = _check(ops, ["/nodes/run/params/max_fixes", "/nodes/run/params/max_improves"])
    assert not r["passed"] and r["n_false_claims"] == 1


def test_unicode_equivalent_keys_are_distinct_paths():
    nfc, nfd = "café", "café"
    a, b = {"params": {nfc: 1}}, {"params": {nfd: 1}}
    real = spec_diff(a, b)
    assert sorted(real) == sorted([f"/params/{nfc}", f"/params/{nfd}"])
    assert not check_claimed_diff(_edit(claimed=[f"/params/{nfc}"]), a, b)["passed"]
    assert not check_claimed_diff(_edit(claimed=[f"/params/{nfd}"]), a, b)["passed"]
    assert check_claimed_diff(_edit(claimed=real), a, b)["passed"]


def test_the_claim_check_never_echoes_a_false_claimed_path():
    secret_like = "/nodes/run/params/x_" + "9" * 30
    r = _check([{"op": "replace", "path": "/nodes/run/params/max_fixes", "value": 2}],
               ["/nodes/run/params/max_fixes", secret_like])
    assert not r["passed"] and secret_like not in json.dumps(r)


# ================================================================ 3. held-out leakage into the diagnosis / prompt
def test_held_out_values_in_error_text_are_normalised_out_of_the_signature(store):
    spec = seed_spec()
    held = [f"subject mean {HELD_OUT} CI [0.7731, 0.9731]", "mean ０.８７３１",  # fullwidth
            f"mean={HELD_OUT:.6e}"]
    for text in held:
        _, ok = _trace(store, spec, GOOD_STEPS)
        _, bad = _trace(store, spec, [*GOOD_STEPS[:2], ("run", {"route": "claim"}, f"ValueError: {text}")])
        assert text in (bad[2].error or "")  # the planted value is in the stored trace
        sig = first_divergence(bad, ok, failing_score=0.0)
        assert sig is not None and sig["mechanism"] == "error"
        blob = json.dumps(sig, ensure_ascii=False)
        for frag in ("8731", "873141", "7731", "9731", "８７３１"):
            assert frag not in blob, (text, blob)


def test_held_out_value_in_a_non_embargoed_content_key_reaches_only_its_name(store):
    """A continuous held-out value under an ordinary key: the divergence names the key, never the value."""
    spec = seed_spec()
    _, ok = _trace(store, spec, [GOOD_STEPS[0], _code("x", stats={"mean": 0.5}), *GOOD_STEPS[2:]])
    _, bad = _trace(store, spec, [GOOD_STEPS[0], _code("x", stats={"mean": HELD_OUT}), *GOOD_STEPS[2:]])
    sig = first_divergence(bad, ok, failing_score=0.0)
    assert sig["content"]["keys"] == ["stats"]
    assert "8731" not in json.dumps(sig)


def test_certificates_differing_only_in_held_out_statistics_are_not_a_one_bit_divergence(store):
    """Not even the BIT 'the held-out means differed' reaches the diagnosis: equal labels, different means/CIs and
    hashes (nested too) are no divergence."""
    spec = seed_spec()

    def steps(mean, h):
        cert = {"hash": h, "label": "REJECTED", "subject_mean": mean, "ci": [mean - 0.1, mean + 0.1]}
        return [*GOOD_STEPS, ("certify", {"certificate": cert, "replica_certificate": dict(cert),
                                          "nested": {"certificate": dict(cert)}, "report": f"mean {mean}",
                                          "publish_blockers": [f"{mean}"], "checklist": {"m": mean}}, None)]

    _, ok = _trace(store, spec, steps(0.5, "a" * 64))
    _, bad = _trace(store, spec, steps(HELD_OUT, "b" * 64))
    assert first_divergence(bad, ok, failing_score=0.0) is None


def test_held_out_value_planted_in_a_route_or_a_key_name_reaches_the_diagnosis_as_the_export_shows_it(store, tmp_path):
    """Route values and output KEY NAMES are not normalised: a value planted there reaches the divergence `cause`
    (route) or `content.keys` (key name) verbatim. Judged harmless as a diagnosis leak, NOT as a policy: (1) the same
    text is in the export the agentic proposer reads (traces are exported with route and key names), so the digest
    adds nothing for it; (2) an evolvable node cannot run after a certificate exists (edges out of protected nodes,
    certify included, are human-only: edits.apply_edit), so no edit can route on a held-out statistic. Reported for
    the one-shot proposer, which reads no export (owner decision)."""
    spec = seed_spec()
    tasks = [SuiteTask("evo0", _task_dir(tmp_path, "evo0"), "evolve", family="A")]
    planted = f"{HELD_OUT}"
    ok_id, ok = _trace(store, spec, GOOD_STEPS, task="evo0")
    bad_id, bad = _trace(store, spec, [*GOOD_STEPS[:2], ("run", {"route": planted, f"k_{planted}": 1}, None)],
                         task="evo0")
    sig = first_divergence(bad, ok, failing_score=0.0)
    assert planted in sig["cause"]  # the planted value reaches the cause...
    exp = export_evidence(store, tmp_path / "exp", runs=[_outcome(bad_id, "evo0", 0.0), _outcome(ok_id, "evo0", 1.0)],
                          tasks=tasks)
    assert planted in (exp.path / "traces" / f"{bad_id}.json").read_text(encoding="utf-8")  # ...as the export shows it


def test_failed_check_reason_text_never_reaches_the_digest(store, tmp_path):
    _, _, ok_id, bad_id, _ = _cert_world(store, tmp_path)
    digest = diagnose([_outcome(bad_id, "e0", 0.0)], store, references=[_outcome(ok_id, "e0", 1.0, seed=1)],
                      evolve_ids={"e0", "e1", "e2"})
    assert digest["divergences"][0]["verifier"]["failing"]["failed_checks"] == [f"{bad_id}#3/K7"]
    blob = json.dumps(digest)
    assert "too close to baseline" not in blob and "873141" not in blob


def test_verifier_fields_carry_only_evolve_outcomes_and_labels(store, tmp_path):
    """score / verified / status / cert_label in the verifier block are the EVOLVE run's own outcome (the export shows
    the same in scores.json); a gate outcome passed as a reference never pairs (dropped by split)."""
    _, _, _, bad_id, gate_id = _cert_world(store, tmp_path)
    gate_ref_honest = RunOutcome("g0", gate_id, 9, 1.0, True, "completed", Cost(), [], cert_label="GATE-ONLY-LABEL")
    digest = diagnose([_outcome(bad_id, "e0", 0.0)], store, references=[gate_ref_honest],
                      evolve_ids={"e0", "e1", "e2"})
    assert digest["divergences"] == [] and digest["n_excluded_split"] == 1
    assert "GATE-ONLY-LABEL" not in json.dumps(digest) and gate_id not in json.dumps(digest)


# --- gate run smuggled into the evaluator cache under an evolve task id
class SmuggleTable(TraceTable):
    """TraceTable whose cache also holds a GATE run (its run record says task g0) filed under evolve task e0, with a
    better score: a reference candidate for the parent's failing e0 runs."""

    def __init__(self, *a, gate_run: str, **kw):
        super().__init__(*a, **kw)
        self.cache[("f" * 64, "e0", 77)] = RunOutcome("e0", gate_run, 77, 9.0, True, "completed", Cost(), [])


GATE_MARK = "gate_only_output_key"


@pytest.mark.xfail(strict=True, reason=(
    "FINDING (propose.diagnose / export.export_evidence trust RunOutcome.task_id; the run record's own task id is "
    "never checked): a gate run filed in the evaluator cache under an evolve task id is used as a divergence "
    "reference and its run id reaches the proposer's prompt. Needs a buggy/compromised Evaluator to happen"))
def test_a_gate_run_smuggled_under_an_evolve_task_id_never_reaches_the_prompt(store, tmp_path):
    parent = seed_spec()
    tasks = _tasks(tmp_path)
    gate_run = _add_run(store, parent, "g0", [*GOOD_STEPS[:1], ("implement", {GATE_MARK: 1}, None), *GOOD_STEPS[2:]])
    assert store.get_run(gate_run).task_id == "g0"  # it really is a gate run
    ev = SmuggleTable(store, tasks, parent.spec_hash(), _gain, lambda a, t, s: _tokens(100), gate_run=gate_run)
    proposer = MockBackend([json.dumps({"edits": [], "decline": "x"})])
    evolve_generation(parent, tasks, evaluator=ev, proposer=proposer, store=store,
                      cfg=EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1))
    prompt = proposer.requests[0].messages[0].content
    assert gate_run not in prompt and GATE_MARK not in prompt


# --- private tokens: canaries that contain digits
DIGIT_CANARY = CANARY_PREFIX + "3f9a1c07b2e48d65a0c3e7f1b9d24c86"  # the shape new_canary() makes (hex with digits)


def _canary_world(store, tmp_path):
    tasks = [SuiteTask(f"e{i}", _task_dir(tmp_path, f"e{i}", DIGIT_CANARY), "evolve", family="A") for i in range(2)]
    tasks += [SuiteTask(f"g{i}", _task_dir(tmp_path, f"g{i}", DIGIT_CANARY), "gate", family="A") for i in range(2)]
    tasks += [SuiteTask("h0", _task_dir(tmp_path, "h0", DIGIT_CANARY), "gate", family="H")]
    return tasks


def test_a_canary_with_digits_in_an_error_never_reaches_the_prompt_even_mangled(store, tmp_path):
    """The existing test plants a canary of letters only ('ab' * 16). A real canary is hex WITH digits: error
    normalisation (evaluate._signature: digits -> <n>) mangles it in the cluster signature and the divergence cause,
    so the verbatim/fragment canary match of the screen misses it while its prefix and letters reach the prompt."""
    tasks = _canary_world(store, tmp_path)
    parent = seed_spec()
    # the parent fails on seed 0 (crashing with the canary in its error) and succeeds on seed 1: a divergence pair

    def score(arm, task, seed):
        return (0.0 if seed == 0 else 1.0) if arm == "parent" else 1.0

    ev = LeakyTable(store, tasks, parent.spec_hash(), score, lambda a, t, s: _tokens(100), secret=DIGIT_CANARY)
    proposer = MockBackend([json.dumps({"edits": [], "decline": "x"})])
    evolve_generation(parent, tasks, evaluator=ev, proposer=proposer, store=store,
                      cfg=EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1))
    rid = next(o.run_id for o in ev.cache.values() if o.task_id == "e0" and o.score == 0.0)
    assert any(DIGIT_CANARY in (st.error or "") for st in store.trace(rid))  # the attack is in the evolve trace
    # ...and reaches the unscreened diagnosis, mangled (so the verbatim/fragment canary match cannot see it)
    outs = [o for o in ev.cache.values() if o.task_id.startswith("e")]
    raw = json.dumps(diagnose(outs, store, references=outs, evolve_ids={"e0", "e1"})["divergences"])
    assert DIGIT_CANARY not in raw and CANARY_PREFIX in raw  # the divergence cause carries it, digits -> <n>
    prompt = proposer.requests[0].messages[0].content
    assert DIGIT_CANARY not in prompt
    assert CANARY_PREFIX.lower() not in prompt.lower(), "a mangled canary reached the proposer prompt"


def test_screen_digest_refuses_a_mangled_canary_in_any_field(store, tmp_path):
    tasks = _canary_world(store, tmp_path)
    mangled = "CHH-CANARY-<n>f<n>a<n>c<n>b<n>e<n>d<n>a<n>c<n>e<n>f<n>b<n>d<n>c<n>"
    for where in ("signature", "cause", "node"):
        digest = {"n_runs": 1, "clusters": [{"id": "C0", "signature": "run: x", "count": 1, "examples": []}],
                  "divergences": [{"id": "D0", "cause": "x", "node": "run", "evidence": []}], "no_reference": []}
        if where == "signature":
            digest["clusters"][0]["signature"] = f"run: ValueError: near {mangled}"
        else:
            digest["divergences"][0][where] = f"ValueError: near {mangled}"
        out = screen_digest(digest, store, runs=[], tasks=tasks)
        assert "chh-canary" not in json.dumps(out).lower(), where
        assert out.get("withheld", 0) >= 1, where
