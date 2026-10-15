"""Exact rational modified nodal analysis: the certificate PRODUCER of the `circuit` adapter.

Not part of the soundness argument: every certificate it emits is re-checked by `check.check` / `check.bounds`
(which never solve), so a bug here can only make an honest claim fail, never make a false one pass. It is still part
of the adapter's identity (hashed), because the adapter's validation evidence uses it to build its golden
certificates.

Method: Gauss-Jordan elimination in fractions.Fraction on the augmented matrix [A | b | I] (pivot = first non-zero
entry in the column; exact, so no pivoting strategy is needed for accuracy). With E the accumulated row operations,
E A = R is the reduced row echelon form of A. Then:
    rank n  -> A regular; E = A^-1; x = E b                          (certificate keys "x", "inverse")
    rank < n -> a free column f gives a null vector                   ("null_vector")
               a zero row i of R with (E b)_i != 0 gives y = E[i]: y^T A = 0, y^T b != 0   ("left_null")
               otherwise the free variables set to 0 give a particular solution x            ("x")
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction

from . import check as C


class SolverError(ValueError):
    pass


@dataclass(frozen=True)
class Reduction:
    rref: list[list[Fraction]]  # R = E A
    rhs: list[Fraction]  # E b
    ops: list[list[Fraction]]  # E
    pivots: list[int]  # pivot column of rows 0..rank-1


def reduce(a: list[list[Fraction]], b: list[Fraction]) -> Reduction:
    n = len(a)
    rows = [[*a[i], b[i], *(Fraction(int(i == j)) for j in range(n))] for i in range(n)]
    pivots: list[int] = []
    r = 0
    for col in range(n):
        pivot = next((i for i in range(r, n) if rows[i][col] != 0), None)
        if pivot is None:
            continue
        rows[r], rows[pivot] = rows[pivot], rows[r]
        p = rows[r][col]
        rows[r] = [v / p for v in rows[r]]
        for i in range(n):
            if i != r and rows[i][col] != 0:
                f = rows[i][col]
                rows[i] = [v - f * w for v, w in zip(rows[i], rows[r], strict=True)]
        pivots.append(col)
        r += 1
    return Reduction(rref=[row[:n] for row in rows], rhs=[row[n] for row in rows],
                     ops=[row[n + 1:] for row in rows], pivots=pivots)


def _s(v: Fraction) -> str:
    return str(v)


def certify(problem: bytes) -> tuple[Fraction, bytes]:
    """(the exact value of the problem's query, a certificate the checker accepts for it). Raises SolverError when
    the query has no determined value (a value quantity of a singular network)."""
    c = C.parse_problem(problem)
    a, b = C.mna(c)
    n = len(a)
    red = reduce(a, b)
    regular = len(red.pivots) == n
    doc: dict[str, object] = {"format": C.CERT_FORMAT, "problem_sha256": c.sha256, "unknowns": list(c.unknowns)}
    q = c.query.quantity
    inverse = [[_s(v) for v in row] for row in red.ops]
    if q in C.VALUE_QUANTITIES:
        if not regular:
            raise SolverError(f"the MNA matrix is singular (rank {len(red.pivots)} < {n}): {C.describe(c)} is not "
                              f"determined")
        x = red.rhs
        value = C.quantity(c, x)
        doc |= {"x": [_s(v) for v in x], "inverse": inverse}
    elif q == "singular":
        value = Fraction(0 if regular else 1)
        if regular:
            doc["inverse"] = inverse
        else:
            free = next(j for j in range(n) if j not in red.pivots)
            v = [Fraction(0)] * n
            v[free] = Fraction(1)
            for i, pc in enumerate(red.pivots):
                v[pc] = -red.rref[i][free]
            doc["null_vector"] = [_s(e) for e in v]
    else:  # inconsistent
        bad = next((i for i in range(len(red.pivots), n) if red.rhs[i] != 0), None)
        value = Fraction(0 if bad is None else 1)
        if bad is None:
            x = [Fraction(0)] * n
            for i, pc in enumerate(red.pivots):
                x[pc] = red.rhs[i]
            doc["x"] = [_s(e) for e in x]
        else:
            doc["left_null"] = [_s(e) for e in red.ops[bad]]
    return value, json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
