"""Per-host pacing for the model gateway (GOAL §5 L0 "Model gateway": per-host pacing).

A HostPacer enforces a minimum interval between the STARTS of calls to one host (scheme-less `host[:port]`), across
every backend and thread that shares the pacer. Slots are reserved under a lock, so N concurrent callers to one host
start at least `interval` apart; different hosts never wait for each other. PacedBackend wraps any Backend with a
pacer and a host key. Pacing is politeness and rate-limit avoidance only: it never changes what a call returns or
costs (waiting time is not charged to the step's model cost; the engine measures wall time itself).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .base import Backend, ChatRequest, ChatResponse

# Minimum seconds between call starts to one host when the host has no entry of its own.
DEFAULT_MIN_INTERVAL_S = 1.0  # UNCALIBRATED (owner decision, GOAL §13.8): conservative; no provider limit consulted
# Per-host overrides (host[:port] -> seconds). Empty until the owner sets them from the providers' published limits.
HOST_MIN_INTERVAL_S: dict[str, float] = {}  # UNCALIBRATED (owner decision, GOAL §13.8)


def host_of(url: str) -> str:
    """`host[:port]`, lower-cased, of a URL (a bare host is returned lower-cased). Refuses an empty host."""
    parts = urlsplit(url if "//" in url else f"//{url}")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"no host in {url!r}")
    return f"{host}:{parts.port}" if parts.port else host


class HostPacer:
    def __init__(self, intervals: Mapping[str, float] | None = None, default_s: float = DEFAULT_MIN_INTERVAL_S, *,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        table = dict(HOST_MIN_INTERVAL_S if intervals is None else intervals)
        for h, v in [*table.items(), ("<default>", default_s)]:
            if isinstance(v, bool) or not (isinstance(v, (int, float)) and v >= 0):
                raise ValueError(f"pacing interval for {h} must be a number >= 0, not {v!r}")
        self._intervals = {k.lower(): float(v) for k, v in table.items()}
        self._default = float(default_s)
        self._clock, self._sleep = clock, sleep
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def interval(self, host: str) -> float:
        return self._intervals.get(host.lower(), self._default)

    def acquire(self, host: str) -> float:
        """Block until this caller may start a call to `host`; returns the seconds waited."""
        host = host.lower()
        with self._lock:
            now = self._clock()
            start = max(now, self._next.get(host, now))
            self._next[host] = start + self.interval(host)
        wait = start - now
        if wait > 0:
            self._sleep(wait)
        return wait


class PacedBackend(Backend):
    """Any backend, paced by host. Same name/provider/mode/provenance as the wrapped backend."""

    def __init__(self, inner: Backend, *, host: str, pacer: HostPacer):
        self._inner, self._pacer = inner, pacer
        self.host = host_of(host)
        self.name, self.provider, self.mode = inner.name, inner.provider, inner.mode
        self.training_on_outputs = inner.training_on_outputs
        self.waited_s = 0.0

    def complete(self, req: ChatRequest) -> ChatResponse:
        self.waited_s += self._pacer.acquire(self.host)
        return self._inner.complete(req)

    def __getattr__(self, item: str) -> Any:
        if item.startswith("_"):
            raise AttributeError(item)
        return getattr(self._inner, item)
