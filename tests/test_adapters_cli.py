"""Domain adapters outside the Kernel: the human CLI entry point and the research loop's fail-fast (GOAL §4.7,
invariant 13)."""

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.cli import main
from custom_harness.config import data_dir
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Ledger
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401


def test_cli_approve_adapter_records_the_human_approval(capsys):
    assert main(["approve-adapter", "ml", "--approver", "alice"]) == 0
    assert '"approver": "alice"' in capsys.readouterr().out
    s = EvidenceStore(data_dir() / "store")
    try:
        (entry,) = Ledger(s).entries("adapter_approval")
        assert entry.body["name"] == "ml" and entry.body["approver"] == "alice"
    finally:
        s.close()
    with pytest.raises(SystemExit, match="unknown adapter"):
        main(["approve-adapter", "no-such-adapter", "--approver", "alice"])  # ("lean" exists since M3)
    with pytest.raises(SystemExit, match=r"NOT approved: .*the Lean version and the Mathlib commit are pinned"):
        main(["approve-adapter", "lean", "--approver", "alice"])  # unusable until the owner pins Mathlib


def test_research_setup_fails_fast_without_an_approved_adapter(tmp_path, rings_task, monkeypatch):  # noqa: F811
    """No model call is wasted on a run whose claims could never be certified (K1)."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    unapproved = EvidenceStore(tmp_path / "bare")
    llm = MockBackend(scripted_llm)
    try:
        res = Engine(HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text())),
                     backends=dict.fromkeys(("proposer", "executor", "judge", "replicator"), llm), store=unapproved,
                     sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
            "rings", {"task_dir": str(rings_task)})
    finally:
        unapproved.close()
    assert res.status == RunStatus.FAILED and "no human-approved adapter" in res.detail
    assert llm.requests == []  # failed at setup: no model call made
