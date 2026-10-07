"""Audit I3 wired to the literature module (GOAL §4.3, K0/I3; docs/literature.md).

When the run state carries `citations`, the protected audit node checks them with an OFFLINE client on the run's
frozen literature snapshot (`literature_snapshot`, a snapshot id in $CHH_DATA_DIR/literature) against the run's own
RetrievalLog (session = run id): I3 = i3_verdict(check_citations(...))["I3"]. No citations -> I3 "n/a". I3 is part
of `passed` (True or "n/a" pass; anything else fails closed). `citations` and `literature_snapshot` are reserved
state: an evolved node cannot write them.

The citations are planted by trusted test modules (the research.literature node that would produce them is not
built yet); each planted module does what that node would: searches inside the run's retrieval session, looks the
cited ids up (so the offline check can replay them) and freezes the snapshot."""

from __future__ import annotations

from datetime import date

import pytest

import custom_harness.research  # noqa: F401
from custom_harness.config import data_dir
from custom_harness.evolve import Edit, EditOp, EditRejected, EditType, apply_edit, score_state
from custom_harness.literature.client import LiteratureClient
from custom_harness.literature.retrieval import RetrievalLog, RetrievalLoop
from custom_harness.literature.snapshot import RecordingTransport, SnapshotStore
from custom_harness.records import RunStatus
from custom_harness.runtime import register
from lit_world import world
from test_evolve import seed_spec
from test_fail_closed import _planted_run
from test_research import rings_task  # noqa: F401

Q = "sparse gradient routing tabular"
# returned by the search Q (retrieved in the session)
HONEST = {"key": "a", "id": "10.5555/lit.2021.001", "title": "Sparse Gradient Routing for Tabular Ensembles",
          "year": 2021, "quotes": ["the method improves accuracy by 1.8 points over gradient boosting"]}
# a real paper (resolves, title/year right) that no search of the run ever returned: its id was supplied, not found
NEVER_RETRIEVED = {"key": "d", "id": "10.5555/lit.2019.042", "title": "Kernel Density Baselines for Anomaly Scoring",
                   "year": 2019}


def _plant(ctx, cites):
    store = SnapshotStore(data_dir() / "literature")
    client = LiteratureClient(RecordingTransport(world(), store), sleep=lambda s: None, max_attempts=2)
    RetrievalLoop(client, RetrievalLog(store, ctx.run_id), max_rounds=1, limit=5).search(Q)
    for c in cites:
        client.lookup(c["id"])  # recorded, so the audit's offline check can replay the lookup
    return {"citations": cites, "literature_snapshot": store.freeze(date(2026, 1, 1))}


@register("test.plant_verified_citation")
def _plant_verified(state, params, ctx):
    return _plant(ctx, [HONEST])


@register("test.plant_never_retrieved_citation")
def _plant_fabricated(state, params, ctx):
    return _plant(ctx, [HONEST, NEVER_RETRIEVED])


def _run(store, tmp_path, task, module, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    res = _planted_run(store, tmp_path, task, module, "report")
    assert res.status == RunStatus.COMPLETED, res.detail
    return res.state["audit"], res


def test_a_citation_never_retrieved_fails_the_audit(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    audit, res = _run(store, tmp_path, rings_task, "test.plant_never_retrieved_citation", monkeypatch)
    assert audit["I3"] is False and audit["passed"] is False, audit
    assert any("citation d: unverified" in p and "not_retrieved" in p for p in audit["problems"]), audit["problems"]
    assert score_state(res.state, res.status) == (0.0, False)  # no credit for a run citing what it never found
    # the other checks were not what failed it
    assert all(audit[k] is True for k in ("I1", "I2", "I4", "I5")), audit


def test_a_verified_citation_passes_the_audit(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    audit, _ = _run(store, tmp_path, rings_task, "test.plant_verified_citation", monkeypatch)
    assert audit["I3"] is True and audit["passed"] is True, audit
    assert audit["citations"]["counts"] == {"verified": 1, "failed": 0, "unverified": 0}
    assert audit["citations"]["snapshot"] and audit["citations"]["session"].startswith("run_")


# ---------------------------------------------------------------- the I3 helper, fail closed
def _state(**kw):
    return {"citations": [HONEST], **kw}


def test_no_citations_is_not_applicable(tmp_path, monkeypatch):
    from custom_harness.research.claims import citation_audit
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    assert citation_audit({}, "run_x")["I3"] == "n/a"
    assert citation_audit({"citations": []}, "run_x")["I3"] == "n/a"


@pytest.mark.parametrize("state", [
    _state(),  # citations but no frozen snapshot
    _state(literature_snapshot="0" * 64),  # a snapshot that does not exist
    _state(literature_snapshot=12),  # not a snapshot id
    {"citations": "10.5555/lit.2021.001", "literature_snapshot": "x"},  # not a list
    {"citations": [{"key": "a", "id": "x", "bogus": 1}], "literature_snapshot": "x"},  # malformed citation
])
def test_citations_that_cannot_be_checked_fail_closed(tmp_path, monkeypatch, state):
    from custom_harness.research.claims import citation_audit
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    out = citation_audit(state, "run_x")
    assert out["I3"] is False and out["problems"], out


# ---------------------------------------------------------------- reserved state
@pytest.mark.parametrize("key", ["citations", "literature_snapshot"])
def test_an_edit_cannot_write_citations(key):
    spec = seed_spec()
    edges = spec.model_dump(mode="json")["edges"]
    i = next(n for n, e in enumerate(edges) if e["source"] == "socratic")
    ops = [EditOp(op="add", path="/nodes/sneak", value={"kind": "llm", "role": "proposer", "prompt": "p",
                                                        "output_key": key}),
           EditOp(op="replace", path=f"/edges/{i}/target", value="sneak"),
           EditOp(op="add", path="/edges/-", value={"source": "sneak", "target": "next"})]
    with pytest.raises(EditRejected, match="reserved state key"):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=ops))
