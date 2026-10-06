"""Failure classification: did the ENVIRONMENT break, or did the HARNESS behave badly? (GOAL L1, CR P0-1)

An infrastructure failure (sandbox daemon down, provider outage, rate limit) says nothing about the
harness being evaluated. Scoring it as 0 lets a Docker hiccup decide an accept/reject. So:
- infra failures end the run as INFRA_FAILED, and evaluation EXCLUDES the unit (never scores it);
- transient infra failures are retried first, and every attempt's cost is still charged;
- everything not classified as infra is a policy failure and is scored as usual (fail closed).
"""

from __future__ import annotations

from collections.abc import Iterator


class InfraError(RuntimeError):
    """The environment failed, not the harness. `transient` = worth retrying after a backoff."""

    def __init__(self, msg: str, *, transient: bool = False):
        super().__init__(msg)
        self.transient = transient


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def infra_cause(exc: BaseException) -> BaseException | None:
    """The first exception in the cause chain that is an infrastructure failure, if any.

    BackendError is infra unless it was raised with infra=False (e.g. the request itself was malformed,
    which is the harness's fault)."""
    for e in _chain(exc):
        if isinstance(e, InfraError) or getattr(e, "infra", False) is True:
            return e
    return None


def is_transient(exc: BaseException) -> bool:
    cause = infra_cause(exc)
    return bool(cause is not None and getattr(cause, "transient", False))
