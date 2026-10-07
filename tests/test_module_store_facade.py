"""Modules get a restricted store (STATUS §D "trusted modules have store access"): reads and the blob store only;
ledger, evidence, claim, record, run and trace writes must go through the Kernel API or the engine.

Residual (stated in runtime/modules.py): in-process Python cannot enforce this against deliberately malicious module
code; the Kernel object (ctx.kernel()) still holds the real store. See test_approval.py for that remaining path."""

import pytest

from custom_harness.kernel import Ledger
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec, ModuleNode, register

FORGED = {"claim_hash": "0" * 64, "label": "CERTIFIED-VALIDATED"}


@register("facade.ledger_append")
def _ledger_append(state, params, ctx):
    import json
    ctx.store.ledger_append("certificate", json.dumps(FORGED), lambda *a: "x" * 64)
    return {}


@register("facade.ledger_object")
def _ledger_object(state, params, ctx):
    Ledger(ctx.store).append("certificate", FORGED)
    return {}


@register("facade.record")
def _record(state, params, ctx):
    ctx.store.append_record("harness_edit", {"forged": True})
    return {}


@register("facade.run_status")
def _run_status(state, params, ctx):
    ctx.store.set_run_status(ctx.run_id, RunStatus.COMPLETED, "forged")
    return {}


@register("facade.evidence")
def _evidence(state, params, ctx):
    from custom_harness.records import Evidence, EvidenceKind, Tier
    ctx.store.add_evidence(Evidence(kind=EvidenceKind.METRIC, tier=Tier.T1_EXECUTABLE, verifier_id="forged",
                                    produced_by_run=ctx.run_id, payload_ref=ctx.store.blobs.put_json({}),
                                    summary="fabricated"))
    return {}


@register("facade.honest")
def _honest(state, params, ctx):
    ref = ctx.store.blobs.put(b"intermediate result")
    return {"n_steps": len(ctx.store.trace(ctx.run_id)), "blob": ctx.store.blobs.get(ref).decode(),
            "n_datasets": len(ctx.kernel().vault.datasets()), "adapters": len(ctx.kernel().ledger.entries())}


def _run(store, module):
    spec = HarnessSpec(id="facade", entry="m", nodes={"m": ModuleNode(module=module)})
    return Engine(spec, backends={}, store=store).run("t", {})


@pytest.mark.parametrize("module", ["facade.ledger_append", "facade.ledger_object", "facade.record",
                                    "facade.run_status", "facade.evidence"])
def test_a_module_cannot_write_the_ledger_records_or_run_status_directly(store, module):
    before = len(Ledger(store).entries())
    res = _run(store, module)
    assert res.status == RunStatus.FAILED and "evidence store" in res.detail, res.detail
    assert len(Ledger(store).entries()) == before and not Ledger(store).entries("certificate")
    assert not store.records("harness_edit") and not store.evidence_for_run(res.run_id)


def test_honest_module_reads_and_uses_the_kernel(store):
    res = _run(store, "facade.honest")
    assert res.status == RunStatus.COMPLETED, res.detail
    assert res.state["blob"] == "intermediate result" and res.state["adapters"] >= 1
