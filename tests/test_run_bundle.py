"""Invariant 9 (GOAL §10): every run, whatever its outcome, has a Reproducibility Bundle and a cost record; the bundle
holds no private bytes; `chh rerun` re-runs it and refuses changed task data."""

import argparse
import zipfile
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.cli import cmd_rerun
from custom_harness.gateway import MockBackend
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store.run_bundle import read_run_bundle
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401

ROLES = ("proposer", "executor", "judge", "replicator")


def _engine(store, tmp_path):
    llm = MockBackend(scripted_llm)
    return Engine(HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text())), backends=dict.fromkeys(ROLES, llm),
                  store=store, sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))


def _bundle(store, run_id) -> Path:
    rec = [r for r in store.records("run_bundle") if r["run_id"] == run_id]
    assert rec and "error" not in rec[-1], rec
    return Path(rec[-1]["path"])


def test_a_failed_run_still_has_a_bundle(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.delenv("CHH_ALLOW_UNISOLATED", raising=False)  # research modules refuse: the run FAILS at setup
    res = _engine(store, tmp_path).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.FAILED
    m = read_run_bundle(_bundle(store, res.run_id))
    assert m["status"] == "failed" and m["trace"] and m["cost"] is not None and m["spec"]["id"] == "research_v1"


def test_a_completed_run_bundle_holds_the_code_and_no_private_bytes(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    res = _engine(store, tmp_path).run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    path = _bundle(store, res.run_id)
    m = read_run_bundle(path)
    assert len(m["trace"]) == res.steps and m["seed"] == 0 and m["environment"]
    assert "solution.py" in m["workspace_snapshot"]["files"] and m["refused_private_blobs"] == []
    assert "private/test_y.csv" in m["task_files"]  # by hash only
    secret = (rings_task / "private" / "test_y.csv").read_bytes()
    canary = (rings_task / "private" / "canary.txt").read_text().strip()
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            body = z.read(name)
            assert secret not in body and canary.encode() not in body, name


def test_private_bytes_planted_in_the_workspace_never_enter_a_bundle(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    from custom_harness.sandbox import take_snapshot
    from custom_harness.store.run_bundle import build_run_bundle
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    res = _engine(store, tmp_path).run("rings", {"task_dir": str(rings_task)})
    ws = tmp_path / "planted"
    ws.mkdir()
    secret = (rings_task / "private" / "test_y.csv").read_bytes()
    (ws / "stolen.csv").write_bytes(secret)
    (ws / "ok.py").write_text("print(1)\n")
    out = tmp_path / "b.zip"
    build_run_bundle(store, res.run_id, out, environment={}, workspace_ref=take_snapshot(ws, store.blobs))
    m = read_run_bundle(out)
    assert m["refused_private_blobs"] == ["stolen.csv"]
    with zipfile.ZipFile(out) as z:
        assert not any(secret in z.read(n) for n in z.namelist() if n != "manifest.json")


def test_rerun_reproduces_the_run_setup_and_refuses_changed_task_data(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    res = _engine(store, tmp_path).run("rings", {"task_dir": str(rings_task)})
    path = _bundle(store, res.run_id)
    args = argparse.Namespace(bundle=str(path), role=[f"{r}=mock" for r in ROLES], allow_same_family=True,
                              task_dir=None, docker=False, dev_sandbox=True)
    assert cmd_rerun(args, store) in (0, 1)  # it ran (the generic mock backend answers every prompt with filler)
    reruns = [r for r in store.list_runs(limit=10) if r.id != res.run_id]
    assert reruns and reruns[-1].seed == 0 and reruns[-1].harness_hash == store.get_run(res.run_id).harness_hash
    (rings_task / "public" / "train.csv").write_text("x1,label\n0,0\n")
    with pytest.raises(SystemExit, match="task files differ"):
        cmd_rerun(args, store)
