"""Red team of the Experience API: each test tries to pull truth, verdict detail, a certificate or a human approval
through it (GOAL 18.5, invariants 15, 18, 19, 21). Each attack is run for real; the matching protection has a
re-introduction proof in agent_worklogs/stage-b/files."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _court_world import ARMS, Case
from _experience_world import World
from custom_harness import cli as root_cli
from custom_harness.court import display as D
from custom_harness.court import minted as C
from custom_harness.court.rehearsal import RehearsalRefused
from custom_harness.experience import Experience, Refused
from custom_harness.experience import api as API
from custom_harness.experience import schemas as M
from custom_harness.human import guard
from custom_harness.kernel import Kernel
from custom_harness.kerneld.client import KernelDUnavailable, RunHandle
from custom_harness.kerneld.releases import released
from custom_harness.lab import promote as P
from custom_harness.scorecard import facts as F
from custom_harness.studio import cli as studio_cli
from custom_harness.studio.graph import research_graph
from test_sealed_project import Project


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "w")
    yield world
    world.close()


@pytest.fixture
def lost(tmp_path):
    c = Case(tmp_path / "c", method=(ARMS / "majority.py").read_bytes() + b"\n# no effect\n")
    c.run()
    yield c
    c.close()


@pytest.fixture
def won(tmp_path):
    c = Case(tmp_path / "c")
    c.run()
    yield c
    c.close()


def cert_of(c: Case):
    return Kernel(c.store).certificate(C.latest(c.store, c.case_id, "outcome")["cert_hash"])


def everything(x: Experience, case_id: str) -> str:
    """All a caller can read of a case through the API."""
    return json.dumps([x.case(case_id).model_dump(mode="json"), x.technical(case_id).model_dump(mode="json"),
                       x.resolve(f"chh://case/{case_id}/test_rows").model_dump(mode="json"),
                       x.record().model_dump(mode="json"), x.cases().model_dump(mode="json")], default=str)


# ---------------------------------------------------------------- truth
def test_no_number_or_reason_of_a_truth_dependent_check_comes_through_any_verb(lost):
    cert = cert_of(lost)
    x = Experience(lost.store)
    text = everything(x, lost.case_id)
    withheld = [str(r) for k in D.TRUTH_DEPENDENT for r in cert.checks[k].reasons]
    assert withheld and not any(r in text for r in withheld)
    res = cert.resolution["targets"][lost.target]
    numbers = [str(res[k]) for k in ("estimate", "lo", "hi") if res.get(k) is not None]
    assert numbers and not any(n in text for n in numbers)


def test_the_api_holds_a_host_store_whatever_it_is_given(won):
    from custom_harness.kerneld.hoststore import HostStore, PrivateDataRefused
    x = Experience(won.store)
    assert isinstance(x.store, HostStore)
    private = won.store.ledger_rows("register_dataset")
    assert private
    ref = next(f["blob"] for r in private for f in json.loads(r[2])["files"].values() if f.get("private"))
    from custom_harness.kernel.vault import KEYED
    address = (Path(won.store.root) / "private" / "index" / ref.removeprefix(KEYED)).read_text(encoding="ascii").strip()
    with pytest.raises(PrivateDataRefused):
        x.store.blobs.get(address)


def test_rehearsing_on_the_sealed_file_is_refused(w):
    w.seal()
    w.session()
    pid = w.freeze()
    with pytest.raises(Refused) as e:
        w.x.rehearse(M.RehearseIn(promotion_id=pid, rows=100, data_file=str(w.data), label_column="outcome"))
    assert e.value.code == "RehearsalRefused" and "sealed" in str(e.value)


def test_rehearsing_on_a_copy_that_holds_sealed_rows_is_refused(w):
    """The file differs from the sealed one, so a hash cannot catch it: its rows do."""
    w.seal()
    w.session()
    pid = w.freeze()
    lines = w.data.read_text(encoding="utf-8").splitlines()
    copy = w.root / "subset.csv"
    copy.write_text("\n".join(lines[:900]) + "\n", encoding="utf-8")  # 900 of 1600 rows must meet a sealed part
    with pytest.raises(Refused) as e:
        w.x.rehearse(M.RehearseIn(promotion_id=pid, rows=100, data_file=str(copy), label_column="outcome"))
    assert "holds rows sealed" in str(e.value)
    swapped = w.root / "swapped.csv"  # the same columns in another order
    head, *rows = [ln.split(",") for ln in lines[:900]]
    order = [1, 0, 2, 3]
    swapped.write_text("\n".join(",".join(r[i] for i in order) for r in [head, *rows]) + "\n", encoding="utf-8")
    with pytest.raises(Refused, match="holds rows sealed"):
        w.x.rehearse(M.RehearseIn(promotion_id=pid, rows=100, data_file=str(swapped), label_column="outcome"))


def test_the_rows_check_passes_a_file_with_other_columns_and_empty_files(w):
    from custom_harness.court.rehearsal import sealed_rows_problem
    w.seal()
    assert sealed_rows_problem(w.store, b"a,b\n1,2\n", "b") is None
    assert sealed_rows_problem(w.store, b"", "b") is None
    assert sealed_rows_problem(w.store, w.data.read_bytes(), "outcome")
    w.store.append_record("lab_project", {"event": "session", "project": "cohort-1"})
    assert sealed_rows_problem(w.store, b"a,b\n1,2\n", "b") is None


def test_the_direct_rehearsal_call_is_protected_too(w):
    from custom_harness.court.rehearsal import rehearse
    w.seal()
    w.session()
    pid = w.freeze()
    with pytest.raises(RehearsalRefused):
        rehearse(w.store, pid, w.data, label_column="outcome", rows=100, sandbox_factory=None)


# ---------------------------------------------------------------- verdict detail and counted looks
def test_reading_lists_records_no_release_and_shows_no_verdict(won):
    x = Experience(won.store)
    x._own("case", won.case_id, x.whoami().researcher)
    before = len(released(won.store))
    rec, listed, wait = x.record(), x.cases(), x.waiting()
    assert len(released(won.store)) == before
    assert rec.outcomes == () and rec.verdicts_unopened.value == 1
    assert "CERTIFIED" not in json.dumps([rec.model_dump(mode="json"), listed.model_dump(mode="json"),
                                          wait.model_dump(mode="json")])
    assert listed.cases[0].looks_remaining.value == 0


def test_opening_a_case_is_a_counted_look_and_shows_looks_remaining(won):
    x = Experience(won.store)
    before = len(released(won.store, won.target))
    v = x.case(won.case_id)
    assert len(released(won.store, won.target)) == before + 1 and v.looks_remaining.value == 0
    x.case(won.case_id)
    assert len(released(won.store, won.target)) == before + 1


def test_a_reviewer_is_a_new_viewer_and_counts(won, tmp_path):
    x = Experience(won.store)
    x.case(won.case_id)
    before = len(released(won.store, won.target))
    x.share(M.ShareIn(case_id=won.case_id, reviewer="Dr Lin", out_file=str(tmp_path / "f.json")))
    assert len(released(won.store, won.target)) == before + 1
    assert "autopsy" not in (tmp_path / "f.json").read_text(encoding="utf-8")


def test_a_case_file_may_not_be_written_into_the_lab(won):
    from custom_harness.lab.walls import lab_root
    x = Experience(won.store)
    with pytest.raises(Refused) as e:
        x.share(M.ShareIn(case_id=won.case_id, reviewer="Dr Lin", out_file=str(lab_root() / "s" / "f.json")))
    assert e.value.code == "InsideLab"


def test_the_second_tests_outcome_is_released_on_its_own_target_when_the_first_case_shows_it(tmp_path):
    """The badges of the first case carry the second case's outcome. Before the fix they reached a viewer with no
    release on the second target."""
    p = Project(tmp_path / "p")
    try:
        p.session()
        body = C.register_sealed(p.store, p.freeze(), p.project, anchor_file=p.anchor(), revalidate=True)
        p.run(body)
        second = body["revalidation"]
        D.view(p.store, body["case_id"], "owner")
        assert not released(p.store, second["target"])
        p.run(second)
        v = D.view(p.store, body["case_id"], "owner")
        assert "RE-VALIDATED" in v["badge"]
        rows = [r for r in released(p.store, second["target"]) if r["note"] == "court:view_badges"]
        assert len(rows) == 1 and rows[0]["alphabet"] == "count"
        D.view(p.store, body["case_id"], "owner")
        assert len([r for r in released(p.store, second["target"]) if r["note"] == "court:view_badges"]) == 1
        assert D.seen(p.store, body["case_id"], "owner")["badge"] == v["badge"]
    finally:
        p.close()


def test_a_could_not_run_after_a_read_is_a_release(tmp_path, monkeypatch):
    c = Case(tmp_path / "c")
    try:
        real, calls = RunHandle.held_out_score, []

        def once(self, *a, **k):
            if calls:
                raise KernelDUnavailable("detail that must stay")
            calls.append(1)
            return real(self, *a, **k)

        with monkeypatch.context() as m:
            m.setattr(RunHandle, "held_out_score", once)
            c.run()
        x = Experience(c.store)
        v = x.case(c.case_id)
        assert v.verdict == "COULD NOT RUN" and v.next_step is None
        assert "detail that must stay" not in v.model_dump_json()
        x.case(c.case_id)
        rows = [r for r in released(c.store, c.target) if r["note"] == "court:view_failure"]
        assert len(rows) == 1 and rows[0]["alphabet"] == "verdict_bit"
    finally:
        c.close()


def test_an_unopened_verdict_stays_out_of_every_other_surface(won, capsys):
    """`chh explain`, `chh bundle`, `chh graph`, `chh predictions` and the scorecard facts were second paths."""
    cert_hash = C.latest(won.store, won.case_id, "outcome")["cert_hash"]
    with pytest.raises(SystemExit, match="retired"):
        studio_cli.cmd_explain(SimpleNamespace(cert=cert_hash[:20], docker=False, dev_sandbox=False, no_verify=True,
                                               json=True), won.store)
    with pytest.raises(SystemExit, match="retired"):
        root_cli.cmd_bundle(SimpleNamespace(cert_hash=cert_hash, out=str(won.root / "b.zip")), won.store)
    assert not (won.root / "b.zip").exists()
    assert root_cli.cmd_predictions(SimpleNamespace(), won.store) == 0
    assert json.loads(capsys.readouterr().out)["predictions"] == 0
    graph = research_graph(won.store)
    node = graph["nodes"][f"cert:{cert_hash}"] if isinstance(graph["nodes"], dict) else next(
        n for n in graph["nodes"] if n["id"] == f"cert:{cert_hash}")
    assert node["label_recorded"] is None and "label" not in node and node["withheld"]
    claim = f"claim:{won.claim_hash}"
    cnode = graph["nodes"][claim] if isinstance(graph["nodes"], dict) else next(
        n for n in graph["nodes"] if n["id"] == claim)
    assert cnode["prediction"] is None
    assert F._certificates(won.store, Kernel(won.store)) == []
    assert D.exposure(won.store, won.claim_hash) == "none"


def test_an_opened_verdict_shows_its_label_elsewhere_and_a_retired_one_shows_all(won, capsys):
    cert_hash = C.latest(won.store, won.case_id, "outcome")["cert_hash"]
    D.view(won.store, won.case_id, "owner")
    assert D.exposure(won.store, won.claim_hash) == "label"
    assert len(F._certificates(won.store, Kernel(won.store))) == 1
    with pytest.raises(SystemExit, match="retired"):
        root_cli.cmd_bundle(SimpleNamespace(cert_hash=cert_hash, out=str(won.root / "b.zip")), won.store)
    D.retire(won.store, won.target, "owner")
    assert D.exposure(won.store, won.claim_hash) == "open"
    assert root_cli.cmd_bundle(SimpleNamespace(cert_hash=cert_hash, out=str(won.root / "b.zip")), won.store) == 0
    assert studio_cli.cmd_explain(SimpleNamespace(cert=cert_hash, docker=False, dev_sandbox=False, no_verify=True,
                                                  json=True), won.store) == 0
    capsys.readouterr()


def test_a_certificate_with_no_court_case_is_not_gated(won):
    assert D.require_autopsy(won.store, "f" * 64, "owner") is None
    D.retire(won.store, won.target, "owner")
    D.require_autopsy(won.store, "", "owner")


def test_a_living_paper_needs_a_retired_target(won):
    x = Experience(won.store)
    with pytest.raises(Refused) as e:
        x.paper(M.PaperIn(case_id=won.case_id, title="T", body="\\chhnum{a}\n", refs={"a": "/label"}))
    assert e.value.code == "DisplayRefused" and "retired" in str(e.value)


def test_a_paper_that_does_not_regenerate_is_refused(won, monkeypatch):
    D.retire(won.store, won.target, "owner")
    monkeypatch.setattr(API, "audit_court_paper", lambda *a, **k: ["differs"])
    with pytest.raises(Refused) as e:
        Experience(won.store).paper(M.PaperIn(case_id=won.case_id, title="T", body="\\chhnum{a}\n",
                                              refs={"a": "/claim/effect/min_effect"}))
    assert e.value.code == "PaperDiffers"


# ---------------------------------------------------------------- a certificate
def test_a_rehearsal_certificate_never_reaches_the_real_ledger(w):
    w.seal()
    w.session()
    pid = w.freeze()
    out = w.x.rehearse(M.RehearseIn(promotion_id=pid, rows=200))
    assert out.would_certify is True
    assert w.store.ledger_rows("certificate") == [] and w.x.cases().cases == ()


def test_lab_output_cannot_write_a_verdict(w):
    """An agent writes a verdict file and a forged idea; the case stays undecided."""
    from custom_harness.lab import session as S
    w.seal()
    w.session()
    ws = S.workspace_of("s-exp")
    (ws / ".court").mkdir()
    (ws / ".court" / "verdict-x.json").write_text('{"label": "CERTIFIED-VALIDATED"}', encoding="utf-8")
    pid = w.freeze()
    done = w.x.promote(M.PromoteIn(promotion_id=pid, route="sealed", project="cohort-1"))
    assert w.x.case(done.case_id).verdict is None and w.store.ledger_rows("certificate") == []


def test_no_verb_can_grant():
    public = {n for n, _ in inspect.getmembers(Experience, inspect.isfunction) if not n.startswith("_")}
    assert public == {"whoami", "set_researcher", "waiting", "approve", "seal", "start", "lab", "explore", "steer",
                      "stop", "freeze", "rehearse", "rehearsal_details", "promote_preview", "promote", "mint",
                      "run_case", "case", "technical", "cases", "share", "paper", "record", "resolve"}


# ---------------------------------------------------------------- a human approval
def ledger_size(store) -> int:
    return len(store.ledger_rows())


def test_approve_takes_no_decision_and_writes_nothing(w):
    w.seal()
    target = w.x.waiting()
    item = next(i for i in target.items if i.human_only and "approve-role" in i.command)
    params = list(inspect.signature(Experience.approve).parameters)
    assert params == ["self", "item_id"]
    before, records = ledger_size(w.store), len(w.store.records("experience_owner"))
    route = w.x.approve(item.id)
    with pytest.raises(TypeError):
        w.x.approve(item.id, decision="approve")  # type: ignore[call-arg]
    assert route.performed is False and "Only a person" in route.note
    assert ledger_size(w.store) == before and len(w.store.records("experience_owner")) == records
    assert w.store.ledger_rows("role_approval") == []


@pytest.mark.parametrize("name", ["claude", "gpt-5", "agent smith", "stage-bot", "b-research", "anthropic/claude", ""])
def test_an_agents_name_cannot_become_the_researcher(w, name):
    with pytest.raises(Refused) as e:
        w.x.set_researcher(name)
    assert e.value.code == "HumanActionRefused"
    assert not w.x.whoami().chosen


def test_an_agent_named_os_user_cannot_take_a_researchers_step(w, monkeypatch):
    monkeypatch.setattr(API.getpass, "getuser", lambda: "claude")
    with pytest.raises(Refused) as e:
        w.seal()
    assert e.value.code == "HumanActionRefused"
    assert w.store.records("lab_project") == []


def test_a_researchers_step_is_refused_from_inside_a_run(w, monkeypatch):
    monkeypatch.setattr(guard, "inside_run_step", lambda *_: True)
    with pytest.raises(Refused, match="inside a run"):
        w.seal()


def test_retiring_or_releasing_as_an_agent_is_refused(won):
    with pytest.raises(D.DisplayRefused):
        D.retire(won.store, won.target, "claude")
    assert not D.retired(won.store, won.target)
    with pytest.raises(P.PromoteError, match="names an agent"):
        P.release_verdict(won.store, "p", "claude")


def test_a_queue_item_is_routed_untouched(w):
    w.seal()
    items = w.x.waiting().items
    assert any(i.human_only for i in items) and all(i.command for i in items)
    optional = [i for i in items if i.optional]
    assert optional and all(i.title.startswith("Optional") for i in optional)
