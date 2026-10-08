"""Exact linear DC circuit certificates: the trusted checker of the `circuit` adapter (computational class, T1).

Scope (the claim-scope vocabulary, closed): a claim is about the IDEAL LINEAR DC MODEL of a registered netlist, never
about a physical circuit. The model has ideal resistors, independent voltage and current sources, and the four
linear controlled sources (VCVS, VCCS, CCVS, CCCS); the equations are modified nodal analysis (MNA) in exact
rationals. Nothing here says whether a built circuit works, survives, meets a rating or matches its model: such a
claim is PHYSICAL (GOAL §4.7) and no adapter serves it, so the Kernel refuses it at K1.

Problem (the claim's subject, a registered artifact; strict JSON: duplicate keys, unknown keys, JSON numbers, NaN
and Infinity are refused):
    {"format": "chh-circuit/1", "ground": "0", "nodes": ["0", "n1", ...],
     "elements": [{"id": "R1", "type": "resistor", "pos": "n1", "neg": "n2", "value": "1000", "unit": "ohm"}, ...],
     "query": {"quantity": Q, ...}, "comment": optional str}
Element types (`value` is an exact rational string "p" or "p/q"; `unit` a registered SI unit WITHOUT prefixes, whose
dimension must match the element equation; i_e is the current INTO terminal `pos`, through the element, out of `neg`):
    resistor  v(pos) - v(neg) = R i_e                       R > 0, unit ohm (V/A)
    vsource   v(pos) - v(neg) = V                           unit V
    isource   i_e = I                                       unit A
    vcvs      v(pos) - v(neg) = k (v(ctrl_pos) - v(ctrl_neg))   unit V/V (dimensionless)
    vccs      i_e = g (v(ctrl_pos) - v(ctrl_neg))           unit S (A/V)
    ccvs      v(pos) - v(neg) = r i_control                 unit ohm (V/A); `control` names a vsource (an ammeter)
    cccs      i_e = k i_control                             unit A/A (dimensionless); `control` names a vsource
Queries (the registered quantities; one metric, `circuit_value`, whose meaning the hashed query fixes):
    {"quantity": "node_voltage", "node": N, "unit": "V"}        v(N), unique: the MNA matrix must be proven regular
    {"quantity": "branch_current", "element": E, "unit": "A"}   i_E, unique (as above)
    {"quantity": "element_power", "element": E, "unit": "W"}    (v(pos) - v(neg)) i_E absorbed by E, unique
    {"quantity": "singular", "unit": "1"}                        1 = the MNA matrix is singular, 0 = it is regular
    {"quantity": "inconsistent", "unit": "1"}                    1 = the MNA equations have no solution, 0 = they do
MNA unknowns, in this order: v(node) for every node except ground in `nodes` order ("v:<node>"), then i_e for every
vsource, vcvs and ccvs in `elements` order ("i:<id>"). Rows: KCL at every non-ground node (sum of currents leaving
the node = 0), then one constitutive row per branch-current element, in the same orders.

Certificate (a registered artifact): {"format": "chh-circuit-certificate/1", "problem_sha256": hex, "unknowns": [...],
plus exactly the witness keys the query and claimed value need}:
    node_voltage / branch_current / element_power:  "x" (A x = b) and "inverse" (M with M A = I: A is regular, so x is
                                                    the ONLY solution and the claimed value is determined)
    singular = 1: "null_vector" v != 0 with A v = 0          singular = 0: "inverse"
    inconsistent = 1: "left_null" y with y^T A = 0 and y^T b != 0 (no x can satisfy A x = b)
    inconsistent = 0: "x" with A x = b
The checker NEVER solves: it stamps A and b from the netlist (linear time) and multiplies. All arithmetic is
fractions.Fraction; this module creates no float. K8 (`bounds`) re-checks a solution x at the ELEMENT level, a
formulation independent of the MNA stamps: KCL at every node including ground, every constitutive law, Tellegen's
theorem (sum of v_e i_e = 0), the SI dimension of every value, and the registered size bounds.
K7: does not apply (computational class, like `lp`): correctness is proven exactly; triviality is not judged.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, NoReturn

FORMAT, CERT_FORMAT = "chh-circuit/1", "chh-circuit-certificate/1"
MODEL = "ideal linear DC circuit model chh-circuit/1"
METRIC = "circuit_value"
# Kernel K1 matches the claim's direction to this. An exact value has no better direction; fixed by convention so
# every claim states the same direction (as for `lp`).
HIGHER_IS_BETTER = True
MAX_BYTES = 2_000_000  # registered size bounds: refused at parse (K3 and K8), so a check cannot be made to hang
MAX_NODES, MAX_ELEMENTS, MAX_UNKNOWNS, MAX_DIGITS = 40, 120, 80, 100
MAX_REASONS = 5  # per kind of mismatch; the count of the rest is reported

_RATIONAL = re.compile(r"-?(0|[1-9][0-9]*)(/[1-9][0-9]*)?")
_NAME = re.compile(r"[A-Za-z0-9_]{1,32}")  # ASCII only: no look-alike node or element names
_HEX64 = re.compile(r"[0-9a-f]{64}")

# SI units without prefixes, as exponents of (volt, ampere): "kohm", "mV", "Ω" and every other spelling are refused
UNITS: dict[str, tuple[int, int]] = {"V": (1, 0), "A": (0, 1), "ohm": (1, -1), "S": (-1, 1), "W": (1, 1),
                                     "V/V": (0, 0), "A/A": (0, 0), "1": (0, 0)}
# dimension each element's value must have, from its equation (see the module docstring)
ELEMENT_DIM: dict[str, tuple[int, int]] = {"resistor": (1, -1), "vsource": (1, 0), "isource": (0, 1),
                                           "vcvs": (0, 0), "vccs": (-1, 1), "ccvs": (1, -1), "cccs": (0, 0)}
BRANCH = ("vsource", "vcvs", "ccvs")  # elements whose current is an MNA unknown
NODE_CONTROLLED, SOURCE_CONTROLLED = ("vcvs", "vccs"), ("ccvs", "cccs")
VALUE_QUANTITIES = {"node_voltage": ("node", (1, 0)), "branch_current": ("element", (0, 1)),
                    "element_power": ("element", (1, 1))}
BOOLEAN_QUANTITIES = ("singular", "inconsistent")


class CircuitError(ValueError):
    pass


def _refuse(msg: str) -> NoReturn:
    raise CircuitError(msg)


@dataclass(frozen=True)
class Element:
    id: str
    type: str
    pos: str
    neg: str
    value: Fraction
    unit: str
    ctrl_pos: str | None = None
    ctrl_neg: str | None = None
    control: str | None = None


@dataclass(frozen=True)
class Query:
    quantity: str
    target: str | None  # the node (node_voltage) or element (branch_current, element_power)
    unit: str


@dataclass(frozen=True)
class Circuit:
    ground: str
    nodes: tuple[str, ...]
    elements: tuple[Element, ...]
    query: Query
    sha256: str

    @property
    def unknowns(self) -> tuple[str, ...]:
        return (*(f"v:{n}" for n in self.nodes if n != self.ground),
                *(f"i:{e.id}" for e in self.elements if e.type in BRANCH))

    def element(self, eid: str) -> Element:
        return next(e for e in self.elements if e.id == eid)


# ------------------------------------------------------------------ strict reading
def parse_rational(text: object, what: str = "number") -> Fraction:
    if not isinstance(text, str) or _RATIONAL.fullmatch(text) is None:
        _refuse(f"{what}: {text!r} is not an exact rational string 'p' or 'p/q' (JSON numbers, decimals and "
                f"exponents are refused)")
    if any(len(part.lstrip("-")) > MAX_DIGITS for part in text.split("/")):
        _refuse(f"{what}: more than {MAX_DIGITS} digits in numerator or denominator (registered size bound)")
    return Fraction(text)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A duplicated key would show a reader one value and the parser another: refused."""
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        _refuse(f"duplicate key {dup[0]!r}")
    return dict(pairs)


def _no_constant(name: str) -> NoReturn:
    _refuse(f"JSON constant {name} is refused")


def _load(data: bytes, fmt: str) -> dict[str, Any]:
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        _refuse(f"document is not bytes of at most {MAX_BYTES}")
    try:
        doc = json.loads(data, object_pairs_hook=_unique, parse_constant=_no_constant)
    except CircuitError:
        raise
    except ValueError as e:
        _refuse(f"not JSON: {e}")
    if not isinstance(doc, dict) or doc.get("format") != fmt:
        _refuse(f"not a {fmt} document")
    if not isinstance(doc.get("comment", ""), str):
        _refuse("'comment' must be a string")
    return doc


def _keys(obj: object, required: set[str], what: str, optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(obj, dict):
        _refuse(f"{what} must be an object")
    extra, missing = set(obj) - required - optional, required - set(obj)
    if extra:
        _refuse(f"{what}: unexpected keys {sorted(extra)}")
    if missing:
        _refuse(f"{what}: missing keys {sorted(missing)}")
    return obj


def _name(v: object, what: str) -> str:
    if not isinstance(v, str) or _NAME.fullmatch(v) is None:
        _refuse(f"{what}: {v!r} is not a name of 1-32 ASCII letters, digits or '_'")
    return v


def _unit(v: object, what: str) -> str:
    if not isinstance(v, str) or v not in UNITS:
        _refuse(f"{what}: unit {v!r} is not a registered SI unit {sorted(UNITS)} (no prefixes: write the value in "
                f"base units)")
    return v


_ELEMENT_KEYS = {"id", "type", "pos", "neg", "value", "unit"}


def _element(obj: object, i: int, nodes: set[str]) -> Element:
    if not isinstance(obj, dict) or obj.get("type") not in ELEMENT_DIM:
        _refuse(f"element {i}: type must be one of {sorted(ELEMENT_DIM)}")
    kind = str(obj["type"])
    extra = {"ctrl_pos", "ctrl_neg"} if kind in NODE_CONTROLLED else {"control"} if kind in SOURCE_CONTROLLED else set()
    _keys(obj, _ELEMENT_KEYS | extra, f"element {i}")
    eid = _name(obj["id"], f"element {i} id")
    terms = {k: _name(obj[k], f"{eid}.{k}") for k in ("pos", "neg", "ctrl_pos", "ctrl_neg") if k in obj}
    unknown = sorted(n for n in terms.values() if n not in nodes)
    if unknown:
        _refuse(f"{eid}: terminal node(s) {unknown} not in 'nodes'")
    value = parse_rational(obj["value"], f"{eid}.value")
    if kind == "resistor" and value <= 0:
        _refuse(f"{eid}: resistance {value} outside the registered range R > 0 (an ideal short is a 0 V vsource)")
    return Element(id=eid, type=kind, pos=terms["pos"], neg=terms["neg"], value=value,
                   unit=_unit(obj["unit"], f"{eid}"), ctrl_pos=terms.get("ctrl_pos"), ctrl_neg=terms.get("ctrl_neg"),
                   control=_name(obj["control"], f"{eid}.control") if "control" in obj else None)


def _query(obj: object, nodes: set[str], elements: dict[str, Element]) -> Query:
    if not isinstance(obj, dict):
        _refuse("query must be an object")
    q = obj.get("quantity")
    if q in VALUE_QUANTITIES:
        key = VALUE_QUANTITIES[str(q)][0]
        _keys(obj, {"quantity", key, "unit"}, "query")
        target = _name(obj[key], f"query.{key}")
        if target not in (nodes if key == "node" else elements):
            _refuse(f"query names {key} {target!r}, which is not in the netlist")
        return Query(quantity=str(q), target=target, unit=_unit(obj["unit"], "query"))
    if q in BOOLEAN_QUANTITIES:
        _keys(obj, {"quantity", "unit"}, "query")
        return Query(quantity=str(q), target=None, unit=_unit(obj["unit"], "query"))
    _refuse(f"query quantity {q!r} is not registered {sorted([*VALUE_QUANTITIES, *BOOLEAN_QUANTITIES])} (physical "
            f"properties of a built circuit are out of scope)")


def parse_problem(data: bytes) -> Circuit:
    doc = _keys(_load(data, FORMAT), {"format", "ground", "nodes", "elements", "query"}, "problem",
                frozenset({"comment"}))
    raw_nodes, raw_elements = doc["nodes"], doc["elements"]
    if not isinstance(raw_nodes, list) or not 2 <= len(raw_nodes) <= MAX_NODES:
        _refuse(f"'nodes' must be a list of 2..{MAX_NODES} names")
    nodes = tuple(_name(n, "node") for n in raw_nodes)
    if len(set(nodes)) != len(nodes):
        _refuse("duplicate node names")
    ground = _name(doc["ground"], "ground")
    if ground not in nodes:
        _refuse(f"ground {ground!r} is not in 'nodes'")
    if not isinstance(raw_elements, list) or not 1 <= len(raw_elements) <= MAX_ELEMENTS:
        _refuse(f"'elements' must be a list of 1..{MAX_ELEMENTS} elements")
    elements = tuple(_element(e, i, set(nodes)) for i, e in enumerate(raw_elements))
    by_id = {e.id: e for e in elements}
    if len(by_id) != len(elements):
        _refuse("duplicate element ids")
    for e in elements:
        if e.control is not None and (e.control not in by_id or by_id[e.control].type != "vsource"):
            _refuse(f"{e.id}: control {e.control!r} must name a vsource (a 0 V source measures a current)")
    c = Circuit(ground=ground, nodes=nodes, elements=elements, query=_query(doc["query"], set(nodes), by_id),
                sha256=hashlib.sha256(data).hexdigest())
    if len(c.unknowns) > MAX_UNKNOWNS:
        _refuse(f"{len(c.unknowns)} MNA unknowns exceed the registered bound {MAX_UNKNOWNS}")
    return c


def _vector(v: object, n: int, what: str) -> list[Fraction]:
    if not isinstance(v, list) or len(v) != n:
        _refuse(f"dimension mismatch: {what} must be a list of {n} numbers")
    return [parse_rational(e, f"{what}[{i}]") for i, e in enumerate(v)]


def witness_keys(c: Circuit, value: Fraction) -> set[str]:
    """The witness keys a certificate for this query and claimed value must carry (no more, no fewer)."""
    q = c.query.quantity
    if q in VALUE_QUANTITIES:
        return {"x", "inverse"}
    if q == "singular":
        return {"null_vector"} if value == 1 else {"inverse"}
    return {"left_null"} if value == 1 else {"x"}


def parse_certificate(c: Circuit, data: bytes, value: Fraction) -> dict[str, Any]:
    keys = witness_keys(c, value)
    doc = _keys(_load(data, CERT_FORMAT), {"format", "problem_sha256", "unknowns", *keys}, "certificate",
                frozenset({"comment"}))
    if not isinstance(doc["problem_sha256"], str) or _HEX64.fullmatch(doc["problem_sha256"]) is None:
        _refuse("problem_sha256 must be a lowercase hex SHA-256")
    if doc["problem_sha256"] != c.sha256:
        _refuse(f"certificate is for problem {doc['problem_sha256'][:12]}, not {c.sha256[:12]}")
    if doc["unknowns"] != list(c.unknowns):
        _refuse(f"unknowns {doc['unknowns']!r} are not the MNA unknowns {list(c.unknowns)} in registered order "
                f"(swapped or renamed nodes/elements)")
    n = len(c.unknowns)
    out: dict[str, Any] = {k: _vector(doc[k], n, k) for k in keys & {"x", "null_vector", "left_null"}}
    if "inverse" in keys:
        inv = doc["inverse"]
        if not isinstance(inv, list) or len(inv) != n:
            _refuse(f"dimension mismatch: inverse must have {n} rows")
        out["inverse"] = [_vector(row, n, f"inverse[{i}]") for i, row in enumerate(inv)]
    return out


# ------------------------------------------------------------------ the MNA stamps (matrix formulation)
def mna(c: Circuit) -> tuple[list[list[Fraction]], list[Fraction]]:
    """A and b of the MNA equations A x = b (unknowns and row order: module docstring)."""
    idx = {u: k for k, u in enumerate(c.unknowns)}
    n = len(idx)
    a = [[Fraction(0)] * n for _ in range(n)]
    b = [Fraction(0)] * n

    def v(node: str | None) -> dict[int, Fraction]:  # the linear form of a node voltage (ground = 0)
        return {} if node is None or node == c.ground else {idx[f"v:{node}"]: Fraction(1)}

    def diff(p: str | None, m: str | None, k: Fraction = Fraction(1)) -> dict[int, Fraction]:
        out: dict[int, Fraction] = {}
        for col, coef in v(p).items():
            out[col] = out.get(col, Fraction(0)) + k * coef
        for col, coef in v(m).items():
            out[col] = out.get(col, Fraction(0)) - k * coef
        return out

    def add(row: int, form: dict[int, Fraction], sign: int = 1) -> None:
        for col, coef in form.items():
            a[row][col] += sign * coef

    for e in c.elements:
        if e.type == "resistor":
            current = diff(e.pos, e.neg, 1 / e.value)
        elif e.type == "isource":
            current = {}
            for node, sign in ((e.pos, -1), (e.neg, 1)):  # a constant current moves to the right-hand side
                if node != c.ground:
                    b[idx[f"v:{node}"]] += sign * e.value
        elif e.type == "vccs":
            current = diff(e.ctrl_pos, e.ctrl_neg, e.value)
        elif e.type == "cccs":
            current = {idx[f"i:{e.control}"]: e.value}
        else:  # vsource, vcvs, ccvs: the current is an unknown, plus one constitutive row
            current = {idx[f"i:{e.id}"]: Fraction(1)}
            row = idx[f"i:{e.id}"]
            add(row, diff(e.pos, e.neg))
            if e.type == "vsource":
                b[row] += e.value
            elif e.type == "vcvs":
                add(row, diff(e.ctrl_pos, e.ctrl_neg, e.value), -1)
            else:
                add(row, {idx[f"i:{e.control}"]: e.value}, -1)
        for node, sign in ((e.pos, 1), (e.neg, -1)):  # KCL: i_e leaves `pos`, enters `neg`
            if node != c.ground:
                add(idx[f"v:{node}"], current, sign)
    return a, b


def _dot(u: list[Fraction], v: list[Fraction]) -> Fraction:
    return sum((p * q for p, q in zip(u, v, strict=True)), Fraction(0))


def _capped(found: list[str], what: str) -> list[str]:
    if len(found) <= MAX_REASONS:
        return found
    return [*found[:MAX_REASONS], f"... and {len(found) - MAX_REASONS} more {what}"]


def _residual(a: list[list[Fraction]], x: list[Fraction], b: list[Fraction], label: str,
              names: tuple[str, ...]) -> list[str]:
    bad = []
    for i, row in enumerate(a):
        lhs = _dot(row, x)
        if lhs != b[i]:
            bad.append(f"{label}: row {i} ({_row_name(names, i)}) gives {lhs}, not {b[i]}")
    return _capped(bad, f"{label} rows")


def _row_name(names: tuple[str, ...], i: int) -> str:
    u = names[i]
    return f"KCL at node {u[2:]}" if u.startswith("v:") else f"law of {u[2:]}"


# ------------------------------------------------------------------ element-level formulation (independent of mna)
def voltages(c: Circuit, x: list[Fraction]) -> dict[str, Fraction]:
    out = {c.ground: Fraction(0)}
    out |= {u[2:]: x[k] for k, u in enumerate(c.unknowns) if u.startswith("v:")}
    return out


def element_current(c: Circuit, x: list[Fraction], e: Element) -> Fraction:
    """i_e (into `pos`, through e, out of `neg`) from a solution x, by e's own law."""
    v = voltages(c, x)
    unknown = {u[2:]: x[k] for k, u in enumerate(c.unknowns) if u.startswith("i:")}
    if e.type == "resistor":
        return (v[e.pos] - v[e.neg]) / e.value
    if e.type == "isource":
        return e.value
    if e.type == "vccs":
        return e.value * (v[str(e.ctrl_pos)] - v[str(e.ctrl_neg)])
    if e.type == "cccs":
        return e.value * unknown[str(e.control)]
    return unknown[e.id]


def quantity(c: Circuit, x: list[Fraction]) -> Fraction:
    """The queried value quantity (node_voltage, branch_current, element_power) of solution x."""
    q, target = c.query.quantity, str(c.query.target)
    if q == "node_voltage":
        return voltages(c, x)[target]
    e = c.element(target)
    i = element_current(c, x, e)
    if q == "branch_current":
        return i
    v = voltages(c, x)
    return (v[e.pos] - v[e.neg]) * i


def describe(c: Circuit) -> str:
    q, t = c.query.quantity, c.query.target
    return {"node_voltage": f"V({t})", "branch_current": f"I({t}) (into pos, out of neg)",
            "element_power": f"P({t}) (absorbed)", "singular": "singularity of the MNA matrix (1 = singular)",
            "inconsistent": "inconsistency of the MNA equations (1 = no solution)"}[q]


def kirchhoff(c: Circuit, x: list[Fraction]) -> list[str]:
    """K8 background laws on a solution x, element by element: KCL at EVERY node (ground included, which no MNA row
    states), every source's constitutive law, and Tellegen's theorem sum_e v_e i_e = 0."""
    v = voltages(c, x)
    current = {e.id: element_current(c, x, e) for e in c.elements}
    unknown = {u[2:]: x[k] for k, u in enumerate(c.unknowns) if u.startswith("i:")}
    bad = []
    for node in c.nodes:
        net = sum((current[e.id] * ((e.pos == node) - (e.neg == node)) for e in c.elements), Fraction(0))
        if net != 0:
            bad.append(f"KCL violated at node {node}: net current leaving = {net}")
    for e in c.elements:
        if e.type not in BRANCH:  # the other laws define element_current, so they hold by construction
            continue
        drop = v[e.pos] - v[e.neg]
        if e.type == "vsource":
            law = e.value
        elif e.type == "vcvs":
            law = e.value * (v[str(e.ctrl_pos)] - v[str(e.ctrl_neg)])
        else:
            law = e.value * unknown[str(e.control)]
        if drop != law:
            bad.append(f"constitutive law of {e.id} ({e.type}) violated: v(pos) - v(neg) = {drop}, law gives {law}")
    power = sum(((v[e.pos] - v[e.neg]) * current[e.id] for e in c.elements), Fraction(0))
    out = _capped(bad, "law violations")
    if power != 0:
        out.append(f"Tellegen's theorem violated: sum of absorbed powers = {power} W, not 0")
    return out


def dimensions(c: Circuit) -> list[str]:
    """K8: every value's SI dimension matches its element equation, and the query's unit matches its quantity."""
    bad = [f"{e.id} ({e.type}): unit {e.unit!r} has dimension V^{UNITS[e.unit][0]} A^{UNITS[e.unit][1]}, the "
           f"element equation needs V^{ELEMENT_DIM[e.type][0]} A^{ELEMENT_DIM[e.type][1]}"
           for e in c.elements if UNITS[e.unit] != ELEMENT_DIM[e.type]]
    q = c.query
    need = VALUE_QUANTITIES[q.quantity][1] if q.quantity in VALUE_QUANTITIES else (0, 0)
    if UNITS[q.unit] != need:
        bad.append(f"query unit {q.unit!r} does not have the dimension of {q.quantity} (V^{need[0]} A^{need[1]})")
    return bad


def scope_phrase(problem: bytes, value: Fraction) -> str:
    """The only statement a circuit claim should carry: what is claimed, and in which model (no physical claim)."""
    c = parse_problem(problem)
    return (f"{describe(c)} = {value} {c.query.unit} exactly, in the {MODEL} of netlist {c.sha256[:12]} (ideal "
            f"elements, DC, exact rationals); this says nothing about any physical circuit")


# ------------------------------------------------------------------ the two functions the Kernel calls
def _range(c: Circuit, value: Fraction) -> list[str]:
    if c.query.quantity in BOOLEAN_QUANTITIES and value not in (0, 1):
        return [f"claimed value {value} is outside the range {{0, 1}} of quantity {c.query.quantity}"]
    if max(len(str(abs(value.numerator))), len(str(value.denominator))) > MAX_DIGITS:
        return [f"the claimed value exceeds {MAX_DIGITS} digits in numerator or denominator"]
    return []


def check(problem: bytes, certificate: bytes, value: Fraction) -> list[str]:
    """K3: why the certificate does NOT prove that the queried quantity of `problem` is exactly `value`
    (empty = proven, in the ideal linear DC model only)."""
    try:
        c = parse_problem(problem)
        out = _range(c, value)
        if out:
            return out
        w = parse_certificate(c, certificate, value)
    except ValueError as e:  # CircuitError, or an integer beyond Python's digit limit
        return [f"unreadable: {e}"]
    a, b = mna(c)
    names, n = c.unknowns, len(c.unknowns)
    cols = [list(col) for col in zip(*a, strict=True)]
    reasons: list[str] = []
    if "x" in w:
        reasons += _residual(a, w["x"], b, "A x != b", names)
    if "inverse" in w:  # M A = I for a square A proves A regular: x is then the only solution
        bad = [f"inverse is not a left inverse: (M A)[{i}][{j}] = {got}, not {int(i == j)}"
               for i, row in enumerate(w["inverse"]) for j in range(n)
               if (got := _dot(row, cols[j])) != Fraction(int(i == j))]
        reasons += _capped(bad, "inverse entries")
    if "null_vector" in w:
        if not any(w["null_vector"]):
            reasons.append("null vector is zero: a zero vector witnesses nothing")
        reasons += _residual(a, w["null_vector"], [Fraction(0)] * n, "A v != 0", names)
    if "left_null" in w:
        y = w["left_null"]
        bad = [f"y^T A != 0: column {j} ({names[j]}) gives {s}" for j in range(n) if (s := _dot(y, cols[j])) != 0]
        reasons += _capped(bad, "columns")
        if _dot(y, b) == 0:
            reasons.append("y^T b = 0: y does not witness inconsistency")
    if c.query.quantity in VALUE_QUANTITIES:
        got = quantity(c, w["x"])
        if got != value:
            reasons.append(f"{describe(c)} = {got} {c.query.unit} from the certificate differs from the claimed value "
                           f"{value}")
    return reasons


def bounds(problem: bytes, certificate: bytes, value: Fraction) -> list[str]:
    """K8: the adapter's registered background invariants: SI dimensions, value range and size, and (when the
    certificate carries a solution x) KCL at every node, constitutive laws and Tellegen's theorem, element by
    element."""
    try:
        c = parse_problem(problem)
        reasons = dimensions(c) + _range(c, value)
        if c.query.quantity in BOOLEAN_QUANTITIES and value not in (0, 1):
            return reasons
        w = parse_certificate(c, certificate, value)
    except ValueError as e:
        return [f"unreadable: {e}"]
    if "x" in w:
        reasons += kirchhoff(c, w["x"])
    return reasons
