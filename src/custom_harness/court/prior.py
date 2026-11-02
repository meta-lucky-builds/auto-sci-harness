"""The measured no-data prior (GOAL 18.6 P1, UPGRADES N9): before any test data exists, a pinned model reads only the
claim's words and says how likely the outcome is. It goes into `Risk.prior_probability`, and K7 uses the larger of that
and its noise model, so a prior can only make the claim harder to certify, never easier.

It is recorded before registration and is part of the claim from then on, so it cannot be chosen after a result. Nothing
in this repository calls a model: `PriorSource` is what a real model must provide, and the tests use a scripted one.
The measured values are UNCALIBRATED.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Protocol

from ..store import EvidenceStore

RECORD_KIND = "court_prior"
_PINNED = re.compile(r"^[a-z0-9][a-z0-9._:/-]*$")


class PriorRefused(RuntimeError):
    pass


class PriorSource(Protocol):
    model: str  # an exact model id: the prior is a measurement, so its instrument is named

    def predict(self, claim_text: str) -> float: ...


def _text(title: str, sentence: str, metric: str, min_effect: float) -> str:
    """All the source sees: the words and numbers fixed in advance. No data, and no Lab results."""
    return f"{title}\n{sentence}\nmeasure: {metric}; smallest effect that counts: {min_effect}"


def measure(store: EvidenceStore, promotion_id: str, source: PriorSource, *, title: str, sentence: str, metric: str,
            min_effect: float) -> dict[str, Any]:
    """Record the prior of a frozen promotion. One per promotion: a second try could shop for a lower number."""
    if measured(store, promotion_id) is not None:
        raise PriorRefused(f"promotion {promotion_id} already has a measured prior (one per promotion)")
    if not _PINNED.fullmatch(source.model) or source.model.endswith("latest"):
        raise PriorRefused(f"{source.model!r} is not an exact model id: a prior needs a pinned model")
    text = _text(title, sentence, metric, min_effect)
    p = source.predict(text)
    if type(p) is not float or not 0 < p < 1:
        raise PriorRefused(f"the model gave {p!r}: a prior is a number strictly between 0 and 1")
    body = {"promotion_id": promotion_id, "model": source.model, "prior": p,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "label": "UNCALIBRATED", "at": time.time()}
    store.append_record(RECORD_KIND, body)
    return body


def measured(store: EvidenceStore, promotion_id: str) -> dict[str, Any] | None:
    rows = store.records_where(RECORD_KIND, "promotion_id", promotion_id)
    return rows[0] if rows else None
