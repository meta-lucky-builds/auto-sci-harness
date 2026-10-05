"""Deterministic scripted backend for tests. Never valid for scorecard runs."""

from __future__ import annotations

from typing import Callable

from ..records import AccessMode, Cost
from .base import Backend, ChatRequest, ChatResponse


class MockBackend(Backend):
    name = "mock"
    provider = "mock"
    mode = AccessMode.MOCK
    training_on_outputs = "allowed"

    def __init__(self, responder: Callable[[ChatRequest], str] | list[str]):
        self._responder = responder
        self._i = 0
        self.requests: list[ChatRequest] = []

    def complete(self, req: ChatRequest) -> ChatResponse:
        self.requests.append(req)
        if callable(self._responder):
            text = self._responder(req)
        else:
            text = self._responder[min(self._i, len(self._responder) - 1)]
            self._i += 1
        prompt_tokens = sum(len(m.content.split()) for m in req.messages)
        return ChatResponse(text=text,
                            cost=Cost(prompt_tokens=prompt_tokens, completion_tokens=len(text.split()),
                                      estimated=True),
                            provenance=self.provenance(req.model or "mock-1"))
