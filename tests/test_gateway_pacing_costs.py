"""Model gateway additions (GOAL §5 L0): per-host pacing, cost tables (UNCALIBRATED placeholders), provider registry
checks. Mock backends only; no network."""

import datetime as dt
import threading

import pytest

from custom_harness.gateway import ChatRequest, Message, MockBackend, load_registry
from custom_harness.gateway.costs import (
    COST_TABLE,
    CostedBackend,
    Rate,
    UnknownRate,
    apply_rate,
    rate_for,
    registry_problems,
    release_problems,
)
from custom_harness.gateway.pacing import HostPacer, PacedBackend, host_of
from custom_harness.records import Cost

REQ = ChatRequest(messages=[Message(role="user", content="hello there")])


class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


# -------------------------------------------------------------------------------------------- pacing
def test_min_interval_is_enforced_per_host():
    clk = FakeClock()
    p = HostPacer({"api.example.com": 2.0}, default_s=0.5, clock=clk, sleep=clk.sleep)
    assert p.acquire("api.example.com") == 0
    assert p.acquire("api.example.com") == pytest.approx(2.0)  # back-to-back: waits the full interval
    clk.t += 0.5
    assert p.acquire("api.example.com") == pytest.approx(1.5)  # waits only what remains
    assert p.acquire("other.host") == 0  # another host is not held up
    assert p.acquire("other.host") == pytest.approx(0.5)  # default interval for hosts without an entry
    clk.t += 10
    assert p.acquire("API.EXAMPLE.COM") == 0  # host keys are case-insensitive; idle long enough: no wait
    assert clk.slept == pytest.approx([2.0, 1.5, 0.5])


def test_concurrent_callers_to_one_host_are_spaced():
    clk = FakeClock()
    lock = threading.Lock()

    def sleep(s):  # threads only record; start times come from the reserved slots
        with lock:
            clk.slept.append(s)

    p = HostPacer({}, default_s=1.0, clock=clk, sleep=sleep)
    waits: list[float] = []
    ts = [threading.Thread(target=lambda: waits.append(p.acquire("h"))) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(waits) == pytest.approx([0.0, 1.0, 2.0, 3.0, 4.0])  # each start at least 1 s after the previous


def test_paced_backend_waits_before_each_call_and_keeps_identity():
    clk = FakeClock()
    pacer = HostPacer({"localhost:11434": 3.0}, clock=clk, sleep=clk.sleep)
    inner = MockBackend(["a", "b"])
    b = PacedBackend(inner, host="http://LocalHost:11434/v1", pacer=pacer)
    assert b.host == "localhost:11434" and b.name == inner.name and b.mode == inner.mode
    b.complete(REQ)
    b.complete(REQ)
    assert clk.slept == [3.0] and b.waited_s == 3.0 and len(inner.requests) == 2


@pytest.mark.parametrize("bad", [-1, float("nan"), True, "1"])
def test_pacing_intervals_must_be_valid(bad):
    with pytest.raises(ValueError):
        HostPacer({"h": bad})


def test_host_of():
    assert host_of("https://api.openai.com/v1/chat") == "api.openai.com"
    assert host_of("http://127.0.0.1:8000") == "127.0.0.1:8000"
    assert host_of("Example.org") == "example.org"
    with pytest.raises(ValueError):
        host_of("http:///nohost")


# -------------------------------------------------------------------------------------------- cost tables
def test_every_bundled_rate_is_a_placeholder_without_invented_prices():
    assert COST_TABLE
    for key, rate in COST_TABLE.items():
        assert rate.placeholder, key
        assert rate.usd_per_mtok_in is None and rate.usd_per_mtok_out is None, key
    with pytest.raises(ValueError, match="never invented"):
        Rate(usd_per_mtok_in=1.0, usd_per_mtok_out=2.0)  # a placeholder may not carry a price
    with pytest.raises(ValueError, match="source"):
        Rate(usd_per_mtok_in=1.0, usd_per_mtok_out=2.0, placeholder=False)


def test_rate_lookup_exact_then_provider_row_and_unknown_fails_closed():
    exact = Rate(quota_per_call=2.0, usd_per_mtok_in=1.0, usd_per_mtok_out=4.0, placeholder=False, source="owner")
    table = {("p", "m1"): exact, ("p", "*"): Rate(quota_per_call=1.0)}
    assert rate_for("p", "m1", table) is exact
    assert rate_for("p", "m2", table).quota_per_call == 1.0
    with pytest.raises(UnknownRate):
        rate_for("q", "m1", table)  # no silent default rate


def test_apply_rate_fills_only_unreported_units():
    placeholder = Rate(quota_per_call=1.0)
    c = apply_rate(Cost(prompt_tokens=1000, completion_tokens=500), placeholder)
    assert c.quota_units == 1.0 and c.usd == 0.0 and c.estimated is True  # no usd from a placeholder
    reported = Cost(prompt_tokens=10, quota_units=1.0, usd=0.5)
    assert apply_rate(reported, placeholder) == reported  # never added on top of what the backend reported
    real = Rate(usd_per_mtok_in=2.0, usd_per_mtok_out=8.0, placeholder=False, source="owner sheet")
    c2 = apply_rate(Cost(prompt_tokens=1_000_000, completion_tokens=500_000), real)
    assert c2.usd == pytest.approx(6.0) and c2.estimated is False


def test_costed_backend_applies_rates_and_refuses_unknown_providers_before_calling():
    inner = MockBackend(["ok"])
    resp = CostedBackend(inner).complete(REQ)
    assert resp.cost.quota_units == 1.0 and resp.cost.estimated is True
    inner2 = MockBackend(["ok"])
    with pytest.raises(UnknownRate):
        CostedBackend(inner2, table={("openai", "*"): Rate(quota_per_call=1.0)}).complete(REQ)
    assert inner2.requests == []  # refused before any spend


# -------------------------------------------------------------------------------------------- registry checks
def test_bundled_registry_is_well_formed_but_not_release_ready():
    reg = load_registry()
    assert registry_problems(reg) == []
    problems = release_problems(reg, dt.date(2026, 10, 8))
    # honest state: no usable entry has a human verifier yet
    for key in ("openai-api", "openai-codex-cli", "anthropic-api", "local-openai-compatible"):
        assert any(p.startswith(f"{key}: not verified by a human") for p in problems), key
    assert not any(p.startswith("anthropic-subscription") for p in problems)  # prohibited entries are not usable


@pytest.mark.parametrize("entry,needle", [
    ({"mode": "api", "byos_status": "n/a", "training_on_outputs": "forbidden"}, "missing provider"),
    ({"provider": "x", "mode": "cloud", "byos_status": "n/a", "training_on_outputs": "unknown"}, "mode"),
    ({"provider": "x", "mode": "byos", "training_on_outputs": "unknown"}, "byos_status"),
    ({"provider": "x", "mode": "api", "byos_status": "allowed", "training_on_outputs": "unknown"}, "'n/a'"),
    ({"provider": "x", "mode": "api", "byos_status": "n/a", "training_on_outputs": "maybe"}, "training_on_outputs"),
    ({"provider": "x", "mode": "byos", "byos_status": "allowed", "training_on_outputs": "unknown"}, "auth_flow"),
    ({"provider": "x", "mode": "ow", "byos_status": "n/a", "training_on_outputs": "unknown",
      "verified_on": "last week"}, "not a date")])
def test_malformed_registry_entries_are_reported(entry, needle):
    problems = registry_problems({"e": entry})
    assert any(needle in p for p in problems), problems


def test_release_check_requires_recent_human_verification_and_a_cost_row():
    ok = {"provider": "openai", "mode": "api", "byos_status": "n/a", "training_on_outputs": "forbidden",
          "verified_by": "owner", "verified_on": dt.date(2026, 9, 1)}
    today = dt.date(2026, 10, 8)
    assert release_problems({"e": ok}, today) == []
    assert any("days ago" in p for p in release_problems({"e": {**ok, "verified_on": dt.date(2025, 1, 1)}}, today))
    assert any("future" in p for p in release_problems({"e": {**ok, "verified_on": dt.date(2027, 1, 1)}}, today))
    assert any("not verified by a human" in p for p in release_problems({"e": {**ok, "verified_by": " "}}, today))
    assert any("no cost-table row" in p for p in release_problems({"e": {**ok, "provider": "newco"}}, today))
