"""The Lab network rule in the session code (GOAL 18.6, lab/egress.py, lab/box.py), with the fake docker client: the box
sits only on its internal network, that network is verified before the box exists, and gateway records reach the
discovery trace. tests/test_lab_network_real.py runs the same objects on real Docker."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from _lab_fakes import FakeDocker, docker
from custom_harness.lab import egress as G
from custom_harness.lab import session as S
from custom_harness.lab import trace as T
from custom_harness.lab.agents import AgentOptions, adapter
from custom_harness.lab.box import BoxSpec, LabBox, host_allow, run_argv
from custom_harness.lab.walls import docker_argv_problems
from custom_harness.store import EvidenceStore

SHA = "a" * 64


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CHH_LAB_ROOT", str(tmp_path / "data" / "lab"))
    store = EvidenceStore(tmp_path / "data" / "store")
    yield store, FakeDocker()
    store.close()


def _local_options(port: int = 11434) -> AgentOptions:
    return AgentOptions(model="m", local_base_url=f"http://host.docker.internal:{port}/v1")


def _start(store, fake, sid="s-net-1", **kw):
    return S.start(store, "opencode", _local_options(), None, session_id=sid, docker=docker(fake), **kw)


def test_order_is_network_then_gateway_then_box(world):
    store, fake = world
    _start(store, fake)
    subs = [(c[0][1], c[0][2]) for c in fake.calls]
    create = subs.index(("network", "create"))
    gw = next(i for i, c in enumerate(fake.calls) if c[0][1] == "run")
    box = fake.calls.index(fake.run_call())
    assert create < gw < box
    assert "--internal" in fake.calls[create][0]


def test_the_box_is_on_its_internal_network_only_with_no_host_alias(world):
    store, fake = world
    _start(store, fake)
    a = fake.run_call()[0]
    assert a[a.index("--network") + 1] == "chh-lab-net-s-net-1"
    assert "--add-host" not in a and "bridge" not in a
    assert "HTTPS_PROXY=http://chh-lab-gw-s-net-1:3128" in a


def test_a_network_that_is_not_internal_stops_the_start_before_any_box(world):
    store, fake = world
    fake.internal = "false"
    with pytest.raises(RuntimeError, match="not internal"):
        _start(store, fake)
    assert not [c for c in fake.calls if c[0][1] == "run"]
    assert S.events(store, "s-net-1")[-1]["event"] == "start_failed"


def test_the_gateway_argv_is_locked_down_and_mounts_nothing():
    a = G.gateway_argv("docker", "sid", "img", ())
    for flag in ("--read-only", "--cap-drop"):
        assert flag in a
    assert a[a.index("--user") + 1] == "1000:1000" and "-v" not in a and "--mount" not in a
    assert "--add-host" not in a
    assert a[-4:-1] == ["python", "-I", "-c"] and a[-1] == G.gateway_source()


def test_only_the_named_host_port_is_allowed(world):
    plan = adapter("opencode").plan(_local_options(1234))
    assert host_allow(plan) == ("host.docker.internal:1234",)
    a = G.gateway_argv("docker", "sid", "img", host_allow(plan))
    assert "CHH_EGRESS_ALLOW=host.docker.internal:1234" in a


def test_a_cloud_agent_gets_no_host_port():
    plan = adapter("opencode").plan(_local_options())
    assert host_allow(replace(plan, host_gateway=False)) == ()
    cloud = adapter("opencode").plan(AgentOptions(model="m", registry_key=sorted(adapter("opencode").credentials)[-1]))
    assert cloud.host_gateway is False and host_allow(cloud) == ()


def test_the_real_box_argv_passes_the_walls_and_bridge_does_not(world, tmp_path):
    plan = adapter("opencode").plan(_local_options())
    ws = tmp_path / "ws"
    a = run_argv("docker", "s-x", ws, BoxSpec(), plan)
    env = {**plan.env, **G.proxy_env("s-x")}
    assert docker_argv_problems(a, workspace=ws, allowed_env=env, network="chh-lab-net-s-x") == []
    b = [("bridge" if x == "chh-lab-net-s-x" else x) for x in a]
    assert docker_argv_problems(b, workspace=ws, allowed_env=env, network="chh-lab-net-s-x")


def test_stop_removes_box_then_gateway_then_network(world):
    store, fake = world
    _start(store, fake)
    S.stop(store, "s-net-1", docker=docker(fake))
    order = [c[0] for c in fake.calls]
    logs = next(i for i, a in enumerate(order) if a[1] == "logs")
    box = next(i for i, a in enumerate(order) if a[:3] == ["docker", "rm", "-f"] and a[3] == "chh-lab-s-net-1")
    gw = next(i for i, a in enumerate(order) if a[:3] == ["docker", "rm", "-f"] and a[3] == "chh-lab-gw-s-net-1")
    net = next(i for i, a in enumerate(order) if a[1:3] == ["network", "rm"])
    assert logs < box < gw < net


def _line(seq: int, **kw) -> str:
    return json.dumps({"seq": seq, "at": 1.0, "ended": 2.0, "method": "CONNECT", "host": "pypi.org", "port": 443,
                       "ip": "1.1.1.1", "status": "ok", "up": 5, "down": 9, **kw})


def _web(store, sid="s-net-1"):
    return [e["payload"] for e in T.events(store, sid, "web")]


def test_gateway_lines_reach_the_trace_once_and_gaps_are_recorded(world):
    store, fake = world
    _start(store, fake)
    fake.log = _line(1) + "\n" + _line(2, method="GET", url="http://x.org/a", response_sha256=SHA) + "\n"
    d = docker(fake)
    assert G.collect_web(store, "s-net-1", d)["web_events"] == 2
    assert G.collect_web(store, "s-net-1", d)["web_events"] == 0  # already recorded
    fake.log = _line(1) + "\n" + _line(2) + "\n" + _line(5) + "\nnot json\n"
    G.collect_web(store, "s-net-1", d)
    rows = _web(store)
    assert [r["op"] for r in rows] == ["connection", "connection", "gap", "connection"]
    assert rows[2]["missing"] == 2 and rows[3]["seq"] == 5
    assert rows[1]["url"] == "http://x.org/a" and rows[1]["response_sha256"] == SHA
    assert T.verify_chain(store, "s-net-1") == []


def test_a_denied_connection_is_in_the_story(world):
    store, fake = world
    _start(store, fake)
    fake.log = _line(1, status="denied", reason="192.168.1.1: not a public internet address") + "\n"
    G.collect_web(store, "s-net-1", docker(fake))
    assert _web(store)[0]["status"] == "denied"


@pytest.mark.parametrize("field", ["url", "host"])
def test_a_key_shaped_name_goes_by_hash_and_never_blocks_a_freeze(world, field):
    store, fake = world
    _start(store, fake)
    key = "sk-" + "a" * 30
    fake.log = _line(1, **{field: f"http://{key}.evil.example/" if field == "url" else f"{key}.evil.example"}) + "\n"
    assert G.collect_web(store, "s-net-1", docker(fake))["web_events"] == 1
    row = _web(store)[0]
    assert key not in json.dumps(row) and len(row["url_sha256"]) == 64 and row["up"] == 5


def test_an_unreadable_log_is_recorded_not_ignored(world):
    store, fake = world
    _start(store, fake)
    fake.log_rc = 1
    assert G.collect_web(store, "s-net-1", docker(fake))["log"] == "unavailable"
    assert _web(store)[0]["op"] == "log_unavailable"


def test_an_agent_cannot_write_web_events(world, tmp_path):
    store, fake = world
    _start(store, fake)
    ws = S.workspace_of("s-net-1")
    (ws / ".lab").mkdir()
    (ws / ".lab" / "trace.jsonl").write_text(json.dumps({"kind": "web", "client_id": "c1", "payload": {
        "op": "connection", "seq": 9, "host": "fake.example", "status": "ok"}}) + "\n", encoding="utf-8")
    T.ingest_agent_events(store, "s-net-1", Path(ws), T.Actor(type="agent", id="x"))
    assert _web(store) == []
    with pytest.raises(T.TraceRefused):
        T.record(store, "s-net-1", "web", {"op": "gap"}, actor=T.human(), source="human")


def test_collect_trace_brings_web_traffic_in(world):
    store, fake = world
    rec = _start(store, fake)
    fake.log = _line(1) + "\n"
    got = S.collect_trace(store, "s-net-1", rec, LabBox("s-net-1", docker(fake)))
    assert got["web_events"] == 1


def test_the_session_record_states_the_rule(world):
    store, fake = world
    rec = _start(store, fake)
    n = rec["network"]
    assert n["network"] == "chh-lab-net-s-net-1" and n["gateway_sha256"] == G.gateway_sha256()
    assert n["host_allow"] == ["host.docker.internal:11434"]
