"""Lab discovery-trace events (discovery_trace.py): no secrets or held-out truth in an event, append-only hash chain,
adapter to another writer. WRITTEN, NOT RUN (lab-compute, build-only phase).

Fail-first reasoning: the module does not exist on origin/int17. The refusal tests fail if `_FORBIDDEN_KEY` loses a
fragment. int18 (stage-a): the JSONL chain test is replaced by tests of the ONE lab trace (LabTraceSink ->
lab/trace.py, chh-lab-trace/1).
"""

from __future__ import annotations

import json

import pytest

from custom_harness.discovery_trace import CallableSink, TraceEvent, TraceRefused, safe_emit


@pytest.mark.parametrize("key", ["api_key", "HF_TOKEN", "secret", "password", "held_out_labels", "sealed_split",
                                 "truth", "verdict", "private_rows", "hidden_scores"])
def test_events_refuse_secret_or_truth_fields(key):
    with pytest.raises(TraceRefused):
        TraceEvent("compute.finished", "j", {key: "x"})
    with pytest.raises(TraceRefused):
        TraceEvent("compute.finished", "j", {"nested": {key: "x"}})


def test_unknown_kinds_and_non_json_values_are_refused():
    with pytest.raises(TraceRefused):
        TraceEvent("compute.exploded", "j", {})
    with pytest.raises(TraceRefused):
        TraceEvent("compute.finished", "j", {"x": float("nan")})
    with pytest.raises(TraceRefused):
        TraceEvent("compute.finished", "j", {"x": object()})


H = "a" * 64
JOB = "ssh:" + "b" * 32


def test_the_default_sink_writes_the_one_lab_trace(tmp_path):
    """int18 (stage-a): one schema, one chain. Every producer event becomes a chh-lab-trace/1 event of the lab trace
    (record kind lab_trace, source backend) whose chain verifies; an edited event breaks it. FAILS on lab-compute
    559305e, where the default sink wrote a separate JSONL chain and lab.trace.events saw nothing."""
    from custom_harness.discovery_trace import UNATTACHED, LabTraceSink
    from custom_harness.lab import trace as T
    from custom_harness.store import EvidenceStore
    store = EvidenceStore(tmp_path / "store")
    try:
        sink = LabTraceSink(store)
        safe_emit(sink, "compute.submitted", JOB, {"backend": "ssh", "job_key": JOB, "command_sha256": H,
                                                   "env_names": ["PATH"], "revision": {"kind": "blob", "ref": H}})
        safe_emit(sink, "compute.log", JOB, {"backend": "ssh", "job_key": JOB, "cursor": 0, "next_cursor": 5,
                                             "eof": True})
        safe_emit(sink, "compute.collected", JOB, {"backend": "ssh", "job_key": JOB, "outputs": {"o.csv": H}})
        safe_emit(sink, "literature.retrieval", "arxiv", {"source": "arxiv", "query": "q", "window": ["2020"],
                                                          "envelopes": [H], "record_ids": ["arXiv:1"],
                                                          "n_records": 1, "complete": True, "errors": []})
        evs = T.events(store, UNATTACHED)
        assert [e["kind"] for e in evs] == ["run", "run", "run", "literature"]
        assert all(e["source"] == "backend" and e["schema"] == T.SCHEMA for e in evs)
        assert evs[0]["payload"]["op"] == "submitted" and evs[2]["payload"]["outputs"][0]["sha256"] == H
        assert T.verify_chain(store, UNATTACHED) == []
        assert not (tmp_path / "store" / "lab_trace").exists()  # no second (JSONL) chain anywhere
    finally:
        store.close()


def test_the_court_paper_certificate_list_never_reaches_the_lab_trace():
    """The Lab trace carries no verdict details (lab/trace.py guards; GOAL §17.2): the producer's `certificates`
    field is dropped by the mapping, only content hashes of the built files remain."""
    from custom_harness.discovery_trace import lab_payload
    kind, payload = lab_payload(TraceEvent("output.paper_built", "x", {"tex_sha256": H, "certificates": ["c" * 64],
                                                                       "manifest_sha256": H}))
    assert kind == "action" and "c" * 64 not in json.dumps(payload)


def test_the_adapter_hands_events_to_another_writer():
    got = []
    safe_emit(CallableSink(got.append), "output.compiled", "p.tex", {"ok": True, "driver": "tectonic"})
    assert got[0]["kind"] == "output.compiled" and got[0]["fields"]["ok"] is True


def test_a_sink_io_failure_never_changes_the_producers_result_but_a_refusal_does():
    class Broken:
        def emit(self, event):
            raise OSError("disk full")
    safe_emit(Broken(), "compute.log", "j", {"cursor": 0})  # swallowed
    with pytest.raises(TraceRefused):
        safe_emit(Broken(), "compute.log", "j", {"token": "x"})
