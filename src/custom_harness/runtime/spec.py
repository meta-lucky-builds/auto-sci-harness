"""Typed harness spec (PLAN B2). This is the object the harness evolver (L6) edits.

Every field here is an edit surface with a known edit type:
  contract        -> LLMNode.output_schema, LLMNode.retries
  hop / info-flow -> Edge list, template references between nodes
  control-flow    -> Edge.when, max_steps, retries
  context policy  -> ContextPolicy
  prompt text     -> LLMNode.system / prompt
  tool            -> ToolNode
  subagent role   -> LLMNode.role
  budget policy   -> HarnessSpec.budget
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..records import content_hash

END = "END"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Condition(_Strict):
    key: str  # dotted path into run state, e.g. "review.verdict" or "_last_error"
    op: Literal["eq", "ne", "lt", "gt", "truthy", "falsy", "contains"]
    value: Any = None


class Edge(_Strict):
    source: str
    target: str  # node id or END
    when: Condition | None = None  # None = unconditional; edges are tried in list order


class LLMNode(_Strict):
    kind: Literal["llm"] = "llm"
    role: str = "executor"  # resolved to a Backend at run time
    system: str = ""
    prompt: str  # template; {{dotted.path}} pulls from run state
    output_key: str
    output_schema: dict[str, Any] | None = None  # contract: output must be JSON matching this
    retries: int = 0  # extra attempts when the contract fails, with the error fed back
    temperature: float | None = None
    max_tokens: int | None = None


class ToolNode(_Strict):
    kind: Literal["tool"] = "tool"
    command: list[str]  # each element is a template
    output_key: str
    timeout_s: float = 600
    snapshot_after: bool = True


class ModuleNode(_Strict):
    kind: Literal["module"] = "module"
    module: str  # id in the module registry
    params: dict[str, Any] = Field(default_factory=dict)


class ApprovalNode(_Strict):
    """A step only a human can complete (GOAL §5 L0, invariant 15). The run goes PAUSED until a decision for
    exactly (run, node, sha256 of the canonical JSON of state[item_key]) is in the ledger, written by
    `chh approve`. The engine writes the outcome to `<node_id>_approval` (and `<node_id>_decision`); edges route on
    it, e.g. key `review_approval.decision` eq `approved`. Not an evolution surface (evolve/edits.py)."""

    kind: Literal["approval"] = "approval"
    item_key: str  # dotted path into run state: the item the human approves
    prompt: str = ""  # shown to the approver


def approval_state_keys(node_id: str) -> tuple[str, str]:
    """State keys only the engine writes for an approval node."""
    return f"{node_id}_approval", f"{node_id}_decision"


Node = Annotated[LLMNode | ToolNode | ModuleNode | ApprovalNode, Field(discriminator="kind")]


class ContextPolicy(_Strict):
    max_value_chars: int = 20_000  # each templated value is truncated to this (head + tail kept)
    head_fraction: float = 0.5


class BudgetSpec(_Strict):
    usd: float | None = None
    gpu_hours: float | None = None
    quota_units: float | None = None
    tokens: float | None = None
    wall_s: float | None = None


class HarnessSpec(_Strict):
    id: str
    description: str = ""
    entry: str
    nodes: dict[str, Node]
    edges: list[Edge] = Field(default_factory=list)
    max_steps: int = 50
    context: ContextPolicy = Field(default_factory=ContextPolicy)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)

    @model_validator(mode="after")
    def _check_graph(self) -> HarnessSpec:
        if self.entry not in self.nodes:
            raise ValueError(f"entry '{self.entry}' is not a node")
        if END in self.nodes:
            raise ValueError(f"'{END}' is reserved")
        for e in self.edges:
            if e.source not in self.nodes:
                raise ValueError(f"edge source '{e.source}' is not a node")
            if e.target != END and e.target not in self.nodes:
                raise ValueError(f"edge target '{e.target}' is not a node")
        return self

    def spec_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))

    def outgoing(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source == node_id]
