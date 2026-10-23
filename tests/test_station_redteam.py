"""Red-team regression tests for the L4 multi-agent research mode ("station", docs/multi_agent_mode.md).

WRITTEN, NOT RUN (s4-station__tests was a build-only sub-agent: every assertion was traced by reading
research/station/{archive,mode}.py). Each test names the code it FAILS WITHOUT. It reuses the helpers of
tests/test_station_mode.py (real Kernel world; only the agent runner is a double).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from custom_harness.kerneld.releases import note_code
from custom_harness.research.station import PubCode, StationError
from custom_harness.research.station.archive import _screen_texts, carries_heldout_value
from custom_harness.research.station.mode import StationLocked
from test_station_mode import LP_PROB, SW, ScriptRunner, cfg, codes, ends, mode, starts

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sw(tmp_path_factory):
    s = SW(tmp_path_factory.mktemp("station_redteam"))
    yield s
    s.w.close()


def _dying(m: Any, typ: str, *, event: str | None = None) -> dict[str, bool]:
    """Make the FIRST `m.log.append(typ, ...)` (with payload event == `event`, if given) raise KeyboardInterrupt
    (the process dies) instead of appending. Returns the state dict ({"done": bool})."""
    real = m.log.append
    state = {"done": False}

    def dying(t: str, payload: dict[str, Any]) -> None:
        if t == typ and not state["done"] and (event is None or payload.get("event") == event):
            state["done"] = True
            raise KeyboardInterrupt
        real(t, payload)

    m.log.append = dying
    return state


# ====================================================================== 1. attribution by run (#1)
def test_certificate_from_foreign_runs_is_not_attributed_to_the_turn(sw):
    """A data certificate whose evidence was produced by runs that are NOT the turn's runs is not the turn's
    result: it is not harvested (certs == []), nothing is published, and it is listed under `unattributed`.
    The same script with the evaluation runs joined to the turn's campaign publishes.
    FAILS WITHOUT archive.StationArchive.attributable's run check (`named <= set(w.runs)`, with evidence_runs):
    the window check alone (certificate and pre-registration inside the turn's ledger window) would attribute it."""
    t = sw.target()
    c = cfg()
    probe = mode(sw, c, ScriptRunner(sw))
    first = probe.order(0)[0]
    foreign = ScriptRunner(sw, {(0, first): lambda s, r: [s.data(t)]})
    foreign.foreign_runs = True
    m = mode(sw, c, foreign)
    m.run()
    h = foreign.out[(0, first)][0]
    end = ends(m)[(0, first)]
    assert end["certs"] == [] and end["unattributed"] == [h]
    assert end["publications"] == [] and m.archive.entries() == []
    assert end["signal"]["gated"] == 0 and end["signal"]["accepted"] is False

    t2 = sw.target()
    c2 = cfg()
    own = ScriptRunner(sw, {(0, first): lambda s, r: [s.data(t2)]})
    m2 = mode(sw, c2, own)
    first2 = m2.order(0)[0]
    own.script = {(0, first2): lambda s, r: [s.data(t2)]}
    m2.run()
    h2 = own.out[(0, first2)][0]
    end2 = ends(m2)[(0, first2)]
    assert end2["certs"] == [h2] and end2["unattributed"] == []
    assert codes(m2, (0, first2)) == [PubCode.PUBLISHED.value]


# ====================================================================== 2. exclusive lock (#1)
def test_exclusive_lock_refuses_a_live_holder_and_takes_over_a_dead_one(sw):
    """A lock file naming THIS live process refuses run() (StationLocked) before anything is written to the mode
    log; a lock file naming a dead pid on this host is taken over and the mode completes (and the lock is removed).
    FAILS WITHOUT StationMode.lock (O_CREAT|O_EXCL file, pid liveness check) being entered by run(): without it
    run() would open the mode and log despite the lock, and a stale lock would not be a decision at all."""
    m = mode(sw, cfg(), ScriptRunner(sw))
    path = m.lock_path()
    try:
        path.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "at": 0}), encoding="utf-8")
        with pytest.raises(StationLocked):
            m.run()
        assert sw.store.records(m.log.kind) == []  # nothing written: not even `open`
        assert path.exists()  # a lock that is not ours is left alone
        path.write_text(json.dumps({"pid": 2**31 - 7, "host": socket.gethostname(), "at": 0}), encoding="utf-8")
        st = m.run()
        assert st["closed"] is True and st["rounds_done"] == 1
        assert not path.exists()  # released at the end
    finally:
        path.unlink(missing_ok=True)


# ====================================================================== 3. log post-append check (#1)
def test_log_append_detects_a_concurrent_writer(sw, monkeypatch):
    """The first append to the mode log is duplicated (a second writer's record lands right after it): _Log.append
    sees two records where it wrote record 1 and fails closed with StationError("concurrent writer").
    FAILS WITHOUT the post-append check in mode._Log.append (`len(after) != seq or after[-1].hash != h`): the
    append would return normally and the chain would silently fork."""
    m = mode(sw, cfg(), ScriptRunner(sw))
    real = sw.store.append_record
    state = {"dup": False}

    def duplicating(kind: str, body: dict[str, Any]) -> None:
        real(kind, body)
        if kind == m.log.kind and not state["dup"]:
            state["dup"] = True
            real(kind, body)

    monkeypatch.setattr(sw.store, "append_record", duplicating)
    with pytest.raises(StationError, match="concurrent writer"):
        m.log.append("open", {"x": 1})
    assert state["dup"] is True


# ====================================================================== 4. lane-intent crash (#2)
def test_crash_after_lane_intent_and_gate_submission_applies_the_shrink_once(sw):
    """The process dies appending the `lane` record, after the `lane_intent` was logged and the shrink was
    submitted to the downgrade gate. The rerun finds the gate's record (same controller id and proposal hash) and
    does not submit again: exactly ONE controller_action record, the lane is logged once, and the agent's cap equals
    the intent's new_cap (not shrunk twice).
    FAILS WITHOUT StationMode._lanes' logged `lane_intent` + _submit_shrink's lookup of the gate's earlier decision:
    a rerun would recompute the proposal from the already-shrunk cap (a different text) and shrink again."""
    script = {(r, "a1"): (lambda s, q: [s.lp()]) for r in range(3)}
    runner = ScriptRunner(sw, script)
    c = cfg(rounds=3, stall_window=2, problems=(LP_PROB, {"id": "rings"}))
    m = mode(sw, c, runner)
    state = _dying(m, "lane")
    with pytest.raises(KeyboardInterrupt):
        m.run()
    assert state["done"] is True
    cid = m._controller_id(1)
    assert len(sw.store.records_where("controller_action", "controller_id", cid)) == 1  # submitted before the crash
    m2 = mode(sw, c, runner)
    m2.run()
    assert len(sw.store.records_where("controller_action", "controller_id", cid)) == 1
    intents = [p for t, p in m2.log.events() if t == "lane_intent" and p["agent"] == "a2"]
    lanes = [p for t, p in m2.log.events() if t == "lane" and p["agent"] == "a2"]
    assert len(intents) == 1 and len(lanes) == 1 and lanes[0]["gate"]["applied"] is True
    new_cap = intents[0]["proposal"]["new_cap"]
    assert lanes[0]["gate"]["new_cap"] == new_cap
    assert m2.agent_caps("a2")["usd"] == pytest.approx(new_cap)
    assert [x[0] for x in runner.calls if x[1] == 0] == ["start", "start"]  # no turn re-run


# ====================================================================== 5. crash before turn_published (#3)
def test_crash_after_publication_before_turn_published_re_harvests_the_same_entry(sw):
    """The process dies appending `turn_published`, after the archive admitted the entry. The rerun re-harvests the
    finished run's window: the same single entry is reported published, the stall signal keeps its accept, and the
    runner is not called again.
    FAILS WITHOUT the `turn_outcome` record (the run id, window end and runs are logged before publication) and the
    `key not in f.published` branch of StationMode._finish plus archive._submit's re-harvest branch: the rerun
    would not know the run's window (or would report `duplicate`)."""
    c = cfg(problems=(LP_PROB,))
    runner = ScriptRunner(sw)
    m = mode(sw, c, runner)
    first = m.order(0)[0]
    runner.script = {(0, first): lambda s, r: [s.lp()]}
    state = _dying(m, "turn_published")
    with pytest.raises(KeyboardInterrupt):
        m.run()
    assert state["done"] is True and len(m.archive.entries()) == 1
    m2 = mode(sw, c, runner)
    m2.run()
    assert len(m2.archive.entries()) == 1
    assert codes(m2, (0, first)) == [PubCode.PUBLISHED.value]
    assert ends(m2)[(0, first)]["signal"]["accepted"] is True
    assert [x[0] for x in runner.calls if x[1] == 0 and x[2] == first] == ["start"]
    assert [t for t, p in m2.log.events() if t == "turn_published" and p["agent"] == first] == ["turn_published"]


# ====================================================================== 6. release journal (#13)
def test_crash_between_release_intent_and_done_releases_again_on_the_safe_side(sw):
    """The process dies appending the `done` journal record of a release, after the `intent` record and after the
    Kernel recorded the release. On rerun the pair has no `done`, so it is released again (the Kernel counts 2 on the
    target: an over-count, the safe side) and the entry is then shown.
    FAILS WITHOUT the intent/done journal (StationMode._journal, archive._release calling journal before and after
    the emit, _Fold.released only from `done`): no `intent` record would exist and the journal would not read
    [intent, intent, done]."""
    t = sw.target()
    c = cfg()
    runner = ScriptRunner(sw)
    m = mode(sw, c, runner)
    first, second = m.order(0)
    runner.script = {(0, first): lambda s, r: [s.data(t)]}
    before = len(sw.kernel.releases(t))
    state = _dying(m, "release", event="done")
    with pytest.raises(KeyboardInterrupt):
        m.run()
    assert state["done"] is True
    assert len(sw.kernel.releases(t)) - before == 1  # released once, not yet journaled done
    assert [p["event"] for typ, p in m.log.events() if typ == "release"] == ["intent"]
    m2 = mode(sw, c, runner)
    m2.run()
    assert len(sw.kernel.releases(t)) - before == 2
    assert [p["event"] for typ, p in m2.log.events() if typ == "release"] == ["intent", "intent", "done"]
    entry = m2.archive.entries()[0]["entry"]
    assert starts(m2)[(0, second)]["archive"]["shown"] == [entry]
    assert m2.status()["archive"]["releases"] == 1


# ====================================================================== 7. redacted entry releases (#4)
def test_redacted_entry_also_releases_its_continuous_targets(sw):
    """a1 reads t1 in round 0, then certifies an exact LP claim in round 1 (no generalization targets): that entry is
    REDACTED for a2 (continuous taint {t1}) and its existence and timing were chosen by an author who had read t1, so
    showing it releases t1 as a `count` release whose note carries the redacted entry's id prefix.
    FAILS WITHOUT archive.render_for's `targets = verdict | (continuous if redacted)`: the entry has no verdict
    target, so no release naming it would be recorded."""
    t1 = sw.target()
    runner = ScriptRunner(sw, {(0, "a1"): lambda s, r: [s.data(t1)], (1, "a1"): lambda s, r: [s.lp()]})
    m = mode(sw, cfg(rounds=3, stall_window=50), runner)
    m.run()
    red = [e for e in m.archive.entries() if e["taint"]["continuous"]]
    assert len(red) == 1 and red[0]["author"] == "a1" and red[0]["round"] == 1
    assert red[0]["taint"]["verdict"] == [] and t1 in red[0]["taint"]["continuous"]
    assert any(red[0]["entry"] in st["archive"]["redacted"] for st in starts(m).values())
    note = note_code("station", m.mid, "entry", red[0]["entry"][4:16])
    rel = [e.body for e in sw.kernel.releases(t1)]
    mine = [b for b in rel if b["note"] == note]
    assert len(mine) == 1 and mine[0]["alphabet"] == "count" and mine[0]["channel"] == "memory"
    assert len(rel) == 2  # plus the round-0 entry's own outcome release


# ====================================================================== 8. numeric screen forms (#11)
def test_numeric_screen_catches_exponent_comma_fraction_and_value_forms():
    """FAILS WITHOUT the wider screen in archive.carries_heldout_value (_NUM exponent / `0,d+` comma decimals with
    _decimal_token's precision shift, _FRAC p/q with q >= 100) and _screen_texts including the claim `value`."""
    vals = [0.5, 2.6, 1.0, 1000.0, 0.873216]
    assert carries_heldout_value(["effect 8.732e-1"], vals)
    assert carries_heldout_value(["effect 0,8732"], vals)
    assert carries_heldout_value(["ratio 873/1000"], vals)
    assert carries_heldout_value(["ratio 873 / 1000"], vals)
    for value in ("0.8732", "873/1000", "8.732e-1"):
        fake = SimpleNamespace(claim={"statement": "a computed value", "effect": {}, "value": value})
        assert carries_heldout_value(_screen_texts(fake), vals), value
    for benign in ("half is 1/2", "ratio 13/5", "about 1,000 rows", "n = 1000"):
        assert not carries_heldout_value([benign], vals), benign
    assert not carries_heldout_value(["effect 8.732e-1"], [])


# ====================================================================== 9. -ABLATED namespace (#9)
def test_ablated_namespace_cannot_collide_with_a_production_mode(sw):
    """No production id can end in -ABLATED (the mode_id pattern is lower case), and an ablated mode "<id>" has a
    different archive namespace, campaign ids and log kind than a production mode whose id is "<id>-ablated".
    FAILS WITHOUT ABLATED_SUFFIX = "-ABLATED" (upper case): with a lower-case suffix the production id
    "<id>-ablated" would map to the same mid, archive namespace, campaigns and budgets as the ablated mode."""
    from custom_harness.scorecard.runner import ablation_set
    with pytest.raises(ValueError):
        cfg(mode_id="m-ABLATED")
    off = ablation_set({"station_coordination"})
    base = cfg()
    abl = mode(sw, base, ScriptRunner(sw, ablation=off), ablation=off)
    prod_cfg = cfg(mode_id=f"{base.mode_id}-ablated")
    prod = mode(sw, prod_cfg, ScriptRunner(sw))
    assert abl.archive.mode_id != prod.archive.mode_id
    assert abl.campaign_id("a1") != prod.campaign_id("a1")
    assert abl.budget_name("a1") != prod.budget_name("a1")
    assert abl.log.kind != prod.log.kind
    assert abl.archive.mode_id == f"{base.mode_id}-ABLATED" and prod.archive.mode_id == f"{base.mode_id}-ablated"
    # lock files too, also on a case-insensitive filesystem (Windows NTFS): FAILS WITHOUT the hash of the exact mode
    # id in StationMode.lock_path (`station-<mid>.lock` would be one file for both)
    assert abl.lock_path().name.lower() != prod.lock_path().name.lower()
    assert abl.lock_path().parent == prod.lock_path().parent


# ====================================================================== 10. agent_caps over every controller (#14)
def test_agent_caps_take_the_min_of_every_applied_shrink_of_the_budget(sw):
    """A controller_action record of ANOTHER controller id on the agent's budget name lowers agent_caps; a rejected
    record, a larger cap, and a record for another budget do not.
    FAILS WITHOUT StationMode.agent_caps querying records_where(ACTION_KIND, "target_id", budget_name) (the old code
    looked at the mode's own controller ids only) or without its `outcome == "applied"` test."""
    m = mode(sw, cfg(), ScriptRunner(sw))

    def shrink(target: str, new_cap: float, outcome: str, tag: str) -> None:
        sw.store.append_record("controller_action", {
            "controller_id": f"other-{tag}", "proposal_sha256": "0" * 64, "index": 0, "action_type": "shrink",
            "target_id": target, "outcome": outcome, "reason": "ok", "unit": "usd", "old_cap": 20.0,
            "new_cap": new_cap})

    assert m.agent_caps("a1") == {"usd": 20.0}
    shrink(m.budget_name("a1"), 12.0, "applied", "x1")
    assert m.agent_caps("a1")["usd"] == 12.0 and m.agent_caps("a2")["usd"] == 20.0
    shrink(m.budget_name("a1"), 3.0, "rejected", "x2")
    assert m.agent_caps("a1")["usd"] == 12.0
    shrink(m.budget_name("a1"), 15.0, "applied", "x3")  # never raised
    assert m.agent_caps("a1")["usd"] == 12.0
    shrink(m.budget_name("a2"), 7.0, "applied", "x4")
    assert m.agent_caps("a2")["usd"] == 7.0 and m.agent_caps("a1")["usd"] == 12.0


# ====================================================================== 11. read-only reports (#15)
def test_status_and_entries_write_nothing_and_flag_pending_retractions(sw):
    """status() and archive.entries() add no store record and no ledger row, even with an entry whose certificate was
    retracted but not yet cascaded; entries() flags that entry retraction_pending.
    FAILS WITHOUT the read-only report paths (StationArchive.entries(sync=False) using the Kernel ledger instead of
    store.sync()/query(), StationMode.status() not opening campaigns): a sync or a logged read would add rows."""
    held: dict[str, str] = {}

    def make(agent: str) -> Any:
        def fn(s: SW, r: Any) -> list[Any]:
            held[agent] = s.lp()
            return []
        return fn

    runner = ScriptRunner(sw, {(0, "a1"): make("a1"), (0, "a2"): make("a2")})
    m = mode(sw, cfg(problems=(LP_PROB,)), runner)
    m.run()
    sw.kernel.retract(held["a1"], "found wrong")

    def counts() -> tuple[int, int]:
        return int(sw.store._rows("SELECT COUNT(*) FROM records")[0][0]), len(sw.store.ledger_rows())

    before = counts()
    st = m.status()
    entries = m.archive.entries()
    assert counts() == before
    by_author = {e["author"]: e for e in entries}
    assert set(by_author) == {"a1", "a2"}
    assert by_author["a1"]["retraction_pending"] is True and by_author["a1"]["status"] == "active"
    assert by_author["a2"]["retraction_pending"] is False
    assert st["archive"]["retraction_pending"] == 1 and st["archive"]["entries"] == 2
    m.archive.corroborations()
    assert counts() == before


# ====================================================================== 12. deferred certificate (#16)
def test_certificate_issued_in_a_later_turn_is_published_as_the_original_turns_deferred_entry(sw):
    """a1 pre-registers an exact claim in round 0 without certifying it; a2 certifies it in round 1. It is not a2's
    result (its pre-registration is outside a2's window), it is listed in a2's `unattributed` and in a1's round-0
    `open_preregs`, and after round 1 a `deferred` record publishes it as a1's round-0 entry.
    FAILS WITHOUT StationMode._deferred + archive.publish_deferred (and _Fold.deferred): the claim would never be
    published, and an a2 turn that attributed it by the certificate's ledger seq alone would publish it as a2's."""
    held: dict[str, Any] = {}

    def a1(s: SW, r: Any) -> list[Any]:
        held["claim"], held["tag"] = s.lp_prereg()
        return []

    def a2(s: SW, r: Any) -> list[Any]:
        held["cert"] = s.lp_certify(held["claim"], held["tag"])
        return []

    runner = ScriptRunner(sw, {(0, "a1"): a1, (1, "a2"): a2})
    m = mode(sw, cfg(rounds=2, stall_window=50, problems=(LP_PROB,)), runner)
    m.run()
    h = held["cert"]
    claim_hash = sw.kernel.certificate(h).claim_hash
    e2 = ends(m)[(1, "a2")]
    assert h not in e2["certs"] and h in e2["unattributed"] and codes(m, (1, "a2")) == []
    assert ends(m)[(0, "a1")]["open_preregs"] == [claim_hash]
    deferred = [p for t, p in m.log.events() if t == "deferred"]
    assert len(deferred) == 1 and deferred[0]["cert"] == h and deferred[0]["agent"] == "a1"
    assert deferred[0]["round"] == 0 and deferred[0]["publication"]["code"] == PubCode.PUBLISHED.value
    entries = m.archive.entries()
    assert len(entries) == 1
    assert (entries[0]["author"], entries[0]["round"], entries[0]["deferred"], entries[0]["cert"]) == ("a1", 0, True, h)
    st = m.status()
    assert st["archive"]["deferred"] == 1 and st["agents"]["a1"]["open_preregs"] == [claim_hash]


# ====================================================================== 13. determinism under hash randomisation (#17)
_SCHEDULE = """
import json, sys
from pathlib import Path
from custom_harness.ablation import NO_ABLATION
from custom_harness.research.station import ModeConfig, StationMode
from custom_harness.store import EvidenceStore


class R:
    ablation = NO_ABLATION

    def model_policy(self, agent):
        return {"families": {}, "invariant_5": "met"}

    def start(self, req):
        raise NotImplementedError

    def resume(self, req, run_id):
        raise NotImplementedError


cfg = ModeConfig.model_validate({
    "mode_id": "det", "problems": [{"id": "p1"}, {"id": "p2"}], "agents": [{"id": "a%d" % i} for i in range(6)],
    "rounds": 5, "seed": 11, "budget": {"unit": "usd", "mode": {"usd": 100.0}, "agent": {"usd": 20.0}}})
m = StationMode(EvidenceStore(Path(sys.argv[1])), cfg, R(), spec_hash="0" * 64)
print(json.dumps([[m.order(r) for r in range(5)],
                  [[m.turn_seed(r, a.id) for a in cfg.agents] for r in range(5)]]))
"""


def test_schedule_is_identical_under_different_hash_seeds(tmp_path):
    """The order of agents and the turn seeds are the same in two fresh processes with PYTHONHASHSEED=1 and =2.
    FAILS WITHOUT the sha256-keyed order/seeds (mode._sha_int): an order keyed on hash(), a set iteration order or
    a dict of str keys would differ between the two processes."""
    outs = []
    for i, seed in enumerate(("1", "2")):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        env.setdefault("CHH_ALLOW_UNISOLATED", "1")
        p = subprocess.run([sys.executable, "-c", _SCHEDULE, str(tmp_path / f"s{i}")], env=env, cwd=str(ROOT),
                           capture_output=True, text=True, timeout=120, check=False)
        assert p.returncode == 0, p.stderr[-2000:]
        outs.append(json.loads(p.stdout.strip().splitlines()[-1]))
    assert outs[0] == outs[1]
    assert len(outs[0][0]) == 5 and len(outs[0][0][0]) == 6
    assert len({tuple(o) for o in outs[0][0]}) > 1  # the order really changes between rounds
