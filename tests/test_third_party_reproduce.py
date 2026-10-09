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
    env = {**os.environ, "PYTHONPATH": str(SRC), "CHH_DATA_DIR": str(fresh_data), "CHH_ALLOW_UNISOLATED": "1"}
    proc = subprocess.run([sys.executable, "-m", "custom_harness.cli", "reproduce", str(bundle), "--dev-sandbox"],
                          cwd=cwd, env=env, capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    out = json.loads(proc.stdout)
    assert out["reproduced"] is True, out["problems"]
    assert out["original_label"] == out["reproduced_label"] == "CERTIFIED-INTERNAL"
    assert out["headline"] == want  # exact: recomputed by the Kernel from the bundled evidence

    # the third party's store held nothing of the original: no run, no certificate before the command
    s = EvidenceStore(fresh_data / "store")
    try:
        assert s.list_runs(limit=10) == [] and s.ledger_rows() == []
    finally:
        s.close()
