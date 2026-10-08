"""Family-separated critic for proposed edits (GOAL §5 L6 step 5; RRSI, fixed). INTERFACE ONLY.

The critic judges the proposer's edits (leakage, degeneracy, grader gaming, unbounded work), so by invariant 5 it
must come from a DIFFERENT model family than the proposer. Only one model family is available today (invariant 5
NOT MET), so no working critic can exist yet: needs owner, a second model family.

Built: the interface, a Socratic review call (questions only, no verdict on acceptance), and the refusal, before
any model call, of a critic whose family equals the proposer's or cannot be determined (fail closed; unlike the run
policy's CHH_ALLOW_SAME_FAMILY there is NO override: a same-family critic is not a critic). The loop records the
review in the lineage as advisory; it does not gate acceptance and no repair loop (<= k repairs) is built.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..gateway import Backend, ChatRequest, Message
from ..policy import PolicyError, model_family
from ..runtime.engine import parse_json_output
from .edits import Edit

CRITIC_CONCERNS = ("leakage", "degeneracy", "grader_gaming", "unbounded_work")
CRITIC_SCHEMA = {"type": "object", "properties": {
    "questions": {"type": "array", "items": {"type": "string"}},
    "concerns": {"type": "array", "items": {"type": "string", "enum": list(CRITIC_CONCERNS)}}},
    "required": ["questions", "concerns"]}


class CriticRefused(PolicyError):
    """The critic would not be family-separated from the proposer (invariant 5)."""


@dataclass
class CriticReview:
    questions: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def as_record(self) -> dict[str, Any]:
        return {"questions": self.questions[:5], "concerns": self.concerns, "problems": self.problems,
                "advisory": True}


def require_family_separated(critic: Any, proposer: Any) -> tuple[str, str]:
    """(critic family, proposer family), or CriticRefused when they are equal or either is unknown."""
    fc, fp = model_family(critic), model_family(proposer)
    if fc.startswith("unknown:") or fp.startswith("unknown:"):
        raise CriticRefused(f"critic family {fc!r} / proposer family {fp!r}: an undetermined family never counts as "
                            "different (invariant 5, fail closed)")
    if fc == fp:
        raise CriticRefused(f"the critic and the proposer are both model family {fc!r}: a critic must come from a "
                            "different family (invariant 5). Needs owner: a second model family")
    return fc, fp


class FamilySeparatedCritic:
    def __init__(self, backend: Backend, *, proposer: Backend):
        self.families = require_family_separated(backend, proposer)
        self.backend = backend
        self.proposer = proposer

    def check_proposer(self, proposer: Any) -> None:
        """Re-checked by the loop against the proposer actually used in a generation."""
        require_family_separated(self.backend, proposer)

    def review(self, edit: Edit) -> CriticReview:
        prompt = ("You are a Socratic critic of a proposed harness edit. Do NOT approve or reject it. Ask at most 5 "
                  "short questions that would expose leakage of held-out data, degenerate behaviour, gaming of the "
                  "grader or unbounded work, and list which of these concerns apply: "
                  f"{list(CRITIC_CONCERNS)}.\nEDIT:\n{json.dumps(edit.model_dump(), default=str)[:6000]}")
        resp = self.backend.complete(ChatRequest(messages=[Message(role="user", content=prompt)],
                                                 json_schema=CRITIC_SCHEMA))
        try:
            raw = parse_json_output(resp.text)
        except json.JSONDecodeError as e:
            return CriticReview(problems=[f"critic output not JSON: {e}"])
        if not isinstance(raw, dict):
            return CriticReview(problems=["critic output is not an object"])
        qs = [q[:300] for q in raw.get("questions", []) if isinstance(q, str)]
        cs = [c for c in raw.get("concerns", []) if c in CRITIC_CONCERNS]
        return CriticReview(questions=qs, concerns=cs)
