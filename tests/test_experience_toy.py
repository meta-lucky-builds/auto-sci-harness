"""`chh exp toy` (experience/toy.py): the minted toy track on a throwaway store, real Kernel, scripted agent, fake
docker, local sandbox. No model is called: the agent's turn is replaced by a function that writes its files."""

from __future__ import annotations

import argparse
import json

import pytest
import yaml

from _experience_world import World
from custom_harness.court import display as D
from custom_harness.experience import Refused
from custom_harness.experience import cli as exp_cli
from custom_harness.experience import schemas as M
from custom_harness.experience import toy as Y
from custom_harness.lab import session as S
from custom_harness.lab.promote import PromoteRequest
from test_sealed_project import ARMS, BEACON

KEY = "sk-lab-0123456789abcdef0123456789"
CRED = M.CredentialIn(registry_key="anthropic-api", spend_cap_usd=5.0, source_env="CHH_LAB_CRED_T", scope="toy, $5")


@pytest.fixture
def w(tmp_path, monkeypatch):
    world = World(tmp_path / "w")
    monkeypatch.setenv("CHH_LAB_CRED_T", KEY)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(Y, "LAB_ROWS", 800)
    monkeypatch.setattr(Y, "REHEARSAL_ROWS", 300)
    world.prompts = []
    world.method = (ARMS / "knn_radius.py").read_bytes()

    def agent_turn(req):
        world.prompts.append(req.prompt)
        ws = S.workspace_of(req.session_id)
        (ws / "method.py").write_bytes(world.method)
        (ws / "baseline.py").write_bytes((ARMS / "majority.py").read_bytes())
        (ws / "requirements.lock").write_text("# standard library only\n", encoding="utf-8")

    world.x.explore = agent_turn
    yield world
    world.close()


def go(w, **kw):
    return Y.run(w.x, Y.ToyIn(**kw), checks=lambda *a, **k: [])


def test_the_whole_track_stops_only_where_a_person_is_needed(w):
    first = go(w, credential=CRED)
    assert "ran the mock trial on Lab data" in first.did and not first.done
    assert "--beacon-round" in first.next_step and len(w.prompts) == 1
    assert "data/rings-lab.csv" in w.prompts[0]
    assert (S.workspace_of("toy-1") / "data" / "rings-lab.csv").read_bytes() == Y.lab_csv()

    second = go(w, beacon_round="r9")
    assert any(d.startswith("fixed the claim") for d in second.did) and "--beacon VALUE" in second.next_step

    third = go(w, beacon_value=BEACON)
    assert "the fresh test data was made" in third.did
    assert third.next_step.startswith("chh approve-role") and "human-only" in third.stopped_because
    case_id = next(c["case_id"] for c in D.listed(w.store))
    assert str(D.public_case(w.store, case_id)["target"]) in third.next_step

    assert go(w).next_step.startswith("chh approve-role")  # nothing moves until the person has acted
    w.approve_role(str(D.public_case(w.store, case_id)["target"]))

    last = go(w)
    assert last.done and last.case["verdict"] == "CERTIFIED"
    assert last.stopped_because.startswith("first certified result (simulator-scoped).")
    assert "discovery" not in last.stopped_because.lower()
    target = str(D.public_case(w.store, case_id)["target"])
    assert last.next_step == f"chh court retire {target} --by NAME"  # the full record is a person's decision

    assert go(w).next_step == last.next_step and not D.retired(w.store, target)
    D.retire(w.store, target, "owner")
    again = go(w)
    assert again.done and again.case["retired"] and again.next_step.startswith(f"chh exp share {case_id}")
    assert len(w.prompts) == 1  # the agent was asked once


def test_a_failed_trial_is_reported_as_what_it_is(w, monkeypatch):
    go(w, credential=CRED)
    go(w, beacon_round="r9")
    go(w, beacon_value=BEACON)
    case_id = next(c["case_id"] for c in D.listed(w.store))
    w.approve_role(str(D.public_case(w.store, case_id)["target"]))
    monkeypatch.setattr(w.x, "run_case", w.x.case)  # no outcome: `case` says so
    r = go(w)
    assert r.done and not r.stopped_because.startswith(Y.RESULT_NAME)


def test_a_gap_stops_the_track_before_anything_starts(w):
    gaps = [Y.Check("Lab image", "missing", "not here", "docker build ...")]
    r = Y.run(w.x, Y.ToyIn(credential=CRED), checks=lambda *a, **k: gaps)
    assert r.blocked_by == ["Lab image: docker build ..."] and r.next_step == "chh exp doctor"
    assert S.events(w.store, "toy-1") == [] and w.prompts == []


def test_the_first_run_needs_a_named_key_and_limit(w):
    r = go(w)
    assert "--spend-cap-usd" in r.next_step and "--credential-env" in r.next_step and w.prompts == []


def test_work_that_cannot_be_frozen_goes_back_to_the_agent(w):
    w.x.explore = lambda req: w.prompts.append(req.prompt)  # the agent wrote nothing
    r = go(w, credential=CRED)
    assert "could not be frozen" in r.stopped_because and r.next_step.startswith("chh exp steer toy-1")


def test_another_refusal_of_the_freeze_is_not_swallowed(w, monkeypatch):
    go(w, credential=CRED)

    def boom(req):
        raise Refused("LabError", "stopped")

    monkeypatch.setattr(w.x, "freeze", boom)
    with pytest.raises(Refused, match="stopped"):
        go(w, new_freeze=True)


def test_a_bad_mock_trial_spends_nothing_and_a_new_freeze_carries_on(w):
    w.method = (ARMS / "majority.py").read_bytes() + b"\n# no effect\n"
    r = go(w, credential=CRED)
    assert "would not certify" in r.stopped_because and "--new-freeze" in r.next_step
    assert D.listed(w.store) == []
    assert "would not certify" in go(w).stopped_because  # the same answer on the next run, no new trial
    w.method = (ARMS / "knn_radius.py").read_bytes()
    w.x.explore(M.ExploreIn(session_id="toy-1", prompt="again"))
    assert "--beacon-round" in go(w, new_freeze=True).next_step


def test_the_lab_data_is_public_and_the_request_is_valid():
    head, *rows = Y.lab_csv().decode().splitlines()
    assert head == "x1,x2,n1,n2,n3,n4,label" and len(rows) == Y.LAB_ROWS and Y.lab_csv() == Y.lab_csv()
    assert PromoteRequest.model_validate(yaml.safe_load(Y.REQUEST)).claim.metric == "accuracy"
    assert "method.py" in Y.AGENT_BRIEF and "build_model(seed)" in Y.AGENT_BRIEF


def test_the_request_file_stays_out_of_the_agents_reach(w):
    from custom_harness.lab.walls import lab_root, overlaps
    assert not overlaps(Y._home("toy-1") / "promote.yaml", lab_root())


def test_the_command_line(w, monkeypatch, capsys):
    parser = argparse.ArgumentParser()
    exp_cli.add_parsers(parser.add_subparsers(dest="cmd"))
    seen = []

    def run(x, o):
        seen.append(o)
        return Y.ToyReport(did=["x"])

    monkeypatch.setattr(Y, "run", run)
    args = ["exp", "toy", "--credential-env", "CHH_LAB_CRED_T", "--spend-cap-usd", "5", "--scope", "toy", "--beacon",
            "v", "--beacon-round", "r", "--new-freeze"]
    assert exp_cli.cmd_exp(parser.parse_args(args), w.store) == 0
    assert json.loads(capsys.readouterr().out)["did"] == ["x"]
    assert seen[0].credential.spend_cap_usd == 5 and seen[0].beacon_value == "v" and seen[0].new_freeze
    assert exp_cli.cmd_exp(parser.parse_args(["exp", "toy"]), w.store) == 0
    assert seen[1].credential is None
