"""A tiny HTTP seam for the REST-driven backends (Ray Jobs, HF Jobs). Tests inject a fake `HttpCall`; the live one
uses httpx with redirects OFF (a redirect could carry a bearer token to another host) and a fixed User-Agent.
Built, NOT test-verified; the live call has never been made.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .jobs import RemoteError


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


HttpCall = Callable[[str, str, Mapping[str, str], bytes | None, float], HttpReply]


def httpx_call(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout_s: float) -> HttpReply:
    import httpx
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False,
                          headers={"User-Agent": "custom-harness-lab-compute/0.0.1"}) as c:
            r = c.request(method, url, headers=dict(headers), content=body)
    except httpx.HTTPError as e:
        raise RemoteError(f"{method} {url.split('?')[0]}: {type(e).__name__}", transient=True) from e
    return HttpReply(r.status_code, r.content, {k.lower(): v for k, v in r.headers.items()})


def check(reply: HttpReply, what: str) -> HttpReply:
    if 200 <= reply.status < 300:
        return reply
    transient = reply.status in (408, 425, 429) or reply.status >= 500
    raise RemoteError(f"{what}: HTTP {reply.status}: {reply.text()[:300]}", transient=transient)
