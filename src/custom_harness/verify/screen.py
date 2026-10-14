"""Float screen -> exact certificate gate (GOAL §5 L3, §4.7 computational class; The Station row of §9).

Searching many candidates with exact arithmetic is slow, so a cheap FLOAT screen runs first. Floats round, so a
screen can call a wrong candidate right (a false positive). The rule: the screen may only PRUNE. Every candidate it
keeps goes to the EXACT checker, and only the exact checker's empty reason list makes a `Confirmed` result. Nothing
the screen returns (a score, a value) is carried into a confirmation; the confirmed value comes from the candidate's
exact rational claim, checked exactly.

Fail-safe direction: a screen that crashes or returns anything but a bool has made no prune decision, so the
candidate goes to the exact checker (costs time, never soundness). An exact checker that crashes, or returns
anything but an empty list, refutes. A pruned true result is lost (recorded in `pruned`), never wrongly certified.

`Confirmed` can only be built inside `screen_then_certify` (a module-private token; trusted code could still forge
one in Python, which is accountability-level protection, like the rest of the trusted base). The Kernel re-checks
every submitted LP certificate exactly anyway (K3 via `kernel.lp.check`), so a confirmation is a filter on what is
submitted, never a certificate by itself.

LP instance (reuses the `lp` adapter's exact checker, `kernel.lp.check`): `lp_float_screen` / `lp_exact`.

Claim path (s3-verifiers): `certify_confirmed_lp` is the submission step for computational (exact-class) LP claims. It
takes only `Confirmed` results (a screen survivor, a bare candidate or a candidate id is refused with TypeError), checks
that the candidate submitted is byte-for-byte the one the exact checker confirmed (`candidate_hash`), and then goes
through the Kernel (pre-register, record the certificate artifact, certify), which re-checks it exactly (K3). A screen
pass alone therefore never reaches a certificate (invariant 21).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from ..kernel import lp
from ..kernel.claim import canonical_json
from . import l3_config as C

_GATE = object()  # only screen_then_certify holds this
# every confirmation the gate issued (its full content): `dataclasses.replace` copies the token, so a forged copy with
# another id/hash/value passes __post_init__ but is not in this set (certify_confirmed_lp checks it)
_ISSUED: set[tuple[str, str, str, Fraction | None]] = set()


@dataclass(frozen=True)
class Confirmed:
    candidate_id: str
    checker: str
    value: Fraction | None
    candidate_hash: str
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _GATE:
            raise TypeError("a Confirmed result is made only by the exact gate (screen_then_certify)")


@dataclass(frozen=True)
class GateResult[T]:
    confirmed: tuple[Confirmed, ...]
    refuted: tuple[tuple[str, tuple[str, ...]], ...]  # kept by the screen, rejected exactly (incl. false positives)
    pruned: tuple[str, ...]  # dropped by the screen (never checked exactly)
    screen_errors: tuple[str, ...]  # screen crashed / returned a non-bool: forwarded to the exact checker


def _hash(candidate: object) -> str:
    try:
        data = canonical_json(_jsonable(candidate))
    except (TypeError, ValueError):
        data = repr(candidate).encode()
    return hashlib.sha256(data).hexdigest()


def _jsonable(candidate: object) -> Any:
    if isinstance(candidate, LPCandidate):
        return {"certificate": candidate.certificate, "value": candidate.value}
    return candidate


def screen_then_certify[T](candidates: Sequence[tuple[str, T]], *, screen: Callable[[T], object],
                        exact: Callable[[T], object], checker: str) -> GateResult[T]:
    ids = [cid for cid, _ in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate candidate ids")
    confirmed: list[Confirmed] = []
    refuted: list[tuple[str, tuple[str, ...]]] = []
    pruned: list[str] = []
    errors: list[str] = []
    for cid, cand in candidates:
        try:
            keep = screen(cand)
        except Exception:  # noqa: BLE001  a crashing screen made no prune decision
            keep = None
        if keep is False:
            pruned.append(cid)
            continue
        if keep is not True:
            errors.append(cid)
        try:
            reasons = exact(cand)
        except Exception as e:  # noqa: BLE001  a crashing exact checker refutes (fail closed)
            reasons = [f"exact checker crashed: {type(e).__name__}"]
        if isinstance(reasons, list) and not reasons:
            conf = Confirmed(candidate_id=cid, checker=checker, value=_value(cand), candidate_hash=_hash(cand),
                             _token=_GATE)
            _ISSUED.add((conf.candidate_id, conf.checker, conf.candidate_hash, conf.value))
            confirmed.append(conf)
        else:
            why = tuple(str(r) for r in reasons) if isinstance(reasons, list) else (
                f"exact checker returned {type(reasons).__name__}, not a list of reasons",)
            refuted.append((cid, why))
    return GateResult(tuple(confirmed), tuple(refuted), tuple(pruned), tuple(errors))


def submittable(result: GateResult[Any]) -> tuple[Confirmed, ...]:
    """What may be submitted to the Kernel: the exact confirmations only (never the screen's survivors)."""
    return result.confirmed


def _value(cand: object) -> Fraction | None:
    return lp.parse_rational(cand.value) if isinstance(cand, LPCandidate) else None


# ------------------------------------------------------------------ LP instance (the `lp` adapter's exact checker)
@dataclass(frozen=True)
class LPCandidate:
    certificate: dict[str, Any]  # chh-lp-certificate/1, exact rational strings
    value: str  # the claimed optimum, an exact rational string


def lp_exact(problem: dict[str, Any]) -> Callable[[LPCandidate], list[str]]:
    p = canonical_json(problem)

    def check(c: LPCandidate) -> list[str]:
        try:
            value = lp.parse_rational(c.value)
        except lp.LPError as e:
            return [f"claimed value: {e}"]
        return lp.check(p, canonical_json(c.certificate), value) + lp.bounds(p, canonical_json(c.certificate), value)
    return check


def lp_float_screen(problem: dict[str, Any], slack: float = C.SCREEN_FLOAT_SLACK) -> Callable[[LPCandidate], bool]:
    """A deliberately ordinary float check of the same conditions (feasibility, dual feasibility, no gap, value),
    with a relative slack. It is allowed to be wrong in both directions; the gate makes that harmless."""
    doc = json.loads(canonical_json(problem))
    c = [float(Fraction(v)) for v in doc["c"]]
    a = [[float(Fraction(v)) for v in row] for row in doc["A"]]
    b = [float(Fraction(v)) for v in doc["b"]]

    def screen(cand: LPCandidate) -> bool:
        x = [float(Fraction(v)) for v in cand.certificate["x"]]
        y = [float(Fraction(v)) for v in cand.certificate["y"]]
        v = float(Fraction(cand.value))

        def le(lhs: float, rhs: float) -> bool:
            return lhs <= rhs + slack * max(1.0, abs(rhs))
        if min(x + y, default=0.0) < -slack:
            return False
        if not all(le(sum(r * xi for r, xi in zip(row, x, strict=True)), bi) for row, bi in zip(a, b, strict=True)):
            return False
        if not all(le(cj, sum(a[i][j] * y[i] for i in range(len(b)))) for j, cj in enumerate(c)):
            return False
        primal = sum(ci * xi for ci, xi in zip(c, x, strict=True))
        dual = sum(bi * yi for bi, yi in zip(b, y, strict=True))
        return abs(primal - dual) <= slack * max(1.0, abs(v)) and abs(primal - v) <= slack * max(1.0, abs(v))
    return screen


def certify_confirmed_lp(kernel: Any, confirmed: Sequence[Confirmed], candidates: Mapping[str, LPCandidate],
                         claim_for: Callable[[str, LPCandidate], Any]) -> dict[str, Any]:
    """Submit exactly the gate-confirmed LP candidates to the Kernel: {candidate id: Kernel certificate}.
    `claim_for(cid, candidate)` builds the computational claim (trusted code). Refuses (TypeError / ValueError), before
    anything is registered, anything that is not a `Confirmed` from this gate, a confirmation without its candidate,
    or a candidate whose content differs from the one confirmed."""
    items = list(confirmed)
    if len({c.candidate_id for c in items if isinstance(c, Confirmed)}) != len(items):
        if not all(isinstance(c, Confirmed) for c in items):
            raise TypeError("only exact-gate confirmations are submitted (a float screen pass never grants)")
        raise ValueError("duplicate confirmations")
    for c in items:
        if not isinstance(c, Confirmed):
            raise TypeError(f"only exact-gate confirmations are submitted, not {type(c).__name__} (a float screen "
                            "pass never grants)")
        if (c.candidate_id, c.checker, c.candidate_hash, c.value) not in _ISSUED:
            raise TypeError(f"confirmation {c.candidate_id!r} was not issued by the exact gate (a copy or forgery)")
        if c.checker != "lp":
            raise ValueError(f"confirmation {c.candidate_id!r} is from checker {c.checker!r}, not the lp checker")
        cand = candidates.get(c.candidate_id)
        if not isinstance(cand, LPCandidate) or _hash(cand) != c.candidate_hash:
            raise ValueError(f"candidate {c.candidate_id!r} is not the one the exact checker confirmed")
    out: dict[str, Any] = {}
    for c in items:
        cand = candidates[c.candidate_id]
        if _hash(cand) != c.candidate_hash:  # re-checked at use: the mapping or certificate dict may have changed
            raise ValueError(f"candidate {c.candidate_id!r} changed after the check")
        claim = claim_for(c.candidate_id, cand)
        kernel.preregister(claim)
        aid = kernel.vault.register_artifact(f"lp-certificate-{c.candidate_hash[:16]}",
                                             canonical_json(cand.certificate), "certificate").artifact_id
        out[c.candidate_id] = kernel.certify(claim, [kernel.record_certificate(claim, aid)])
    return out
