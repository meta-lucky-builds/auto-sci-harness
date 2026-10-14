"""The host store facade and the Kernel's per-claim grants (GOAL §16.5.2, kerneld/hoststore.py, kerneld/scope.py), and
kerneld's private-ingest op (kerneld/ingest.py).

Written 2026-10-14 (s4-kerneld) to FAIL on origin/int17 684f213 and pass with the branch:
- on int17 there is no `custom_harness.kerneld.hoststore` / `scope` module, no ingest op, the Engine and ModuleContext
  hold the raw EvidenceStore, and `register_task` reads the labels in-process; every test below then fails (import
  error, or a private read/ingest that succeeds where it must be refused).
NOT RUN when written (owner: no test runs this phase).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from custom_harness.kernel import DatasetRole, Kernel, Label, Vault
from custom_harness.kerneld.client import KernelD, KernelDError, KernelDUnavailable
from custom_harness.kerneld.hoststore import HostStore, PrivateDataRefused, host_store, open_host_store
from custom_harness.kerneld.scope import ScopedKernel, screening_canaries
from kernel_world import World

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("hoststore"), seed=11, n_train=80, n_test=80)
    claim = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(claim)
    w.claim_obj = claim  # type: ignore[attr-defined]
    w.evidence = w.evaluate_all(claim)  # type: ignore[attr-defined]  # raw store: the test world's trusted path
    yield w
    w.close()


def _private_blob(w: World, ds: str = "test") -> str:
    return next(f.blob for f in w.vault.dataset(ds).files.values() if f.private)


def _public_blob(w: World, ds: str = "test") -> str:
    return next(f.blob for f in w.vault.dataset(ds).files.values() if not f.private)


def _ledger_len(w: World) -> int:
    return len(w.store.ledger_rows())


# ------------------------------------------------------------------------------------------- 1. the facade refuses
def test_host_store_refuses_private_and_canary_blobs_but_serves_public_ones(world):
    hs = host_store(world.store)
    assert isinstance(hs, HostStore) and host_store(hs) is hs
    assert hs.blobs.get(_public_blob(world)) == world.store.blobs.get(_public_blob(world))
    for digest in (_private_blob(world), world.vault.dataset("test").canary_ref):
        assert digest is not None
        with pytest.raises(PrivateDataRefused):
            hs.blobs.get(digest)
        with pytest.raises(PrivateDataRefused):
            hs.blobs.get_json(digest)
    # the refusal is not an OSError/ValueError: "missing"/"malformed" handlers cannot swallow it into a skip
    assert not issubclass(PrivateDataRefused, (OSError, ValueError))


def test_vault_read_through_the_host_store_is_refused_before_it_is_logged(world):
    v = Vault(host_store(world.store))
    before = _ledger_len(world)
    for purpose, kw in (("kernel", {"claim_hash": world.claim_obj.claim_hash()}),
                        ("evaluate", {"claim_hash": world.claim_obj.claim_hash(), "arm": "subject", "seed": 99}),
                        ("audit", {}), ("leakscan", {})):
        with pytest.raises(PrivateDataRefused):
            v.read("test", "test_y.csv", purpose=purpose, **kw)  # type: ignore[arg-type]
    assert _ledger_len(world) == before  # no data_access entry: a refused read is never a logged "peek"
    with pytest.raises(PrivateDataRefused):
        v.canary("test")


def test_host_store_refuses_private_ingest_and_keeps_public_registration(tmp_path):
    s = open_host_store(tmp_path / "store")
    try:
        v = Vault(s)
        with pytest.raises(PrivateDataRefused):
            v.register_dataset("t", DatasetRole.TEST, public={"x.csv": b"x\n1\n"}, private={"y.csv": b"y\n0\n"})
        with pytest.raises(PrivateDataRefused):
            v.register_dataset("c", DatasetRole.DEV, public={"x.csv": b"x\n2\n"}, private={}, canary="CHH-CANARY-c")
        assert v.datasets() == {}
        assert v.register_dataset("d", DatasetRole.DEV, public={"tr.csv": b"a\n1\n"}, private={}).dataset_id == "d"
    finally:
        s.close()


# ------------------------------------------------------------------------------------------- 2. per-claim grants
def test_scoped_kernel_certifies_like_the_kernel_and_a_bare_kernel_fails_closed(world):
    claim, ids = world.claim_obj, world.evidence
    ref = world.kernel.certify(claim, ids, record=False)  # the Kernel's own (raw) store
    scoped = ScopedKernel(host_store(world.store), replayer=world.replayer).certify(claim, ids, record=False)
    assert scoped.label == ref.label and scoped.failed() == ref.failed()
    assert ref.label == Label.CERTIFIED_INTERNAL
    bare = Kernel(host_store(world.store), replayer=world.replayer).certify(claim, ids, record=False)
    assert bare.label != Label.CERTIFIED_INTERNAL and bare.failed()  # its private reads raised: failed checks


def test_grant_is_narrowed_to_the_claim_and_closed_after_the_call(world):
    hs = host_store(world.store)
    k = ScopedKernel(hs, replayer=world.replayer)
    k.certify(world.claim_obj, world.evidence, record=False)
    with pytest.raises(PrivateDataRefused):  # the grant ended with the call
        hs.blobs.get(_private_blob(world))
    # test2 is not a target of the claim: even inside a certify grant its labels are not readable
    from custom_harness.kerneld import scope
    seen: list[str] = []
    orig = Kernel.certify

    def probe(self, *a, **kw):
        try:
            hs.blobs.get(_private_blob(world, "test2"))
            seen.append("read")
        except PrivateDataRefused:
            seen.append("refused")
        return orig(self, *a, **kw)

    try:
        Kernel.certify = probe  # type: ignore[method-assign]
        scope.ScopedKernel(hs, replayer=world.replayer).certify(world.claim_obj, world.evidence, record=False)
    finally:
        Kernel.certify = orig  # type: ignore[method-assign]
    assert seen == ["refused"]


def test_bundle_through_the_scoped_kernel(world, tmp_path):
    cert = world.kernel.certify(world.claim_obj, world.evidence)
    out = tmp_path / "b.zip"
    h = ScopedKernel(host_store(world.store), replayer=world.replayer).bundle(cert.cert_hash(), out)
    assert out.is_file() and any(e.body["bundle_hash"] == h for e in world.kernel.ledger.entries("bundle"))


def test_screening_canaries_reads_canaries_only(world):
    hs = host_store(world.store)
    got = screening_canaries(Vault(hs))
    assert got["test"] == world.vault.canary("test") and got["test2"] == world.vault.canary("test2")
    with pytest.raises(PrivateDataRefused):
        hs.blobs.get(_private_blob(world))


# ------------------------------------------------------------------------------------------- 3. the Engine side
def test_engine_and_module_context_hold_the_host_store(world):
    from custom_harness.runtime.modules import ModuleContext
    ctx = ModuleContext(run_id="r", store=world.store, backends={}, budget=None, sandbox=None)  # type: ignore[arg-type]
    assert isinstance(ctx._store, HostStore) and isinstance(ctx.kernel(), ScopedKernel)
    with pytest.raises(PrivateDataRefused):
        ctx.store.blobs.get(_private_blob(world))
    import inspect

    from custom_harness.runtime import engine
    assert "host_store(store)" in inspect.getsource(engine.Engine.__init__)


def test_cli_opens_the_store_through_kerneld():
    import inspect

    from custom_harness import cli
    src = inspect.getsource(cli.main)
    assert "open_host_store(" in src and "EvidenceStore(" not in src


# ------------------------------------------------------------------------------------------- 4. ingest op
@pytest.fixture
def rings(tmp_path):
    out = tmp_path / "rings"
    subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(out), "--n-train", "60",
                    "--n-test", "40"], check=True, capture_output=True)
    return out


def test_ingest_research_task_registers_in_kerneld_with_a_single_use_token(tmp_path, rings):
    from custom_harness.research.task import load_task
    s = open_host_store(tmp_path / "store")
    try:
        task = load_task(rings)
        with KernelD(s.root) as kd:
            run = kd.open_run("run-ingest")
            ids = run.ingest_research_task(task.id, rings)
            v = Vault(s)
            rec = v.dataset(ids["test"])
            assert rec.role == DatasetRole.TEST and any(f.private for f in rec.files.values())
            assert v.dataset(ids["train"]).role == DatasetRole.DEV and ids["baseline"] in v.artifacts()
            assert [r["subject"] for r in s.records("kerneld_ingest")] == [task.id]
            # single use; bound to its kind and subject
            tok = run.ingest_token("research_task", task.id)
            run._call("ingest", token=tok, kind="research_task", task_dir=str(rings))
            with pytest.raises(KernelDError) as ei:
                run._call("ingest", token=tok, kind="research_task", task_dir=str(rings))
            assert ei.value.code == "token_spent"
            tok = run.ingest_token("research_task", "another-task")
            with pytest.raises(KernelDError) as ei:
                run._call("ingest", token=tok, kind="research_task", task_dir=str(rings))
            assert ei.value.code == "token_scope"
            tok = run.ingest_token("research_task", task.id)
            with pytest.raises(KernelDError) as ei:
                run._call("score", token=tok, metric="accuracy", preds=[])
            assert ei.value.code == "token_scope"  # an ingest token reads nothing
            run.close()
    finally:
        s.close()


def test_ingest_refuses_a_task_whose_canary_differs_for_the_same_data(tmp_path, rings):
    from custom_harness.research.task import load_task
    twin = tmp_path / "twin"
    shutil.copytree(rings, twin)
    (twin / "private" / "canary.txt").write_text("CHH-CANARY-different\n")
    s = open_host_store(tmp_path / "store")
    try:
        with KernelD(s.root) as kd:
            run = kd.open_run("run-canary")
            run.ingest_research_task(load_task(rings).id, rings)
            with pytest.raises(KernelDError) as ei:
                run.ingest_research_task(load_task(twin).id, twin)
            assert ei.value.code == "canary_mismatch" and str(ei.value) == "canary_mismatch"
    finally:
        s.close()


def test_ingest_issue_refusals(tmp_path):
    s = open_host_store(tmp_path / "store")
    try:
        with KernelD(s.root) as kd:
            run = kd.open_run("run-refusals")
            for kind, subject, claim, code in (("nope", "x", None, "bad_request"),
                                               ("openml_split", "openml-1-ab", None, "not_registered"),
                                               ("openml_split", "openml-1-ab", "0" * 64, "not_registered")):
                with pytest.raises(KernelDError) as ei:
                    run.ingest_token(kind, subject, claim_hash=claim)
                assert ei.value.code == code
    finally:
        s.close()


def test_register_task_fails_closed_when_kerneld_is_unavailable(tmp_path, rings, monkeypatch):
    from custom_harness.kerneld import client as client_mod
    from custom_harness.research.claims import register_task
    from custom_harness.research.task import load_task
    from custom_harness.runtime.modules import ModuleContext

    def dead(self, *a, **kw):
        raise KernelDUnavailable()

    monkeypatch.setattr(client_mod.KernelD, "__init__", dead)
    s = open_host_store(tmp_path / "store")
    try:
        ctx = ModuleContext(run_id="r", store=s, backends={}, budget=None, sandbox=None)  # type: ignore[arg-type]
        with pytest.raises(KernelDUnavailable):
            register_task(ctx, load_task(rings), rings)
        assert Vault(s).datasets() == {}  # no fallback: nothing was registered in-process
    finally:
        s.close()


def test_measurement_refusals_carry_closed_classes_only():
    from custom_harness.kerneld import ingest as I
    from custom_harness.kerneld.protocol import MEASUREMENT_PROBLEMS
    msgs = ["data is not UTF-8 text", "columns ['a'] differ from the protocol's ['b']",
            "line 3: 2 values for 3 columns", "line 4: load='SECRET-VALUE' is not a finite number",
            "arm 'x': 1 replicates < the protocol's minimum 3", "something new"]
    classes = [I._problem_class(m) for m in msgs]
    assert classes == ["not_utf8", "columns", "row_length", "non_finite", "replicates", "other"]
    assert set(classes) <= set(MEASUREMENT_PROBLEMS) and not any("SECRET" in c for c in classes)
    assert json.dumps(classes)  # wire-safe
