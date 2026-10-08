"""Validation evidence of the `circuit` adapter: golden cases with hand-checkable answers and planted faults.

`approve` (kernel/adapters.py) runs CIRCUIT_CHECKS before it records a human approval; one failing or crashing check
makes the adapter unapprovable (invariant 13). Every golden value below is written by hand (derivation in the
comment), NOT taken from the solver: the solver only builds the witness, and the checker must agree with the hand
value. Every planted fault must be rejected FOR THE RIGHT REASON (a substring of the expected reason), so a checker
that rejects for another reason fails validation. Tolerance: exact, i.e. zero (every comparison is in Fraction).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from fractions import Fraction
from typing import Any

from . import check as C
from . import solver as S

Checks = tuple[tuple[str, Callable[[], bool]], ...]


def problem(elements: list[dict[str, str]], query: dict[str, str], nodes: tuple[str, ...] = ("0", "n1", "n2"),
            **extra: Any) -> bytes:
    return json.dumps({"format": C.FORMAT, "ground": "0", "nodes": list(nodes), "elements": elements,
                       "query": query, **extra}, sort_keys=True).encode()


def r(eid: str, pos: str, neg: str, value: str, unit: str = "ohm") -> dict[str, str]:
    return {"id": eid, "type": "resistor", "pos": pos, "neg": neg, "value": value, "unit": unit}


def vs(eid: str, pos: str, neg: str, value: str, unit: str = "V") -> dict[str, str]:
    return {"id": eid, "type": "vsource", "pos": pos, "neg": neg, "value": value, "unit": unit}


def node(n: str) -> dict[str, str]:
    return {"quantity": "node_voltage", "node": n, "unit": "V"}


SINGULAR, INCONSISTENT = {"quantity": "singular", "unit": "1"}, {"quantity": "inconsistent", "unit": "1"}

# Golden 1, divider: 10 V across R1 = 2 ohm and R2 = 3 ohm in series. V(n2) = 10 * 3/(2+3) = 6 V; the loop current is
# 10/5 = 2 A, which leaves V1's + terminal, so i(V1) (into pos) = -2 A and V1 absorbs 10 * -2 = -20 W; R2 absorbs
# 6 * 2 = 12 W. Tellegen: -20 + 8 + 12 = 0.
DIVIDER = [vs("V1", "n1", "0", "10"), r("R1", "n1", "n2", "2"), r("R2", "n2", "0", "3")]
# Golden 2, controlled sources: 2 V source, R1 = 1 ohm into a 0 V ammeter Vx (i(Vx) = 2 A). CCCS F1 = 3 i(Vx) = 6 A is
# pushed into n3 through R3 = 1 ohm: V(n3) = 6. CCVS H1 = 5 ohm * 2 A = 10 V at n4. VCVS E1 = 3 V(n1) = 6 V at n5.
# VCCS G1 = 2 S * V(n1) = 4 A drawn out of n6 through R6 = 1/2 ohm: V(n6) = -2.
CONTROLLED_NODES = ("0", "n1", "n2", "n3", "n4", "n5", "n6")
CONTROLLED = [
    vs("Vs", "n1", "0", "2"), r("R1", "n1", "n2", "1"), vs("Vx", "n2", "0", "0"),
    {"id": "F1", "type": "cccs", "pos": "0", "neg": "n3", "control": "Vx", "value": "3", "unit": "A/A"},
    r("R3", "n3", "0", "1"),
    {"id": "H1", "type": "ccvs", "pos": "n4", "neg": "0", "control": "Vx", "value": "5", "unit": "ohm"},
    r("R4", "n4", "0", "1"),
    {"id": "E1", "type": "vcvs", "pos": "n5", "neg": "0", "ctrl_pos": "n1", "ctrl_neg": "0", "value": "3",
     "unit": "V/V"},
    r("R5", "n5", "0", "1"),
    {"id": "G1", "type": "vccs", "pos": "n6", "neg": "0", "ctrl_pos": "n1", "ctrl_neg": "0", "value": "2",
     "unit": "S"},
    r("R6", "n6", "0", "1/2")]
CONTROLLED_GOLDEN = {"n3": Fraction(6), "n4": Fraction(10), "n5": Fraction(6), "n6": Fraction(-2)}
# Golden 3: two ideal sources in parallel. Equal values (1 V, 1 V): consistent but singular (the split of current
# between them is undetermined). Different values (1 V, 2 V): no solution (KVL around the loop fails).
PARALLEL_EQUAL = [vs("V1", "n1", "0", "1"), vs("V2", "n1", "0", "1"), r("R1", "n1", "0", "1")]
PARALLEL_CLASH = [vs("V1", "n1", "0", "1"), vs("V2", "n1", "0", "2"), r("R1", "n1", "0", "1")]
# Golden 4: a 1 A current source into a node with nothing else attached: KCL at n1 cannot hold (no solution).
CUTSET = [{"id": "I1", "type": "isource", "pos": "0", "neg": "n1", "value": "1", "unit": "A"}, r("R1", "n2", "0", "1")]


def solved(p: bytes) -> tuple[Fraction, dict[str, Any]]:
    value, cert = S.certify(p)
    return value, json.loads(cert)


def reasons(p: bytes, cert: dict[str, Any] | bytes, value: Fraction | int | str) -> list[str]:
    data = cert if isinstance(cert, bytes) else json.dumps(cert, sort_keys=True).encode()
    v = Fraction(value)
    return C.check(p, data, v) + C.bounds(p, data, v)


def accepted(p: bytes, value: Fraction | int | str) -> bool:
    got, cert = solved(p)
    return got == Fraction(value) and reasons(p, cert, value) == []


def rejects(why: str, p: bytes, value: Fraction | int | str, change: Callable[[dict[str, Any]], Any] | None = None,
            raw: bytes | None = None) -> bool:
    """The planted fault (a certificate edit, raw bytes, or a faulty problem) is rejected, with `why` in a reason."""
    try:
        _, cert = solved(p)
        if change is not None:
            change(cert)
    except ValueError:  # an unreadable planted problem: give the checker some certificate
        cert = {"format": C.CERT_FORMAT}
    return any(why in x for x in reasons(p, raw if raw is not None else cert, value))


def _set(cert: dict[str, Any], key: str, i: int, value: object) -> None:
    cert[key][i] = value


def _swap(cert: dict[str, Any], key: str, i: int, j: int) -> None:
    cert[key][i], cert[key][j] = cert[key][j], cert[key][i]


def _float_screen_never_grants() -> bool:
    """A certificate wrong by 10^-30 passes a float residual test; the exact checker rejects it."""
    p = problem(DIVIDER, node("n2"))
    _, cert = solved(p)
    cert["x"][1] = "6000000000000000000000000000001/1000000000000000000000000000000"
    x = [float(Fraction(s)) for s in cert["x"]]
    floats_say_ok = abs(x[1] - 6.0) < 1e-12
    return floats_say_ok and any("A x != b" in m for m in reasons(p, cert, "6"))


def _kcl_catches_a_wrong_solution() -> bool:
    c = C.parse_problem(problem(DIVIDER, node("n2")))
    good = [Fraction(10), Fraction(6), Fraction(-2)]
    bad = C.kirchhoff(c, [Fraction(10), Fraction(5), Fraction(-2)])
    return C.kirchhoff(c, good) == [] and any("KCL violated at node 0" in m for m in bad) and \
        any("Tellegen" in m for m in bad)


def _controlled_golden() -> bool:
    return all(accepted(problem(CONTROLLED, node(n), CONTROLLED_NODES), v) for n, v in CONTROLLED_GOLDEN.items())


def _rebind(original: bytes, other: bytes) -> bytes:
    """The (valid) certificate of `original`, relabelled as a certificate of `other`."""
    _, cert = solved(original)
    cert["problem_sha256"] = C.parse_problem(other).sha256
    return json.dumps(cert).encode()


_DIV = problem(DIVIDER, node("n2"))
_PAR_V = problem(PARALLEL_EQUAL, node("n1"), ("0", "n1"))
_SWAPPED = problem([vs("V1", "0", "n1", "10"), *DIVIDER[1:]], node("n2"))  # true V(n2) = -6
_DUP = _DIV.replace(b'"ground": "0"', b'"ground": "0", "ground": "n1"')

CIRCUIT_CHECKS: Checks = (
    # golden cases (hand-checkable)
    ("divider: V(n2) = 6 V is accepted", lambda: accepted(_DIV, 6)),
    ("divider: i(V1) = -2 A is accepted",
     lambda: accepted(problem(DIVIDER, {"quantity": "branch_current", "element": "V1", "unit": "A"}), -2)),
    ("divider: V1 absorbs -20 W", lambda: accepted(
        problem(DIVIDER, {"quantity": "element_power", "element": "V1", "unit": "W"}), -20)),
    ("divider: R2 absorbs 12 W", lambda: accepted(
        problem(DIVIDER, {"quantity": "element_power", "element": "R2", "unit": "W"}), 12)),
    ("controlled sources (CCCS, CCVS, VCVS, VCCS) give the hand values", _controlled_golden),
    ("divider is regular (singular = 0)", lambda: accepted(problem(DIVIDER, SINGULAR), 0)),
    ("parallel equal sources: singular = 1 with a null vector",
     lambda: accepted(problem(PARALLEL_EQUAL, SINGULAR, ("0", "n1")), 1)),
    ("parallel equal sources: consistent (inconsistent = 0)",
     lambda: accepted(problem(PARALLEL_EQUAL, INCONSISTENT, ("0", "n1")), 0)),
    ("parallel clashing sources: inconsistent = 1 with a left-null witness",
     lambda: accepted(problem(PARALLEL_CLASH, INCONSISTENT, ("0", "n1")), 1)),
    ("current source into an open node: inconsistent = 1", lambda: accepted(problem(CUTSET, INCONSISTENT), 1)),
    # planted faults: wrong claims and forged certificates
    ("an off-by-one claimed value is rejected", lambda: rejects("differs from the claimed value", _DIV, 7)),
    ("an off-by-one rational in x is rejected",
     lambda: rejects("A x != b", _DIV, 6, lambda c: _set(c, "x", 1, "61/10"))),
    ("a corrupted inverse is rejected",
     lambda: rejects("not a left inverse", _DIV, 6, lambda c: c["inverse"][0].__setitem__(0, "7"))),
    ("swapped node voltages in x are rejected", lambda: rejects("A x != b", _DIV, 6, lambda c: _swap(c, "x", 0, 1))),
    ("swapped unknown names are rejected",
     lambda: rejects("registered order", _DIV, 6, lambda c: _swap(c, "unknowns", 0, 1))),
    ("the certificate of the original netlist is rejected for the netlist with V1's nodes swapped", lambda: rejects(
        "A x != b", _SWAPPED, 6, raw=_rebind(_DIV, _SWAPPED))),
    ("a certificate of another problem is rejected", lambda: rejects(
        "certificate is for problem", _DIV, 6, lambda c: c.update(problem_sha256="0" * 64))),
    ("a value claim on a singular network is rejected (no inverse exists)", lambda: rejects(
        "not a left inverse", _PAR_V, 1, raw=json.dumps({
            "format": C.CERT_FORMAT, "problem_sha256": C.parse_problem(_PAR_V).sha256,
            "unknowns": ["v:n1", "i:V1", "i:V2"], "x": ["1", "0", "-1"],  # a true solution (one of many)
            "inverse": [["1", "0", "0"], ["0", "1", "0"], ["0", "0", "1"]]}).encode())),
    ("a value claim without an inverse is refused",
     lambda: rejects("missing keys", _DIV, 6, lambda c: c.pop("inverse"))),
    ("a zero null vector is rejected", lambda: rejects(
        "null vector is zero", problem(PARALLEL_EQUAL, SINGULAR, ("0", "n1")), 1,
        lambda c: c.update(null_vector=["0", "0", "0"]))),
    ("a vector that is not null is rejected", lambda: rejects(
        "A v != 0", problem(PARALLEL_EQUAL, SINGULAR, ("0", "n1")), 1,
        lambda c: c.update(null_vector=["1", "0", "0"]))),
    ("a regular network claimed singular is rejected", lambda: rejects(
        "A v != 0", problem(DIVIDER, SINGULAR), 1, raw=json.dumps({
            "format": C.CERT_FORMAT, "problem_sha256": C.parse_problem(problem(DIVIDER, SINGULAR)).sha256,
            "unknowns": ["v:n1", "v:n2", "i:V1"], "null_vector": ["1", "1", "1"]}).encode())),
    ("an inconsistency witness with y^T b = 0 is rejected", lambda: rejects(
        "does not witness inconsistency", problem(PARALLEL_EQUAL, INCONSISTENT, ("0", "n1")), 1,
        raw=json.dumps({"format": C.CERT_FORMAT,
                        "problem_sha256": C.parse_problem(problem(PARALLEL_EQUAL, INCONSISTENT, ("0", "n1"))).sha256,
                        "unknowns": ["v:n1", "i:V1", "i:V2"], "left_null": ["0", "-1", "1"]}).encode())),
    ("a boolean quantity outside {0, 1} is rejected", lambda: rejects("outside the range", problem(DIVIDER, SINGULAR),
                                                                      2)),
    # strict reading and units
    ("a duplicated JSON key is refused", lambda: rejects("duplicate key", _DUP, 6)),
    ("a JSON number instead of a rational string is refused",
     lambda: rejects("exact rational string", _DIV, 6, lambda c: _set(c, "x", 1, 6.0))),
    ("a decimal string is refused", lambda: rejects("exact rational string", problem(
        [vs("V1", "n1", "0", "10"), r("R1", "n1", "n2", "2.0"), r("R2", "n2", "0", "3")], node("n2")), 6)),
    ("a NaN constant is refused", lambda: rejects("JSON constant NaN", _DIV.replace(b'"10"', b"NaN"), 6)),
    ("an unknown key is refused", lambda: rejects("unexpected keys", _DIV, 6, lambda c: c.update(note="trust me"))),
    ("a prefixed unit (kohm) is refused", lambda: rejects("not a registered SI unit", problem(
        [vs("V1", "n1", "0", "10"), r("R1", "n1", "n2", "2", "kohm"), r("R2", "n2", "0", "3")], node("n2")), 6)),
    ("a resistor in volts violates the dimension check", lambda: rejects("element equation needs", problem(
        [vs("V1", "n1", "0", "10"), r("R1", "n1", "n2", "2", "V"), r("R2", "n2", "0", "3")], node("n2")), 6)),
    ("a voltage query in amperes violates the dimension check", lambda: rejects(
        "does not have the dimension", problem(DIVIDER, {"quantity": "node_voltage", "node": "n2", "unit": "A"}), 6)),
    ("a zero resistance is refused", lambda: rejects("R > 0", problem(
        [vs("V1", "n1", "0", "10"), r("R1", "n1", "n2", "0"), r("R2", "n2", "0", "3")], node("n2")), 6)),
    ("a physical (unregistered) query is refused", lambda: rejects("is not registered", problem(
        DIVIDER, {"quantity": "works_when_built", "unit": "1"}), 1)),
    ("a number beyond the registered digit bound is refused", lambda: rejects(
        "digits", _DIV, 6, lambda c: _set(c, "x", 1, "1" * (C.MAX_DIGITS + 1)))),
    # K8: element-level laws are an independent second check; float screens may only prune
    ("KCL at ground and Tellegen catch a wrong solution", _kcl_catches_a_wrong_solution),
    ("a float-equal but exactly wrong certificate is rejected", _float_screen_never_grants),
)
