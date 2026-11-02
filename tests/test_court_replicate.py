"""Replication of a Court case (GOAL §17.1 REPLICATED, invariant 5; court/replicate.py, int18 stage-a), and the
int18 integration item `"lab"` in commitment.AGENT_READABLE_DATA_SUBDIRS (docs/lab.md D15).

Fail-first reasoning: on int18 before stage-a there is no custom_harness.court (ImportError), and
AGENT_READABLE_DATA_SUBDIRS lacked "lab" (the readable-roots test fails). Each test pins one rule:
  - the author's family comes from the Lab session (model name first, else the agent's vendor); unknown stays
    unknown (fail closed: never "different");
  - the replica code is the first fenced python block of the answer;
  - an uncertified case is never replicated (nothing to agree with), before any model call;
  - owner decision 2026-10-23: a certified, reproducible replica by a DIFFERENT AGENT counts toward REPLICATED
    whatever its model family; the badge names the families ("openai -> openai, same family") and invariant 5 is a
    note (FAILS against the first Stage A build, where a NOT MET replica never counted); a replica without a
    reproducible certificate does not count;
  - Lab workspaces are an agent-readable root for every secret/truth scan; the promotion intake is not.
Built, NOT test-verified (owner rule: no test runs this round)."""

from __future__ import annotations

import pytest

from custom_harness.court import minted as C
from custom_harness.court.replicate import ReplicationRefused, author_family, extract_code, replicate
from custom_harness.gateway import MockBackend
from custom_harness.store import EvidenceStore


@pytest.mark.parametrize("session,family", [
    ({"agent": "claude-code", "options": {"model": "claude-opus-4"}}, "anthropic"),
    ({"agent": "claude-code", "options": {}}, "anthropic"),
    ({"agent": "codex", "options": {}}, "openai"),
    ({"agent": "opencode", "options": {"model": "qwen2.5-coder"}}, "alibaba"),
])
def test_author_family_from_the_lab_session(session, family):
    assert author_family(session) == family


def test_an_unknown_author_family_stays_unknown():
    assert author_family({"agent": "acp", "options": {}}).startswith("unknown:")


def test_extract_code_takes_the_fenced_block():
    assert extract_code("text\n```python\ndef build_model(seed):\n    pass\n```\nmore") == \
        "def build_model(seed):\n    pass\n"
    assert extract_code("def build_model(seed): pass") == "def build_model(seed): pass\n"


def test_an_uncertified_case_is_never_replicated(tmp_path):
    store = EvidenceStore(tmp_path / "store")
    try:
        store.append_record(C.RECORD_KIND, {"case_id": "c1", "event": "registered", "role": "promotion",
                                            "claim_hash": "a" * 64, "session_id": "s1", "promotion_id": "p1"})
        calls = []
        backend = MockBackend(lambda r: calls.append(r) or "```python\npass\n```")
        with pytest.raises(ReplicationRefused, match="not certified"):
            replicate(store, "c1", backend, beacon_round="r2")
        assert calls == []  # refused before any model call
    finally:
        store.close()


def _cases(store, rep_cert, families, inv5="NOT MET (explicit override): same family"):
    store.append_record(C.RECORD_KIND, {"case_id": "c1", "event": "registered", "role": "promotion",
                                        "claim_hash": "a" * 64})
    store.append_record(C.RECORD_KIND, {"case_id": "r1", "event": "registered", "role": "replica", "of_case": "c1",
                                        "claim_hash": "b" * 64, "invariant_5": inv5, "families": families})
    store.append_record(C.RECORD_KIND, {"case_id": "r1", "event": "outcome", "claim_hash": "b" * 64,
                                        "label": "CERTIFIED-INTERNAL" if rep_cert else "CERTIFIED-VALIDATED",
                                        "cert_hash": rep_cert})


def test_a_same_family_replica_counts_and_is_named(tmp_path):
    from custom_harness.court.levels import replication_facts
    from custom_harness.kernel import DatasetRole
    from custom_harness.kerneld.scope import ScopedKernel
    from kernel_world import World
    w = World(tmp_path / "w", seed=61, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST, n=120)
        claim = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), targets=("t",), n_boot=1000)
        w.kernel.preregister(claim)
        h = w.kernel.certify(claim, w.evaluate_all(claim)).cert_hash()
        ScopedKernel(w.store).bundle(h, tmp_path / "b.zip")  # the replica's certificate is REPRODUCIBLE
        _cases(w.store, h, {"executor": "openai", "replicator": "openai"})
        facts = replication_facts(w.store, "x" * 64, "a" * 64)
        assert facts["counts"] is True and facts["form"] == "openai -> openai, same family"
        assert facts["invariant_5"].startswith("NOT MET")  # recorded as a note, never a block
    finally:
        w.close()


def test_a_replica_without_a_reproducible_certificate_does_not_count(tmp_path):
    from custom_harness.court.levels import replication_facts
    store = EvidenceStore(tmp_path / "store")
    try:
        _cases(store, None, {"executor": "anthropic", "replicator": "openai"}, inv5="met")
        facts = replication_facts(store, "x" * 64, "a" * 64)
        assert facts["case"] == "c1" and [r["case_id"] for r in facts["replicas"]] == ["r1"]
        assert facts["counts"] is False and facts["replicas"][0]["relation"] == "different family"
    finally:
        store.close()


def test_lab_workspaces_are_agent_readable_but_the_intake_is_not(tmp_path, monkeypatch):
    from custom_harness.commitment import AGENT_READABLE_DATA_SUBDIRS, default_agent_readable_roots
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CHH_AGENT_READABLE", raising=False)
    roots = {p.resolve() for p in default_agent_readable_roots(existing_only=False)}
    assert "lab" in AGENT_READABLE_DATA_SUBDIRS
    assert (tmp_path / "data" / "lab").resolve() in roots
    assert (tmp_path / "data" / "lab_court").resolve() not in roots
