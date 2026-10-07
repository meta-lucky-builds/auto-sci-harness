"""Request-caused provider errors are the harness's fault; genuine outages stay infrastructure (R2-3)."""

import httpx
import pytest

from custom_harness.errors import infra_cause, is_transient
from custom_harness.gateway.base import BackendError, ChatRequest, Message, request_fault
from custom_harness.gateway.openai_compat import OpenAICompatBackend
from custom_harness.records import AccessMode


@pytest.mark.parametrize(("text", "fault"), [
    ("context_length_exceeded: Your input exceeds the context window", True),
    ("HTTP 413 Request Entity Too Large", True),
    ("invalid_request_error: bad schema", True),
    ("HTTP 503 Service Unavailable", False),
    ("rate limit reached, retry later", False),
    ("connection reset by peer", False),
])
def test_request_fault_classification(text, fault):
    assert request_fault(text) is fault


@pytest.mark.parametrize(("status", "body", "infra", "transient"), [
    (413, "payload too large", False, False),
    (400, "context_length_exceeded", False, False),
    (500, "server error", True, True),
    (429, "rate limit", True, True),
    (503, "maximum context reached", False, False),  # a request-caused message wins even on a 5xx
])
def test_openai_compat_backend_classifies_by_cause(monkeypatch, status, body, infra, transient):
    b = OpenAICompatBackend(name="t", base_url="http://x", model="m", mode=AccessMode.API_KEY)
    b._client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, text=body)))
    with pytest.raises(BackendError) as ei:
        b.complete(ChatRequest(messages=[Message(role="user", content="hi")]))
    assert (infra_cause(ei.value) is not None) is infra and is_transient(ei.value) is transient
