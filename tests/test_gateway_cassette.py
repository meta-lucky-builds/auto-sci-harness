"""Cassettes (GOAL §16.8): recorded model calls replay as zero-call regression runs. Replayed output is marked
(`replayed=True`) and never falls back to a live call; invariant 21 (replay is never evidence) is enforced by the
Kernel, which refuses marked output (workstream kstats)."""

import json

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import BackendError, ChatRequest, Message, MockBackend
from custom_harness.gateway.cassette import (
    CassetteMiss,
    RecordingBackend,
    ReplayBackend,
    load_cassette,
    replay_backends,
    request_key,
)
from custom_harness.policy import model_family
from custom_harness.records import AccessMode, Provenance, StepKind
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_research import SPEC, scripted_llm
from test_research import rings_task as rings_task  # a pytest fixture, used by name below

ROLES = ("proposer", "executor", "judge", "replicator")


def _req(text, **kw):
    return ChatRequest(messages=[Message(role="user", content=text)], **kw)


# --- format -------------------------------------------------------------------------------------------------


def test_request_key_is_sha256_of_canonical_request_and_covers_model_and_schema():
    import hashlib
    r = _req("hi")
    canon = json.dumps(r.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    assert request_key(r) == hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert request_key(r) == request_key(_req("hi"))  # deterministic
    others = [_req("hi", model="gpt-x"), _req("hi", json_schema={"type": "object"}), _req("hi", max_tokens=5),
              _req("hi", temperature=0.5), _req("hi!"),
              ChatRequest(messages=[Message(role="system", content="hi")])]
    keys = {request_key(o) for o in others}
    assert request_key(r) not in keys and len(keys) == len(others)


def test_recording_writes_one_jsonl_record_per_call_and_keeps_identity(tmp_path):
    path = tmp_path / "c.jsonl"
    inner = MockBackend(["a", "b"])
    inner.family = "family-a"
    rec = RecordingBackend(inner, path, role="proposer")
    assert (rec.name, rec.provider, rec.mode, rec.training_on_outputs) == ("mock", "mock", AccessMode.MOCK,
                                                                          "allowed")
    assert model_family(rec) == "family-a" == model_family(inner)
    r1 = rec.complete(_req("one"))
    r2 = rec.complete(_req("two"))
    assert (r1.text, r2.text) == ("a", "b") and not r1.replayed and not r1.provenance.replayed
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == 2
    docs = [json.loads(x) for x in lines]
    assert [d["seq"] for d in docs] == [0, 1]
    for d, r, text in zip(docs, (_req("one"), _req("two")), ("a", "b"), strict=True):
        assert set(d) == {"v", "key", "role", "seq", "request", "response"}
        assert d["v"] == 1 and d["role"] == "proposer" and d["key"] == request_key(r)
        assert d["request"] == r.model_dump(mode="json")
        assert set(d["response"]) == {"text", "cost", "provenance"} and d["response"]["text"] == text
        assert "replayed" not in d["response"]["provenance"]
    assert load_cassette(path) == docs


def test_recording_does_not_record_failed_calls(tmp_path):
    class Boom(MockBackend):
        def complete(self, req):
            raise BackendError("outage", transient=True)
    rec = RecordingBackend(Boom(["x"]), tmp_path / "c.jsonl")
    with pytest.raises(BackendError):
        rec.complete(_req("q"))
    assert load_cassette(tmp_path / "c.jsonl") == []


def test_default_provenance_dumps_without_replayed_key_so_stored_hashes_are_unchanged():
    p = Provenance(backend="mock", provider="mock", model="m", mode=AccessMode.MOCK, training_on_outputs="allowed")
    assert p.replayed is False
    assert "replayed" not in p.model_dump() and "replayed" not in p.model_dump(mode="json")
    assert "replayed" not in p.model_dump_json()
    old = '{"backend":"mock","provider":"mock","model":"m","mode":"mock","training_on_outputs":"allowed"}'
    assert p.model_dump_json() == old and Provenance.model_validate_json(old) == p
    q = Provenance.model_validate({**p.model_dump(), "replayed": True})
    assert q.model_dump()["replayed"] is True and Provenance.model_validate_json(q.model_dump_json()).replayed


# --- replay -------------------------------------------------------------------------------------------------


def _record(tmp_path, texts_by_prompt, role=None):
    path = tmp_path / "c.jsonl"
    rec = RecordingBackend(MockBackend(lambda r: texts_by_prompt[r.messages[-1].content].pop(0)), path, role=role)
    return path, rec


def test_replay_serves_by_key_marks_output_and_never_calls_a_model(tmp_path, monkeypatch):
    path, rec = _record(tmp_path, {"q": ["answer"]})
    live = rec.complete(_req("q"))
    monkeypatch.setattr(MockBackend, "complete", lambda self, req: pytest.fail("a model was called"))
    rb = ReplayBackend(path)
    out = rb.complete(_req("q"))
    assert out.text == "answer" and out.cost == live.cost
    assert out.replayed is True and out.provenance.replayed is True
    assert out.provenance.model_dump(exclude={"replayed"}) == live.provenance.model_dump()
    assert rb.served == 1 and rb.misses == []
    # identity comes from the recorded provenance, so the family policy sees the same family
    assert (rb.provider, rb.model, rb.mode) == ("mock", "mock-1", AccessMode.MOCK)
    assert model_family(rb) == model_family(MockBackend(["x"]))


def test_replay_miss_raises_and_never_falls_back(tmp_path):
    path, rec = _record(tmp_path, {"q": ["answer"]})
    rec.complete(_req("q"))
    rb = ReplayBackend(path)
    with pytest.raises(CassetteMiss) as ei:
        rb.complete(_req("q changed"))
    e = ei.value
    assert isinstance(e, BackendError) and e.infra is False and e.transient is False
    assert e.key == request_key(_req("q changed")) and rb.misses == [e.key] and rb.served == 0
    with pytest.raises(CassetteMiss):  # another model name is another request
        rb.complete(_req("q", model="other"))
    assert len(rb.misses) == 2
    from custom_harness.errors import infra_cause, is_transient
    assert infra_cause(e) is None and not is_transient(e)  # the harness's fault, never excluded as infra


def test_identical_requests_replay_in_recorded_order_then_miss(tmp_path):
    path, rec = _record(tmp_path, {"q": ["first", "second", "third"]})
    assert [rec.complete(_req("q")).text for _ in range(3)] == ["first", "second", "third"]
    rb = ReplayBackend(path)
    assert [rb.complete(_req("q")).text for _ in range(3)] == ["first", "second", "third"]
    with pytest.raises(CassetteMiss):
        rb.complete(_req("q"))  # exhausted: a fourth identical call was never recorded
    assert rb.served == 3 and len(rb.misses) == 1


def test_replay_backends_bind_each_role_to_its_own_records(tmp_path):
    path = tmp_path / "c.jsonl"
    a = MockBackend(lambda r: "from-a")
    b = MockBackend(lambda r: "from-b")
    a.family, b.family = "family-a", "family-b"
    RecordingBackend(a, path, role="proposer").complete(_req("same"))
    RecordingBackend(b, path, role="judge").complete(_req("same"))
    assert [d["seq"] for d in load_cassette(path)] == [0, 1]  # seq is per cassette file
    rbs = replay_backends(path, families={"proposer": "family-a", "judge": "family-b"})
    assert set(rbs) == {"proposer", "judge"}
    assert rbs["proposer"].complete(_req("same")).text == "from-a"
    assert rbs["judge"].complete(_req("same")).text == "from-b"
    assert model_family(rbs["proposer"]) == "family-a" and model_family(rbs["judge"]) == "family-b"
    with pytest.raises(CassetteMiss):
        rbs["judge"].complete(_req("same"))


def test_cassette_with_unknown_version_is_refused(tmp_path):
    path = tmp_path / "c.jsonl"
    path.write_text(json.dumps({"v": 2, "key": "k", "role": None, "seq": 0, "request": {}, "response": {}}) + "\n",
                    encoding="utf-8", newline="")
    with pytest.raises(ValueError, match="version"):
        load_cassette(path)


# --- end to end: a recorded research_v1 run replays with zero model calls ---------------------------------


def _run(store, ws, task, backends):
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    return Engine(spec, backends=backends, store=store,
                  sandbox=LocalSandbox(ws, i_understand_no_isolation=True)).run("rings", {"task_dir": str(task)})


def _llm_provenances(store, run_id):
    """(node_id, provenance dict) of every model call in the trace: kind:llm steps and module-internal calls."""
    out = []
    for st in store.trace(run_id):
        if st.kind == StepKind.LLM and st.provenance is not None:
            out.append((st.node_id, st.provenance.model_dump(mode="json")))
        for call in st.output.get("_module_llm_calls", []) or []:
            out.append((st.node_id, call["provenance"]))
    return out


def test_recorded_research_run_replays_with_zero_model_calls(store, tmp_path, rings_task, monkeypatch):
    path = tmp_path / "run.cassette.jsonl"
    llm = MockBackend(scripted_llm)
    rec = _run(store, tmp_path / "ws", rings_task, {r: RecordingBackend(llm, path, role=r) for r in ROLES})
    n_calls = len(llm.requests)
    records = load_cassette(path)
    assert len(records) == n_calls > 0
    rec_trace = store.trace(rec.run_id)
    rec_prov = _llm_provenances(store, rec.run_id)
    assert any(st.kind == StepKind.LLM for st in rec_trace)  # both kinds of model call were recorded
    assert any(node == "tournament" for node, _ in rec_prov)
    assert all("replayed" not in p for _, p in rec_prov)

    # Replay into a scratch store: every role bound to a ReplayBackend, and any real model call fails the test.
    monkeypatch.setattr(MockBackend, "complete", lambda self, req: pytest.fail("a model was called during replay"))
    scratch = EvidenceStore(tmp_path / "scratch_store")
    from custom_harness.kernel import Kernel
    Kernel(scratch).approve_adapter("ml", approver="pytest")
    try:
        rbs = replay_backends(path, roles=ROLES)
        rep = _run(scratch, tmp_path / "ws", rings_task, rbs)
        assert len(llm.requests) == n_calls  # zero new calls reached the scripted model
        assert [rb.misses for rb in rbs.values()] == [[], [], [], []]
        assert rep.status == rec.status and rep.detail == rec.detail
        assert rep.state["model_policy"] == rec.state["model_policy"]
        # Invariant 21 (replayed model output is never evidence; Kernel K0, 2778d48): the recorded run certified and
        # replicated, the replay's certificate is REJECTED at K0 with K0_REPLAYED_EVIDENCE, so certify routes to the
        # report and the replication branch is not taken. Everything up to certify replays call for call; the
        # recorded calls left unserved are exactly the replicator's (the branch after certify). Coverage limit, for
        # the owner: a replay of a certified run cannot exercise the nodes after certify (docs/int18_test_plan.md).
        assert (rec.state.get("certificate") or {}).get("label") != "REJECTED"
        assert rec.state.get("replica_certificate") is not None
        assert (rep.state.get("certificate") or {}).get("label") == "REJECTED"
        refused = Kernel(scratch).certificate(rep.state["certificate"]["hash"])
        assert "K0" in refused.failed() and "K0_REPLAYED_EVIDENCE" in refused.checks["K0"].codes, refused.checks["K0"]
        assert rep.state.get("replica_certificate") is None
        rec_nodes, rep_nodes = [s.node_id for s in rec_trace], [s.node_id for s in scratch.trace(rep.run_id)]
        cut = rec_nodes.index("certify") + 1
        assert rep_nodes == rec_nodes[:cut] + ["report", "audit"] and rec_nodes[cut] == "replicate", rep_nodes
        after = [d for d in records if d["role"] == "replicator"]
        assert after and {r: rb.served for r, rb in rbs.items()} == {
            r: sum(d["role"] == r for d in records) - (len(after) if r == "replicator" else 0) for r in ROLES}
        rep_prov = _llm_provenances(scratch, rep.run_id)
        assert [n for n, _ in rep_prov] == [n for n, _ in rec_prov if n != "replicate"]
        assert len(rep_prov) == sum(rb.served for rb in rbs.values()) == n_calls - len(after)
        assert all(p.get("replayed") is True for _, p in rep_prov), rep_prov
    finally:
        scratch.close()


def test_a_changed_prompt_misses_the_cassette_and_the_run_does_not_go_live(store, tmp_path, rings_task,
                                                                          monkeypatch):
    path = tmp_path / "run.cassette.jsonl"
    llm = MockBackend(scripted_llm)
    _run(store, tmp_path / "ws", rings_task, {r: RecordingBackend(llm, path, role=r) for r in ROLES})
    n_calls = len(llm.requests)
    docs = load_cassette(path)
    # simulate a harness edit: the first proposer prompt no longer matches what was recorded
    first = next(i for i, d in enumerate(docs) if d["role"] == "proposer")
    docs[first]["request"]["messages"][-1]["content"] += " (edited)"
    docs[first]["key"] = request_key(ChatRequest.model_validate(docs[first]["request"]))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.writelines(json.dumps(d) + "\n" for d in docs)
    monkeypatch.setattr(MockBackend, "complete", lambda self, req: pytest.fail("a model was called during replay"))
    scratch = EvidenceStore(tmp_path / "scratch_store")
    from custom_harness.kernel import Kernel
    Kernel(scratch).approve_adapter("ml", approver="pytest")
    try:
        rbs = replay_backends(path, roles=ROLES)
        rep = _run(scratch, tmp_path / "ws2", rings_task, rbs)
        assert len(llm.requests) == n_calls
        assert rbs["proposer"].misses, (rep.status, rep.detail[:500], {r: (b.served, b.misses) for r, b in rbs.items()},
                                        [(st.node_id, (st.error or "")[:200]) for st in scratch.trace(rep.run_id)])
        errors = [st.error for st in scratch.trace(rep.run_id) if st.error]
        assert any("cassette miss" in e for e in errors), errors  # the miss surfaced as the step's own failure
    finally:
        scratch.close()
