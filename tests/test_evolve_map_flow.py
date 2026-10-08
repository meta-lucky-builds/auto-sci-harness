"""R2 evolve gaps for maps (STATUS §6.2): the evolve information-flow rules (R2-5 generalised, the tool-node taint
rule) must also hold through a MapNode. A map renders `items_key` into its body's templates under `item_key`, and a
map whose body is a tool node carries tool output into its own `output_key`. Without these checks an edit could route
tool output or private / host state into a model prompt through a map."""

from __future__ import annotations

import pytest

from custom_harness.evolve.edits import (
    Edit,
    EditOp,
    EditRejected,
    EditType,
    _apply_op,
    _check_information_flow,
    apply_edit,
)
from custom_harness.runtime import HarnessSpec
from test_evolve import seed_spec
from test_evolve_hardening import _insert_between

TOOL = {"kind": "tool", "command": ["python", "-V"], "output_key": "tool_check"}
LLM_BODY = {"kind": "llm", "prompt": "Think about {{item}}", "output_key": "mb_out"}


def _map(items_key: str, body: str = "mb", output_key: str = "mapped") -> dict:
    return {"kind": "map", "items_key": items_key, "body": body, "output_key": output_key, "reducer": "collect"}


def _edit(spec: HarnessSpec, map_node: dict, body: dict, *, with_tool: bool = True, extra=()) -> Edit:
    ops = [*(_insert_between(spec, "hypothesize", "tournament", "t1", TOOL) if with_tool else []),
           *_insert_between(spec, "check_answers", "next", "m1", map_node),
           EditOp(op="add", path=f"/nodes/{map_node['body']}", value=body), *extra]
    return Edit(type=EditType.TOOL, rationale="x", ops=ops)


def _unchecked(spec: HarnessSpec, edit: Edit) -> HarnessSpec:
    """The edited spec built WITHOUT apply_edit's checks (spec validation still runs)."""
    doc = spec.model_dump(mode="json")
    for op in edit.ops:
        _apply_op(doc, op)
    return HarnessSpec.model_validate(doc)


def test_positive_control_a_map_over_model_output_with_an_llm_body_passes_the_flow_rules():
    """The flow rules accept an honest map. (apply_edit as a whole still refuses every map edit on the research spec,
    because a map body has no outgoing edges and so counts as a terminal node: the 'runs end only at audit' rule.
    That refusal is incidental; these flow rules are what must hold once maps become an evolution surface.)"""
    spec = seed_spec()
    edit = _edit(spec, _map("hypotheses"), LLM_BODY, with_tool=False)
    new = _unchecked(spec, edit)
    _check_information_flow(spec, new)  # no EditRejected
    with pytest.raises(EditRejected, match="runs must end only at 'audit'"):
        apply_edit(spec, edit)


def test_a_map_cannot_render_tool_output_into_its_llm_body():
    spec = seed_spec()
    with pytest.raises(EditRejected, match="tool output"):
        apply_edit(spec, _edit(spec, _map("tool_check"), LLM_BODY))


@pytest.mark.parametrize("items_key", ["input", "input.task_dir", "_private_dev_labels_ref", "critique_raw"])
def test_a_map_cannot_render_private_host_or_unfiltered_state_into_its_llm_body(items_key):
    spec = seed_spec()
    with pytest.raises(EditRejected, match=r"would render|private data"):  # _private* is refused by the text screen too
        apply_edit(spec, _edit(spec, _map(items_key), LLM_BODY, with_tool=False))


def test_a_map_with_a_tool_body_carries_tool_output_and_must_write_a_tool_key():
    spec = seed_spec()
    tool_body = {"kind": "tool", "command": ["python", "-c", "print('{{item}}')"], "output_key": "tool_branch"}
    with pytest.raises(EditRejected, match="tool_"):
        apply_edit(spec, _edit(spec, _map("hypotheses", body="tb", output_key="rows"), tool_body, with_tool=False))
    # with a tool_ key the flow rules accept it, but no prompt may then render the map's output
    ok = _edit(spec, _map("hypotheses", body="tb", output_key="tool_rows"), tool_body, with_tool=False)
    _check_information_flow(spec, _unchecked(spec, ok))
    reader = _insert_between(spec, "hypothesize", "tournament", "r1",
                             {"kind": "llm", "prompt": "{{tool_rows}}", "output_key": "r1"})
    with pytest.raises(EditRejected, match=r"tool output|rendered into model prompt"):
        apply_edit(spec, _edit(spec, _map("hypotheses", body="tb", output_key="tool_rows"), tool_body,
                               with_tool=False, extra=reader))


def test_changing_only_the_body_of_an_existing_map_is_checked_too():
    """A human-written map over run inputs with a harmless body; an edit that only changes the body to render the
    item must be rejected (the map itself is unchanged)."""
    spec = seed_spec()
    human = _unchecked(spec, _edit(spec, _map("input"), {"kind": "llm", "prompt": "count", "output_key": "mb_o"},
                                   with_tool=False))
    body_only = Edit(type=EditType.PROMPT_TEXT, rationale="x", ops=[
        EditOp(op="replace", path="/nodes/mb/prompt", value="Think about {{item}}")])
    with pytest.raises(EditRejected, match="would render 'input'"):
        _check_information_flow(human, _unchecked(human, body_only))
    with pytest.raises(EditRejected, match="would render 'input'"):
        apply_edit(human, body_only)
