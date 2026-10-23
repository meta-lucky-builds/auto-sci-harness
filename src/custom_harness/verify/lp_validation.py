"""Fault-injection validation of the `lp` adapter: one golden certificate that must pass, and each planted fault, which
must be rejected FOR THE RIGHT REASON (a checker failing for another reason fails validation).

Moved verbatim from kernel/adapters.py by s4-kernel (2026-10-15), as verify/lean_validation.py; listed in the LP
adapter's modules, so a fixture edit changes the adapter's hash and needs a new human approval.
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction
from typing import Any

from ..kernel import lp
from ..kernel.claim import canonical_json
from .ml_validation import raises

# golden case: max x1 + x2 s.t. 2 x1 + x2 <= 4, x1 + 3 x2 <= 5, x >= 0; optimum 13/5, x = (7/5, 6/5), y = (2/5, 1/5)
_LP_P = {"format": "chh-lp/1", "sense": "max", "c": ["1", "1"], "A": [["2", "1"], ["1", "3"]], "b": ["4", "5"]}
_LP_C = {"format": "chh-lp-certificate/1", "x": ["7/5", "6/5"], "y": ["2/5", "1/5"]}


def _lp(cert: dict[str, Any], value: Fraction = Fraction(13, 5)) -> list[str]:
    return lp.check(canonical_json(_LP_P), canonical_json({**_LP_C, **cert}), value)


def _lp_rejects(cert: dict[str, Any], why: str, value: Fraction = Fraction(13, 5)) -> bool:
    return any(why in r for r in _lp(cert, value))


LP_CHECKS: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("a correct optimality certificate is accepted", lambda: _lp({}) == []),
    ("an infeasible primal point is rejected", lambda: _lp_rejects({"x": ["2", "1"]}, "primal infeasible")),
    ("an infeasible dual point is rejected", lambda: _lp_rejects({"y": ["1/5", "1/5"]}, "dual infeasible")),
    ("a nonzero duality gap is rejected", lambda: _lp_rejects({"x": ["1", "1"]}, "duality gap")),
    ("an objective other than the claimed value is rejected",
     lambda: _lp_rejects({}, "differs from the claimed value", Fraction(3))),
    ("a float-encoded certificate value is refused", lambda: _lp_rejects({"x": [1.4, 1.2]}, "exact rational string")),
    ("a float-encoded rational is refused", lambda: raises(lambda: lp.parse_rational("2.6"))),
    ("wrong dimensions are refused", lambda: _lp_rejects({"x": ["7/5", "6/5", "0"]}, "dimension mismatch")),
    ("a number beyond the registered size bound is flagged",
     lambda: bool(lp.bounds(canonical_json(_LP_P), canonical_json(_LP_C), Fraction(10**lp.MAX_DIGITS)))),
)
