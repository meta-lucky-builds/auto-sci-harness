"""Second-model-family role exams (GOAL §16.8 last bullet, §10 invariant 5). FAKE backends only: no second family
is installed, so these tests check the code path (builders, grading, phi, admission, ledger), never a real model."""

import dataclasses
import inspect
import json
from pathlib import Path

import pytest

from custom_harness.gateway import BackendError, MockBackend
from custom_harness.gateway import role_exam as rx
from custom_harness.gateway.base import Backend, ChatResponse
from custom_harness.gateway.codex_cli import CodexCLIBackend
from custom_harness.records import AccessMode, Cost
from custom_harness.store import EvidenceStore

TASK = {"id": "wine-demo", "description": "Predict the wine class from chemistry.", "metric": "accuracy",
        "target_column": "label", "test_labels_file": "test_y.csv"}
PROFILE = {"n_rows": 120, "columns": [
    {"name": "alcohol", "type": "numeric", "role": "feature", "min": 11.0, "max": 14.8},
    {"name": "proline", "type": "numeric", "role": "feature", "min": 278.0, "max": 1680.0},
    {"name": "hue", "type": "numeric", "role": "feature", "min": 0.48, "max": 1.71},
    {"name": "row_id", "type": "numeric", "role": "id-like", "min": 0, "max": 119},
    {"name": "label", "type": "categorical", "role": "target"}],
    "target": {"name": "label", "task_type": "classification",
               "top_features": [{"name": "proline", "association": 0.6}, {"name": "alcohol", "association": 0.5}]},
    "flags": []}
SOURCE = {"dataset_role": "dev", "ref": "tasks/wine-demo/public/train.csv"}


class Fake(Backend):
    """A tiny backend with a DECLARED family (no model behind it): answers from a prompt -> text table."""
    mode = AccessMode.OPEN_WEIGHTS

    def __init__(self, family, answers, *, name="fake-local", provider="ollama", model="fake-4b"):
        self.family, self.answers = family, answers
        self.name, self.provider, self.model = name, provider, model
        self.requests = []

    def complete(self, req):
        self.requests.append(req)
        text = self.answers(req.messages[-1].content)
        return ChatResponse(text=text, cost=Cost(estimated=True), provenance=self.provenance(self.model))


def judge_answers(exam, *, miss=(), flip=()):
    """prompt -> JSON answer: correct except item ids in `miss` (wrong plan) ; `flip` = answer 'A' always."""
    table = {}
    for it in exam.items:
        right = it.expected["better"]
        wrong = "B" if right == "A" else "A"
        ans = "A" if it.id in flip else (wrong if it.id in miss else right)
        table[it.prompt] = json.dumps({"better": ans, "reason": "r"})
    return table.__getitem__


def critic_answers(exam, *, miss=(), false_flag=()):
    table = {}
    for it in exam.items:
        tags = list(it.expected["flaws"])
        if it.id in miss:
            tags = []
        if it.id in false_flag:
            tags = tags + ["no_validation"]
        table[it.prompt] = json.dumps({"flaws": tags, "questions": ["why?"]})
    return table.__getitem__


@pytest.fixture
def judge_exam():
    return rx.build_judge_exam(TASK, PROFILE, source=SOURCE)


@pytest.fixture
def critic_exam():
    return rx.build_critic_exam(TASK, PROFILE, source=SOURCE)


def _ids(exam):
    return [it.id for it in exam.items]


# --- builders ---------------------------------------------------------------------------------------------------

def test_judge_exam_is_deterministic_both_orders_with_every_defect(judge_exam):
    again = rx.build_judge_exam(TASK, PROFILE, source=SOURCE)
    assert again.content_hash == judge_exam.content_hash and len(judge_exam.content_hash) == 64
    defects = {it.meta["defect"] for it in judge_exam.items}
    assert defects == {"test_label_peek", "unscaled_distance", "wrong_metric_direction", "target_leak_feature"}
    groups = {}
    for it in judge_exam.items:
        groups.setdefault(it.group, []).append(it)
    assert all(len(g) == 2 and {i.expected["better"] for i in g} == {"A", "B"} for g in groups.values())
    assert len(judge_exam.items) >= 24
    leak = next(it for it in judge_exam.items if it.meta["defect"] == "test_label_peek")
    assert "test_y.csv" in leak.prompt  # the planted defect is really in the plan text


def test_exam_hash_changes_when_an_item_changes(judge_exam):
    items = list(judge_exam.items)
    items[3] = dataclasses.replace(items[3], prompt=items[3].prompt + " ")
    changed = dataclasses.replace(judge_exam, items=tuple(items))
    assert changed.content_hash != judge_exam.content_hash
    other_task = rx.build_judge_exam({**TASK, "metric": "f1_macro"}, PROFILE, source=SOURCE)
    assert other_task.content_hash != judge_exam.content_hash


def test_items_carry_dev_public_provenance(judge_exam, critic_exam):
    for it in judge_exam.items + critic_exam.items:
        assert it.source["dataset_role"] == "dev" and it.source["ref"] == SOURCE["ref"]
        assert it.source["task_id"] == "wine-demo" and len(it.source["profile_sha256"]) == 64
    pub = rx.build_critic_exam(TASK, PROFILE, source={"dataset_role": "public", "ref": "uci/wine"})
    assert {it.source["dataset_role"] for it in pub.items} == {"public"}


@pytest.mark.parametrize("bad", [
    {"dataset_role": "private", "ref": "x"}, {"dataset_role": "held_out", "ref": "x"},
    {"dataset_role": "gate", "ref": "x"}, {"dataset_role": "sealed", "ref": "x"}, {"dataset_role": "test", "ref": "x"},
    {"ref": "x"}, {"dataset_role": "dev"}, {"dataset_role": "dev", "ref": "tasks/wine/private/test_y.csv"},
    {"dataset_role": "DEV ", "ref": "x"}, "dev"])
@pytest.mark.parametrize("builder", ["build_judge_exam", "build_critic_exam"])
def test_builders_refuse_private_or_held_out_sources(builder, bad):
    with pytest.raises(rx.ExamSourceError):
        getattr(rx, builder)(TASK, PROFILE, source=bad)


@pytest.mark.parametrize("builder", ["build_judge_exam", "build_critic_exam"])
def test_builders_refuse_inputs_marked_held_out_anywhere(builder):
    marked = {**PROFILE, "extra": [{"dataset_role": "held_out"}]}
    with pytest.raises(rx.ExamSourceError):
        getattr(rx, builder)(TASK, marked, source=SOURCE)
    with pytest.raises(rx.ExamSourceError):
        getattr(rx, builder)({**TASK, "dataset_role": "private"}, PROFILE, source=SOURCE)


# --- grading ----------------------------------------------------------------------------------------------------

def test_run_exam_sends_schema_and_grades(judge_exam):
    ids = _ids(judge_exam)
    be = Fake("alibaba", judge_answers(judge_exam, miss={ids[0]}))
    res = rx.run_exam(be, judge_exam)
    assert len(be.requests) == len(ids) and all(r.json_schema for r in be.requests)
    assert res.exam_hash == judge_exam.content_hash and res.misses[0] and sum(res.misses) == 1
    assert res.score == pytest.approx(1 - 1 / len(ids))
    assert res.order_consistency == pytest.approx(1 - 1 / (len(ids) // 2))


@pytest.mark.parametrize("text", ["I prefer plan A.", "```json\n{\"better\": \"A\"}\n```", "{\"better\": \"C\"}",
                                  "[]", "", "{\"better\": null}"])
def test_unparseable_judge_output_counts_as_miss(judge_exam, text):
    res = rx.run_exam(Fake("alibaba", lambda p: text), judge_exam)
    assert all(res.misses) and res.score == 0.0 and res.n_unparsed == len(judge_exam.items)
    assert res.order_consistency == 0.0


def test_unparseable_critic_output_and_backend_errors_count_as_miss(critic_exam):
    res = rx.run_exam(Fake("alibaba", lambda p: json.dumps({"flaws": ["made_up_tag"], "questions": []})),
                      critic_exam)
    assert all(res.misses) and res.tag_recall == 0.0 and res.false_flag_rate == 1.0  # fail closed on sound plans

    class Broken(Fake):
        def complete(self, req):
            raise BackendError("outage")
    res = rx.run_exam(Broken("alibaba", None), critic_exam)
    assert all(res.misses) and res.n_unparsed == len(critic_exam.items)


def test_critic_exam_grades_typed_tags_and_false_flags(critic_exam):
    flawed = [it.id for it in critic_exam.items if it.expected["flaws"]]
    sound = [it.id for it in critic_exam.items if not it.expected["flaws"]]
    assert set(rx.FLAW_TAGS) >= {"test_label_peek", "unscaled_distance", "wrong_metric_direction",
                                 "target_leak_feature"} and len(sound) >= 6 and len(flawed) >= 12
    schema = critic_exam.items[0].schema
    assert schema["properties"]["flaws"]["items"]["enum"] == list(rx.FLAW_TAGS)  # closed enum
    res = rx.run_exam(Fake("alibaba", critic_answers(critic_exam, miss=set(flawed[:3]),
                                                     false_flag={sound[0]})), critic_exam)
    assert res.tag_recall == pytest.approx(1 - 3 / len(flawed))
    assert res.false_flag_rate == pytest.approx(1 / len(sound))
    assert res.order_consistency is None


def test_miss_phi_values_and_undefined_cases():
    assert rx.miss_phi([True, False, True, False], [True, False, True, False]) == pytest.approx(1.0)
    assert rx.miss_phi([True, False, True, False], [False, True, False, True]) == pytest.approx(-1.0)
    assert rx.miss_phi([True, True, False, False], [True, False, True, False]) == pytest.approx(0.0)
    assert rx.miss_phi([False] * 4, [True, False, True, False]) is None  # constant: undefined
    assert rx.miss_phi([True, False], [True, True]) is None
    assert rx.miss_phi([], []) is None
    with pytest.raises(ValueError):
        rx.miss_phi([True], [True, False])


# --- admission --------------------------------------------------------------------------------------------------

def _results(exam, cand_kw, prop_kw, answers=judge_answers, family="alibaba"):
    cand = rx.run_exam(Fake(family, answers(exam, **cand_kw)), exam)
    prop = rx.run_exam(Fake("openai", answers(exam, **prop_kw), name="codex-cli", provider="openai", model="gpt-x"),
                       exam)
    return cand, prop


def test_other_family_that_answers_correctly_is_admitted(judge_exam):
    ids = _ids(judge_exam)
    cand, prop = _results(judge_exam, {"miss": {ids[0]}}, {"miss": {ids[5]}})
    adm = rx.admit("judge", cand, prop, "alibaba", "openai")
    assert adm.admitted and adm.reasons == () and adm.phi is not None and adm.phi < 0.8


def test_candidate_missing_like_the_proposer_is_refused_on_phi(judge_exam):
    ids = _ids(judge_exam)
    cand, prop = _results(judge_exam, {"miss": {ids[0]}}, {"miss": {ids[0]}})
    adm = rx.admit("judge", cand, prop, "alibaba", "openai")
    assert not adm.admitted and adm.phi == pytest.approx(1.0)
    assert len(adm.reasons) == 1 and "phi" in adm.reasons[0]  # every other check passed: phi alone refused it


@pytest.mark.parametrize("cand_family,prop_family", [("openai", "openai"), ("unknown:ollama/mystery", "openai"),
                                                      ("alibaba", "unknown:x/y"), ("mock", "openai"), ("", "openai")])
def test_same_unknown_or_mock_family_refused(judge_exam, cand_family, prop_family):
    ids = _ids(judge_exam)
    cand, prop = _results(judge_exam, {"miss": {ids[0]}}, {"miss": {ids[5]}})
    adm = rx.admit("judge", cand, prop, cand_family, prop_family)
    assert not adm.admitted and len(adm.reasons) == 1 and "family" in adm.reasons[0]


def test_order_inconsistent_judge_refused(judge_exam):
    # always answers "A" on 3 of the 12 pairs: score 0.875 passes, order consistency 0.75 does not
    groups = sorted({it.group for it in judge_exam.items})[:3]
    flip = {it.id for it in judge_exam.items if it.group in groups}
    ids = _ids(judge_exam)
    cand, prop = _results(judge_exam, {"flip": flip}, {"miss": {ids[-1]}})
    assert cand.score >= 0.8 and cand.order_consistency == pytest.approx(0.75)
    adm = rx.admit("judge", cand, prop, "alibaba", "openai")
    assert not adm.admitted and len(adm.reasons) == 1 and "order consistency" in adm.reasons[0]


def test_low_score_refused(judge_exam):
    ids = _ids(judge_exam)
    cand, prop = _results(judge_exam, {"miss": set(ids[0:12:2])}, {"miss": {ids[5]}})
    adm = rx.admit("judge", cand, prop, "alibaba", "openai")
    assert not adm.admitted and any("score" in r for r in adm.reasons)


def test_constant_miss_vectors_phi_none_refused(judge_exam):
    ids = _ids(judge_exam)
    cand, prop = _results(judge_exam, {}, {"miss": {ids[5]}})  # candidate perfect: constant miss vector
    adm = rx.admit("judge", cand, prop, "alibaba", "openai")
    assert adm.phi is None and not adm.admitted and "diversity not shown" in adm.reasons[0]
    cand, prop = _results(judge_exam, {"miss": {ids[0]}}, {})  # proposer perfect
    assert not rx.admit("judge", cand, prop, "alibaba", "openai").admitted


def test_critic_admission_bars(critic_exam):
    flawed = [it.id for it in critic_exam.items if it.expected["flaws"]]
    sound = [it.id for it in critic_exam.items if not it.expected["flaws"]]
    cand, prop = _results(critic_exam, {"miss": {flawed[0]}}, {"miss": {flawed[4]}}, answers=critic_answers)
    assert rx.admit("critic", cand, prop, "alibaba", "openai").admitted
    flagger = rx.run_exam(Fake("alibaba", critic_answers(critic_exam, miss={flawed[0]},
                                                         false_flag=set(sound[:2]))), critic_exam)
    adm = rx.admit("critic", flagger, prop, "alibaba", "openai")
    assert flagger.false_flag_rate > 0.3 and not adm.admitted and any("false-flag" in r for r in adm.reasons)
    weak = rx.run_exam(Fake("alibaba", critic_answers(critic_exam, miss=set(flawed[:6]))), critic_exam)
    adm = rx.admit("critic", weak, prop, "alibaba", "openai")
    assert weak.tag_recall < 0.6 and not adm.admitted and any("recall" in r for r in adm.reasons)


def test_admission_refuses_mismatched_exams_and_roles(judge_exam, critic_exam):
    ids = _ids(judge_exam)
    cand, _ = _results(judge_exam, {"miss": {ids[0]}}, {"miss": {ids[5]}})
    flawed = [it.id for it in critic_exam.items if it.expected["flaws"]]
    _, prop_c = _results(critic_exam, {}, {"miss": {flawed[0]}}, answers=critic_answers)
    adm = rx.admit("judge", cand, prop_c, "alibaba", "openai")  # different exams: phi meaningless
    assert not adm.admitted and any("different exams" in r for r in adm.reasons)
    # a proposer result with the same length but another exam hash is still refused on the hash alone
    _, prop = _results(judge_exam, {}, {"miss": {ids[5]}})
    assert rx.admit("judge", cand, prop, "alibaba", "openai").admitted  # control: same exam is admitted
    adm = rx.admit("judge", cand, dataclasses.replace(prop, exam_hash="f" * 64), "alibaba", "openai")
    assert not adm.admitted and adm.reasons == ("candidate and proposer took different exams: miss correlation is "
                                                "not comparable",)
    adm = rx.admit("critic", cand, prop, "alibaba", "openai")  # judge result offered for the critic role
    assert not adm.admitted and any("not 'critic'" in r for r in adm.reasons)
    adm = rx.admit("replicator", dataclasses.replace(cand, role="replicator"), prop, "alibaba", "openai")
    assert not adm.admitted and adm.reasons == ("replicator exam NOT BUILT: cannot admit",)


def test_replicator_exam_is_not_built():
    with pytest.raises(NotImplementedError, match="NOT BUILT"):
        rx.build_replicator_exam([])
    with pytest.raises(NotImplementedError, match="NOT BUILT"):
        rx.grade_replicator_item(rx.ReplicatorItem(claim_id="c", replica_dev_ref="r", source=SOURCE), {})


def test_admission_is_ledgered_and_read_back_bound_to_identity_and_exam_hash(tmp_path, judge_exam):
    store = EvidenceStore(tmp_path / "store")
    ids = _ids(judge_exam)
    good = Fake("alibaba", judge_answers(judge_exam, miss={ids[0]}))
    cand = rx.run_exam(good, judge_exam)
    prop = rx.run_exam(Fake("openai", judge_answers(judge_exam, miss={ids[5]}), name="codex-cli",
                            provider="openai", model="gpt-x"), judge_exam)
    adm = rx.admit("judge", cand, prop, "alibaba", "openai")
    body = rx.record_admission(store, good, adm)
    rows = store.records("role_exam")
    assert rows == [body] and body["admitted"] is True and body["exam_hash"] == judge_exam.content_hash
    assert {body["backend"], body["provider"], body["model"], body["family"]} == {"fake-local", "ollama", "fake-4b",
                                                                                 "alibaba"}
    hashes = {"judge": judge_exam.content_hash}
    assert rx.admitted_roles(store, good, exam_hashes=hashes) == {"judge"}
    # a different model, provider or name behind the same family is NOT admitted
    assert rx.admitted_roles(store, Fake("alibaba", None, model="fake-7b"), exam_hashes=hashes) == set()
    assert rx.admitted_roles(store, Fake("alibaba", None, provider="vllm"), exam_hashes=hashes) == set()
    assert rx.admitted_roles(store, Fake("alibaba", None, name="other"), exam_hashes=hashes) == set()
    # a changed exam invalidates the admission
    assert rx.admitted_roles(store, good, exam_hashes={"judge": "0" * 64}) == set()
    assert rx.admitted_roles(store, good, exam_hashes={}) == set()
    # a later refusal on the same exam supersedes the admission
    bad = rx.admit("judge", cand, cand, "alibaba", "openai")
    rx.record_admission(store, good, bad)
    assert rx.admitted_roles(store, good, exam_hashes=hashes) == set()
    # the recorded family must be the backend's family
    with pytest.raises(ValueError):
        rx.record_admission(store, Fake("meta", None), adm)
    store.close()


def test_second_family_status_is_needs_owner_today():
    codex = CodexCLIBackend()
    status = rx.second_family_status({"proposer": codex, "executor": codex, "judge": MockBackend(["{}"])})
    assert status.startswith("needs owner: install a local model (e.g. via Ollama) or provide an allowlisted API")
    m = MockBackend(["{}"])
    m.family = "alibaba"  # a mock that DECLARES another family is still a mock
    assert rx.second_family_status({"proposer": codex, "judge": m}).startswith("needs owner")
    assert rx.second_family_status({"proposer": codex, "judge": Fake("unknown:ollama/x", None)}).startswith(
        "needs owner")
    ok = rx.second_family_status({"proposer": codex, "judge": Fake("alibaba", None)})
    assert not ok.startswith("needs owner") and "fake-local" in ok and "exam" in ok
    assert rx.second_family_status({}).startswith("needs owner")


def test_thresholds_live_in_one_place_marked_uncalibrated():
    t = rx.ExamThresholds()
    assert (t.score_bar, t.order_consistency_bar, t.critic_tag_recall_bar, t.critic_false_flag_max, t.max_phi) == (
        0.80, 0.85, 0.6, 0.3, 0.8)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.max_phi = 0.99  # frozen
    src = inspect.getsource(rx.ExamThresholds)
    for field in ("score_bar", "order_consistency_bar", "critic_tag_recall_bar", "critic_false_flag_max"):
        line = next(ln for ln in src.splitlines() if ln.strip().startswith(field))
        assert "# UNCALIBRATED (owner decision, GOAL §13.8)" in line
    assert "GOAL §16.8" in next(ln for ln in src.splitlines() if ln.strip().startswith("max_phi"))
    assert Path(rx.__file__).read_bytes().count(b"\r\n") == 0
