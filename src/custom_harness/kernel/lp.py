"""Exact LP optimality certificates: the trusted checker of the `lp` adapter (computational class, GOAL §4.7).

Problem (the claim's subject, a registered artifact; JSON): maximise c^T x subject to A x <= b, x >= 0,
    {"format": "chh-lp/1", "sense": "max", "c": [n], "A": [m rows of n], "b": [m], "comment": optional str}
Certificate (a registered artifact; JSON): a primal point x and a dual point y,
    {"format": "chh-lp-certificate/1", "x": [n], "y": [m], "comment": optional str}
Every number is an exact rational STRING "p" or "p/q". JSON numbers (a float would be silently rounded), decimals
and exponents are refused, as are unknown keys.

The claimed optimum v is proven iff  A x <= b, x >= 0 (x is feasible),  A^T y >= c, y >= 0 (y is dual feasible) and
c^T x = b^T y = v: by weak duality every feasible x' has c^T x' <= b^T y = v, and x attains v. All arithmetic is
fractions.Fraction; this module never creates a float. It is deliberately small: it is part of the trusted base.
"""

from __future__ import annotations

import hashlib
import json
import re
from fractions import Fraction
from typing import Any

HIGHER_IS_BETTER = True  # the claimed value is a maximum
MAX_BYTES = 1_000_000  # K3 refuses to parse a larger document
MAX_DIM = 500  # K8 sanity bounds (registered background invariants of this adapter)
MAX_DIGITS = 100

_RATIONAL = re.compile(r"-?(0|[1-9][0-9]*)(/[1-9][0-9]*)?")
Problem = tuple[list[Fraction], list[list[Fraction]], list[Fraction]]


class LPError(ValueError):
    pass


def parse_rational(text: object) -> Fraction:
    if not isinstance(text, str) or _RATIONAL.fullmatch(text) is None:
        raise LPError(f"{text!r} is not an exact rational string 'p' or 'p/q' (JSON numbers and decimals are refused)")
    return Fraction(text)


def _numbers(v: object, what: str, n: int | None) -> list[Fraction]:
    if not isinstance(v, list):
        raise LPError(f"{what} must be a list")
    if n is not None and len(v) != n:
        raise LPError(f"dimension mismatch: {what} has {len(v)} entries, expected {n}")
    return [parse_rational(e) for e in v]


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A duplicated key would show a reader one value and the checker another (json keeps the last): refused."""
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise LPError(f"duplicate key {sorted({k for k in keys if keys.count(k) > 1})[0]!r}")
    return dict(pairs)


def _load(data: bytes, fmt: str, keys: set[str]) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise LPError(f"document is larger than {MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")  # canonical UTF-8 only: json would also read UTF-16/32 and a BOM,
        if text.startswith("\ufeff"):  # which K9's byte scan of the same document could not see through
            raise LPError("a byte-order mark is refused")
        doc = json.loads(text, object_pairs_hook=_unique)
    except ValueError as e:
        raise LPError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != fmt:
        raise LPError(f"not a {fmt} document")
    extra, missing = set(doc) - keys - {"format", "comment"}, keys - set(doc)
    if extra:
        raise LPError(f"unexpected keys {sorted(extra)}")
    if missing:
        raise LPError(f"missing keys {sorted(missing)}")
    return doc


def parse_problem(data: bytes) -> Problem:
    doc = _load(data, "chh-lp/1", {"sense", "c", "A", "b"})
    if doc["sense"] != "max":
        raise LPError("sense must be 'max' (state a minimisation as the maximisation of -c^T x)")
    c = _numbers(doc["c"], "'c'", None)
    if not c:
        raise LPError("the problem needs at least one variable")
    if not isinstance(doc["A"], list):
        raise LPError("'A' must be a list of rows")
    a = [_numbers(row, f"row {i} of 'A'", len(c)) for i, row in enumerate(doc["A"])]
    return c, a, _numbers(doc["b"], "'b'", len(a))


def parse(problem: bytes, certificate: bytes) -> tuple[Problem, list[Fraction], list[Fraction]]:
    c, a, b = parse_problem(problem)
    doc = _load(certificate, "chh-lp-certificate/1", {"x", "y"})
    return (c, a, b), _numbers(doc["x"], "'x'", len(c)), _numbers(doc["y"], "'y'", len(b))


def scope_phrase(problem: bytes, value: Fraction) -> str:
    """K1: the only statement an LP claim may carry (no free prose can widen it beyond the registered instance)."""
    c, a, _ = parse_problem(problem)
    return (f"the optimum of LP instance {hashlib.sha256(problem).hexdigest()[:12]} (maximise c^T x subject to A x <= "
            f"b, x >= 0; {len(a)} constraint(s), {len(c)} variable(s)) is exactly {value}; nothing beyond this "
            f"instance is claimed")


def _dot(u: list[Fraction], v: list[Fraction]) -> Fraction:
    return sum((p * q for p, q in zip(u, v, strict=True)), Fraction(0))


def check(problem: bytes, certificate: bytes, value: Fraction) -> list[str]:
    """K3: why the certificate does NOT prove that the optimum of `problem` is `value` (empty = proven)."""
    try:
        (c, a, b), x, y = parse(problem, certificate)
    except ValueError as e:  # LPError, or an integer beyond Python's digit limit
        return [f"unreadable: {e}"]
    reasons = [f"primal infeasible: x[{j}] = {v} < 0" for j, v in enumerate(x) if v < 0]
    for i, row in enumerate(a):
        lhs = _dot(row, x)
        if lhs > b[i]:
            reasons.append(f"primal infeasible: (A x)[{i}] = {lhs} > b[{i}] = {b[i]}")
    reasons += [f"dual infeasible: y[{i}] = {v} < 0" for i, v in enumerate(y) if v < 0]
    for j in range(len(c)):
        col = _dot([row[j] for row in a], y)
        if col < c[j]:
            reasons.append(f"dual infeasible: (A^T y)[{j}] = {col} < c[{j}] = {c[j]}")
    primal, dual = _dot(c, x), _dot(b, y)
    if primal != dual:
        reasons.append(f"duality gap: c^T x = {primal} != b^T y = {dual}")
    if primal != value:
        reasons.append(f"objective c^T x = {primal} differs from the claimed value {value}")
    return reasons


def bounds(problem: bytes, certificate: bytes, value: Fraction) -> list[str]:
    """K8: the adapter's registered sanity bounds on dimensions and number sizes (the claimed value included)."""
    try:
        (c, a, b), x, y = parse(problem, certificate)
    except ValueError as e:
        return [f"unreadable: {e}"]
    reasons = []
    if max(len(c), len(b)) > MAX_DIM:
        reasons.append(f"dimensions {len(b)} x {len(c)} exceed the registered bound {MAX_DIM}")
    numbers = [value, *c, *b, *x, *y, *(q for row in a for q in row)]
    big = [q for q in numbers if max(len(str(abs(q.numerator))), len(str(q.denominator))) > MAX_DIGITS]
    if big:
        reasons.append(f"{len(big)} number(s) exceed {MAX_DIGITS} digits in numerator or denominator")
    return reasons
