"""Delegation driver (GOAL §15 last paragraph; OR orx-agent-delegation, enforced in code, not by the prompt).

Every attack test asserts THREE things: the attempt reached the driver (a `refused` ledger entry naming the
attacker and the attempted action exists), it was refused (the call raised Refused with the expected closed-enum
reason), and nothing changed (no new session, no lock moved, no spend counted)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_harness.orchestration import params
from custom_harness.orchestration.brief import HelperBrief, parse_brief
from custom_harness.orchestration.driver import Driver, IntegrityError, Refused


def brief(**over):
    b = {
        "brief_id": "adapter-fem-1",
        "project": "custom-harness",
        "branch": "helper/fem-1",
        "worktree": "wt/fem-1",
        "files": ["src/custom_harness/adapters/fem.py", "tests/test_fem.py"],
        "allowed_spends": [
            {"kind": "compute", "resource": "local:cpu", "unit": "cpu_seconds", "limit": 600},
            {"kind": "model_quota", "resource": "model:fake-family-a", "unit": "calls", "limit": 5},
        ],
        "expected_output": "A FEM adapter module and its tests, committed on helper/fem-1.",
        "definition_of_done": "tests/test_fem.py passes and was shown failing before the change.",
        "instructions": ("Implement the structural FEM adapter in src/custom_harness/adapters/fem.py against the "
                         "adapter interface in src/custom_harness/kernel/adapters.py, with a mesh-convergence "
                         "check. Write tests first."),
    }
    b.update(over)
    return b


@pytest.fixture
def drv(tmp_path: Path) -> Driver:
    return Driver(tmp_path / "orch", max_in_flight=2, max_total_per_lead=4)


def lead(drv: Driver, name: str = "lead-a", spends=None) -> str:
    return drv.register_lead(name, allowed_spends=spends or [])


def refusals(drv: Driver, action: str) -> list[dict]:
    return [e["data"] for e in drv.ledger.entries() if e["kind"] == "refused" and e["data"]["action"] == action]


# ---------------------------------------------------------------- happy path
def test_lead_spawns_helper_with_valid_brief(drv: Driver):
    t = lead(drv)
    h = drv.spawn(t, brief())
    assert h.session.startswith("helper-")
    st = drv.status()
    assert st["in_flight"] == [h.session]
    assert st["locks"]["branches"]["helper/fem-1"] == h.session
    kinds = [e["kind"] for e in drv.ledger.entries()]
    assert kinds == ["lead_registered", "spawned"]
    spawned = drv.ledger.entries()[1]["data"]
    assert spawned["parent"] == "lead-a" and spawned["brief"]["branch"] == "helper/fem-1"
    assert "token" not in json.dumps(spawned).replace("token_hash", "")  # the secret itself is never stored


def test_complete_releases_branch_and_slot(drv: Driver):
    t = lead(drv)
    h = drv.spawn(t, brief())
    drv.complete(h.token, output="commit abc123 on helper/fem-1")
    st = drv.status()
    assert st["in_flight"] == [] and "helper/fem-1" not in st["locks"]["branches"]
    with pytest.raises(Refused) as e:  # a finished helper can do nothing more
        drv.spend(h.token, "compute", "local:cpu", "cpu_seconds", 1)
    assert e.value.reason == "session_finished"


# ---------------------------------------------------------------- attack: nested spawn
def test_attack_helper_spawning_a_helper_is_refused_and_ledgered(drv: Driver):
    t = lead(drv)
    h = drv.spawn(t, brief())
    before = drv.status()
    with pytest.raises(Refused) as e:
        drv.spawn(h.token, brief(brief_id="nested", branch="helper/nested", worktree="wt/nested"))
    assert e.value.reason == "nested_spawn"
    rows = refusals(drv, "spawn")
    assert len(rows) == 1 and rows[0]["requester"] == h.session and rows[0]["reason"] == "nested_spawn"
    assert rows[0]["detail"]["brief_id"] == "nested"  # the attempted brief was seen by the driver
    after = drv.status()
    assert after["in_flight"] == before["in_flight"] == [h.session]
    assert "helper/nested" not in after["locks"]["branches"]


def test_attack_unknown_token_cannot_spawn(drv: Driver):
    lead(drv)
    with pytest.raises(Refused) as e:
        drv.spawn("not-a-real-token", brief())
    assert e.value.reason == "unknown_session"
    assert refusals(drv, "spawn")[0]["requester"] is None
    assert drv.status()["in_flight"] == []


# ---------------------------------------------------------------- attack: spends
def test_attack_over_budget_spend_is_refused_and_ledgered(drv: Driver):
    t = lead(drv)
    h = drv.spawn(t, brief())
    drv.spend(h.token, "model_quota", "model:fake-family-a", "calls", 4)
    with pytest.raises(Refused) as e:
        drv.spend(h.token, "model_quota", "model:fake-family-a", "calls", 2)  # 4 + 2 > 5
    assert e.value.reason == "spend_over_budget"
    rows = refusals(drv, "spend")
    assert len(rows) == 1 and rows[0]["requester"] == h.session
    assert rows[0]["detail"] == {"kind": "model_quota", "resource": "model:fake-family-a", "unit": "calls",
                                 "amount": 2.0, "used": 4.0, "limit": 5.0}
    assert drv.used(h.session) == {("model_quota", "model:fake-family-a", "calls"): 4.0}  # refused spend not counted
    drv.spend(h.token, "model_quota", "model:fake-family-a", "calls", 1)  # exactly the limit is allowed
    assert drv.used(h.session)[("model_quota", "model:fake-family-a", "calls")] == 5.0


def test_attack_unauthorised_spend_is_refused_and_ledgered(drv: Driver):
    t = lead(drv)
    h = drv.spawn(t, brief())
    with pytest.raises(Refused) as e:
        drv.spend(h.token, "compute", "gpu:modal", "gpu_seconds", 1)  # not in the brief: forbidden
    assert e.value.reason == "spend_unauthorized"
    with pytest.raises(Refused) as e2:
        drv.spend(h.token, "model_quota", "model:fake-family-a", "tokens", 1)  # right model, wrong unit
    assert e2.value.reason == "spend_unauthorized"
    rows = refusals(drv, "spend")
    assert [r["reason"] for r in rows] == ["spend_unauthorized", "spend_unauthorized"]
    assert rows[0]["detail"]["resource"] == "gpu:modal"
    assert drv.used(h.session) == {}


@pytest.mark.parametrize("amount", [0, -3, float("nan"), float("inf")])
def test_attack_nonpositive_or_nonfinite_spend_is_refused(drv: Driver, amount):
    t = lead(drv)
    h = drv.spawn(t, brief())
    with pytest.raises(Refused) as e:
        drv.spend(h.token, "compute", "local:cpu", "cpu_seconds", amount)
    assert e.value.reason == "spend_invalid"
    assert len(refusals(drv, "spend")) == 1 and drv.used(h.session) == {}


def test_lead_spends_are_bounded_by_its_own_registration(drv: Driver):
    t = lead(drv, spends=[{"kind": "compute", "resource": "local:cpu", "unit": "cpu_seconds", "limit": 10}])
    drv.spend(t, "compute", "local:cpu", "cpu_seconds", 10)
    with pytest.raises(Refused) as e:
        drv.spend(t, "compute", "local:cpu", "cpu_seconds", 0.5)
    assert e.value.reason == "spend_over_budget"
    assert refusals(drv, "spend")[0]["requester"] == "lead-a"


# ---------------------------------------------------------------- attack: held branch / worktree
def test_attack_claim_on_branch_held_by_another_helper_is_refused(drv: Driver):
    t = lead(drv)
    h1 = drv.spawn(t, brief())
    with pytest.raises(Refused) as e:
        drv.spawn(t, brief(brief_id="second", worktree="wt/other"))  # same branch helper/fem-1
    assert e.value.reason == "branch_held"
    rows = refusals(drv, "spawn")
    assert len(rows) == 1 and rows[0]["detail"]["branch"] == "helper/fem-1"
    assert rows[0]["detail"]["holder"] == h1.session
    st = drv.status()
    assert st["locks"]["branches"]["helper/fem-1"] == h1.session and st["in_flight"] == [h1.session]
    assert list(st["locks"]["worktrees"].values()) == [h1.session]  # no lock was taken for the refused helper
    assert drv._norm_wt("wt/other") not in st["locks"]["worktrees"]


def test_attack_claim_on_branch_held_by_another_lead_session_is_refused(drv: Driver):
    ta = lead(drv, "lead-a")
    tb = lead(drv, "lead-b")
    drv.claim(tb, branch="or-deleg", worktree="wt/lead-b")
    with pytest.raises(Refused) as e:
        drv.spawn(ta, brief(branch="or-deleg"))
    assert e.value.reason == "branch_held" and refusals(drv, "spawn")[0]["detail"]["holder"] == "lead-b"
    with pytest.raises(Refused) as e2:
        drv.spawn(ta, brief(worktree="wt/lead-b"))  # a free branch, but in lead-b's worktree
    assert e2.value.reason == "worktree_held"
    assert drv.status()["in_flight"] == []


def test_attack_branch_checked_out_in_another_git_worktree_is_refused(tmp_path: Path):
    d = Driver(tmp_path / "orch", max_in_flight=2, max_total_per_lead=4,
               checked_out=lambda: {"helper/fem-1": str(tmp_path / "somebody-elses-worktree")})
    t = d.register_lead("lead-a", allowed_spends=[])
    with pytest.raises(Refused) as e:
        d.spawn(t, brief())
    assert e.value.reason == "branch_held"
    assert refusals(d, "spawn")[0]["detail"]["holder"].startswith("git-worktree:")


def test_branch_names_are_compared_exactly_not_by_lock_file_name(drv: Driver):
    t = lead(drv)
    drv.spawn(t, brief(branch="a/b", worktree="wt/1"))
    drv.spawn(t, brief(brief_id="x2", branch="a-b", worktree="wt/2"))  # a different branch: allowed
    assert set(drv.status()["locks"]["branches"]) == {"a/b", "a-b"}


def test_worktree_paths_are_normalised(drv: Driver, tmp_path: Path):
    t = lead(drv)
    drv.spawn(t, brief(worktree=str(tmp_path / "wt" / "x")))
    with pytest.raises(Refused) as e:
        drv.spawn(t, brief(brief_id="y", branch="other", worktree=str(tmp_path / "wt" / "." / "x")))
    assert e.value.reason == "worktree_held"


def test_helper_cannot_claim_branches(drv: Driver):
    t = lead(drv)
    h = drv.spawn(t, brief())
    with pytest.raises(Refused) as e:
        drv.claim(h.token, branch="sneaky", worktree="wt/sneaky")
    assert e.value.reason == "helper_cannot_claim"
    assert "sneaky" not in drv.status()["locks"]["branches"] and refusals(drv, "claim")


def test_only_the_holder_releases(drv: Driver):
    ta, tb = lead(drv, "lead-a"), lead(drv, "lead-b")
    drv.claim(ta, branch="main-work", worktree="wt/a")
    with pytest.raises(Refused) as e:
        drv.release(tb, branch="main-work")
    assert e.value.reason == "not_holder"
    assert drv.status()["locks"]["branches"]["main-work"] == "lead-a"
    drv.release(ta, branch="main-work")
    assert "main-work" not in drv.status()["locks"]["branches"]


# ---------------------------------------------------------------- attack: incomplete brief
@pytest.mark.parametrize("missing", ["project", "branch", "worktree", "files", "allowed_spends", "expected_output",
                                     "definition_of_done", "instructions", "brief_id"])
def test_attack_brief_missing_a_required_field_is_refused(drv: Driver, missing):
    t = lead(drv)
    b = brief()
    del b[missing]
    with pytest.raises(Refused) as e:
        drv.spawn(t, b)
    assert e.value.reason == "brief_invalid"
    rows = refusals(drv, "spawn")
    assert len(rows) == 1 and rows[0]["requester"] == "lead-a"
    assert missing in rows[0]["detail"]["problems"][0]
    st = drv.status()
    assert st["in_flight"] == [] and st["locks"]["branches"] == {} and st["locks"]["worktrees"] == {}


@pytest.mark.parametrize("field,value", [
    ("expected_output", "   "), ("files", []), ("files", ["../outside.py"]), ("files", ["/abs/path.py"]),
    ("branch", "bad branch name"), ("branch", "-rf"), ("branch", "a..b"),
    ("instructions", "Do what we discussed, as discussed above."),
    ("instructions", "short"),
])
def test_brief_with_empty_unsafe_or_context_dependent_field_is_refused(drv: Driver, field, value):
    t = lead(drv)
    with pytest.raises(Refused) as e:
        drv.spawn(t, brief(**{field: value}))
    assert e.value.reason == "brief_invalid" and refusals(drv, "spawn")


def test_brief_spends_must_be_well_formed():
    with pytest.raises(ValueError):
        parse_brief(brief(allowed_spends=[{"kind": "compute", "resource": "local:cpu", "unit": "cpu_seconds",
                                           "limit": -1}]))
    with pytest.raises(ValueError):
        parse_brief(brief(allowed_spends=[{"kind": "network", "resource": "x", "unit": "calls", "limit": 1}]))
    with pytest.raises(ValueError):  # unknown extra field: forbidden (the brief is a closed schema)
        parse_brief(brief(notes="extra"))
    dup = {"kind": "compute", "resource": "local:cpu", "unit": "cpu_seconds", "limit": 1}
    with pytest.raises(ValueError):  # the same grant twice would double the budget
        parse_brief(brief(allowed_spends=[dup, dup]))
    assert isinstance(parse_brief(brief(allowed_spends=[])), HelperBrief)  # explicit "no spends at all" is fine


# ---------------------------------------------------------------- caps
def test_in_flight_cap(drv: Driver):
    t = lead(drv)
    h1 = drv.spawn(t, brief(brief_id="b1", branch="h/1", worktree="wt/1"))
    drv.spawn(t, brief(brief_id="b2", branch="h/2", worktree="wt/2"))
    with pytest.raises(Refused) as e:
        drv.spawn(t, brief(brief_id="b3", branch="h/3", worktree="wt/3"))
    assert e.value.reason == "in_flight_cap"
    assert refusals(drv, "spawn")[0]["detail"] == {"brief_id": "b3", "in_flight": 2, "cap": 2}
    assert "h/3" not in drv.status()["locks"]["branches"]
    drv.complete(h1.token, output="done")
    drv.spawn(t, brief(brief_id="b3", branch="h/3", worktree="wt/3"))  # a slot was freed


def test_total_cap_per_lead(tmp_path: Path):
    d = Driver(tmp_path / "o", max_in_flight=10, max_total_per_lead=2)
    t = d.register_lead("lead-a", allowed_spends=[])
    for i in range(2):
        d.complete(d.spawn(t, brief(brief_id=f"b{i}", branch=f"h/{i}", worktree=f"wt/{i}")).token, output="x")
    with pytest.raises(Refused) as e:
        d.spawn(t, brief(brief_id="b9", branch="h/9", worktree="wt/9"))
    assert e.value.reason == "total_cap"


def test_default_caps_come_from_the_one_config_place(tmp_path: Path):
    d = Driver(tmp_path / "o")
    assert d.max_in_flight == params.MAX_IN_FLIGHT and d.max_total_per_lead == params.MAX_TOTAL_PER_LEAD
    src = Path(params.__file__).read_text(encoding="utf-8")
    assert src.count("# UNCALIBRATED (owner decision, GOAL §13.8)") >= 4


def test_duplicate_lead_name_refused(drv: Driver):
    lead(drv, "lead-a")
    with pytest.raises(Refused) as e:
        lead(drv, "lead-a")
    assert e.value.reason == "duplicate_session"


# ---------------------------------------------------------------- persistence and integrity
def test_state_survives_a_new_driver_process(tmp_path: Path):
    d1 = Driver(tmp_path / "o", max_in_flight=1, max_total_per_lead=4)
    t = d1.register_lead("lead-a", allowed_spends=[])
    h = d1.spawn(t, brief())
    d1.spend(h.token, "compute", "local:cpu", "cpu_seconds", 599)
    d2 = Driver(tmp_path / "o", max_in_flight=1, max_total_per_lead=4)  # e.g. another CLI invocation
    with pytest.raises(Refused) as e:
        d2.spend(h.token, "compute", "local:cpu", "cpu_seconds", 2)
    assert e.value.reason == "spend_over_budget"
    with pytest.raises(Refused) as e2:
        d2.spawn(t, brief(brief_id="b2", branch="h/2", worktree="wt/2"))
    assert e2.value.reason == "in_flight_cap"


def test_tampered_ledger_is_an_integrity_error(drv: Driver):
    t = lead(drv)
    h = drv.spawn(t, brief())
    drv.spend(h.token, "compute", "local:cpu", "cpu_seconds", 500)
    p = drv.ledger.path
    lines = p.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[-1])
    row["data"]["amount"] = 1.0  # rewrite history to free up budget
    lines[-1] = json.dumps(row)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    with pytest.raises(IntegrityError):
        drv.spend(h.token, "compute", "local:cpu", "cpu_seconds", 200)


def test_deleted_ledger_line_is_an_integrity_error(drv: Driver):
    t = lead(drv)
    h = drv.spawn(t, brief())
    drv.spend(h.token, "compute", "local:cpu", "cpu_seconds", 1)
    p = drv.ledger.path
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8", newline="")
    with pytest.raises(IntegrityError):
        drv.status()


def test_lock_file_without_ledger_record_still_blocks(drv: Driver):
    """A lock written by a session outside this driver (another tool, a human) is honoured: fail closed."""
    t = lead(drv)
    drv.locks.write_foreign("branch", "helper/fem-1", holder="human:owner")
    with pytest.raises(Refused) as e:
        drv.spawn(t, brief())
    assert e.value.reason == "branch_held" and refusals(drv, "spawn")[0]["detail"]["holder"] == "human:owner"


def test_force_release_is_ledgered(drv: Driver):
    t = lead(drv)
    drv.spawn(t, brief())
    drv.force_release("helper/fem-1", by="human:owner", reason="helper crashed")
    assert "helper/fem-1" not in drv.status()["locks"]["branches"]
    assert drv.ledger.entries()[-1]["kind"] == "force_released"


def test_only_the_parent_lead_aborts_a_helper(drv: Driver):
    ta, tb = lead(drv, "lead-a"), lead(drv, "lead-b")
    h = drv.spawn(ta, brief())
    with pytest.raises(Refused) as e:
        drv.abort(tb, h.session, reason="hostile takeover")
    assert e.value.reason == "not_parent" and refusals(drv, "abort")[0]["requester"] == "lead-b"
    assert drv.status()["in_flight"] == [h.session]
    drv.abort(ta, h.session, reason="wrong approach")
    st = drv.status()
    assert st["in_flight"] == [] and st["locks"]["branches"] == {}
