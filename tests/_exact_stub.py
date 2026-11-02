"""A minimal computational-class adapter module for Kernel tests: the shape of the circuit adapter on origin/s3-domain
(83956f4, `adapters/circuit/check.py`: HIGHER_IS_BETTER, check, bounds, scope_phrase ending "this says nothing about
any physical circuit"), without its MNA checker. The instance is {"value": "<p/q>"}; the certificate must repeat it."""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction

HIGHER_IS_BETTER = True


def check(problem: bytes, certificate: bytes, value: Fraction) -> list[str]:
    want = Fraction(json.loads(problem)["value"])
    return [] if want == value == Fraction(json.loads(certificate)["value"]) else ["value differs"]


def bounds(problem: bytes, certificate: bytes, value: Fraction) -> list[str]:
    return []


def scope_phrase(problem: bytes, value: Fraction) -> str:
    return (f"node_voltage(out) of netlist {hashlib.sha256(problem).hexdigest()[:12]} = {value} V exactly, in the "
            f"ideal linear DC model; this says nothing about any physical circuit")
