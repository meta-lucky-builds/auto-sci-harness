"""Product basics: settings, spending limits, the default Lab model, backup, restore and safe upgrades. No model is
called anywhere: the fake docker answers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from _experience_world import World
from custom_harness import backup as B
from custom_harness import settings
from custom_harness.experience import Refused
from custom_harness.experience import schemas as M
from custom_harness.kernel import Kernel
from custom_harness.lab import session as S
from custom_harness.lab import spending
from custom_harness.lab import trace as T
from custom_harness.lab.agents import AgentOptions
from custom_harness.store import EvidenceStore


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "w")
    yield world
    world.close()


# ---------------------------------------------------------------- settings
def test_settings_default_to_the_owners_model_and_a_limit(data):
    assert settings.load()["lab_default_model"] == "claude-haiku-5-5"
    assert settings.save(session_spend_limit_usd=2.5)["session_spend_limit_usd"] == 2.5
    assert settings.load()["session_spend_limit_usd"] == 2.5


@pytest.mark.parametrize("bad", ['{"session_spend_limit_usd": -1}', '{"session_spend_limit_usd": "9"}',
                                 '{"session_spend_limit_usd": true}', '{"anything": 1}', "not json", "[]"])
def test_a_damaged_settings_file_is_refused_never_replaced_by_a_default(data, bad):
    settings.settings_file().write_text(bad, encoding="utf-8")
    with pytest.raises(settings.SettingsRefused):
        settings.load()


# ---------------------------------------------------------------- spending
def cost(w, sid, usd):
    T.record(w.store, sid, "cost", {"agent": "a", "model": "m", "usd": usd, "reported_by": "agent-cli"},
             actor=T.Actor(type="agent", id="a"))


def test_a_session_at_its_limit_makes_no_call(w):
    w.seal()
    w.session()
    cost(w, "s-exp", settings.SESSION_SPEND_LIMIT_USD)
    calls = len(w.fake.calls)
    with pytest.raises(Refused) as e:
        w.x.explore(M.ExploreIn(session_id="s-exp", prompt="go on"))
    assert e.value.code == "SpendingRefused" and "limit" in str(e.value)
    assert len(w.fake.calls) == calls  # nothing reached the box


def test_the_steer_route_is_stopped_at_the_limit_and_the_story_keeps_the_words(w):
    w.seal()
    w.session()
    cost(w, "s-exp", settings.SESSION_SPEND_LIMIT_USD + 1)
    calls = len(w.fake.calls)
    with pytest.raises(Refused):
        w.x.steer(M.SteerIn(session_id="s-exp", text="try again"))
    assert len(w.fake.calls) == calls
    assert any(e["payload"].get("op") == "steer" for e in T.events(w.store, "s-exp", "human"))


def test_a_warning_comes_before_the_limit_and_unknown_is_never_zero(w):
    w.seal()
    w.session()
    assert spending.spend(w.store, "s-exp").state == "ok"
    cost(w, "s-exp", settings.SESSION_SPEND_LIMIT_USD * 0.85)
    s = spending.spend(w.store, "s-exp")
    assert s.state == "warn" and "may go over" in s.sentence
    assert "$4.25" in w.x.lab("s-exp").spending


def test_one_unknown_cost_does_not_hide_the_known_spend(w):
    w.seal()
    w.session()
    cost(w, "s-exp", settings.SESSION_SPEND_LIMIT_USD)
    cost(w, "s-exp", None)
    assert spending.spend(w.store, "s-exp").state == "stop"


def test_too_many_calls_of_unknown_cost_stop_the_session(w):
    w.seal()
    w.session()
    for _ in range(settings.UNKNOWN_COST_CALLS + 1):
        cost(w, "s-exp", None)
    s = spending.spend(w.store, "s-exp")
    assert s.state == "stop" and "unknown" in s.sentence


def test_every_prompt_route_passes_the_spending_check():
    src = Path(S.__file__).read_text(encoding="utf-8")
    body = src[src.index("def run_prompt"):]
    assert body.index("require_room") < body.index("lb.docker.call") and body.index("require_room") < body.index("_acp_prompt")


# ---------------------------------------------------------------- the default model
def start(w, agent, model=None, **kw):
    return S.start(w.store, agent, AgentOptions(model=model, **kw), None, session_id="s-model", docker=w.x.docker)


def test_a_claude_session_with_no_model_gets_the_cheap_default_and_records_it(w):
    from custom_harness.lab.credentials import CredentialSpec
    cred = CredentialSpec(registry_key="anthropic-api", box_env="UNSET", spend_cap_usd=1.0, source_env="CHH_LAB_CRED_X",
                          scope="test")
    rec = S.start(w.store, "claude-code", AgentOptions(registry_key="anthropic-api"), cred, session_id="s-model",
                  docker=w.x.docker, environ={"CHH_LAB_CRED_X": "fake-value-not-a-key"})
    assert rec["options"]["model"] == "claude-haiku-5-5"
    assert "claude-haiku-5-5" in json.dumps(rec["plan"])


def test_a_named_model_wins_and_other_agents_are_left_alone():
    from dataclasses import replace
    o = AgentOptions(model="other")
    assert replace(o).model == "other"
    src = Path(S.__file__).read_text(encoding="utf-8")
    assert 'if agent == "claude-code" and options.model is None:' in src


# ---------------------------------------------------------------- backup and restore
def real_case(w):
    w.seal()
    w.session()
    pid = w.freeze()
    case_id = w.x.promote(M.PromoteIn(promotion_id=pid, route="sealed", project="cohort-1")).case_id
    from custom_harness.court import minted as C
    w.approve_role(C.case(w.store, case_id)["target"])
    w.x.run_case(case_id)
    return case_id


def test_backup_and_restore_keep_the_record_and_the_sealed_answers(w, tmp_path):
    case_id = real_case(w)
    out = B.backup(w.store, tmp_path / "backups", "owner")
    assert B.verify(Path(out["path"]))["ok"] and out["ledger_entries"] > 0
    B.restore(Path(out["path"]), tmp_path / "restored", "owner")
    st = EvidenceStore(tmp_path / "restored")
    try:
        from custom_harness.court import display as D
        assert D.is_case(st, case_id)
        k = Kernel(st)
        assert len(k.ledger.entries()) == out["ledger_entries"]
        assert (tmp_path / "restored" / "private").is_dir()
    finally:
        st.close()


def test_a_changed_backup_is_refused(w, tmp_path):
    w.seal()
    path = Path(B.backup(w.store, tmp_path / "b", "owner")["path"])
    victim = next(p for p in (path / "blobs").rglob("*") if p.is_file())
    victim.write_bytes(victim.read_bytes() + b"x")
    with pytest.raises(B.BackupRefused, match="changed"):
        B.verify(path)
    with pytest.raises(B.BackupRefused):
        B.restore(path, tmp_path / "restored", "owner")
    assert not (tmp_path / "restored").exists()


def test_a_changed_ledger_row_is_refused_even_with_matching_file_hashes(w, tmp_path):
    w.seal()
    path = Path(B.backup(w.store, tmp_path / "b", "owner")["path"])
    db = sqlite3.connect(path / B.DB)
    assert db.execute("UPDATE ledger SET body = replace(body, 'e', 'E') WHERE seq = 1").rowcount == 1
    db.commit()
    db.close()
    doc = json.loads((path / B.MANIFEST).read_text(encoding="utf-8"))
    doc["files"][B.DB] = B._sha(path / B.DB)
    (path / B.MANIFEST).write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(B.BackupRefused, match="ledger"):
        B.verify(path)


def test_a_backup_cannot_go_where_a_lab_reads_or_inside_the_store(w, tmp_path):
    from custom_harness.lab.walls import lab_root
    with pytest.raises(B.BackupRefused, match="Lab"):
        B.backup(w.store, lab_root() / "x", "owner")
    with pytest.raises(B.BackupRefused, match="inside the store"):
        B.backup(w.store, Path(w.store.root) / "x", "owner")


def test_a_restore_never_writes_over_a_store(w, tmp_path):
    path = Path(B.backup(w.store, tmp_path / "b", "owner")["path"])
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x", encoding="utf-8")
    with pytest.raises(B.BackupRefused, match="not empty"):
        B.restore(path, tmp_path / "full", "owner")


@pytest.mark.parametrize("name", ["claude", "", "opencode-agent"])
def test_an_agent_cannot_take_a_store_step(w, tmp_path, name):
    with pytest.raises(B.BackupRefused):
        B.backup(w.store, tmp_path / "b", name)


# ---------------------------------------------------------------- upgrades
def test_an_upgrade_backs_up_first_and_runs_once(w, tmp_path):
    root = Path(w.store.root)
    assert B.store_format(root) == 0 and B.upgrade_status(root) == "older"
    before = len(Kernel(w.store).ledger.entries())
    got = B.upgrade(w.store, tmp_path / "b", "owner")
    assert got["to"] == B.STORE_FORMAT and B.verify(Path(got["backup"]))["ok"]
    assert B.upgrade_status(root) == "current" and len(Kernel(w.store).ledger.entries()) == before
    assert B.upgrade(w.store, tmp_path / "b", "owner")["backup"] is None


def test_a_failed_step_leaves_the_store_at_the_old_format(w, tmp_path, monkeypatch):
    def boom(db):
        raise sqlite3.OperationalError("no")
    monkeypatch.setitem(B.STEPS, 1, boom)
    with pytest.raises(sqlite3.OperationalError):
        B.upgrade(w.store, tmp_path / "b", "owner")
    assert B.store_format(Path(w.store.root)) == 0


def test_a_store_newer_than_the_code_is_refused_and_untouched(w, tmp_path):
    db = sqlite3.connect(Path(w.store.root) / B.DB)
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    with pytest.raises(B.BackupRefused, match="newer"):
        B.upgrade(w.store, tmp_path / "b", "owner")
    assert not (tmp_path / "b").exists() and B.store_format(Path(w.store.root)) == 99
