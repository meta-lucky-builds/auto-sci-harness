"""s3-verifiers review M1: `hypotheses` and `memory` are read by trusted code (claims._hypotheses idea text in reports,
the skill writer, recall_memory's quoted context), so no added or changed node may start writing them.

Fail-first reasoning (NOT run: owner rule): at 82719e1 neither key was in RESERVED_OUTPUT_KEYS and
research.recall_memory was not in RESERVED_WRITING_MODULES, so the first three attempts
below were ACCEPTED by apply_edit.
"""

from __future__ import annotations

import pytest

from custom_harness.evolve.edits import (
    RESERVED_OUTPUT_KEYS,
    RESERVED_WRITING_MODULES,
    Edit,
    EditOp,
    EditRejected,
    EditType,
    apply_edit,
)
from test_evolve import seed_spec
from test_evolve_hardening import _insert_between


def _llm(key: str) -> dict:
    return {"kind": "llm", "prompt": "say something", "output_key": key}


@pytest.mark.parametrize("key", ["hypotheses", "memory"])
def test_no_added_node_may_write_hypotheses_or_memory(key: str) -> None:
    assert key in RESERVED_OUTPUT_KEYS
    spec = seed_spec()
    ops = _insert_between(spec, "hypothesize", "tournament", "x1", _llm(key))
    with pytest.raises(EditRejected, match="reserved"):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=ops))


def test_no_added_node_may_run_recall_memory() -> None:
    assert "research.recall_memory" in RESERVED_WRITING_MODULES
    spec = seed_spec()
    node = {"kind": "module", "module": "research.recall_memory"}
    ops = _insert_between(spec, "hypothesize", "tournament", "x2", node)
    with pytest.raises(EditRejected):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=ops))


def test_the_existing_hypothesize_node_stays_editable() -> None:
    """Its prompt is the proposer's main surface: keeping its own output key under its own id is allowed."""
    spec = seed_spec()
    prompt = spec.nodes["hypothesize"].prompt  # type: ignore[union-attr]
    op = EditOp(op="replace", path="/nodes/hypothesize/prompt", value=prompt + "\nBe bold.")
    new = apply_edit(spec, Edit(type=EditType.PROMPT_TEXT, rationale="x", ops=[op]))
    assert new.nodes["hypothesize"].output_key == "hypotheses"  # type: ignore[union-attr]


def test_another_node_may_not_take_over_hypotheses() -> None:
    spec = seed_spec()
    other = "implement"  # an evolvable LLM node writing its own key (impl)
    with pytest.raises(EditRejected, match="reserved"):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x",
                              ops=[EditOp(op="replace", path=f"/nodes/{other}/output_key", value="hypotheses")]))
