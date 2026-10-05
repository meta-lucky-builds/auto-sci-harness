"""Model gateway interface. Every LLM call in the system goes through a Backend (PLAN L0, B6)."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel

from ..records import AccessMode, Cost, Provenance


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    json_schema: dict[str, Any] | None = None  # ask for structured output when the backend supports it


class ChatResponse(BaseModel):
    text: str
    cost: Cost
    provenance: Provenance
    raw_ref: str | None = None  # blob hash of the raw backend response, if stored


class BackendError(RuntimeError):
    pass


class Backend(ABC):
    name: str
    provider: str
    mode: AccessMode
    training_on_outputs: str = "unknown"

    @abstractmethod
    def complete(self, req: ChatRequest) -> ChatResponse: ...

    def provenance(self, model: str) -> Provenance:
        return Provenance(backend=self.name, provider=self.provider, model=model,
                          mode=self.mode, training_on_outputs=self.training_on_outputs)


def to_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert a JSON schema to OpenAI strict structured-output form.

    Objects get additionalProperties=false and every property becomes required; properties that
    were optional become nullable. The engine still validates against the ORIGINAL schema.
    """
    s = dict(schema)
    if s.get("type") == "object" or "properties" in s:
        props = {k: to_strict_schema(v) for k, v in (s.get("properties") or {}).items()}
        required = set(s.get("required") or [])
        for k, v in props.items():
            if k not in required:
                t = v.get("type")
                if isinstance(t, str):
                    v["type"] = [t, "null"]
                elif isinstance(t, list) and "null" not in t:
                    v["type"] = t + ["null"]
        s["properties"] = props
        s["required"] = list(props)
        s["additionalProperties"] = False
    if isinstance(s.get("items"), dict):
        s["items"] = to_strict_schema(s["items"])
    return s


def schema_instruction(schema: dict[str, Any]) -> str:
    import json
    return ("Reply with ONLY a JSON value (no prose, no code fences) that matches this JSON Schema:\n"
            + json.dumps(schema))


# Secret redaction applied to anything a backend logs (PLAN F3).
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWTs
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text
