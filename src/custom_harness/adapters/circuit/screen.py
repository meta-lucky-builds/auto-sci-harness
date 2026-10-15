"""Float screen -> exact gate for circuit certificates (GOAL §4.7: float screens may only PRUNE).

`float_screen(problem)` is a deliberately ordinary double-precision residual test; it may be wrong in both
directions. Candidates go through `custom_harness.verify.screen.screen_then_certify`, where only the exact checker
(`check.check` + `check.bounds`, Fraction arithmetic) can confirm one. This module is not part of the adapter's
identity and is never called by the Kernel: the Kernel re-checks every submitted certificate exactly anyway (K3, K8).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from . import check as C

SLACK = 1e-9  # relative; UNCALIBRATED and irrelevant to soundness (the exact gate decides)


@dataclass(frozen=True)
class CircuitCandidate:
    certificate: dict[str, Any]  # chh-circuit-certificate/1
    value: str  # the claimed exact value


def exact(problem: bytes) -> Callable[[CircuitCandidate], list[str]]:
    def run(cand: CircuitCandidate) -> list[str]:
        try:
            value = C.parse_rational(cand.value, "claimed value")
        except C.CircuitError as e:
            return [str(e)]
        cert = json.dumps(cand.certificate).encode()
        return C.check(problem, cert, value) + C.bounds(problem, cert, value)
    return run


def float_screen(problem: bytes) -> Callable[[CircuitCandidate], bool]:
    c = C.parse_problem(problem)
    a, b = C.mna(c)
    af = [[float(v) for v in row] for row in a]
    bf = [float(v) for v in b]

    def run(cand: CircuitCandidate) -> bool:
        x = [float(Fraction(s)) for s in cand.certificate["x"]]
        scale = max(1.0, *(abs(v) for v in bf))
        return all(abs(sum(p * q for p, q in zip(row, x, strict=True)) - bi) <= SLACK * scale
                   for row, bi in zip(af, bf, strict=True))
    return run
