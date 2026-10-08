"""Tests written because a museum patch SURVIVED (GOAL §16.6, invariant 19): the old test no longer attacked."""

from __future__ import annotations

import pytest

from custom_harness.research.claims import _hypotheses


@pytest.mark.parametrize("state", [
    {"hypotheses": "a string, not a mapping"},
    {"hypotheses": {"hypotheses": "not a list"}},
    {"hypotheses": {"hypotheses": [1, "x", None, {"id": "h1"}]}},
    {"hypotheses": None},
    {},
])
def test_e5_trusted_module_parses_malformed_evolvable_hypotheses_without_crashing(state):
    """E5 (STATUS §A): trusted modules crashed on malformed evolvable state. The museum patch re-introducing it
    survived every existing test, so this one pins the defensive parse directly."""
    got = _hypotheses(state)
    assert isinstance(got, list) and all(isinstance(h, dict) for h in got)
