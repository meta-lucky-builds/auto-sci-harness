"""No held-out estimate or interval enters memory (invariant 18; s4-station finding, orchestrator decision 2026-10-15).

Memory is rendered into later runs' prompts (research.recall_memory) without a release, so negative memory keeps
only the verdict, the failing check codes and the pre-registered claim fields, and every write is screened for
held-out value fields and for a cited certificate's held-out estimate/bounds quoted in its text.

Fail-first reasoning (NOT run: owner rule): at 82719e1 negative_proposal wrote "Held-out: <target>: estimate x
[lo, hi]" into the text and the same numbers into data["targets"], the store had no such screen, and render showed
old entries' values; `chh memory harvest-negatives` built a Kernel with no replayer.
"""

from __future__ import annotations

import pytest

from custom_harness.memory import Kind, MemoryRefused, negative_proposal
from custom_harness.memory.store import HELDOUT_VALUE_KEYS, heldout_value_problems
from test_memory import MW, P, store


@pytest.fixture(scope="module")
def world(tmp_path_factory):  # the memory fixture world of tests/test_memory.py
    m = MW(tmp_path_factory.mktemp("memory-heldout"))
    yield m
    m.w.close()


def _resolution_values(mw: MW, h: str) -> list[float]:
    cert = mw.kernel.certificate(h)
    return [float(s[k]) for s in (cert.resolution.get("targets") or {}).values() for k in ("estimate", "lo", "hi")
            if isinstance(s.get(k), (int, float))]


def test_negative_memory_carries_no_heldout_value(world: MW) -> None:
    mw = world
    prop = negative_proposal(mw.kernel, mw.N)
    assert prop is not None
    assert "Held-out:" not in prop.text and "estimate" not in prop.text
    assert not (set(map(str.lower, map(str, prop.data))) & HELDOUT_VALUE_KEYS)
    assert isinstance(prop.data["targets"], list)  # target ids only
    assert prop.data["verdict"] == "REJECTED" and prop.data["failed"] == ["K6"]
    assert heldout_value_problems(mw.kernel, prop) == []
    assert _resolution_values(mw, mw.N), "the fixture's failure must have held-out values for this test to bite"


def test_a_write_quoting_a_heldout_value_is_refused(world: MW) -> None:
    mw = world
    s = store(mw)
    x = _resolution_values(mw, mw.N)[0]
    with pytest.raises(MemoryRefused, match="invariant 18"):
        s.propose(P(Kind.NEGATIVE, "x", (mw.N,), text=f"we saw {x!r} on held-out"), writer="w")
    with pytest.raises(MemoryRefused, match="held-out value field"):
        s.propose(P(Kind.NEGATIVE, "y", (mw.N,), data={"targets": {"t": {"estimate": 0.5}}}), writer="w")


def test_harvest_cli_fails_closed_without_a_replayer(tmp_path, monkeypatch) -> None:
    from custom_harness import cli

    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(SystemExit, match="re-derives"):
        cli.main(["memory", "harvest-negatives"])
