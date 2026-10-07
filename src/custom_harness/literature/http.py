"""HTTP for the literature sources: transports, per-host pacing, bounded retries on transient errors only.

Error classes follow `custom_harness.errors` / `gateway.base.BackendError`: `infra` (the environment failed,
not the harness) and `transient` (worth retrying after a backoff). A request the harness got wrong (HTTP 400/422)
is infra=False. 404/410 are answers ("no such record"), not errors.

Nothing identifying is sent: a fixed User-Agent with no contact address, no API keys, no cookies. Only the
parameters the source module builds (the query or identifier) leave the machine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

USER_AGENT = "custom-harness-literature/0.0.1"
DEFAULT_INTERVAL_S = 3.0  # hosts without a published limit get the most conservative interval we use
ANSWER_STATUSES = frozenset({404, 410})


class LiteratureError(RuntimeError):
    def __init__(self, msg: str, *, transient: bool = False, infra: bool = True):
        super().__init__(msg)
        self.transient = transient
        self.infra = infra


@dataclass(frozen=True)
class HttpResponse:
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    retrieved_at: str | None = None  # set when served from / recorded into a snapshot
    envelope: str | None = None      # digest of the stored envelope (provenance)


class Transport(Protocol):
    offline: bool

    def get(self, url: str, params: dict[str, str]) -> HttpResponse: ...


class HttpxTransport:
    """Live HTTP. `mock` lets tests inject an httpx.MockTransport (no network)."""

    offline = False

    def __init__(self, timeout_s: float = 30.0, mock: httpx.BaseTransport | None = None):
        self._client = httpx.Client(timeout=timeout_s, follow_redirects=True, transport=mock,
                                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"})

    def get(self, url: str, params: dict[str, str]) -> HttpResponse:
        try:
            r = self._client.get(url, params=params)
        except httpx.HTTPError as e:
            raise LiteratureError(f"GET {urlsplit(url).netloc}: {type(e).__name__}: {e}", transient=True) from e
        return HttpResponse(status=r.status_code, text=r.text, headers={k.lower(): v for k, v in r.headers.items()})


class ScriptedTransport:
    """Replays scripted responses per URL in order (tests and fixtures). Unscripted requests fail closed."""

    offline = False

    def __init__(self, routes: dict[str, list[Any]]):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str]) -> HttpResponse:
        self.calls.append((url, dict(params)))
        queue = self.routes.get(url)
        if not queue:
            raise LiteratureError(f"no scripted response for {url}", transient=False)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(url, params)
        return item


class Pacer:
    """Minimum interval between requests to the same host (sources publish per-host rate limits)."""

    def __init__(self, intervals: dict[str, float], *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.intervals = dict(intervals)
        self.clock, self.sleep = clock, sleep
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        interval = self.intervals.get(host, DEFAULT_INTERVAL_S)
        last = self._last.get(host)
        if last is not None:
            delay = last + interval - self.clock()
            if delay > 0:
                self.sleep(delay)
        self._last[host] = self.clock()


class PacedTransport:
    offline = False

    def __init__(self, inner: Transport, pacer: Pacer):
        self.inner, self.pacer = inner, pacer

    def get(self, url: str, params: dict[str, str]) -> HttpResponse:
        self.pacer.wait(urlsplit(url).netloc)
        return self.inner.get(url, params)


def _retry_after(resp: HttpResponse) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


def fetch(transport: Transport, url: str, params: dict[str, str], *, max_attempts: int = 4, base_delay: float = 1.0,
          max_delay: float = 30.0, sleep: Callable[[float], None] = time.sleep) -> HttpResponse:
    """GET with bounded retries on transient failures only. Returns 2xx and 404/410 responses; raises
    LiteratureError for everything else (classified infra / not, transient / not)."""
    host = urlsplit(url).netloc
    for attempt in range(1, max_attempts + 1):
        last = attempt == max_attempts
        delay = min(base_delay * 2 ** (attempt - 1), max_delay)
        try:
            resp = transport.get(url, params)
        except LiteratureError as e:
            if not e.transient or last or transport.offline:
                raise
            sleep(delay)
            continue
        if 200 <= resp.status < 300 or resp.status in ANSWER_STATUSES:
            return resp
        msg = f"GET {host}: HTTP {resp.status}: {resp.text[:300]}"
        if resp.status in (400, 422):
            raise LiteratureError(msg, infra=False)
        if resp.status not in (408, 425, 429) and resp.status < 500:
            raise LiteratureError(msg)  # 401/403/other: blocked or misconfigured; retrying will not help
        wait = _retry_after(resp)
        if wait is not None and wait > max_delay:
            raise LiteratureError(f"{msg} (Retry-After {wait:.0f}s exceeds the {max_delay:.0f}s cap)", transient=True)
        if last or transport.offline:
            raise LiteratureError(f"{msg} (gave up after {attempt} attempts)", transient=True)
        sleep(wait if wait is not None and wait >= 0 else delay)
    raise AssertionError("unreachable")  # pragma: no cover
