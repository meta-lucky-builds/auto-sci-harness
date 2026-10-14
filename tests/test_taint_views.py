"""The evolver's diagnosis (evolve/propose.diagnose, evolve/diagnosis.py) is an agent-visible view of traces, like the
export and the failure signatures: labelled steps must be withheld there too (w2-taint, generalised from the
failure-signature fix; docs/taint.md). Held-out target `tfx-held` (test_taint_flow.py), mean 4/7."""

from __future__ import annotations

import dataclasses
import json

from test_taint_flow import _evolve_run


def _failing_and_ok(store, tmp_path):
    held = {"kind": "module", "module": "tf.export_held"}
    copy_fail = {"kind": "module", "module": "tf.export_copy", "params": {"fail": True}}
    copy_ok = {"kind": "module", "module": "tf.export_copy"}
    bad, _task = _evolve_run(store, tmp_path / "a", {"held": held, "copy": copy_fail}, ["held", "copy"])
    ok, _task = _evolve_run(store, tmp_path / "b", {"held": held, "copy": copy_ok}, ["held", "copy"])
    return bad, ok


def test_diagnosis_excerpts_withhold_target_derived_error_text(store, tmp_path):
    from custom_harness.evolve.propose import diagnose
    bad, _ok = _failing_and_ok(store, tmp_path)
    assert bad.status == "failed"
    raw = [s.error for s in store.trace(bad.run_id) if s.error]
    assert any("POSITIVE" in e for e in raw)  # live: the error text quotes a target-derived fact
    digest = diagnose([bad], store, evolve_ids={"tfx"})
    text = json.dumps(digest, default=str)
    assert digest["clusters"] and "POSITIVE" not in text and "NEGATIVE" not in text, text[:2000]


def test_diagnosis_divergence_signature_withholds_target_derived_error_text(store, tmp_path):
    from custom_harness.evolve.propose import diagnose
    bad, ok = _failing_and_ok(store, tmp_path)
    ref = dataclasses.replace(ok, score=1.0)  # a better-scoring run of the same task: the divergence is computed
    digest = diagnose([bad], store, references=[ref], evolve_ids={"tfx"})
    assert digest["divergences"], digest  # live: a signature was produced
    text = json.dumps(digest["divergences"], default=str)
    assert "POSITIVE" not in text and "NEGATIVE" not in text, text
