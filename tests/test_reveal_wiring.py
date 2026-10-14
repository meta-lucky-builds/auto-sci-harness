"""L6.11 temporal reveal and L6.9 route table, wired into the evolve driver (GOAL §5). Early-reveal attempts
(forged or backdated watermark records, future watermarks, re-sealing, writing into the seal or outside the mounts,
a seal inside a task directory), the watermark rule at day and timezone boundaries, undated items, and the driver
hooks. No model calls: the generation itself is stubbed (its own tests are test_evolve*.py, test_router.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import custom_harness.evolve.driver as driver
from custom_harness.evolve import SuiteTask
from custom_harness.evolve.branches import BranchConfig
from custom_harness.evolve.loop import GenerationResult
from custom_harness.evolve.reveal import REVEAL_KIND, WATERMARK_KIND, LabelItem, RevealRefused, label_time
from custom_harness.kerneld.releases import released
from test_reveal import FUTURE_LABEL, NOW, _ids, _stream

DAY = 86400.0


# ---------------------------------------------------------------------------------------------- watermark rule
def test_watermark_rule_at_day_and_timezone_boundaries(store, tmp_path):
    r = _stream(store, tmp_path)
    r.seal([LabelItem("west", 8, "2026-09-20T23:30:00-05:00"),  # = 2026-09-21T04:30Z
            LabelItem("east", 9, "2026-09-21T03:00:00+05:00"),  # = 2026-09-20T22:00Z
            LabelItem("same-day", 10, "2026-09-21")])  # date-only: the END of 2026-09-21 in UTC-12
    r.advance("2026-09-21")  # start of 2026-09-21 UTC
    vis = _ids(r.visible())
    assert "east" in vis and "west" not in vis and "same-day" not in vis
    r.advance("2026-09-21T23:59:59+00:00")
    assert "same-day" not in _ids(r.visible())  # still before the end of its day
    r.advance("2026-09-22")
    assert "west" in _ids(r.visible()) and "same-day" not in _ids(r.visible())  # 2026-09-21 not over in UTC-12
    r.advance("2026-09-22T11:59:59+00:00")
    assert "same-day" not in _ids(r.visible())
    r.advance("2026-09-22T12:00:00+00:00")  # the day has ended everywhere on earth
    assert "same-day" in _ids(r.visible())


def test_undated_items_stay_sealed_at_the_latest_possible_watermark(store, tmp_path):
    r = _stream(store, tmp_path)
    r.seal([LabelItem("blank", 1, "  "), LabelItem("int-date", 2, 20260901), LabelItem("week", 3, "2026-W36")])
    r.advance(datetime.fromtimestamp(NOW, tz=UTC).isoformat())  # the watermark may not pass the clock
    vis = set(_ids(r.visible()))
    assert not {"undated", "naive", "garbage", "blank", "int-date", "week"} & vis
    assert label_time("2026-W36") is None and label_time(20260901) is None
    # no reveal record and no release for any undated item
    assert {x["item_id"] for x in store.records(REVEAL_KIND)} == vis


# ---------------------------------------------------------------------------------------------- early reveals
def test_attack_forged_watermark_records_reveal_nothing_early(store, tmp_path):
    """Records appended to the store directly, around `advance`: a watermark ahead of its own recording time, one
    ahead of the clock, one that moves backwards, and one with no recording time. Each fails closed; none reveals
    the future label."""
    attempts = [
        {"watermark": "2026-10-01", "at": NOW - 10 * DAY},  # recorded on 09-28, claims 10-01 (backdated record)
        {"watermark": "2026-12-01", "at": NOW + 90 * DAY},  # a future record and a future watermark
        {"watermark": "2026-10-05"},  # no recording time
    ]
    for i, att in enumerate(attempts):
        r = _stream(store, tmp_path / f"s{i}")
        object.__setattr__(r, "stream_id", f"s-forged-{i}")
        store.append_record(WATERMARK_KIND, {"stream": r.stream_id, **att})
        with pytest.raises(RevealRefused, match="fail closed"):
            r.visible()
        with pytest.raises(RevealRefused):
            r.write_revealed(tmp_path / f"s{i}" / "export" / "labels.jsonl")
    # backwards: a legitimate advance, then a record that moves back, then a forged jump forward
    r = _stream(store, tmp_path / "back")
    object.__setattr__(r, "stream_id", "s-back")
    r.advance("2026-09-20")
    store.append_record(WATERMARK_KIND, {"stream": "s-back", "watermark": "2026-09-01", "at": NOW})
    with pytest.raises(RevealRefused, match="backwards"):
        r.visible()
    assert not [b for b in released(store) if FUTURE_LABEL in json.dumps(b)]
    assert "late" not in {x["item_id"] for x in store.records(REVEAL_KIND)}


def test_attack_early_reveal_through_the_api_is_refused(store, tmp_path):
    r = _stream(store, tmp_path)
    with pytest.raises(RevealRefused, match="future"):
        r.advance("2026-10-09")  # one day past the clock
    with pytest.raises(RevealRefused, match="already sealed"):
        r.seal([LabelItem("late", FUTURE_LABEL, "2026-09-01")])  # a re-dated copy of a sealed item
    r.advance("2026-09-20")
    with pytest.raises(RevealRefused, match="only moves forward"):
        r.advance("2026-09-10")
    with pytest.raises(RevealRefused, match="outside the seal"):
        r.write_revealed(r.root / "out.jsonl")
    with pytest.raises(RevealRefused, match="evolver mount"):
        r.write_revealed(tmp_path / "elsewhere" / "out.jsonl")
    assert "late" not in _ids(r.visible())


# ---------------------------------------------------------------------------------------------- driver wiring
class _Stub:
    """Stands in for evolve_generation (no model, no harness run); optionally advances a stream between
    generations, as time passing would."""

    def __init__(self, on_call=None):
        self.calls, self.on_call = 0, on_call

    def __call__(self, parent, working, **kw):
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        return GenerationResult(parent.spec_hash(), parent, False, "stub: no candidate", counters={})


def _drive(monkeypatch, store, tmp_path, suite, stub, **kw):
    from test_router import TRUNK
    monkeypatch.setattr(driver, "evolve_generation", stub)
    monkeypatch.setattr(driver, "check_campaign", lambda *a, **k: 0)
    out = tmp_path / "out"
    driver.evolve(TRUNK, suite, generations=2, backends={}, proposer=None, store=store,
                  sandbox_factory=lambda k: None, out_dir=out, log=lambda s: None, **kw)
    return out


def test_driver_reveals_labels_before_each_generation_and_ledgers_them(store, tmp_path, monkeypatch):
    r = _stream(store, tmp_path)
    dest = tmp_path / "export" / "stream" / "labels.jsonl"
    suite = [SuiteTask("e0", tmp_path / "tasks" / "e0", "evolve", family="A"),
             SuiteTask("g0", tmp_path / "tasks" / "g0", "gate", family="B")]
    seen: list[list[str]] = []

    def tick(n):
        seen.append(sorted(json.loads(x)["item_id"] for x in dest.read_text(encoding="utf-8").splitlines()))
        if n == 1:
            r.advance("2026-10-01T12:00:00+00:00")  # time passes between generations

    r.advance("2026-09-02T12:00:00+00:00")
    _drive(monkeypatch, store, tmp_path, suite, _Stub(tick), reveals=[(r, dest)])
    assert seen == [["old"], ["edge-day", "late", "old", "tz"]]
    assert sorted(x["item_id"] for x in store.records(REVEAL_KIND)) == ["edge-day", "late", "old", "tz"]
    assert len([b for b in released(store) if b["note"].startswith("reveal")]) == 4  # one release per item


def test_driver_refuses_a_seal_inside_a_task_directory_before_anything_runs(store, tmp_path, monkeypatch):
    r = _stream(store, tmp_path)
    suite = [SuiteTask("e0", r.root.parent, "evolve", family="A"),  # the task dir CONTAINS the seal
             SuiteTask("g0", tmp_path / "tasks" / "g0", "gate", family="B")]
    stub = _Stub()
    with pytest.raises(RevealRefused, match="overlap"):
        _drive(monkeypatch, store, tmp_path, suite, stub, reveals=[(r, tmp_path / "export" / "l.jsonl")])
    assert stub.calls == 0
    stub2 = _Stub()
    with pytest.raises(RevealRefused, match="evolver mount"):  # a destination outside every declared mount
        _drive(monkeypatch, store, tmp_path, suite[1:], stub2, reveals=[(r, tmp_path / "nowhere" / "l.jsonl")])
    assert stub2.calls == 0


def test_driver_records_the_route_table_of_the_final_trunk(store, tmp_path, monkeypatch):
    from test_router import CA, CB, _branches, _gate_tasks
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    out = _drive(monkeypatch, store, tmp_path, tasks, _Stub(), branching=BranchConfig())
    rec = store.records(driver.ROUTE_KIND)[-1]
    assert rec["status"] == "built" and rec["table"]["routes"] == {"A": CA.spec_hash(), "B": CB.spec_hash()}
    assert json.loads((out / "router.json").read_text(encoding="utf-8")) == rec
    # no gate value in the table: only hashes and cluster names
    assert all(isinstance(v, str) for v in rec["table"]["routes"].values())
    # without branching the driver builds no router (unchanged behaviour)
    n = len(store.records(driver.ROUTE_KIND))
    _drive(monkeypatch, store, tmp_path / "nb", tasks, _Stub())
    assert len(store.records(driver.ROUTE_KIND)) == n


def test_driver_records_a_refused_router_and_keeps_the_trunk(store, tmp_path, monkeypatch):
    from custom_harness.evolve.branches import propose_branch
    from custom_harness.kerneld.releases import Releases
    from test_router import NAMES, SEEDS, TRUNK, Script, _gate_tasks, _spec
    tasks = _gate_tasks(tmp_path)
    ca, ca2 = _spec("branch-a"), _spec("branch-a2")
    sc = Script(store, tasks, names=NAMES | {ca2.spec_hash(): "CA"})
    for c in (ca, ca2):  # two branches win cluster A: the router refuses to choose
        propose_branch(TRUNK, c, tasks, seeds=SEEDS, outcome=sc.outcome, store=store,
                       releases=Releases.live(store), globally_accepted=False)
    out = _drive(monkeypatch, store, tmp_path, tasks, _Stub(), branching=BranchConfig())
    rec = store.records(driver.ROUTE_KIND)[-1]
    assert rec["status"] == "refused" and "won by 2 branches" in rec["reason"]
    assert not (out / "router.json").exists()


def test_cli_lists_branches_and_routes_tasks(store, tmp_path, monkeypatch, capsys):
    """`chh evolve branches` / `chh evolve route`: read-only views of the branch records and the route table."""
    import yaml

    from custom_harness import cli
    from test_router import CA, CB, TINY, _branches, _gate_tasks
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    spec = tmp_path / "trunk.yaml"
    spec.write_text(yaml.safe_dump(TINY | {"id": "trunk"}), encoding="utf-8")
    suite = tmp_path / "suite.yaml"
    dirs = {}
    for name in ("sA", "sB", "sC"):
        dirs[name] = tmp_path / "t" / name
    suite.write_text(yaml.safe_dump({"tasks": [
        {"id": n, "dir": str(d), "split": "sealed", "family": n[1]} for n, d in dirs.items()]}), encoding="utf-8")
    monkeypatch.setattr(cli, "EvidenceStore", lambda *_: store)
    monkeypatch.setattr(store, "close", lambda: None)
    assert cli.main(["evolve", "branches", str(spec)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {b["spec_hash"] for b in listed["branches"]} == {CA.spec_hash(), CB.spec_hash()}
    assert all(set(b) == {"branch_id", "spec_hash", "cluster_key", "won", "lofo_passed", "routable"}
               for b in listed["branches"])
    assert cli.main(["evolve", "route", str(spec), "--suite", str(suite)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["routes"] == {"A": CA.spec_hash(), "B": CB.spec_hash()}
    assert out["tasks"] == {"sA": CA.spec_hash(), "sB": CB.spec_hash(), "sC": out["trunk"]}
    assert not any(d.exists() for d in dirs.values())  # routing never needed a task directory
