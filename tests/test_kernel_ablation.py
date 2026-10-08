"""The Kernel never mints a certificate from an ABLATED run (custom_harness/ablation.py; GOAL invariant 12's ablation
runs are measurement arms, never sources of certified truth). A tightening only: certification is refused (KernelError,
nothing ledgered) when any evidence was produced by a run whose record is marked ablated.

Every branch of `Kernel._ablated_runs` is driven here (this file is in the Kernel coverage gate)."""

from __future__ import annotations

import pytest

from custom_harness.kernel import KernelError
from custom_harness.records import Evidence, Run
from kernel_world import World


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "w", seed=7, n_train=60, n_test=60)
    yield w
    w.close()


def _claim(w):
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    return c, w.evaluate_all(c)


def _rehome(w, ev_id: str, run_id: str | None) -> str:
    """The same payload as evidence `ev_id`, recorded as produced by `run_id`."""
    ev = w.store.get_evidence(ev_id)
    new = Evidence(kind=ev.kind, tier=ev.tier, verifier_id=ev.verifier_id, produced_by_run=run_id,
                   payload_ref=ev.payload_ref, summary=ev.summary)
    w.store.add_evidence(new)
    return new.id


def test_certification_on_evidence_from_an_ablated_run_is_refused_and_nothing_is_ledgered(world):
    c, ids = _claim(world)
    # positive control first (more evaluations of the claim in the store would be selective reporting): the claim on
    # these payloads from production runs certifies
    assert world.kernel.certify(c, ids, record=False).label.value == "CERTIFIED-INTERNAL"
    ablated = Run(harness_id="h", harness_hash="h", task_id="t", ablated=("some_switch",))
    world.store.add_run(ablated)
    moved = [_rehome(world, ids[0], ablated.id), _rehome(world, ids[1], ablated.id), *ids[2:]]
    before = len(world.store.ledger_rows("certificate"))
    with pytest.raises(KernelError, match=rf"ABLATED run\(s\) \['{ablated.id}'\]"):  # listed once
        world.kernel.certify(c, moved)
    with pytest.raises(KernelError, match="ABLATED"):
        world.kernel.certify(c, moved, record=False)  # not even an unrecorded certificate
    assert len(world.store.ledger_rows("certificate")) == before


def test_unresolvable_ids_and_runs_without_a_record_are_not_ablated(world):
    c, ids = _claim(world)
    production = Run(harness_id="h", harness_hash="h", task_id="t")
    world.store.add_run(production)
    extra = [_rehome(world, ids[0], None),  # no producing run
             _rehome(world, ids[1], "run_without_a_record"),  # run id with no run record
             _rehome(world, ids[2], production.id)]  # a production run
    assert world.kernel._ablated_runs([*extra, "ev_does_not_exist"]) == []
    cert = world.kernel.certify(c, [*ids, "ev_does_not_exist"])  # left to K0, which fails it
    assert cert.label.value == "REJECTED" and cert.checks["K0"].status == "fail"
