"""s3-verifiers review M4 (invariant 20): every look at a synthesis suite's sealed half is recorded before it is taken,
looks are budgeted per problem, and the synthesis module is human-only (no evolve edit may add or change its node).

Fail-first reasoning (NOT run: owner rule): at 82719e1 research/verifier_synthesis.py validated against the sealed half
with no record and no budget (record_sealed_look did not exist: ImportError), usable_tier recomputed the tier of
any stored verifier, and "research.synthesize_verifier" was not in HUMAN_ONLY_MODULE_PREFIXES.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_harness.research import verifier_synthesis as VS
from custom_harness.store import EvidenceStore


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.store = EvidenceStore(root)
        self.run_id = "run-1"


def test_looks_are_recorded_once_per_verifier_and_budgeted(tmp_path: Path) -> None:
    ctx = _Ctx(tmp_path / "store")
    assert VS.record_sealed_look(ctx, "p", "v0")  # type: ignore[arg-type]
    assert VS.record_sealed_look(ctx, "p", "v0")  # type: ignore[arg-type]  # same verifier: not counted again
    assert len(ctx.store.records(VS.LOOK_KIND)) == 1
    for i in range(1, VS.SYNTH_MAX_SEALED_LOOKS):
        assert VS.record_sealed_look(ctx, "p", f"v{i}")  # type: ignore[arg-type]
    assert not VS.record_sealed_look(ctx, "p", "one-too-many")  # type: ignore[arg-type]
    assert VS.record_sealed_look(ctx, "other-problem", "v0")  # type: ignore[arg-type]  # budgets are per problem
    assert len(ctx.store.records(VS.LOOK_KIND)) == VS.SYNTH_MAX_SEALED_LOOKS + 1


def test_synthesis_node_is_human_only() -> None:
    from custom_harness.evolve.edits import Edit, EditRejected, EditType, apply_edit
    from test_evolve import seed_spec
    from test_evolve_hardening import _insert_between

    spec = seed_spec()
    ops = _insert_between(spec, "hypothesize", "tournament", "synth",
                          {"kind": "module", "module": "research.synthesize_verifier", "params": {"problem": "sort"}})
    with pytest.raises(EditRejected):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=ops))
