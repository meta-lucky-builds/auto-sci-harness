"""The resolution-rule language: tiny, closed and pure.

A rule is {"kind": "threshold", "format": "json"|"csv", "field": ..., "op": ..., "value": ..., "where": {...}?}.
- json: `field` is a dotted path into a JSON object; the value there must be a JSON number (or a string for ==/!=).
- csv: `field` is a column; `where` (optional, {column: exact cell}) must select exactly one data row.
Unknown kinds, unknown keys, booleans and non-finite values are refused. Evaluation reads only the observation bytes:
no clock, no randomness, no I/O. If the observation does not determine the outcome (missing field, wrong type, zero or
several rows) evaluation raises RuleUndetermined, never guesses.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

ORDER_OPS = frozenset({">=", ">", "<=", "<"})


class RuleUndetermined(ValueError):
    """The observation does not determine the rule's outcome."""


class ThresholdRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["threshold"]
    format: Literal["json", "csv"]
    field: str
    op: Literal[">=", ">", "<=", "<", "==", "!="]
    value: int | float | str
    where: dict[str, str] | None = None

    @model_validator(mode="before")
    @classmethod
    def _closed(cls, data: Any) -> Any:
        if isinstance(data, ThresholdRule):
            return data
        if not isinstance(data, dict):
            raise ValueError("a resolution rule must be a mapping")
        kind = data.get("kind")
        if kind != "threshold":
            raise ValueError(f"unknown rule kind {kind!r}: the rule language is closed (only 'threshold')")
        v = data.get("value")
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError("rule value must be a number or a string")
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("rule value must be finite")
        if isinstance(v, str) and data.get("op") in ORDER_OPS:
            raise ValueError("ordering operators need a numeric rule value")
        if data.get("where") is not None and data.get("format") != "csv":
            raise ValueError("'where' applies only to csv rules")
        if not str(data.get("field") or "").strip():
            raise ValueError("rule field must be non-empty")
        return data


def rule_hash(rule: ThresholdRule) -> str:
    return hashlib.sha256(json.dumps(rule.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def _observed(rule: ThresholdRule, content: bytes) -> Any:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuleUndetermined("observation is not UTF-8") from e
    if rule.format == "json":
        try:
            obj: Any = json.loads(text)
        except ValueError as e:
            raise RuleUndetermined("observation is not valid JSON") from e
        for part in rule.field.split("."):
            if not isinstance(obj, dict) or part not in obj:
                raise RuleUndetermined(f"field {rule.field!r} is not in the observation")
            obj = obj[part]
        return obj
    rows = list(csv.DictReader(io.StringIO(text)))
    if rule.where:
        where = rule.where
        rows = [r for r in rows if all((r.get(k) or "").strip() == v for k, v in where.items())]
    if len(rows) != 1:
        raise RuleUndetermined(f"csv rule selects {len(rows)} rows (exactly 1 needed)")
    cell = rows[0].get(rule.field)
    if not isinstance(cell, str):
        raise RuleUndetermined(f"column {rule.field!r} is not in the observation")
    return cell.strip()


def evaluate(rule: ThresholdRule, content: bytes) -> bool:
    """The rule's outcome on these observation bytes (pure)."""
    obs = _observed(rule, content)
    v = rule.value
    if isinstance(v, str):
        if not isinstance(obs, str):
            raise RuleUndetermined(f"field {rule.field!r} is not a string")
        return (obs == v) if rule.op == "==" else (obs != v)
    if isinstance(obs, bool):
        raise RuleUndetermined(f"field {rule.field!r} is a boolean, not a number")
    if isinstance(obs, str) and rule.format == "csv":
        try:
            obs = float(obs)
        except ValueError as e:
            raise RuleUndetermined(f"column {rule.field!r} is not numeric") from e
    if not isinstance(obs, (int, float)) or not math.isfinite(obs):
        raise RuleUndetermined(f"field {rule.field!r} is not a finite number")
    x = float(obs)
    y = float(v)
    return {">=": x >= y, ">": x > y, "<=": x <= y, "<": x < y, "==": x == y, "!=": x != y}[rule.op]
