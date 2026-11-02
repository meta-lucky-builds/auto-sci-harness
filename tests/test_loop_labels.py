"""Invariant 10: every loop is labelled MECHANICAL or VALIDATED (GOAL §6.1), in reports, ledgers and the scorecard.

Before 2026-10-09 an evolve generation's own record (`generation_stats`) and the driver's per-generation summary
(gen<N>.json) carried no loop label; only the decision records did. The runtime check (`loop_labels.audit_store`) and
the static check (`loop_labels.static_problems`) fail on an unlabelled or mislabelled loop instance.
"""

from __future__ import annotations

import time
from pathlib import Path

from custom_harness.records import Run, StepKind, TraceStep
from test_evolve_evidence import _gen

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"


def test_an_evolve_generation_record_carries_its_loop_label(store, tmp_path):
    res, _ = _gen(store, tmp_path)
    rec = store.records("generation_stats")[-1]
    assert rec.get("loop_label") in ("MECHANICAL", "VALIDATED"), rec  # the loop instance's own record
    assert rec["loop_label"] == res.loop_label == "MECHANICAL"  # fixture runs carry no CERTIFIED-VALIDATED label
    from custom_harness.loop_labels import audit_store
    a = audit_store(store)
    assert a["unlabelled"] == [], a["unlabelled"]
    assert a["counts"]["generation_stats"] == {"MECHANICAL": 1, "VALIDATED": 0, "unlabelled": 0}
    assert a["counts"]["harness_edit"]["unlabelled"] == 0 and a["counts"]["lineage"]["unlabelled"] == 0


def test_the_driver_summary_carries_the_loop_label():
    from custom_harness.loop_labels import static_problems
    assert static_problems(SRC) == []


def test_static_check_catches_an_unlabelled_writer(tmp_path):
    from custom_harness.loop_labels import static_problems
    root = tmp_path / "src"
    (root / "evolve").mkdir(parents=True)
    (root / "research").mkdir()
    (root / "research" / "claims.py").write_text("x = 'Loop label (GOAL §6.1): **{loop_label(state)}**'\n",
                                                 encoding="utf-8")
    (root / "evolve" / "bad.py").write_text(
        "def f(store, ok):\n"
        "    store.append_record('harness_edit', {'accepted': ok})\n"
        "    rows = [{'status': 'accepted' if ok else 'rejected_gate', 'candidate': 'c'}]\n"
        "    return rows\n", encoding="utf-8")
    got = static_problems(root)
    assert len(got) == 2 and "harness_edit" in got[0] and "decision record" in got[1], got
    (root / "research" / "claims.py").write_text("x = 1\n", encoding="utf-8")
    assert any("loop label line" in p for p in static_problems(root))


def _run(store, state: dict) -> str:
    run = Run(harness_id="fixture", harness_hash="h", task_id="t")
    store.add_run(run)
    now = time.time()
    store.add_step(TraceStep(run_id=run.id, index=0, node_id="report", kind=StepKind.MODULE, input={}, output={},
                             started_at=now, ended_at=now, checkpoint_ref=store.blobs.put_json({"state": state})))
    return run.id


def test_runtime_check_flags_unlabelled_and_mislabelled_instances(store):
    from custom_harness.loop_labels import audit_store
    line = "Loop label (GOAL §6.1): **{}** · invariant 5 (model families): met\n"
    good = _run(store, {"report": "# r\n\n" + line.format("MECHANICAL"),
                        "certificate": {"label": "CERTIFIED-INTERNAL"}, "checklist": {"loop_label": "MECHANICAL"}})
    _run(store, {"report": "# r\n\n" + line.format("VALIDATED"),  # claims VALIDATED on internal data
                 "certificate": {"label": "CERTIFIED-INTERNAL"}, "checklist": {"loop_label": "VALIDATED"}})
    _run(store, {"report": "# r without a label line\n"})
    # a run from before the checklist item existed: labelled by its report line (the missing item is axis F's)
    _run(store, {"report": "# r\n\n" + line.format("MECHANICAL"), "certificate": {"label": "CERTIFIED-INTERNAL"},
                 "checklist": {"seeds": "[0]"}})
    _run(store, {"report": "# r\n\n" + line.format("MECHANICAL"), "certificate": {"label": "CERTIFIED-VALIDATED"}})
    store.append_record("harness_edit", {"parent": "p", "accepted": False})
    store.append_record("lineage", {"parent": "p", "status": "accepted", "candidate": "c"})
    store.append_record("lineage", {"parent": "p", "status": "raced", "candidate": "c"})  # not a loop decision
    store.append_record("generation_stats", {"parent": "p", "loop_label": "mechanical"})
    store.append_record("generation_ledger", {"refs": {"generation_stats": 7}})
    a = audit_store(store)
    probs = sorted(i["problem"] for i in a["unlabelled"])
    assert len(a["unlabelled"]) == 7, probs
    assert any("report states loop label 'VALIDATED', the certificate gives MECHANICAL" in p for p in probs)
    assert any("report states loop label 'MECHANICAL', the certificate gives VALIDATED" in p for p in probs)
    assert any("report states loop label None" in p for p in probs)
    assert any("does not resolve" in p for p in probs)
    assert a["counts"]["research_run"] == {"MECHANICAL": 2, "VALIDATED": 0, "unlabelled": 3}
    assert a["counts"]["lineage"] == {"MECHANICAL": 0, "VALIDATED": 0, "unlabelled": 1}
    assert next(i for i in a["instances"] if not i["problem"])["ref"].startswith(f"run:{good}:")
