"""`chh lab` wiring (lab/cli.py and the three lines in cli.py).

Fail-first: before the wiring, `chh lab ...` was an argparse error (exit 2). These tests use no Docker: `agents` and
`check` touch no box, and `start` is refused by the credential policy before any docker call.

Written 2026-10-23 by lab-agents; built, NOT run.
"""

from __future__ import annotations

import json

import pytest

from custom_harness.cli import main


def test_lab_agents_lists_deferred_agents_with_reasons(capsys):
    assert main(["lab", "agents"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["agents"]["claude-code"]["supported"] is True
    assert out["agents"]["cursor"]["supported"] is False and out["agents"]["cursor"]["reason"]
    assert out["agents"]["antigravity"]["supported"] is False


def test_lab_check_is_clean_by_default(capsys):
    assert main(["lab", "check"]) == 0
    assert json.loads(capsys.readouterr().out)["problems"] == []


def test_lab_start_refuses_the_owner_key_variable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-owner-0123456789abcdef0123")
    from custom_harness.lab.credentials import CredentialRefused
    with pytest.raises(CredentialRefused):
        main(["lab", "start", "--agent", "claude-code", "--registry-key", "anthropic-api", "--credential-env",
              "ANTHROPIC_API_KEY", "--spend-cap-usd", "5", "--scope", "x"])


def test_lab_start_refuses_a_local_model_without_the_opt_in():
    with pytest.raises(SystemExit, match="refused.*--local-model-server"):
        main(["lab", "start", "--agent", "opencode", "--model", "m", "--session", "s-noopt"])
