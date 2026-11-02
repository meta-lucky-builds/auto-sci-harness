"""Agentic proposer (GOAL §5 L6 step 2) and the family-separated critic interface (L6 step 5).

The agent runs against scripted MockBackends (no live model). Its tools run inside a LocalSandbox (tests only) whose
workspace holds just the evidence export copy and the tool script. Every attack test asserts that the attack was
actually carried out (the call was sent, executed where relevant, and the target content exists) and that it was
refused or came to nothing (the content never reached any model request, nothing was written).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from custom_harness.evolve import EvolveConfig, evolve_generation
from custom_harness.evolve.critic import CriticRefused, FamilySeparatedCritic, require_family_separated
from custom_harness.evolve.export_tools import run_tool
from custom_harness.evolve.proposer_agent import WITHHELD, ProposerAgent, tree_digest
from custom_harness.gateway import MockBackend
from custom_harness.sandbox import LocalSandbox
from test_evolve import PROPOSALS, seed_spec
from test_evolve_evidence import ONE_EDIT, CostTable, _gain, _tasks, _tokens

SECRET = "CHH-CANARY-" + "9e" * 16
PLAIN = "gate labels 0 1 1 0 1 plain-secret-7731"


class Boxes:
    """Sandbox factory that remembers what it created (to inspect workspaces after a session)."""

    def __init__(self, root: Path):
        self.root, self.made = root, []

    def __call__(self, key: str) -> LocalSandbox:
        box = LocalSandbox(self.root / key, i_understand_no_isolation=True)
        self.made.append(box)
        return box


def _final(proposal: str | dict) -> str:
    return json.dumps({"final": json.loads(proposal) if isinstance(proposal, str) else proposal})


def _agent(tmp_path, turns: list[str], **kw) -> tuple[ProposerAgent, MockBackend, Boxes]:
    backend = MockBackend(turns)
    boxes = Boxes(tmp_path / "boxes")
    return ProposerAgent(backend, export_root=tmp_path / "exports", sandbox_factory=boxes, **kw), backend, boxes


def _generation(store, tmp_path, agent, *, critic=None, cfg=None):
    parent = seed_spec()
    tasks = _tasks(tmp_path)
    ev = CostTable(store, tasks, parent.spec_hash(), _gain, lambda a, t, s: _tokens(100))
    return evolve_generation(parent, tasks, evaluator=ev, proposer=agent, store=store, critic=critic,
                             cfg=cfg or EvolveConfig(seeds=(0, 1), n_edits=3, race_rounds=1))


def _seen_by_model(backend: MockBackend) -> str:
    return "\n".join(m.content for r in backend.requests for m in r.messages if m.role == "user")


def _tool_results(backend: MockBackend) -> list[dict]:
    """The tool results the model received (user messages after the first), decoded."""
    out = []
    for m in backend.requests[-1].messages[1:]:
        if m.role == "user":
            out.append(json.loads(m.content.split(" ...[truncated")[0]) if m.content.startswith("{") else
                       {"text": m.content})
    return out


# ================================================================ happy path
def test_agent_reads_the_export_in_its_sandbox_and_its_typed_edit_goes_through_the_loop(store, tmp_path):
    agent, backend, _ = _agent(tmp_path, [
        json.dumps({"tool": "list", "path": ""}),
        json.dumps({"tool": "read", "path": "scores.json"}),
        json.dumps({"tool": "grep", "pattern": "e0", "path": ""}),
        _final(ONE_EDIT)])
    res = _generation(store, tmp_path, agent)
    assert res.accepted and res.new_parent.nodes["run"].params["max_fixes"] == 2, res.decision
    results = _tool_results(backend)
    assert results[0]["ok"] and "scores.json" in results[0]["result"] and "manifest.json" in results[0]["result"]
    assert results[1]["ok"] and '"task_id": "e0"' in results[1]["result"]["text"]
    assert results[2]["ok"] and results[2]["result"]["hits"]
    # the tools ran in a fresh sandbox whose workspace held only the export copy and the tool script
    ws = Path(agent.sessions[0]["workspace"])
    assert sorted(p.name for p in ws.iterdir()) == ["_tools", "export"]
    assert sorted(p.name for p in (ws / "_tools").iterdir()) == ["export_tools.py"]
    session = store.records("proposer_session")[-1]
    assert [t.get("tool") for t in session["transcript"]] == ["list", "read", "grep", None]
    assert all(t.get("executed") for t in session["transcript"][:3])


# ================================================================ attack: read outside the export
def test_agent_cannot_read_gate_or_sealed_task_files_or_anything_outside_the_export(store, tmp_path):
    # plain content (no canary, no task id): only the path confinement, not the host content filter, can stop it
    outside = tmp_path / "gate_private.txt"
    outside.write_text(PLAIN, encoding="utf-8")
    calls = [{"tool": "read", "path": str(outside)},                       # absolute host path
             {"tool": "read", "path": outside.as_posix()},
             {"tool": "read", "path": "../../../gate_private.txt"},        # traversal
             {"tool": "read", "path": "candidates/../../gate_private.txt"},
             {"tool": "list", "path": ".."},
             {"tool": "read", "path": "..\\..\\gate_private.txt"},          # Windows separators
             {"tool": "grep", "pattern": "PLAIN", "path": "../"},
             {"tool": "read", "path": "C:gate_private.txt"}]               # drive-relative
    agent, backend, _ = _agent(tmp_path, [*(json.dumps(c) for c in calls), _final({"edits": []})],
                                   max_turns=len(calls) + 1)
    _generation(store, tmp_path, agent)
    results = _tool_results(backend)
    assert len(results) == len(calls)
    assert all(r["ok"] is False and r["refused"] for r in results), results
    assert PLAIN not in _seen_by_model(backend)
    transcript = store.records("proposer_session")[-1]["transcript"]
    assert all(t["executed"] for t in transcript[:len(calls)])  # each was really run in the sandbox, and refused


def test_tools_refuse_unlisted_planted_files_and_links(tmp_path):
    root = tmp_path / "exp"
    (root / "traces").mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"files": ["scores.json"]}), encoding="utf-8")
    (root / "scores.json").write_text("[]", encoding="utf-8")
    (root / "traces" / "planted.json").write_text(json.dumps({"x": SECRET}), encoding="utf-8")
    assert (root / "traces" / "planted.json").read_text(encoding="utf-8").count(SECRET) == 1
    r = run_tool(root, {"tool": "read", "path": "traces/planted.json"})
    assert r["ok"] is False and "not a file of this export" in r["refused"]
    g = run_tool(root, {"tool": "grep", "pattern": "CHH-CANARY", "path": ""})
    assert g["ok"] is True and g["result"]["hits"] == []
    assert run_tool(root, {"tool": "list", "path": ""})["result"] == ["manifest.json", "scores.json"]
    # a link inside the export pointing outside it (a directory junction on Windows: no privilege needed)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text(json.dumps({"x": SECRET}), encoding="utf-8")
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(outside), str(root / "linked"))
        else:
            os.symlink(outside, root / "linked")
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("cannot create a link here")
    assert SECRET in (root / "linked" / "secret.json").read_text(encoding="utf-8")  # the link works on the host
    (root / "manifest.json").write_text(json.dumps({"files": ["scores.json", "linked/secret.json"]}),
                                        encoding="utf-8")
    r = run_tool(root, {"tool": "read", "path": "linked/secret.json"})
    assert r["ok"] is False and "link" in r["refused"]
    g = run_tool(root, {"tool": "grep", "pattern": "CHH", "path": ""})
    assert g["ok"] is False and "link" in g["refused"]


# ================================================================ attack: gate / private content inside the export
def test_tool_results_carrying_held_out_or_private_content_are_withheld(store, tmp_path):
    # an older lineage record (written before tasks_at_risk was restricted) names gate task g2, and a rejection reason
    # quotes a `_private` key: both reach lineage.json, so the export itself carries them
    store.append_record("lineage", {"parent": "p" * 64, "status": "rejected_static", "reason":
                                    "read {{_private_task_dir}} refused",
                                    "edit": json.loads(ONE_EDIT)["edits"][0] | {"tasks_at_risk": ["g2"]}})
    agent, backend, _ = _agent(tmp_path, [json.dumps({"tool": "read", "path": "lineage.json"}),
                                          json.dumps({"tool": "grep", "pattern": "_private", "path": ""}),
                                          json.dumps({"tool": "grep", "pattern": "g2", "path": "lineage.json"}),
                                          _final({"edits": []})])
    _generation(store, tmp_path, agent)
    exported = (Path(agent.sessions[0]["export"]) / "lineage.json").read_text(encoding="utf-8")
    assert '"g2"' in exported and "_private_task_dir" in exported  # the content was there to be read
    results = _tool_results(backend)
    assert [r.get("refused") for r in results] == [WITHHELD] * 3
    seen = _seen_by_model(backend)
    assert "_private_task_dir" not in seen and '"g2"' not in seen


# ================================================================ attack: write / execute
def test_agent_has_no_write_or_exec_tool_and_nothing_is_written(store, tmp_path):
    calls = [{"tool": "write", "path": "scores.json", "content": "[]"},
             {"tool": "exec", "command": ["python", "-c", "open('pwned','w')"]},
             {"tool": "shell", "cmd": "echo hi > pwned"},
             {"tool": "read", "path": "scores.json", "mode": "w"}]  # extra fields are dropped, not honoured
    agent, _, boxes = _agent(tmp_path, [*(json.dumps(c) for c in calls), _final({"edits": []})])
    _generation(store, tmp_path, agent)
    transcript = store.records("proposer_session")[-1]["transcript"]
    assert [t["executed"] for t in transcript[:3]] == [False, False, False]
    assert all("unknown tool" in t["refused"] for t in transcript[:3])
    assert transcript[3]["executed"] and transcript[3]["ok"]
    ws = boxes.made[0].workspace
    assert not list(ws.rglob("pwned"))
    assert sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file())[0] == "_tools/export_tools.py"


def test_a_session_whose_workspace_changed_is_refused(store, tmp_path):
    """If the sandbox were not read-only in practice (a write happened during the session), the proposal is void."""
    boxes = Boxes(tmp_path / "boxes")

    def responder(req):
        if len(req.messages) == 1:
            return json.dumps({"tool": "read", "path": "scores.json"})
        (boxes.made[0].workspace / "export" / "scores.json").chmod(0o600)
        (boxes.made[0].workspace / "export" / "scores.json").write_text("[]", encoding="utf-8")
        return _final(ONE_EDIT)

    agent = ProposerAgent(MockBackend(responder), export_root=tmp_path / "exports", sandbox_factory=boxes)
    res = _generation(store, tmp_path, agent)
    assert not res.accepted and res.counters["raced"] == 0
    assert any("workspace changed" in r.get("problem", "") for r in store.records("lineage"))


def test_workspace_must_start_empty(store, tmp_path):
    boxes = Boxes(tmp_path / "boxes")

    def dirty(key):
        box = boxes(key)
        (box.workspace / "host_file.txt").write_text("x", encoding="utf-8")
        return box

    agent = ProposerAgent(MockBackend([_final(ONE_EDIT)]), export_root=tmp_path / "exports", sandbox_factory=dirty)
    res = _generation(store, tmp_path, agent)
    assert not res.accepted and agent.backend.requests == []
    assert any("not empty" in r.get("problem", "") for r in store.records("lineage"))


# ================================================================ attack: non-typed / protected edits
def test_agent_non_typed_and_protected_node_edits_are_refused(store, tmp_path):
    good = PROPOSALS["edits"][0]
    final = {"edits": [{**good, "patch": "+max_fixes: 9"},
                       {"type": "control_flow", "rationale": "free text", "diff": "s/0/2/"},
                       PROPOSALS["edits"][2]]}  # /nodes/claim/params/seeds: a protected (Kernel-integrated) node
    agent, _, _ = _agent(tmp_path, [_final(final)])
    res = _generation(store, tmp_path, agent)
    st = [r["status"] for r in store.records("lineage")]
    assert st.count("malformed") == 2 and st.count("rejected_static") == 1, st
    rej = next(r for r in store.records("lineage") if r["status"] == "rejected_static")
    assert "protected" in rej["reason"]
    assert res.counters["raced"] == 0 and not res.accepted


def test_agent_exfiltration_through_predicted_effect_is_refused(store, tmp_path):
    item = PROPOSALS["edits"][0] | {"rationale": "the gate task h0 regresses", "tasks_at_risk": ["h0", SECRET]}
    agent, _, _ = _agent(tmp_path, [_final({"edits": [item]})])
    _generation(store, tmp_path, agent)
    rec = store.records("lineage")[-1]
    assert rec["status"] == "rejected_static" and "withheld" in rec["reason"]
    blob = json.dumps(store.records("lineage"))
    assert "h0 regresses" not in blob and SECRET not in blob


def test_agent_without_final_answer_proposes_nothing(store, tmp_path):
    agent, backend, _ = _agent(tmp_path, [json.dumps({"tool": "list", "path": ""})], max_turns=3)
    res = _generation(store, tmp_path, agent)
    assert len(backend.requests) == 3 and not res.accepted
    assert any("no final answer" in r.get("problem", "") for r in store.records("lineage"))


def test_tree_digest_detects_change(tmp_path):
    (tmp_path / "a").write_text("1", encoding="utf-8")
    d = tree_digest(tmp_path)
    (tmp_path / "a").write_text("2", encoding="utf-8")
    assert tree_digest(tmp_path) != d


# ================================================================ family-separated critic (interface)
def _fam(name: str, responses=None) -> MockBackend:
    b = MockBackend(responses or [json.dumps({"questions": ["Does this read held-out data?"],
                                              "concerns": ["leakage", "not-a-concern"]})])
    b.family = name
    return b


def test_same_family_critic_is_refused_before_any_model_call(store, tmp_path):
    with pytest.raises(CriticRefused, match=r"needs owner|different family"):
        FamilySeparatedCritic(MockBackend(["{}"]), proposer=MockBackend(["{}"]))  # both family "mock"
    unknown = MockBackend(["{}"])
    unknown.provider = "localhost"
    with pytest.raises(CriticRefused, match="undetermined"):
        require_family_separated(unknown, _fam("openai"))
    # a critic built for one proposer is re-checked against the proposer actually used by the generation
    critic = FamilySeparatedCritic(_fam("anthropic"), proposer=_fam("openai"))
    agent, backend, _ = _agent(tmp_path, [_final(ONE_EDIT)])
    agent.backend.family = "anthropic"
    with pytest.raises(CriticRefused):
        _generation(store, tmp_path, agent, critic=critic)
    assert backend.requests == [] and critic.backend.requests == []


def test_family_separated_critic_review_is_recorded_as_advisory(store, tmp_path):
    proposer = _fam("openai")
    critic = FamilySeparatedCritic(_fam("anthropic"), proposer=proposer)
    agent, _, _ = _agent(tmp_path, [_final(ONE_EDIT)])
    agent.backend.family = "openai"
    res = _generation(store, tmp_path, agent, critic=critic)
    rec = next(r for r in store.records("lineage") if r.get("critic"))
    assert rec["critic"] == {"questions": ["Does this read held-out data?"], "concerns": ["leakage"],
                             "problems": [], "advisory": True}
    assert res.accepted  # advisory only: no gating rule exists until a second family is available and calibrated
