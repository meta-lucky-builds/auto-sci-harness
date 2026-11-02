"""The Lab discovery trace (chh-lab-trace/1): lab/trace.py and its wiring in session.py / promote.py.

Fail-first design, one property per test:
- stable ids and an append-only hash chain (a tampered or reordered event is detected);
- the idea tree (abandon needs a reason; the path of an idea is root-to-leaf);
- no held-out truth (test splits, held-out datasets, copies of private files), no verdict details (only
  promote.release_verdict may record a label), no secrets, no inlined prompt text;
- agent-agnostic intake: an agent's JSON lines are validated, tagged agent-reported, de-duplicated, and may not
  claim promote / human / session / file events;
- costs: unknown stays unknown (never a silent 0);
- the story: a claim hash leads back to the session, the promotion and the idea path.
A trace module without one of these guards fails the matching test.

Written 2026-10-23 by lab-agents; built, NOT run. No Docker is used (tests/_lab_fakes.FakeDocker).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from _lab_fakes import FakeDocker, docker
from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.lab import EXPLORATORY
from custom_harness.lab import promote as P
from custom_harness.lab import session as S
from custom_harness.lab import trace as T
from custom_harness.lab.agents import AgentOptions

AG = T.Actor(type="agent", id="opencode", agent="opencode", model="m")


def _idea(i, op="propose", parent=None, **kw):
    return {"op": op, "idea_id": i, **({"parent_id": parent} if parent else {}), **kw}


def test_events_have_stable_ids_and_an_intact_chain(store):
    a = T.record(store, "s-t", "idea", _idea("i1", title="smaller trees"), actor=AG)
    b = T.record(store, "s-t", "idea", _idea("i2", "branch", "i1"), actor=AG)
    assert a["label"] == EXPLORATORY and a["schema"] == T.SCHEMA
    assert (a["seq"], b["seq"], b["prev"]) == (1, 2, a["event_id"])
    body = {k: v for k, v in a.items() if k != "event_id"}
    assert hashlib.sha256(T._canonical(body)).hexdigest()[:32] == a["event_id"]
    assert T.verify_chain(store, "s-t") == []
    forged = dict(b, payload={**b["payload"], "title": "rewritten"}, seq=3, prev=b["event_id"])
    store.append_record(T.RECORD_KIND, forged)  # an appended event whose id does not match its content
    assert T.verify_chain(store, "s-t")


def test_idea_tree_abandon_needs_a_reason_and_paths_are_root_to_leaf(store):
    T.record(store, "s-i", "idea", _idea("root"), actor=AG)
    T.record(store, "s-i", "idea", _idea("b1", "branch", "root"), actor=AG)
    T.record(store, "s-i", "idea", _idea("b2", "branch", "b1"), actor=AG)
    with pytest.raises(T.TraceRefused):
        T.record(store, "s-i", "idea", _idea("b1", "abandon"), actor=AG)
    T.record(store, "s-i", "idea", _idea("b1", "abandon", reason="no dev gain"), actor=AG)
    tree = T.idea_tree(store, "s-i")
    assert tree["b1"]["status"] == "abandoned" and tree["b2"]["parent_id"] == "b1"
    assert T.idea_path(store, "s-i", "b2") == ["root", "b1", "b2"]


@pytest.mark.parametrize("payload", [
    {"run_id": "r1", "command": "python x.py", "metrics": [{"name": "acc", "value": 0.9, "split": "test"}]},
    {"run_id": "r1", "command": "python x.py", "metrics": [{"name": "acc", "value": 0.9, "split": "dev"}],
     "verdict": "CERTIFIED"},
    {"run_id": "r1", "command": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz python x.py"},
])
def test_truth_verdict_and_secret_shaped_runs_are_refused(store, payload):
    with pytest.raises(T.TraceRefused):
        T.record(store, "s-g", "run", payload, actor=AG)


def test_metric_on_a_held_out_dataset_and_a_copied_label_file_are_refused(store):
    labels = b"y\n1\n0\n0\n1\n"
    Kernel(store).vault.register_dataset("t-held", DatasetRole.TEST, public={}, private={"test_y.csv": labels})
    with pytest.raises(T.TraceRefused, match="DEV only"):
        T.record(store, "s-h", "run", {"run_id": "r", "command": "c", "metrics": [
            {"name": "acc", "value": 0.8, "split": "dev", "dataset": "t-held"}]}, actor=AG)
    with pytest.raises(T.TraceRefused, match="PRIVATE"):
        T.record(store, "s-h", "run", {"run_id": "r", "command": "c", "outputs": [
            {"path": "out.csv", "sha256": hashlib.sha256(labels).hexdigest()}]}, actor=AG)


def test_only_release_verdict_may_record_a_label(store):
    with pytest.raises(T.TraceRefused):
        T.record(store, "s-v", "promote", {"op": "verdict_released", "promotion_id": "p1",
                                            "released_label": "REJECTED"}, actor=AG, source="agent-reported")
    with pytest.raises(T.TraceRefused):
        T.record(store, "s-v", "promote", {"op": "frozen", "promotion_id": "p1", "released_label": "X"}, actor=AG)


def test_agent_reported_intake_is_validated_tagged_and_deduplicated(store, tmp_path):
    ws = tmp_path / "ws"
    (ws / ".lab").mkdir(parents=True)
    lines = [{"kind": "idea", "client_id": "c1", "payload": _idea("i1", title="try lasso")},
             {"kind": "promote", "client_id": "c2", "payload": {"op": "frozen", "promotion_id": "x"}},
             {"kind": "run", "client_id": "c3", "payload": {"run_id": "r", "command": "c", "secret_token":
                                                            "abcdefghijklmnopqrstuvwxyz"}},
             "not json"]
    (ws / T.AGENT_TRACE_FILE).write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines),
                                         encoding="utf-8")
    r = T.ingest_agent_events(store, "s-a", ws, AG)
    assert (r["ingested"], r["refused"]) == (1, 3)
    again = T.ingest_agent_events(store, "s-a", ws, AG)
    assert again["ingested"] == 1 and len(T.events(store, "s-a")) == 1  # the same client_id is not added twice
    assert T.events(store, "s-a")[0]["source"] == "agent-reported"


def test_costs_unknown_stays_unknown(store):
    T.record(store, "s-c", "cost", {"agent": "codex", "usd": 0.5, "reported_by": "agent-cli"}, actor=AG)
    T.record(store, "s-c", "cost", {"agent": "claude-code", "usd": None, "reported_by": "estimate"}, actor=AG)
    c = T.costs(store, "s-c")
    assert c["usd_total"] is None and c["usd_known_part"] == 0.5 and c["unknown_usd_events"] == 1
    assert set(c["per_agent"]) == {"codex", "claude-code"}


def test_a_headless_prompt_traces_the_human_the_cost_and_the_files_without_inlining_the_prompt(store):
    fake = FakeDocker()
    S.start(store, "opencode", AgentOptions(model="m", local_model_server=True), None,
            session_id="s-run", docker=docker(fake))
    (S.workspace_of("s-run") / "notes.md").write_text("idea", encoding="utf-8")
    S.run_prompt(store, "s-run", "please try gradient boosting", docker=docker(fake))
    evs = T.events(store, "s-run")
    kinds = [e["kind"] for e in evs]
    assert kinds[0] == "session" and "human" in kinds and "cost" in kinds and "file" in kinds
    assert "please try gradient boosting" not in json.dumps(evs)
    prompt = next(e for e in evs if e["kind"] == "human" and e["payload"]["op"] == "prompt")
    assert store.blobs.get(prompt["payload"]["prompt_blob"]) == b"please try gradient boosting"
    assert any(e["kind"] == "file" and e["payload"].get("path") == "notes.md" for e in evs)
    assert T.costs(store, "s-run")["usd_total"] is None  # nothing reported a price
    assert T.verify_chain(store, "s-run") == []


def test_story_links_a_claim_back_to_the_session_and_its_idea_path(store, tmp_path):
    fake = FakeDocker()
    S.start(store, "opencode", AgentOptions(model="m", local_model_server=True), None,
            session_id="s-story", docker=docker(fake))
    T.record(store, "s-story", "idea", _idea("root", title="ensembles"), actor=AG)
    T.record(store, "s-story", "idea", _idea("rf", "branch", "root", title="random forest"), actor=AG)
    ws = S.workspace_of("s-story")
    ws.joinpath("method.py").write_bytes(b"def build_model(seed):\n    return None\n")
    ws.joinpath("baseline.py").write_bytes(b"def build_model(seed):\n    return 1\n")
    ws.joinpath("requirements.lock").write_bytes(b"numpy==2.1.0\n")
    req = tmp_path / "req.yaml"
    req.write_text("""format: chh-lab-promote/1
title: story test
sentence: The forest beats the baseline by at least 0.01 accuracy.
method: method.py
baseline: baseline.py
lock: requirements.lock
idea: rf
claim: {metric: accuracy, min_effect: 0.01, probability: 0.6, seeds: [0, 1], resolve_by: 2027-01-01}
budget: {timeout_s: 60}
""", encoding="utf-8")
    tg = tmp_path / "t.yaml"
    tg.write_text('format: chh-lab-targets/1\ndatasets:\n  - {openml_id: 31, sha256: "%s", target: class, '
                  'licence: CC0}\n' % ("b" * 64), encoding="utf-8")
    fr = P.freeze(store, "s-story", req, docker=docker(fake))
    ho = P.handoff(store, fr["promotion_id"], tg)
    st = T.story(store, ho["claim_hash"])
    assert (st["session_id"], st["promotion_id"]) == ("s-story", fr["promotion_id"])
    assert st["idea_path"] == ["root", "rf"] and st["chain_problems"] == []
    ops = [e["payload"]["op"] for e in st["events"] if e["kind"] == "promote"]
    assert ops == ["frozen", "handed_off"]
    loaded_claim = P.load_spec(P.intake_root() / fr["promotion_id"] / "audit.yaml").claim()
    assert loaded_claim.scope.conditions["paper_doi"] == f"lab:s-story:{fr['promotion_id']}"  # cert -> Lab link


def test_promotion_naming_an_unknown_idea_is_refused(store, tmp_path):
    fake = FakeDocker()
    S.start(store, "opencode", AgentOptions(model="m", local_model_server=True), None,
            session_id="s-noidea", docker=docker(fake))
    ws = S.workspace_of("s-noidea")
    ws.joinpath("m.py").write_bytes(b"a = 1\n")
    ws.joinpath("b.py").write_bytes(b"b = 2\n")
    ws.joinpath("l.txt").write_bytes(b"numpy==2.1.0\n")
    req = tmp_path / "req.yaml"
    req.write_text("format: chh-lab-promote/1\ntitle: x y z\nsentence: a sentence of ten+ chars\nmethod: m.py\n"
                   "baseline: b.py\nlock: l.txt\nidea: ghost\nclaim: {metric: accuracy, min_effect: 0.01, "
                   "probability: 0.6, seeds: [0], resolve_by: 2027-01-01}\nbudget: {timeout_s: 60}\n",
                   encoding="utf-8")
    with pytest.raises(P.PromoteError, match="not in the session's discovery trace"):
        P.freeze(store, "s-noidea", req, docker=docker(fake))
