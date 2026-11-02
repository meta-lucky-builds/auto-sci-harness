"""Pinned ODE simulator: the trusted re-execution checker of the `sim` adapter (simulated class, GOAL §4.7).

Simulator S = chh-ode-rk4, version 1: classical fixed-step 4th-order Runge-Kutta in IEEE-754 double precision,
implemented here in pure Python (no external binary, no licence). Python's float + - * are correctly rounded and the
operation order below is fixed, so a run is bit-for-bit reproducible on every platform. Solver settings are pinned:
method RK4 (order 4), and every configuration is re-run at the declared step h and at h/2 and h/4.

Config (the claim's subject, a registered artifact; its sha256 is the "config" the claim's scope names):
    {"format": "chh-sim/1", "simulator": "chh-ode-rk4", "version": "1", "model": M, "params": {...}, "x0": [...],
     "t_end": "p/q", "steps": N, "component": i, "tolerance": "p/q", "comment": optional str}
The registered model family (every number an exact rational string, rounded once to the nearest double):
    linear             x' = A x                                 params {"A": n x n}, 1 <= n <= MAX_DIM
    damped_oscillator  x0' = x1, x1' = -w^2 x0 - 2 z w x1       params {"omega": w, "zeta": z}
    sir                S' = -b S I, I' = b S I - g I, R' = g I  params {"beta": b, "gamma": g}; x0 >= 0, sums to 1
Result (a registered artifact: the run the harness reports): {"format": "chh-sim-result/1", "config_sha256": hex,
    "values": [q(h), q(h/2), q(h/4)] as repr() strings of doubles}, q = x_component(t_end).

The claim "x_i(t_end) of the ODE is `value` within +- tolerance" is supported (K3) iff the three reported values are
reproduced bit-for-bit and finite; the refinements are in RK4's asymptotic range (|q(h)-q(h/2)| >= 8 |q(h/2)-q(h/4)|,
observed order >= 3, unless the differences are at the rounding floor); the solution is stable under refinement
(|q(h) - q(h/4)| <= tol); the Richardson estimate of q(h)'s discretisation error, E = |q(h) - q(h/2)| * 16/15, is
<= tol; and |value - q(h)| + E + floor <= tol. Every decision is made in exact fractions of the doubles.
UNCALIBRATED (owner decision): MIN_RATIO 8, ROUNDING_FLOOR 1e-9 relative, MAX_REL_TOLERANCE 1/100, the ranges below.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

SIMULATOR, VERSION = "chh-ode-rk4", "1"
ORDER = 4
REFINEMENTS = (1, 2, 4)  # the declared step h, then h/2 and h/4
MIN_RATIO = Fraction(8)  # |q(h)-q(h/2)| / |q(h/2)-q(h/4)| >= 2^(ORDER-1): observed order >= 3
ROUNDING_FLOOR = Fraction(1, 10**9)  # relative to max(1, |q|): differences below this are float rounding
MAX_REL_TOLERANCE = Fraction(1, 100)  # K8: tolerance <= 1/100 * max(1, |value|)
MAX_BYTES, MAX_DIM, MAX_STEPS, MAX_DIGITS = 100_000, 6, 20_000, 100
MAX_T_END, MAX_ABS = Fraction(1000), Fraction(100)
PARAMS = {"linear": ("A",), "damped_oscillator": ("omega", "zeta"), "sir": ("beta", "gamma")}
# registered parameter ranges: (low, high, low end open)
RANGES: dict[str, tuple[Fraction, Fraction, bool]] = {
    "omega": (Fraction(0), MAX_ABS, True), "zeta": (Fraction(0), MAX_ABS, False),
    "beta": (Fraction(0), MAX_ABS, True), "gamma": (Fraction(0), MAX_ABS, True)}
_RATIONAL = re.compile(r"-?(0|[1-9][0-9]*)(/[1-9][0-9]*)?")
_KEYS = {"simulator", "version", "model", "params", "x0", "t_end", "steps", "component", "tolerance"}


class SimError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    model: str
    params: dict[str, Any]  # floats (A: list of float rows)
    x0: tuple[float, ...]
    t_end: Fraction
    steps: int
    component: int
    tolerance: Fraction
    sha256: str


def _rational(text: object, what: str) -> Fraction:
    if not isinstance(text, str) or _RATIONAL.fullmatch(text) is None:
        raise SimError(f"{what}: {text!r} is not an exact rational string 'p' or 'p/q' (JSON numbers are refused)")
    return Fraction(text)


def _ranged(text: object, what: str, lo: Fraction, hi: Fraction, lo_open: bool = False) -> Fraction:
    v = _rational(text, what)
    if v > hi or v < lo or (lo_open and v == lo):
        raise SimError(f"{what} = {v} outside the registered range {'(' if lo_open else '['}{lo}, {hi}]")
    return v


def _int(v: object, what: str, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool):
        raise SimError(f"{what} must be an integer")
    if not lo <= v < hi:
        raise SimError(f"{what} {v} outside [{lo}, {hi})")
    return v


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A duplicated key would show a reader one value and the parser another: refused."""
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise SimError(f"duplicate key {dup[0]!r}")
    return dict(pairs)


def _load(data: bytes, fmt: str, keys: set[str]) -> dict[str, Any]:
    if len(data) > MAX_BYTES:
        raise SimError(f"document is larger than {MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")  # canonical UTF-8 only: json would also read UTF-16/32 and a BOM,
        if text.startswith("\ufeff"):  # which K9's byte scan of the same document could not see through
            raise SimError("a byte-order mark is refused")
        doc = json.loads(text, object_pairs_hook=_unique)
    except ValueError as e:
        raise SimError(f"not JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != fmt:
        raise SimError(f"not a {fmt} document")
    extra, missing = set(doc) - keys - {"format", "comment"}, keys - set(doc)
    if extra:
        raise SimError(f"unexpected keys {sorted(extra)}")
    if missing:
        raise SimError(f"missing keys {sorted(missing)}")
    return doc


def parse_config(data: bytes) -> Config:
    doc = _load(data, "chh-sim/1", _KEYS)
    if doc["simulator"] != SIMULATOR:
        raise SimError(f"simulator {doc['simulator']!r} is not the pinned simulator {SIMULATOR}")
    if doc["version"] != VERSION:
        raise SimError(f"version {doc['version']!r} is not the pinned version {VERSION}")
    model, p = doc["model"], doc["params"]
    if model not in PARAMS:
        raise SimError(f"unknown model {model!r}; registered: {sorted(PARAMS)}")
    if not isinstance(p, dict) or set(p) != set(PARAMS[model]):
        raise SimError(f"params must be an object with exactly the keys {list(PARAMS[model])}")
    params: dict[str, Any] = {}
    if model == "linear":
        a = p["A"]
        if not isinstance(a, list) or not 1 <= len(a) <= MAX_DIM or \
                not all(isinstance(r, list) and len(r) == len(a) for r in a):
            raise SimError(f"'A' must be a square matrix of size 1..{MAX_DIM}")
        params["A"] = [[float(_ranged(v, f"A[{i}][{j}]", -MAX_ABS, MAX_ABS)) for j, v in enumerate(r)]
                       for i, r in enumerate(a)]
        dim = len(a)
    else:
        params = {k: float(_ranged(p[k], k, *RANGES[k])) for k in PARAMS[model]}
        dim = 2 if model == "damped_oscillator" else 3
    if not isinstance(doc["x0"], list) or len(doc["x0"]) != dim:
        raise SimError(f"x0 must be a list of {dim} numbers")
    x0 = [_ranged(v, f"x0[{i}]", -MAX_ABS, MAX_ABS) for i, v in enumerate(doc["x0"])]
    if model == "sir" and (min(x0) < 0 or sum(x0) != 1):
        raise SimError("sir state must be non-negative and sum to 1 (S, I, R fractions)")
    t_end = _ranged(doc["t_end"], "t_end", Fraction(0), MAX_T_END, True)
    tol = _rational(doc["tolerance"], "tolerance")
    if tol <= 0:
        raise SimError(f"tolerance {tol} must be > 0")
    return Config(model=model, params=params, x0=tuple(float(v) for v in x0), t_end=t_end,
                  steps=_int(doc["steps"], "steps", 1, MAX_STEPS + 1),
                  component=_int(doc["component"], "component", 0, dim), tolerance=tol,
                  sha256=hashlib.sha256(data).hexdigest())


def _rhs(cfg: Config) -> Callable[[list[float]], list[float]]:
    p = cfg.params
    if cfg.model == "linear":
        return lambda x: [sum((a * b for a, b in zip(row, x, strict=True)), 0.0) for row in p["A"]]
    if cfg.model == "damped_oscillator":
        w2, c = p["omega"] * p["omega"], 2.0 * p["zeta"] * p["omega"]
        return lambda x: [x[1], -w2 * x[0] - c * x[1]]
    b, g = p["beta"], p["gamma"]
    return lambda x: [-b * x[0] * x[1], b * x[0] * x[1] - g * x[1], g * x[1]]


def simulate(cfg: Config, steps: int) -> list[float]:
    """The state at t_end after `steps` fixed RK4 steps (h = t_end / steps)."""
    f, h = _rhs(cfg), float(cfg.t_end / steps)
    x = list(cfg.x0)
    for _ in range(steps):
        k1 = f(x)
        k2 = f([xi + 0.5 * h * ki for xi, ki in zip(x, k1, strict=True)])
        k3 = f([xi + 0.5 * h * ki for xi, ki in zip(x, k2, strict=True)])
        k4 = f([xi + h * ki for xi, ki in zip(x, k3, strict=True)])
        x = [xi + h / 6.0 * (a + 2.0 * b + 2.0 * c + d) for xi, a, b, c, d in zip(x, k1, k2, k3, k4, strict=True)]
    return x


def quantity(cfg: Config, steps: int) -> float:
    return simulate(cfg, steps)[cfg.component]


def scope_phrase(config: bytes, value: Fraction) -> str:
    """The rendering every simulated claim's statement must contain: what is claimed, and IN WHICH simulator."""
    cfg = parse_config(config)
    return (f"x[{cfg.component}](t={cfg.t_end}) of {cfg.model} = {value} ± {cfg.tolerance} in simulator {SIMULATOR} "
            f"(version {VERSION}, config {cfg.sha256[:12]})")


def check(config: bytes, result: bytes, value: Fraction) -> list[str]:
    """K3: why re-running simulator S does NOT support the claim (empty = supported, in S only)."""
    try:
        cfg = parse_config(config)
        doc = _load(result, "chh-sim-result/1", {"config_sha256", "values"})
    except SimError as e:
        return [f"unreadable: {e}"]
    reported = doc["values"]
    if not isinstance(reported, list) or len(reported) != 3 or not all(isinstance(v, str) for v in reported):
        return ["unreadable: values must be 3 strings (repr of q(h), q(h/2), q(h/4))"]
    if doc["config_sha256"] != cfg.sha256:
        return [f"result is for config {str(doc['config_sha256'])[:12]}, not {cfg.sha256[:12]}"]
    q = [quantity(cfg, cfg.steps * r) for r in REFINEMENTS]
    if not all(math.isfinite(v) for v in q):
        return [f"non-finite simulated value(s) {q}: the simulation diverged"]
    reasons = [f"reported q(h/{r}) = {s} not reproduced: re-execution gives {v!r}"
               for r, s, v in zip(REFINEMENTS, reported, q, strict=True) if s != repr(v)]
    q1, q2, q4 = (Fraction(v) for v in q)
    floor = ROUNDING_FLOOR * max(Fraction(1), abs(q4))
    d12, d24, tol = abs(q1 - q2), abs(q2 - q4), cfg.tolerance
    if d24 > floor and d12 < MIN_RATIO * d24:
        reasons.append(f"unconverged: refinements not in RK4's asymptotic range (|q(h)-q(h/2)| = {float(d12):.3g} "
                       f"< {MIN_RATIO} * |q(h/2)-q(h/4)| = {float(d24):.3g})")
    if abs(q1 - q4) > tol:
        reasons.append(f"unconverged: unstable under refinement, |q(h) - q(h/4)| = {float(abs(q1 - q4)):.3g} > "
                       f"tolerance {tol}")
    est = d12 * Fraction(2**ORDER, 2**ORDER - 1)
    if est > tol:
        reasons.append(f"unconverged: Richardson error estimate {float(est):.3g} of q(h) exceeds the tolerance {tol}")
    if abs(value - q1) + est + floor > tol:
        reasons.append(f"claimed value {value} differs from the simulated value q(h) = {q[0]!r} by more than the "
                       f"tolerance {tol} (after the error estimate {float(est):.3g})")
    return reasons


def bounds(config: bytes, result: bytes, value: Fraction) -> list[str]:
    """K8: registered sanity bounds and the model family's background laws at the declared step."""
    try:
        cfg = parse_config(config)
    except SimError as e:
        return [f"unreadable: {e}"]
    reasons = []
    if cfg.tolerance > MAX_REL_TOLERANCE * max(Fraction(1), abs(value)):
        reasons.append(f"tolerance {cfg.tolerance} is looser than {MAX_REL_TOLERANCE} * max(1, |value|)")
    if max(len(str(abs(value.numerator))), len(str(value.denominator))) > MAX_DIGITS:
        reasons.append(f"the claimed value exceeds {MAX_DIGITS} digits in numerator or denominator")
    x, x0 = simulate(cfg, cfg.steps), cfg.x0
    w2 = cfg.params.get("omega", 0.0) * cfg.params.get("omega", 0.0)
    if not all(math.isfinite(v) for v in x):
        reasons.append(f"non-finite state at t_end: {x}")
    elif cfg.model == "sir" and (abs(sum(x) - 1.0) > 1e-9 or min(x) < -1e-9):
        reasons.append(f"sir conservation/positivity violated at t_end: S, I, R = {x}")
    elif cfg.model == "damped_oscillator" and x[1] * x[1] + w2 * x[0] * x[0] > (x0[1] * x0[1] + w2 * x0[0] * x0[0]) \
            * (1 + 1e-9):
        reasons.append(f"energy grew from x0 = {list(x0)} to {x}: a damped oscillator cannot gain energy")
    return reasons


# ------------------------------------------------------------------ fault-injection validation (golden + planted)
_OSC = {"format": "chh-sim/1", "simulator": SIMULATOR, "version": VERSION, "model": "damped_oscillator",
        "params": {"omega": "1", "zeta": "0"}, "x0": ["1", "0"], "t_end": "1", "steps": 50, "component": 0,
        "tolerance": "1/10000"}  # x'' = -x, x(0) = 1: x(1) = cos(1)


def _cfg(**changes: Any) -> bytes:
    return json.dumps({**_OSC, **changes}, sort_keys=True).encode()


def _res(config: bytes, values: list[str] | None = None, sha: str | None = None) -> bytes:
    cfg = parse_config(config)
    vals = values if values is not None else [repr(quantity(cfg, cfg.steps * r)) for r in REFINEMENTS]
    return json.dumps({"format": "chh-sim-result/1", "config_sha256": sha or cfg.sha256, "values": vals}).encode()


def _rejects(why: str, value: str = "5403/10000", config: bytes | None = None, result: bytes | None = None) -> bool:
    """The planted fault is rejected FOR THE RIGHT REASON (K3 or K8)."""
    c = config if config is not None else _cfg()
    try:
        r = result if result is not None else _res(c)
    except SimError:
        r = _res(_cfg())
    return any(why in x for x in check(c, r, Fraction(value)) + bounds(c, r, Fraction(value)))


def _closed_form() -> bool:
    osc = parse_config(_cfg(steps=200))
    decay = parse_config(_cfg(model="linear", params={"A": [["-1"]]}, x0=["1"]))
    return abs(quantity(osc, 200) - math.cos(1)) < 1e-10 and abs(quantity(decay, 200) - math.exp(-1)) < 1e-11


_SIR = _cfg(model="sir", params={"beta": "3", "gamma": "1"}, x0=["99/100", "1/100", "0"], t_end="5", steps=200,
            component=1, tolerance="1/1000")
SIM_CHECKS: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("RK4 matches closed form solutions (cos, exp)", _closed_form),
    ("a correct converged claim is accepted", lambda: check(_cfg(), _res(_cfg()), Fraction(5403, 10000)) == []),
    ("sir golden run conserves S+I+R", lambda: bounds(_SIR, _res(_SIR), Fraction(1, 10)) == []),
    ("a planted wrong value is rejected", lambda: _rejects("differs from the simulated value", "54/100")),
    ("an unconverged step is rejected",
     lambda: _rejects("unconverged", "-1/1000", _cfg(t_end="10", steps=4, tolerance="1/100"))),
    ("a wrong simulator version is refused", lambda: _rejects("pinned version", config=_cfg(version="2"))),
    ("a wrong simulator id is refused", lambda: _rejects("pinned simulator", config=_cfg(simulator="other"))),
    ("a result of another config is rejected", lambda: _rejects("result is for config", result=_res(_cfg(steps=60)))),
    ("a reported value that is not reproduced is rejected",
     lambda: _rejects("not reproduced", result=_res(_cfg(), ["0.5403", "0.5403", "0.5403"]))),
    ("a NaN reported value is rejected", lambda: _rejects("not reproduced", result=_res(_cfg(), ["nan"] * 3))),
    ("a diverging (inf/NaN) simulation is rejected", lambda: _rejects("non-finite", "1", _cfg(
        model="linear", params={"A": [["100"]]}, x0=["1"], t_end="1000", steps=10, tolerance="1"))),
    ("a NaN/float parameter is refused", lambda: _rejects("exact rational string", config=_cfg(
        params={"omega": float("nan"), "zeta": "0"}))),
    ("an out-of-range parameter is refused", lambda: _rejects("outside the registered range", config=_cfg(
        params={"omega": "1000", "zeta": "0"}))),
    ("an out-of-range step count is refused", lambda: _rejects("outside", config=_cfg(steps=MAX_STEPS + 1))),
    ("a too loose tolerance is flagged", lambda: _rejects("looser than", config=_cfg(tolerance="1"))),
    ("an energy-gaining (unstable) step violates the background law",
     lambda: _rejects("energy grew", config=_cfg(t_end="3", steps=1))),
    ("a duplicated JSON key is refused",
     lambda: _rejects("duplicate key", config=_cfg().replace(b'"steps": 50', b'"steps": 50, "steps": 51'))),
)
