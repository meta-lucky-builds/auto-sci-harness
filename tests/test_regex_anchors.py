"""Trailing-newline class (b-research's finding, generalised 2026-10-08): in Python `$` also matches just BEFORE a
final "\\n", so `re.compile(r"^...$").match("ok\\n")` accepts a value with a trailing newline. Every validator of an
untrusted id below anchors with `\\Z` instead. Each case: a valid value is accepted, the same value plus "\\n" is
refused. (Patterns used only with `.fullmatch`, and pydantic `pattern=` constraints, are not affected and are not
listed; tests/test_regex_anchors.py::test_pydantic_patterns_refuse_a_trailing_newline pins the latter.)"""

from __future__ import annotations

import importlib
import json

import pytest

CASES = [
    ("custom_harness.figures.audit", "_REF_ID", "fig_1"),
    ("custom_harness.kerneld.protocol", "HEX64", "a" * 64),
    ("custom_harness.kerneld.releases", "NOTE", "evolve.gate:decision"),
    ("custom_harness.literature.fulltext", "_PMCID", "PMC123"),
    ("custom_harness.literature.records", "_DOI", "10.1234/abc"),
    ("custom_harness.literature.records", "_ARXIV_NEW", "2101.00001"),
    ("custom_harness.literature.records", "_ARXIV_OLD", "hep-th/9901001"),
    ("custom_harness.literature.records", "_PMID", "pmid:123"),
    ("custom_harness.literature.records", "_DATE", "2020-01-02"),
    ("custom_harness.literature.retrieval", "_SESSION", "s1"),
    ("custom_harness.memory.store", "_NAMESPACE", "ns"),
    ("custom_harness.memory.store", "_CATEGORY", "a/b"),
    ("custom_harness.memory.store", "_HEX64", "a" * 64),
    ("custom_harness.scorecard.registry", "_HEX64", "a" * 64),
    ("custom_harness.orchestration.brief", "_SLUG", "abc"),
    ("custom_harness.orchestration.brief", "_BRANCH", "b-research"),
    ("custom_harness.orchestration.brief", "_RESOURCE", "gpu:0"),
]


@pytest.mark.parametrize(("module", "name", "valid"), CASES, ids=[f"{m.rsplit('.', 1)[1]}.{n}" for m, n, _ in CASES])
def test_id_validators_refuse_a_trailing_newline(module, name, valid):
    pat = getattr(importlib.import_module(module), name)
    assert pat.match(valid)  # not vacuous: the valid value passes
    assert pat.match(valid + "\n") is None
    assert pat.search(valid + "\n") is None


def test_kerneld_launch_secret_with_a_trailing_newline_is_refused():
    """The behaviour behind one of the cases: a launch line whose secret ends in a newline is a protocol error."""
    from custom_harness.kerneld import protocol as P
    assert P.decode_launch(json.dumps({"launch_secret": "a" * 64})) == "a" * 64
    with pytest.raises(P.ProtocolError):
        P.decode_launch(json.dumps({"launch_secret": "a" * 64 + "\n"}))


def test_pydantic_patterns_refuse_a_trailing_newline():
    """pydantic `pattern=` constraints run on pydantic-core's regex engine, where `$` is the end of the text: pinned."""
    from pydantic import TypeAdapter, ValidationError

    from custom_harness.memory.model import HEX64, CertHash  # noqa: F401
    ta = TypeAdapter(CertHash)
    assert ta.validate_python("a" * 64)
    with pytest.raises(ValidationError):
        ta.validate_python("a" * 64 + "\n")
