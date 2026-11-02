"""Lab sessions (start / status / stop) through a FAKE docker client: lab/session.py, lab/box.py.

Fail-first design: the attacks are a credential leaking into argv or the store, a box started over a contaminated
workspace, a session that writes Kernel evidence, and config files written outside the box's HOME. A version of
session.start that skipped the wall checks or put `-e NAME=value` in argv fails these tests.

Written 2026-10-23 by lab-agents; built, NOT run. No Docker is used (tests/lab_fakes.FakeDocker).
"""

from __future__ import annotations

import pytest

from _lab_fakes import FakeDocker, docker
from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.lab import EXPLORATORY
from custom_harness.lab import session as S
from custom_harness.lab.agents import AgentOptions
from custom_harness.lab.box import LabBox
from custom_harness.lab.credentials import CredentialRefused, CredentialSpec
from custom_harness.lab.walls import LabWallError

KEY = "sk-lab-0123456789abcdef0123456789"


def _cred() -> CredentialSpec:
    return CredentialSpec(registry_key="anthropic-api", box_env="UNSET", spend_cap_usd=10.0, scope="lab ws, $10",
                          source_env="CHH_LAB_CRED_A")


def test_start_keeps_the_key_out_of_argv_and_records(store, monkeypatch):
    monkeypatch.setenv("CHH_LAB_CRED_A", KEY)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = FakeDocker()
    ledger_before = len(store.ledger_rows())
    rec = S.start(store, "claude-code", AgentOptions(registry_key="anthropic-api"), _cred(), session_id="s-one",
                  docker=docker(fake))
    argv, env, _ = fake.run_call()
    assert KEY not in " ".join(argv)
    assert "ANTHROPIC_API_KEY" in argv and env is not None and env["ANTHROPIC_API_KEY"] == KEY
    assert rec["label"] == EXPLORATORY and KEY not in repr(store.records(S.RECORD_KIND))
    assert rec["credential"]["fingerprint"] and rec["credential"]["spend_cap_usd"] == 10.0
    assert all(p.startswith("/home/lab/") for p in fake.files)
    assert len(store.ledger_rows()) == ledger_before  # a Lab session writes no Kernel ledger entry
    assert argv.count("-v") == 1


def test_claude_code_without_a_credential_is_refused(store):
    with pytest.raises(CredentialRefused):
        S.start(store, "claude-code", AgentOptions(registry_key="anthropic-api"), None, session_id="s-two",
                docker=docker(FakeDocker()))


def test_a_contaminated_workspace_refuses_the_start(store):
    """Attack: a held-out label file already sits in the workspace the box would mount."""
    labels = b"y\n0\n1\n1\n"
    Kernel(store).vault.register_dataset("t-x", DatasetRole.TEST, public={}, private={"test_y.csv": labels})
    ws = S.workspace_of("s-dirty")
    ws.mkdir(parents=True)
    (ws / "labels.csv").write_bytes(labels)
    fake = FakeDocker()
    with pytest.raises(LabWallError):
        S.start(store, "opencode", AgentOptions(model="m", local_model_server=True), None,
                session_id="s-dirty", docker=docker(fake))
    assert "run" not in fake.subcommands()


def test_failed_docker_run_is_recorded_and_cleaned_up(store):
    fake = FakeDocker(run_rc=125)
    with pytest.raises(RuntimeError):
        S.start(store, "opencode", AgentOptions(model="m", local_model_server=True), None,
                session_id="s-fail", docker=docker(fake))
    assert S.events(store, "s-fail")[-1]["event"] == "start_failed"
    assert "rm" in fake.subcommands()


def test_status_and_stop(store):
    fake = FakeDocker()
    S.start(store, "opencode", AgentOptions(model="m", local_model_server=True), None,
            session_id="s-three", docker=docker(fake))
    rows = S.status(store, "s-three", docker=docker(fake))
    assert rows[0]["box"] == "running" and rows[0]["label"] == EXPLORATORY
    S.stop(store, "s-three", docker=docker(fake))
    assert S.status(store, "s-three", docker=docker(fake))[0]["last_event"] == "stopped"
    assert S.workspace_of("s-three").is_dir()  # the workspace outlives the box
    with pytest.raises(S.LabError):
        S.stop(store, "s-three", docker=docker(fake))


def test_config_files_only_go_to_the_box_home():
    with pytest.raises(LabWallError):
        LabBox("s-x", docker(FakeDocker())).write_file("/workspace/.claude/settings.json", "{}")
    with pytest.raises(LabWallError):
        LabBox("s-x", docker(FakeDocker())).write_file("/home/lab/../../etc/passwd", "x")
