"""API-key and open-weights backend over the OpenAI-compatible chat completions protocol.

Covers: OpenAI API (mode=api), and local servers such as Ollama / vLLM / llama.cpp (mode=ow).
Keys come from an environment variable for now; OS-keychain storage is a planned upgrade (PLAN F3).
"""

from __future__ import annotations

import os
import time

import httpx

from ..records import AccessMode, Cost
from .base import Backend, BackendError, ChatRequest, ChatResponse, redact, request_fault, to_strict_schema


class OpenAICompatBackend(Backend):
    def __init__(self, *, name: str, base_url: str, model: str, mode: AccessMode,
                 api_key_env: str | None = None, provider: str = "openai",
                 usd_per_mtok_in: float = 0.0, usd_per_mtok_out: float = 0.0,
                 training_on_outputs: str = "unknown", timeout_s: float = 600):
        if mode not in (AccessMode.API_KEY, AccessMode.OPEN_WEIGHTS):
            raise ValueError("OpenAICompatBackend serves api or ow mode only")
        self.name, self.provider, self.mode = name, provider, mode
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.training_on_outputs = training_on_outputs
        self.price_in, self.price_out = usd_per_mtok_in, usd_per_mtok_out
        key = os.environ.get(api_key_env) if api_key_env else None
        if api_key_env and not key:
            raise BackendError(f"environment variable {api_key_env} is not set")
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._client = httpx.Client(timeout=timeout_s)

    def complete(self, req: ChatRequest) -> ChatResponse:
        model = req.model or self.model
        body: dict = {"model": model, "messages": [m.model_dump() for m in req.messages]}
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.json_schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "output", "strict": True,
                                                       "schema": to_strict_schema(req.json_schema)}}
        t0 = time.time()
        try:
            r = self._client.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers)
        except httpx.HTTPError as e:
            raise BackendError(redact(f"{self.name}: {e}"), transient=True) from e
        wall = time.time() - t0
        if r.status_code != 200:
            # 400/413/422 or a request-caused message (e.g. context length): the harness's fault, never infra
            infra = r.status_code not in (400, 413, 422) and not request_fault(r.text)
            transient = infra and (r.status_code in (408, 409, 429) or r.status_code >= 500)
            raise BackendError(redact(f"{self.name}: HTTP {r.status_code}: {r.text[:2000]}"),
                               transient=transient, infra=infra)
        data = r.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise BackendError(f"{self.name}: unexpected response shape") from e
        usage = data.get("usage") or {}
        pin, pout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        cost = Cost(prompt_tokens=pin, completion_tokens=pout,
                    usd=(pin * self.price_in + pout * self.price_out) / 1e6,
                    wall_s=wall, estimated=not usage)
        return ChatResponse(text=text, cost=cost, provenance=self.provenance(model))
