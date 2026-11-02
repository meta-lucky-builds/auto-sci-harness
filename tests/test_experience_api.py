"""The Experience API verb by verb, and one full session through it alone (GOAL 18.3, 18.5). Real kerneld, real
Kernel, scripted programs, no model calls (tests/_experience_world.py). Human steps are played by the fixture outside
the API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from _court_world import Case
from _experience_world import World
from custom_harness import cli as root_cli
from custom_harness.audit import claim_audit as CA
from custom_harness.court import display as D
from custom_harness.court import rehearsal as R
from custom_harness.experience import Experience, Refused
from custom_harness.experience import cli as exp_cli
from custom_harness.experience import schemas as M
from custom_harness.experience import words as W
from custom_harness.kernel import Kernel
from custom_harness.kerneld.releases import released
from custom_harness.lab import session as S
from custom_harness.lab.agents import AgentOptions
from test_sealed_project import BEACON, rings_csv

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "w")
    yield world
    world.close()


def test_a_full_session_through_the_api_alone(w):
    """start, seal, explore, rehearse, promote, verdict with badges, case, record. Only the human steps are outside."""
    x = w.x
    me = x.set_researcher("Asha Rao")
    assert me.researcher == "Asha Rao" and me.chosen
    w.clean(me)

    sealed = w.seal()
    w.clean(sealed)
    assert sealed.lab_rows.value == 800 and [n.value for n in sealed.sealed_rows] == [400, 400]

    session = w.session()
    w.clean(session)
    explored = x.explore(M.ExploreIn(session_id="s-exp", prompt="try a radius rule"))
    w.clean(explored)
    assert explored.reply.startswith("agent output") and explored.finished
    steered = x.steer(M.SteerIn(session_id="s-exp", text="why the radius?"))
    w.clean(steered)
    assert steered.sent and steered.reply and steered.court_results_seen.value == 0
    lab = x.lab("s-exp")
    w.clean(lab)
    assert lab.dropped_ideas.value == 1 and lab.open_ideas.value == 1 and lab.steers.value == 1
    assert lab.cost_usd.value is None and lab.costs_unknown.value >= 1  # unknown stays unknown

    frozen = x.freeze(M.FreezeIn(session_id="s-exp", request_file=str(w.request)))
    w.clean(frozen)
    pid = frozen.promotion_id
    rehearsal = x.rehearse(M.RehearseIn(promotion_id=pid, rows=200))
    w.clean(rehearsal)
    assert rehearsal.ran and rehearsal.would_certify is True and rehearsal.notice == W.REHEARSAL_NOTICE
    assert {c.status for c in rehearsal.checks} == {"pass"} and len(rehearsal.checks) == 10
    assert len(rehearsal.scores) == 6 and rehearsal.readiness.rows
    assert x.rehearsal_details(pid).details["checks"]["K6"]["status"] == "pass"
    assert len(released(w.store)) == 0  # a rehearsal spends nothing

    ask = M.PromoteIn(promotion_id=pid, route="sealed", project="cohort-1", second_part=True)
    preview = x.promote_preview(ask)
    w.clean(preview)
    assert preview.finding_possible and preview.looks_remaining.value == 1 and preview.what_happens
    assert x.cases().cases == ()  # a preview registers nothing
    promoted = x.promote(ask)
    w.clean(promoted)
    first, second = promoted.case_id, promoted.second_case_id
    assert first and second

    waiting = x.waiting()
    w.clean(waiting)
    roles = [i for i in waiting.items if i.title == W.WAITING["role"][0]]
    assert len(roles) == 2 and all(i.human_only and "chh approve-role" in i.command for i in roles)
    route = x.approve(roles[0].id)
    assert route.performed is False and route.binds["dataset_hash"]
    assert x.case(first).command.startswith("chh approve-role")

    seal_targets = Experience(w.store).technical(first).details["target"], x.technical(second).details["target"]
    for t in seal_targets:  # the human step, outside the API
        w.approve_role(t)
    assert any(i.title == W.WAITING["ready_to_run"][0] for i in x.waiting().items)

    won = x.run_case(first)
    w.clean(won)
    assert won.verdict == "CERTIFIED" and "VALIDATED" in [b.name for b in won.badges]
    assert won.second_test == W.SECOND_TEST["declared"]
    assert won.scope.startswith("On your sealed data") and won.retired is False and won.rerun is None
    assert won.reason is None and won.next_step is None and won.story.idea_path == ("radius",) and won.story.intact
    assert won.looks_remaining.value == 0
    again = x.run_case(second)
    assert again.verdict == "CERTIFIED"
    won = x.case(first)
    w.clean(won)
    assert {b.name for b in won.badges} == {"REPRODUCIBLE", "VALIDATED", "RE-VALIDATED"}
    assert won.second_test == W.SECOND_TEST["passed"]

    record = x.record()
    w.clean(record)
    assert record.researcher == "Asha Rao" and len(record.outcomes) == 2 and record.verdicts_unopened.value == 0
    assert record.calibration.claims.value == 2 and record.calibration.mean_squared_error.value is not None
    assert [e.title for e in record.dead_ends] == ["Angle"] and record.harness_improvement == W.IMPROVEMENT
    assert x.record("radius").outcomes and not x.record("radius").dead_ends
    assert x.record("angle").dead_ends and not x.record("angle").outcomes
    line = x.cases()
    w.clean(line)
    assert [c.state for c in line.cases] == ["CERTIFIED", "CERTIFIED"]

    out = w.root / "case-file.json"
    shared = x.share(M.ShareIn(case_id=first, reviewer="Dr Lin", out_file=str(out)))
    w.clean(shared)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["verdict"] == "CERTIFIED" and doc["rerun"] is None and doc["command"] is None

    D.retire(w.store, seal_targets[0], "Asha Rao")  # the human step, outside the API
    opened = x.case(first)
    assert opened.retired and opened.rerun.startswith("chh reproduce ")
    paper = x.paper(M.PaperIn(case_id=first, title="A radius rule", body="The gap is \\chhnum[3f]{gap}.\n",
                              refs={"gap": "/claim/effect/min_effect"}))
    w.clean(paper)
    assert "0.100" in Path(paper.path).read_text(encoding="utf-8") and paper.numbers.value == 1
    assert x.stop("s-exp").box == "absent"


def test_the_generated_route_and_a_failed_claim(w):
    x = w.x
    w.session(project=None)
    pid = w.freeze()
    ask = M.PromoteIn(promotion_id=pid, route="generated", family="rings", beacon_round="r9", n_test=600,
                      second_round="r10", dev_pairs=None)
    assert x.promote_preview(ask).readiness.sentence.startswith("Not estimated")
    done = x.promote(ask)
    assert done.command == f"chh exp mint {done.case_id} --beacon VALUE" and done.second_case_id
    assert any(i.id == f"needs_beacon-{done.case_id}" for i in x.waiting().items)
    assert x.case(done.case_id).verdict is None and x.case(done.case_id).verdict_sentence == "No verdict yet."
    after = x.mint(done.case_id, BEACON)
    w.clean(after)
    assert after.command.startswith("chh approve-role")
    assert "generator-scoped" in after.scope
    assert x.lab("s-exp").project is None


def test_a_rehearsal_that_could_not_run_says_why_and_needs_a_data_file(w):
    w.session(project=None)
    broken = b"def build_model(seed):\n    raise ValueError('typo in my code')\n"
    (S.workspace_of("s-exp") / "method.py").write_bytes(broken)
    pid = w.freeze()
    with pytest.raises(Refused) as e:
        w.x.rehearse(M.RehearseIn(promotion_id=pid, rows=100))
    assert e.value.code == "NoLabData"
    lab = w.root / "lab.csv"
    lab.write_bytes(rings_csv(300, seed=9))
    out = w.x.rehearse(M.RehearseIn(promotion_id=pid, rows=100, data_file=str(lab), label_column="outcome"))
    assert out.ran is False and out.would_certify is None and "typo in my code" in out.reason and out.scores == ()
    assert w.x.rehearsal_details("nothing").details == {}


def test_readiness_uses_the_rehearsals_counts_and_can_object(w):
    w.session(project=None)
    pid = w.freeze()
    w.store.append_record(R.RECORD_KIND, {"promotion_id": pid, "dev_pairs": [500, 300, 10, 190]})
    ask = M.PromoteIn(promotion_id=pid, route="generated", family="rings", beacon_round="r9")
    good = w.x.promote_preview(ask)
    assert good.readiness.sentence.startswith("No objection at ") and good.readiness.rows
    w.clean(good)
    bad = w.x.promote_preview(ask.model_copy(update={"dev_pairs": (500, 10, 300, 190)}))
    assert bad.readiness.sentence.startswith("Unlikely to certify") and all(r.objection for r in bad.readiness.rows)


def test_a_public_route_is_labelled_up_front_and_cannot_be_a_finding(w):
    w.session(project=None)
    pid = w.freeze()
    targets = w.root / "targets.yaml"
    targets.write_text('format: chh-lab-targets/1\ndatasets:\n  - {openml_id: 40983, sha256: "%s", target: class, '
                       'licence: CC-BY}\n' % ("a" * 64), encoding="utf-8")
    ask = M.PromoteIn(promotion_id=pid, route="public", targets_file=str(targets))
    preview = w.x.promote_preview(ask)
    assert preview.finding_possible is False and "cannot be a finding" in preview.what_happens[-1]
    done = w.x.promote(ask)
    assert done.next_step == W.PUBLIC_NEXT and done.command.startswith("chh audit show")
    w.clean(done)


def test_preview_refuses_an_unsealed_project_and_a_route_without_its_fields(w):
    w.session(project=None)
    pid = w.freeze()
    with pytest.raises(Refused) as e:
        w.x.promote_preview(M.PromoteIn(promotion_id=pid, route="sealed", project="nope"))
    assert e.value.code == "NotSealed"
    with pytest.raises(ValueError, match="needs"):
        M.PromoteIn(promotion_id=pid, route="generated")


def test_preview_shows_the_looks_a_sealed_target_has(w):
    target = w.seal()
    w.session()
    pid = w.freeze()
    ask = M.PromoteIn(promotion_id=pid, route="sealed", project="cohort-1")
    t = Experience(w.store).promote_preview(ask).looks_remaining.link.split("/")[3]
    Kernel(w.store).open_alpha_account(t, 0.05, 3, "fixture-human")
    assert w.x.promote_preview(ask).looks_remaining.value == 3
    D.retire(w.store, t, "owner")
    assert w.x.promote_preview(ask).looks_remaining.value == 0
    assert target.project == "cohort-1"


def test_whoami_defaults_to_the_os_user_and_owners_are_followed(w):
    me = w.x.whoami()
    assert me.researcher == me.os_user and not me.chosen and "grants nothing" in me.note
    w.seal()
    w.session()
    w.x.set_researcher("Someone Else")
    assert w.x.record().dead_ends == ()  # the session belongs to the name that started it
    assert w.x.cases().cases == ()


def test_a_session_started_on_the_command_line_counts_for_its_os_user(w):
    S.start(w.store, "opencode", AgentOptions(model="m", local_model_server=True), None, session_id="s-cli",
            docker=w.x.docker)
    w.lab_work("s-cli")
    w.x.explore(M.ExploreIn(session_id="s-cli", prompt="go"))
    assert [e.title for e in w.x.record().dead_ends] == ["Angle"]


def test_a_could_not_run_case_says_why_and_how_to_retry(tmp_path):
    c = Case(tmp_path / "c", method=b"def build_model(seed):\n    raise ValueError('typo in my code')\n")
    try:
        c.run()
        x = Experience(c.store)
        v = x.case(c.case_id)
        assert v.verdict == "COULD NOT RUN" and "typo in my code" in v.reason and v.next_step == W.RETRY
        assert v.command == f"chh exp run {c.case_id}" and v.story is None
        assert "could not run" in v.verdict_sentence
        x._own("case", c.case_id, x.whoami().researcher)
        assert [o.verdict for o in x.record().outcomes] == ["COULD NOT RUN"]
        assert x.record().dead_ends == ()
    finally:
        c.close()


def test_a_rejected_case_gives_a_closed_code_and_a_dead_end(tmp_path):
    same = (Path(__file__).parent / "fixtures/audit/arms/majority.py").read_bytes() + b"\n# no effect\n"
    c = Case(tmp_path / "c", method=same)
    try:
        c.run()
        x = Experience(c.store)
        x._own("case", c.case_id, x.whoami().researcher)
        v = x.case(c.case_id)
        assert v.verdict == "NOT CERTIFIED" and v.reason_code in W.REASONS and v.badges == ()
        assert W.REASONS[v.reason_code] in v.verdict_sentence
        rec = x.record()
        assert [e.source for e in rec.dead_ends] == ["verdict"] and rec.outcomes[0].verdict == "NOT CERTIFIED"
    finally:
        c.close()


def test_a_certified_claim_that_is_not_fresh_is_not_called_a_finding():
    text = W.verdict_sentence("CERTIFIED", ["REPRODUCIBLE"], "Scope.", None, retrospective=False)
    assert text.endswith(W.NOT_A_FINDING)
    retro = W.verdict_sentence("CERTIFIED", [], "Scope.", None, retrospective=True)
    assert retro.endswith(W.RETROSPECTIVE)
    assert W.verdict_sentence("CERTIFIED", ["VALIDATED"], "Scope.", None, retrospective=False) == "Certified. Scope."


def test_replicated_badges_state_their_risk():
    same = W.badge_text("REPLICATED (a -> a, same family)")
    assert W.SAME_FAMILY in same["meaning"] and W.REPLICATED_RISK in same["meaning"]
    assert W.UNKNOWN_FAMILY in W.badge_text("REPLICATED (unknown -> b, family unknown)")["meaning"]
    other = W.badge_text("REPLICATED (a -> b, different family)")
    assert W.SAME_FAMILY not in other["meaning"] and W.REPLICATED_RISK in other["meaning"]


def test_the_verdict_word_comes_from_the_kernels_label_only():
    assert W.verdict_of("CERTIFIED-INTERNAL", "DECIDED") == "CERTIFIED"
    assert W.verdict_of("REJECTED", "DECIDED") == "NOT CERTIFIED"
    assert W.verdict_of(None, "COULD NOT RUN") == "COULD NOT RUN"
    assert W.verdict_of(None, "no outcome yet") is None


def test_a_claim_audit_is_a_case_with_its_full_report(tmp_path):
    """Reproduce-this-paper: the audit's own outcome record, on a real certificate."""
    c = Case(tmp_path / "c")
    try:
        out = c.run()
        x = Experience(c.store)
        for audit_id, cert in (("a-1", out["cert_hash"]), ("a-2", None)):
            c.store.append_record(CA.RECORD_KIND, {"audit_id": audit_id, "claim_hash": c.claim_hash,
                                                   "verdict": "CERTIFIED" if cert else "COULD NOT RUN",
                                                   "cert_hash": cert, "reason": "no data", "needs_owner": False})
        audit = x.case("a-1")
        assert audit.verdict == "CERTIFIED" and W.RETROSPECTIVE in audit.verdict_sentence
        assert audit.scope.startswith("On public data") and audit.badges
        assert "# Claim Audit" in x.technical("a-1").details["report"]
        assert [r["note"] for r in released(c.store, c.target)].count("court:audit_report") == 1
        assert x.case("a-2").verdict == "COULD NOT RUN"
        assert x.resolve(audit.looks_remaining.link).value == 0
        with pytest.raises(Refused) as e:
            x.case("no-such-case")
        assert e.value.code == "NoCase"
        assert x.technical("no-such-case").details == {}
    finally:
        c.close()


def test_every_waiting_kind_has_words():
    from custom_harness.human.queue import KIND_ORDER
    assert set(KIND_ORDER) <= set(W.WAITING)
    assert {"needs_beacon", "needs_role_approval", "ready_to_run", "outcome_recorded"} == set(W.STEPS)


def test_resolve_refuses_what_is_not_a_link(w):
    for bad in ("nonsense", "chh://case/x", "chh://nothing/x/y"):
        with pytest.raises(Refused) as e:
            w.x.resolve(bad)
        assert e.value.code == "BadLink"
    with pytest.raises(Refused):
        w.x.resolve("chh://target/new/nothing")
    assert w.x.resolve("chh://target/new/looks").value == 1
    assert w.x._facts("paper", "none") == {} and w.x._facts("seal", "none")["lab_rows"] is None
    assert w.x._facts("readiness", "none") == {} and w.x._facts("rehearsal", "none") == {}


def test_a_refusal_keeps_its_kind_and_words(w):
    with pytest.raises(Refused) as e:
        w.x.freeze(M.FreezeIn(session_id="nope", request_file="x"))
    assert e.value.code == "LabError" and "nope" in str(e.value)
    with pytest.raises(Refused) as e2:
        w.x.approve("nothing")
    assert e2.value.code == "NotWaiting"


def test_a_credential_is_named_never_passed(w):
    with pytest.raises(Refused) as e:
        w.x.start(M.StartIn(agent="opencode", model="m", box_env="KEY", credential=M.CredentialIn(
            registry_key="r", spend_cap_usd=1.0, source_env="CHH_LAB_CRED_X")))
    assert e.value.code == "CredentialRefused"
    assert "api_key" not in json.dumps(M.CredentialIn.model_json_schema())


def test_the_default_sandbox_is_the_courts_docker_sandbox(tmp_path, monkeypatch):
    from custom_harness import audit
    seen = {}

    def fake(a, root):
        seen.update(docker=a.docker, dev=a.dev_sandbox, root=str(root))
        return "factory"

    monkeypatch.setattr(audit.cli, "_factory", fake)
    world = World(tmp_path / "d")
    try:
        assert Experience(world.store)._factories(tmp_path) == "factory"
        assert seen["docker"] is True and seen["dev"] is False
    finally:
        world.close()


def test_the_command_line_speaks_for_the_same_doorway(w, capsys, monkeypatch):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    exp_cli.add_parsers(sub)

    def run(*argv):
        return exp_cli.cmd_exp(parser.parse_args(["exp", *argv]), w.store)

    assert run("whoami", "--set", "Asha Rao") == 0
    assert json.loads(capsys.readouterr().out)["researcher"] == "Asha Rao"
    assert run("waiting") == 0 and run("cases") == 0 and run("record", "--query", "x") == 0
    capsys.readouterr()
    assert run("schema") == 0
    assert "Case" in json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit, match="refused"):
        run("approve", "nothing")
    monkeypatch.setattr("sys.argv", ["chh", "exp", "whoami"])
    assert root_cli.main() == 0  # wired into `chh`
    assert json.loads(capsys.readouterr().out)["chosen"] is False  # the root command opens its own store


def test_the_published_schema_is_current():
    doc = json.loads((ROOT / "docs" / "experience_api.schema.json").read_text(encoding="utf-8"))
    assert doc == json.loads(json.dumps(M.api_schema()))


def test_a_refused_agent_configuration_is_a_refusal_of_the_doorway(w):
    with pytest.raises(Refused) as e:
        w.x.start(M.StartIn(agent="opencode", session_id="s-nomodel"))
    assert e.value.code == "AdapterRefused"


def test_the_local_model_opt_in_goes_through_the_doorway(w, capsys):
    parser = argparse.ArgumentParser()
    exp_cli.add_parsers(parser.add_subparsers(dest="cmd"))
    with pytest.raises(Refused, match="--local-model-server"):
        w.x.start(M.StartIn(agent="opencode", model="m", session_id="s-noopt"))
    a = parser.parse_args(["exp", "start", "opencode", "--model", "m", "--local-model-server"])
    assert a.local_model_server is True
