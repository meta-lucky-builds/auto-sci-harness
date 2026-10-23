"""Lab discovery-trace events (discovery_trace.py): no secrets or held-out truth in an event, append-only hash chain,
adapter to another writer. WRITTEN, NOT RUN (lab-compute, build-only phase).

Fail-first reasoning: the module does not exist on origin/int17. The refusal tests fail if `_FORBIDDEN_KEY` loses a
fragment; the chain test fails if `verify` stops checking `prev`.
"""

from __future__ import annotations

import json

import pytest

from custom_harness.discovery_trace import CallableSink, JsonlTraceSink, TraceEvent, TraceRefused, safe_emit


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


def test_jsonl_sink_is_hash_chained(tmp_path):
    sink = JsonlTraceSink(tmp_path / "t.jsonl")
    for i in range(3):
        sink.emit(TraceEvent("compute.log", "j", {"cursor": i}))
    assert len(sink.verify()) == 3
    lines = sink.path.read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[1])
    e["event"]["fields"]["cursor"] = 99
    lines[1] = json.dumps(e)
    sink.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        sink.verify()


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
