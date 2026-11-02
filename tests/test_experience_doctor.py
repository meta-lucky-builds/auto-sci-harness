"""`chh exp doctor` (experience/doctor.py): each prerequisite, its next step, and that no key is ever shown."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from custom_harness.court import sealed as Z
from custom_harness.experience import cli as exp_cli
from custom_harness.experience import doctor as Doc
from custom_harness.lab.box import DockerCLI, Result
from custom_harness.store import EvidenceStore

KEY = "sk-lab-0123456789abcdef0123456789"
SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness" / "experience"


@pytest.fixture
def store(tmp_path):
    s = EvidenceStore(tmp_path / "store")
    yield s
    s.close()


def up(argv, env, stdin, timeout):
    return Result(0, "29.0\n", "")


def no_docker(argv, env, stdin, timeout):
    raise FileNotFoundError("docker")


def row(checks, name):
    return next(c for c in checks if c.name == name)


def ready(monkeypatch, items=()):
    monkeypatch.setattr(Doc, "pending", lambda store: list(items))
    monkeypatch.setattr(Doc, "static_problems", list)
    monkeypatch.setattr(Doc.H, "passing", lambda store: {"health_hash": "x"})


def test_with_nothing_set_up_every_gap_has_its_next_step(store):
    checks = Doc.doctor(store, docker=DockerCLI(runner=no_docker), environ={})
    assert row(checks, "Docker").state == Doc.MISSING and "Docker Desktop" in row(checks, "Docker").next_step
    assert "docker build -f docker/lab.Dockerfile" in row(checks, "Lab image").next_step
    assert row(checks, "Scoped key").state == Doc.MISSING
    assert row(checks, "Court health run").next_step == "chh court health --docker"
    assert row(checks, "Adapter approval (ml)").next_step.startswith("chh approve-adapter ml --approver NAME")
    assert all(c.next_step for c in checks if c.state == Doc.MISSING)
    assert {"Docker", "Lab image", "Scoped key", "Court health run"} <= {c.name for c in Doc.blocking(checks)}
    text = Doc.render(checks)
    assert "[MISSING] Docker" in text and "next: chh court health --docker" in text
    assert "stand before a first session" in text


def test_a_timeout_counts_as_no_answer(store):
    import subprocess

    def slow(argv, env, stdin, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert row(Doc.doctor(store, docker=DockerCLI(runner=slow), environ={}), "Docker").state == Doc.MISSING


def test_ready_shows_nothing_blocking(store, monkeypatch):
    ready(monkeypatch)
    checks = Doc.doctor(store, docker=DockerCLI(runner=up), environ={"CHH_LAB_CRED_A": KEY})
    assert Doc.blocking(checks) == []
    assert Doc.render(checks).endswith("Nothing stands before a first session.")
    # what code cannot check is listed as the person's, never as passed
    assert row(checks, "Feedback table").state == Doc.YOURS and row(checks, "Public random source").state == Doc.YOURS


def test_a_key_is_named_and_its_value_never_appears(store, monkeypatch):
    ready(monkeypatch)
    checks = Doc.doctor(store, docker=DockerCLI(runner=up), environ={"CHH_LAB_CRED_A": KEY, "CHH_LAB_CRED_B": ""})
    shown = Doc.render(checks) + json.dumps([asdict(c) for c in checks])
    assert KEY not in shown and "CHH_LAB_CRED_A" in shown and "CHH_LAB_CRED_B" not in shown


def test_a_missing_image_is_named(store, monkeypatch):
    ready(monkeypatch)

    def no_lab(argv, env, stdin, timeout):
        return Result(1, "", "") if Doc.LAB_IMAGE in argv else Result(0, "", "")

    checks = Doc.doctor(store, docker=DockerCLI(runner=no_lab), environ={"CHH_LAB_CRED_A": KEY})
    assert [c.name for c in Doc.blocking(checks)] == ["Lab image"]
    assert row(checks, "Lab image").next_step == Doc.LAB_BUILD
    assert row(checks, "Court sandbox image").state == Doc.OK


def test_lab_walls_problems_are_shown(store, monkeypatch):
    ready(monkeypatch)
    monkeypatch.setattr(Doc, "static_problems", lambda: ["the lab root overlaps the store"])
    c = row(Doc.doctor(store, docker=DockerCLI(runner=up), environ={}), "Lab walls")
    assert c.state == Doc.MISSING and "overlaps" in c.detail and c.next_step == "chh lab check"


def test_queue_rows_say_what_a_person_runs_and_what_can_wait(store, monkeypatch):
    items = [{"kind": "adapter", "name": "ml", "decide": "chh approve-adapter ml"},
             {"kind": "adapter", "name": "sim", "decide": "chh approve-adapter sim"},
             {"kind": "provider_signoff", "key": "anthropic-api", "decide": "chh sign-off-provider anthropic-api"},
             {"kind": "provider_signoff", "key": "other", "decide": "chh sign-off-provider other"},
             {"kind": "human_check", "decide": "chh register-human-check ID"},
             {"kind": "role", "decide": "chh approve-role T"},
             {"kind": "queue_error", "error": "broken source", "decide": "x"}]
    ready(monkeypatch, items)
    checks = Doc.doctor(store, docker=DockerCLI(runner=up), environ={"CHH_LAB_CRED_A": KEY})
    assert row(checks, "Adapter approval (ml)").next_step == "chh approve-adapter ml"
    assert row(checks, "Provider sign-off (anthropic-api)").next_step == "chh sign-off-provider anthropic-api"
    assert row(checks, "Human-held check").state == Doc.MISSING
    assert row(checks, "The list of human steps").detail == "broken source"
    names = {c.name for c in Doc.blocking(checks)}
    assert "Other adapters (lp, sim, circuit)" not in names and "Role approvals" not in names  # later, or if used
    assert row(checks, "Role approvals").state == Doc.MISSING


def test_another_providers_signoff_does_not_stand_in_the_way(store, monkeypatch):
    ready(monkeypatch, [{"kind": "provider_signoff", "key": "other", "decide": "x"}])
    checks = Doc.doctor(store, docker=DockerCLI(runner=up), environ={"CHH_LAB_CRED_A": KEY})
    assert row(checks, "Provider sign-off (anthropic-api)").state == Doc.OK


def test_the_sealed_track_rows(store, monkeypatch, tmp_path):
    ready(monkeypatch)
    monkeypatch.setattr(Doc, "truth_root", lambda: tmp_path / "absent")
    checks = Doc.doctor(store, docker=DockerCLI(runner=up), environ={"CHH_LAB_CRED_A": KEY})
    assert row(checks, "Sealed dataset").state == Doc.MISSING and row(checks, "Separate OS user").state == Doc.YOURS
    assert Doc.blocking(checks) == []  # the toy track does not need them
    store.append_record(Z.RECORD_KIND, {"event": "sealed", "project": "p"})
    monkeypatch.setattr(Doc, "truth_root", lambda: tmp_path)
    checks = Doc.doctor(store, docker=DockerCLI(runner=up), environ={})
    assert row(checks, "Sealed dataset").state == Doc.OK and row(checks, "Separate OS user").state == Doc.MISSING


def test_a_truth_root_this_user_cannot_read_passes(store, monkeypatch, tmp_path):
    def denied(path):
        raise PermissionError(path)

    monkeypatch.setattr(Doc.os, "scandir", denied)
    assert Doc._os_user(tmp_path).state == Doc.OK


def test_the_command_line_exit_code_and_json(store, monkeypatch, capsys):
    parser = argparse.ArgumentParser()
    exp_cli.add_parsers(parser.add_subparsers(dest="cmd"))
    ready(monkeypatch)
    monkeypatch.setattr(Doc, "doctor", lambda *a, **k: [Doc.Check("Docker", Doc.OK, "fine")])
    assert exp_cli.cmd_exp(parser.parse_args(["exp", "doctor"]), store) == 0
    assert "[ok" in capsys.readouterr().out
    monkeypatch.setattr(Doc, "doctor", lambda *a, **k: [Doc.Check("Docker", Doc.MISSING, "down", "start it")])
    assert exp_cli.cmd_exp(parser.parse_args(["exp", "doctor", "--json"]), store) == 1
    assert json.loads(capsys.readouterr().out)[0]["next_step"] == "start it"


def test_doctor_and_the_toy_driver_write_nothing_themselves():
    """Every write goes through the doorway's own verbs: neither file holds a store write or a human step."""
    for name in ("doctor.py", "toy.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        for word in ("append_record", "approve_role", "approve_adapter", "sign_off", "register_check", "D.retire"):
            assert word not in text, f"{name}: {word}"
