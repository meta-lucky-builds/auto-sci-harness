"""Version 1 features of GOAL 18.6. Where a real model would be needed, a scripted stand-in plays it. Nothing here calls
a model or real Docker."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from _court_world import ARMS, Case, part
from _experience_world import REQUEST, World
from custom_harness import settings
from custom_harness.court import display as D
from custom_harness.court import levels as L
from custom_harness.court import minted as C
from custom_harness.court import prior as Pr
from custom_harness.evolve.selection import gate
from custom_harness.experience import Experience, Refused
from custom_harness.experience import schemas as M
from custom_harness.experience import words as W
from custom_harness.kernel import Kernel
from custom_harness.kerneld.mint import mintable_families, minted_files
from custom_harness.kerneld.seal import SealSpecError, future_files
from custom_harness.lab import defaults
from custom_harness.lab import session as S
from custom_harness.lab.promote import PromoteRequest
from custom_harness.research.suite import FAMILIES
from custom_harness.store import EvidenceStore

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"


@dataclass
class Scripted:
    """Plays a pinned model. It remembers what it was shown."""

    value: float
    model: str = "scripted-model-1"
    seen: str = ""

    def predict(self, claim_text: str) -> float:
        self.seen = claim_text
        return self.value


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "w")
    yield world
    world.close()


def with_prior(tmp_path, value):
    store = EvidenceStore(tmp_path / "c" / "store")
    Pr.measure(store, "p-fixture", Scripted(value), title="t", sentence="s", metric="accuracy", min_effect=0.1)
    store.close()
    return Case(tmp_path / "c")


# ---------------------------------------------------------------- the measured no-data prior
def test_a_high_prior_can_only_make_the_claim_harder(tmp_path):
    c = with_prior(tmp_path, 0.99)
    try:
        out = c.run()
        assert not str(out["label"]).startswith("CERTIFIED")
        assert D.view(c.store, c.case_id, "owner")["reason_class"] == "OUTCOME_TOO_PREDICTABLE"
    finally:
        c.close()


def test_a_low_prior_changes_nothing(tmp_path):
    c = with_prior(tmp_path, 0.01)
    try:
        assert str(c.run()["label"]).startswith("CERTIFIED")
        claim = next(e.body["claim"] for e in Kernel(c.store).ledger.entries("prereg"))
        assert claim["risk"]["prior_probability"] == 0.01
        assert any("measured no-data prior" in a and "UNCALIBRATED" in a for a in claim["assumptions"])
    finally:
        c.close()


def test_the_source_sees_only_the_words_and_a_second_try_is_refused(w):
    src = Scripted(0.3)
    Pr.measure(w.store, "p1", src, title="A title", sentence="A sentence", metric="accuracy", min_effect=0.1)
    assert "A sentence" in src.seen and "0.1" in src.seen
    with pytest.raises(Pr.PriorRefused, match="one per promotion"):
        Pr.measure(w.store, "p1", Scripted(0.01), title="t", sentence="s", metric="accuracy", min_effect=0.1)


@pytest.mark.parametrize("model", ["latest", "claude-latest", "Has Spaces", ""])
def test_a_prior_needs_a_pinned_model(w, model):
    with pytest.raises(Pr.PriorRefused, match="pinned"):
        Pr.measure(w.store, "p1", Scripted(0.3, model=model), title="t", sentence="s", metric="accuracy",
                   min_effect=0.1)


@pytest.mark.parametrize("value", [0.0, 1.0, -0.2, 1.5, 1, "0.5", float("nan")])
def test_a_prior_must_be_a_real_chance(w, value):
    with pytest.raises(Pr.PriorRefused, match="strictly between"):
        Pr.measure(w.store, "p1", Scripted(value), title="t", sentence="s", metric="accuracy", min_effect=0.1)


def test_no_model_is_called_unless_a_source_is_given(w):
    w.seal()
    w.session()
    pid = w.freeze()
    with pytest.raises(Refused) as e:
        w.x.measure_prior(pid)
    assert e.value.code == "NoPriorSource"
    x = Experience(w.store, docker=w.x.docker, prior_source=Scripted(0.2))
    assert x.measure_prior(pid).value == 0.2
    preview = x.promote_preview(M.PromoteIn(promotion_id=pid, route="sealed", project="cohort-1"))
    assert preview.no_data_prior.value == 0.2 and x.resolve(preview.no_data_prior.link).value == 0.2


# ---------------------------------------------------------------- INCONCLUSIVE
def failed_k6(hi, delta=0.1):
    checks = {k: SimpleNamespace(status="fail" if k == "K6" else "pass", reasons=()) for k in D.CHECK_IDS}
    return SimpleNamespace(checks=checks, failed=lambda: ["K6"], claim={"effect": {"min_effect": delta}},
                           resolution={"targets": {"t": {"lo": -0.2, "hi": hi}}})


def test_a_failed_effect_is_inconclusive_only_when_the_claimed_size_is_still_inside_the_interval():
    assert D.split_checks(failed_k6(0.15))[1] == D.TOO_CLOSE
    assert D.split_checks(failed_k6(0.1))[1] == D.TOO_CLOSE
    assert D.split_checks(failed_k6(0.09))[1] == "EFFECT_NOT_SHOWN"
    assert D.split_checks(failed_k6(None))[1] == "EFFECT_NOT_SHOWN"


def test_inconclusive_is_never_a_pass_and_never_shows_a_number():
    assert W.verdict_of("REJECTED", "DECIDED", D.TOO_CLOSE) == "INCONCLUSIVE"
    assert W.verdict_of("REJECTED", "DECIDED", "EFFECT_NOT_SHOWN") == "NOT CERTIFIED"
    assert W.verdict_of("CERTIFIED-VALIDATED", "DECIDED", D.TOO_CLOSE) == "CERTIFIED"
    assert W.verdict_of("REJECTED", "COULD NOT RUN", D.TOO_CLOSE) == "COULD NOT RUN"
    sentence = W.verdict_sentence("INCONCLUSIVE", [], "s", D.TOO_CLOSE, retrospective=False)
    assert sentence.startswith("Inconclusive.") and not any(ch.isdigit() for ch in sentence)


def test_a_real_case_near_the_line_reads_inconclusive_and_records_a_closed_class(tmp_path):
    """A real weak method against the same baseline, with a claimed size the interval still holds."""
    c = Case(tmp_path / "c", method=(ARMS / "majority.py").read_bytes() + b"\n# no effect\n",
             claim=part(min_effect=0.02))
    try:
        c.run()
        view = D.view(c.store, c.case_id, "owner")
        cert = Kernel(c.store).certificate(C.latest(c.store, c.case_id, "outcome")["cert_hash"])
        hi = cert.resolution["targets"][c.target]["hi"]
        want = D.TOO_CLOSE if hi >= 0.02 else "EFFECT_NOT_SHOWN"
        assert view["reason_class"] == want
        assert str(hi) not in json.dumps({k: v for k, v in view.items() if k != "claim"})
    finally:
        c.close()


def test_the_list_links_resolve(w):
    w.seal()
    w.session()
    for view, link in ((w.x.sessions(), "total"), (w.x.trust(), "steps_left"), (w.x.frozen(), "total")):
        n = getattr(view, link)
        assert w.x.resolve(n.link).value == n.value


@pytest.mark.parametrize(("kind", "payload", "actor", "want"), [
    ("idea", {"op": "abandon", "idea_id": "a", "title": "A", "reason": "r"}, "agent", "abandon: A (r)"),
    ("idea", {"op": "propose", "idea_id": "a"}, "agent", "propose: a"),
    ("run", {"run_id": "r1", "command": "go", "metrics": [{"name": "acc", "value": 0.5, "split": "dev"}]}, "agent",
     "go  acc 0.5 (dev)"),
    ("run", {"run_id": "r1", "message": "done"}, "system", "done"),
    ("action", {"tool": "read", "target": "f"}, "agent", "read f"),
    ("human", {"op": "steer", "note": "n"}, "human", "n"),
    ("human", {"op": "stop"}, "human", "stop"),
    ("web", {"op": "connection", "host": "h"}, "agent", "connection: h"),
    ("web", {"op": "gap"}, "system", "gap"),
    ("session", {"op": "start"}, "system", "start")])
def test_a_story_event_reads_as_one_line(kind, payload, actor, want):
    line = W.lab_line({"kind": kind, "payload": payload, "actor": {"type": actor}, "at": 0.0})
    assert line["text"] == want and line["who"] == {"human": "you", "agent": "agent"}.get(actor, "system")


def test_file_diffs_and_costs_stay_off_the_story_lines():
    assert W.lab_line({"kind": "file", "payload": {}, "actor": {}, "at": 0.0}) is None


# ---------------------------------------------------------------- the time window
def request_with(tmp_path, window):
    text = REQUEST.replace("time_window: {start: 2024-01-01, end: 2025-12-31}\n", window)
    path = tmp_path / "promote2.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_sealed_route_refuses_a_claim_with_no_time_window(w, tmp_path):
    w.seal()
    w.session()
    pid = w.x.freeze(M.FreezeIn(session_id="s-exp", request_file=str(request_with(tmp_path, "")))).promotion_id
    with pytest.raises(Refused, match="time window"):
        w.x.promote(M.PromoteIn(promotion_id=pid, route="sealed", project="cohort-1"))


def test_the_window_is_in_the_claim_and_the_scope_sentence(w):
    w.seal()
    w.session()
    pid = w.freeze()
    case_id = w.x.promote(M.PromoteIn(promotion_id=pid, route="sealed", project="cohort-1")).case_id
    claim = next(e.body["claim"] for e in Kernel(w.store).ledger.entries("prereg"))
    assert any("2024-01-01 to 2025-12-31" in a for a in claim["assumptions"])
    assert "from 2024-01-01 to 2025-12-31" in w.x.case(case_id).scope


def test_a_backwards_window_is_refused():
    with pytest.raises(ValueError, match="starts before"):
        PromoteRequest.model_validate({
            "format": "chh-lab-promote/1", "title": "abc", "sentence": "a sentence of ten", "method": "m",
            "baseline": "b", "lock": "l", "claim": part().model_dump(mode="json"), "budget": {"timeout_s": 10},
            "time_window": {"start": "2025-01-02", "end": "2025-01-01"}})


# ---------------------------------------------------------------- future-data projects
def future_world(w, tmp_path):
    today = datetime.now(UTC).date()
    window = f"time_window: {{start: {today}, end: {today + timedelta(days=90)}}}\n"
    w.seal()
    w.session()
    pid = w.x.freeze(M.FreezeIn(session_id="s-exp", request_file=str(request_with(tmp_path, window)))).promotion_id
    return pid, w.x.promote(M.PromoteIn(promotion_id=pid, route="future", project="cohort-1", n_test=500)).case_id


def later_rows(tmp_path, n=500, *, seed=99):
    from test_sealed_project import rings_csv
    rows = rings_csv(n, seed).decode().splitlines()
    path = tmp_path / "later.csv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_a_future_case_waits_for_its_data_then_runs_and_counts_as_fresh(w, tmp_path):
    _, case_id = future_world(w, tmp_path)
    assert D.progress(w.store, case_id) == "needs_data"
    assert any("future-data" in i.command for i in w.x.waiting().items)
    with pytest.raises(C.CourtRefused, match="no data yet"):
        C.run(w.store, case_id, w.factory)
    with pytest.raises(C.CourtRefused, match="nothing is minted"):
        C.mint(w.store, case_id, "x" * 32)
    w.x.future_data(M.FutureDataIn(case_id=case_id, data_file=str(later_rows(tmp_path)), label_column="outcome"))
    assert D.progress(w.store, case_id) == "needs_role_approval"
    with pytest.raises(Refused, match="already has its data"):
        w.x.future_data(M.FutureDataIn(case_id=case_id, data_file=str(later_rows(tmp_path)), label_column="outcome"))
    target = C.case(w.store, case_id)["target"]
    w.approve_role(target)
    w.x.run_case(case_id)
    v = D.view(w.store, case_id, "owner")
    assert v["data_kind"] == ["future"] and "VALIDATED" in v["held"], v


def test_a_future_window_must_not_start_before_the_freeze(w, tmp_path):
    w.seal()
    w.session()
    pid = w.freeze()  # 2024-01-01: long past
    with pytest.raises(Refused, match="before the freeze"):
        w.x.promote(M.PromoteIn(promotion_id=pid, route="future", project="cohort-1", n_test=500))


def test_a_future_case_refuses_a_second_test_and_a_file_a_lab_can_read(w, tmp_path):
    from custom_harness.lab.walls import lab_root
    _, case_id = future_world(w, tmp_path)
    with pytest.raises(C.CourtRefused, match="no second test"):
        C.declare_revalidation(w.store, case_id, "r2")
    with pytest.raises(C.CourtRefused, match="lab root"):
        C.arrive(w.store, case_id, lab_root() / "x.csv", label_column="outcome")
    with pytest.raises(C.CourtRefused, match="not a future-data case"):
        c = Case(tmp_path / "other")
        try:
            C.arrive(c.store, c.case_id, tmp_path / "x.csv", label_column="y")
        finally:
            c.close()


@pytest.mark.parametrize(("name", "raw", "column", "n"), [
    ("not text", b"\xff\xfe", "y", 1), ("no label column", b"a,b\n1,2\n", "y", 1), ("empty", b"", "y", 1),
    ("other columns", b"z,b,y\n1,2,3\n", "y", 1), ("too few rows", b"a,b,y\n1,2,3\n", "y", 2),
    ("short row", b"a,b,y\n1,2\n", "y", 1), ("a Lab row", b"a,b,y\n8,9,1\n", "y", 1)])
def test_kerneld_refuses_a_future_file_that_is_not_what_was_promised(name, raw, column, n):
    with pytest.raises(SealSpecError):
        future_files(raw, column, n, b"a,b,label\n8,9,0\n")


def test_a_good_future_file_keeps_its_labels_apart():
    got = future_files(b"a,b,y\n1,2,x\n", "y", 1, b"a,b,label\n8,9,0\n")
    assert b"x" not in got.test_x and got.test_y == b"label\nx\n" and got.n == 1


def test_kerneld_refuses_the_wrong_protocol(w, tmp_path):
    from custom_harness.kerneld.client import KernelD, KernelDError
    _, case_id = future_world(w, tmp_path)
    c = C.case(w.store, case_id)
    with KernelD(w.store.root) as kd:
        run = kd.open_run("t")
        try:
            with pytest.raises(KernelDError) as e:
                run.ingest_future_heldout(c["claim_hash"], c["target"], file=later_rows(tmp_path), project="other",
                                          label_column="outcome", n_test=500, anchor_file=c["anchor_file"])
            assert e.value.code == "token_scope"
        finally:
            run.close()


# ---------------------------------------------------------------- parallel worlds
@pytest.mark.parametrize("family", ["parallel-gravity", "parallel-decay"])
def test_a_parallel_world_family_can_be_minted(family):
    assert family in mintable_families()
    files = minted_files(family, "a" * 64, "b" * 32, 200, 300)
    assert files.n_train == 200 and files.n_test == 300
    header = files.test_x.decode().splitlines()[0].split(",")
    assert FAMILIES[family].target not in header


def test_the_parallel_gravity_law_is_not_the_textbook_one():
    import math
    import random
    f, lab = random.Random(1), random.Random(2)
    rows = FAMILIES["parallel-gravity"].rows(f, lab, 4000)

    def right(power):
        pull = [math.log(float(r[0]) * float(r[1])) - power * math.log(float(r[2])) for r in rows]
        return sum(int(p > 0.2) == int(r[4]) for p, r in zip(pull, rows, strict=True)) / len(rows)

    assert right(2.5) > right(2.0) + 0.01


# ---------------------------------------------------------------- the same-budget baseline
GOOD = [0.2, 0.25, 0.3, 0.22, 0.28, 0.26]


def test_a_win_that_more_compute_alone_gives_is_rejected():
    assert gate(GOOD, GOOD, n_boot=500).accepted
    assert gate(GOOD, GOOD, n_boot=500, same_budget=GOOD).accepted
    tie = [0.01, -0.02, 0.0, 0.02, -0.01, 0.0]
    d = gate(GOOD, GOOD, n_boot=500, same_budget=tie)
    assert not d.accepted and "same budget" in d.reason


def test_a_same_budget_baseline_with_too_few_units_fails_closed():
    assert not gate(GOOD, GOOD, n_boot=500, same_budget=[0.2]).accepted


# ---------------------------------------------------------------- Lab defaults
def test_the_pack_is_optional_and_only_files(w):
    w.seal()
    w.session()
    ws = S.workspace_of("s-exp")
    assert sorted(p.name for p in (ws / ".lab" / "skills").iterdir()) == sorted(defaults.SKILLS)
    assert (ws / ".lab" / "dead_ends.md").is_file()
    w.x.start(M.StartIn(agent="opencode", session_id="s-bare", model="m", local_model_server=True, lab_defaults=False))
    assert not (S.workspace_of("s-bare") / ".lab" / "skills").exists()


def test_dead_ends_wait_to_be_asked_for_and_hold_no_verdict(w):
    w.seal()
    w.session()
    w.x.start(M.StartIn(agent="opencode", session_id="s-next", model="m", local_model_server=True))
    assert "No" in (S.workspace_of("s-next") / ".lab" / "dead_ends.md").read_text(encoding="utf-8")
    src = (SRC / "experience" / "api.py").read_text(encoding="utf-8")
    body = src[src.index("def _dead_idea_notes"):src.index("def _facts_session")]
    assert "_dead_ideas" in body and "record(" not in body
    assert defaults.dead_ends([{"title": "a", "reason": "b"}]).count("- a: b") == 1


def test_the_prompt_is_not_changed_by_the_pack():
    src = (SRC / "lab" / "session.py").read_text(encoding="utf-8")
    assert "defaults" not in src.replace("lab_default_model", "")


# ---------------------------------------------------------------- the second family
def test_a_replicator_can_be_set_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path))
    assert settings.load()["replicator"] == ""
    assert settings.save(replicator="ollama:some-model")["replicator"] == "ollama:some-model"
    from custom_harness.experience.doctor import OK, _second_family
    assert _second_family().state == OK


# ---------------------------------------------------------------- LLM judging and literature stay out of the Court
def imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(n, ast.ImportFrom):
            out |= {f"{n.module or ''}.{a.name}" for a in n.names}
        elif isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
    return out


@pytest.mark.parametrize("folder", ["court", "kerneld", "experience", "lab"])
def test_no_llm_judge_is_reachable_from_the_court_side(folder):
    banned = ("verify.judge", "research.tournament", "research.critic_answers", "evolve.critic", "research.hypotheses")
    for p in (SRC / folder).rglob("*.py"):
        assert not [i for i in imports(p) if any(b in i for b in banned)], p


def test_the_court_side_uses_only_the_citation_and_dated_parts_of_literature():
    allowed = ("literature.cite", "literature.snapshot", "literature.client")
    for folder in ("court", "kerneld", "experience", "audit", "kernel"):
        for p in (SRC / folder).rglob("*.py"):
            bad = [i for i in imports(p) if "literature" in i and not any(a in i for a in allowed)]
            assert not bad, (p, bad)


def test_the_researcher_never_sees_supported_on_a_main_view():
    assert "SUPPORTED" not in json.dumps(W.VERDICTS) and W.jargon_problems({"note": "SUPPORTED"})


def test_every_badge_text_is_honest_about_its_limit():
    assert "does not mean true" in W.badge_text("REPRODUCIBLE")["meaning"]
    assert "share mistakes" in W.badge_text("REPLICATED (a -> b, different family)")["meaning"]
    assert "errors may be shared" in W.badge_text("REPLICATED (a -> a, same family)")["meaning"]
    assert "finding" in W.badge_text("VALIDATED")["meaning"]
    assert "generator-scoped" in W.scope_sentence({"metric": "m", "min_effect": 1}, family="f", route="minted")
    assert L.BADGES  # the words read the Court's list, they do not decide it
