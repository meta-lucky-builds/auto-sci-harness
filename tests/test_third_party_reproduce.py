"""Third-party reproduction (GOAL §2.4: "a third party reproduces every headline number from the bundles with one
command"; GOAL §7 first row: `chh reproduce` on a clean machine).

The claim comes from a SCRIPTED research run (mock gateway, trusted stdlib solution code): no live run has produced a
certified claim yet (STATUS §5), so this is the only research-loop certificate available. What is tested is the
reproduction path, not the research quality.

The third party gets ONLY the bundle file: it is copied to a fresh directory, then the original store, data directory,
workspace and bundle are deleted. The one command runs as a separate process, in another working directory, with
another CHH_DATA_DIR (an empty store), and must re-derive the same label and the same headline numbers (the
held-out effect estimate and interval per target, recomputed by the Kernel from the bundled evidence).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

import custom_harness
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401  (fixture re-export)

SRC = Path(custom_harness.__file__).resolve().parents[1]  # the src/ of the code under test (PYTHONPATH trap)


def test_a_third_party_reproduces_the_headline_numbers_from_the_bundle_alone(tmp_path, rings_task,  # noqa: F811
                                                                             monkeypatch):
    origin = tmp_path / "origin"
    data = origin / "data"
    monkeypatch.setenv("CHH_DATA_DIR", str(data))
    store = EvidenceStore(origin / "store")
    Kernel(store).approve_adapter("ml", approver="pytest")  # test stand-in for the human approval
    spec = HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text()))
    llm = MockBackend(scripted_llm)
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(origin / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    info = res.state["certificate"]
    assert info["label"] == "CERTIFIED-INTERNAL", info
    cert = Kernel(store).certificate(info["hash"])
    want = {"label": cert.label.value, "tier": cert.tier,
            "targets": {t: {k: v[k] for k in ("estimate", "lo", "hi")}
                        for t, v in sorted(cert.resolution["targets"].items())}}
    assert want["targets"] and all(v["estimate"] is not None for v in want["targets"].values())
    assert {t: v["estimate"] for t, v in want["targets"].items()} == {
        next(iter(want["targets"])): info["effect"]}  # the run state reports the same number
    store.close()

    third = tmp_path / "third_party"
    third.mkdir()
    bundle = third / "claim_bundle.zip"
    shutil.copyfile(info["bundle_path"], bundle)
    shutil.rmtree(origin)  # store, data dir (with the original bundle and run bundles), workspace: all gone
    shutil.rmtree(rings_task)  # and the task's files (data and hidden labels): only the bundle carries them now
    assert not origin.exists() and not Path(info["bundle_path"]).exists() and not rings_task.exists()

    cwd, fresh_data = third / "cwd", third / "chh-data"
    cwd.mkdir()
    assert not fresh_data.exists()  # the third party starts with nothing
    # no CHH_* setting of this test process leaks into the third party's (only the trusted-code switch is set)
    env = {**{k: v for k, v in os.environ.items() if not k.upper().startswith("CHH_")},
           "PYTHONPATH": str(SRC), "CHH_DATA_DIR": str(fresh_data), "CHH_ALLOW_UNISOLATED": "1"}
    proc = subprocess.run([sys.executable, "-m", "custom_harness.cli", "reproduce", str(bundle), "--dev-sandbox"],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    out = json.loads(proc.stdout)
    assert out["reproduced"] is True, out["problems"]
    assert out["original_label"] == out["reproduced_label"] == "CERTIFIED-INTERNAL"
    assert out["headline"] == want  # exact: recomputed by the Kernel from the bundled evidence
    assert out["original_headline"] == want  # and compared by the command itself with the bundled certificate

    # the command wrote nothing into the third party's store (it reproduces in a temporary store)
    s = EvidenceStore(fresh_data / "store")
    try:
        assert s.list_runs(limit=10) == [] and s.ledger_rows() == []
    finally:
        s.close()


def test_reproduce_fails_when_a_headline_number_differs(tmp_path, monkeypatch, capsys):
    """`chh reproduce` itself compares the re-derived headline numbers with the bundled certificate's (a third party
    has only the bundle): a difference makes it fail even when the label and every check status agree."""
    from custom_harness import cli
    from custom_harness.kernel.bundle import build_bundle
    from kernel_world import World
    w = World(tmp_path / "world", seed=11)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    bundle = tmp_path / "b.zip"
    build_bundle(w.kernel, cert.cert_hash(), bundle)
    w.close()
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "third"))
    capsys.readouterr()
    assert cli.main(["reproduce", str(bundle), "--dev-sandbox"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reproduced"] is True and out["headline"] == out["original_headline"] and out["headline"]["targets"]
    real = cli._original_headline

    def shifted(path):  # the bundled certificate's number differs from the re-derived one by 1e-9
        h = real(path)
        t = next(iter(h["targets"]))
        h["targets"][t] = {**h["targets"][t], "estimate": h["targets"][t]["estimate"] + 1e-9}
        return h
    monkeypatch.setattr(cli, "_original_headline", shifted)
    assert cli.main(["reproduce", str(bundle), "--dev-sandbox"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["reproduced"] is False and any("headline numbers differ" in p for p in out["problems"])
