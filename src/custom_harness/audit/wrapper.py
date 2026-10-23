"""The contract wrapper both arms of an audit run through (GOAL §16.10: "both arms through the same wrapper").

An arm file defines `build_model(seed)` returning an object with `fit(X, y)` and `predict(X)` (the sklearn
estimator API; X is a list of float rows, y a list of label strings). The program an arm runs is its file followed
by WRAPPER_SOURCE, byte for byte the same for both arms. The wrapper owns everything the contract fixes: it reads
the train and test files the Kernel's command contract passes (--train, --test, --out, --seed), parses every
non-label column as a float (empty -> nan), calls the arm, and writes the predictions file. Definitions after the
arm's code win, so an arm cannot replace the wrapper's I/O.

Trust: the arm code is untrusted and runs in the candidate sandbox; the wrapper is trusted host code (hashed into
every audit claim as `wrapper_sha256`).
"""

from __future__ import annotations

import hashlib
from typing import Any

WRAPPER_SOURCE = '''

# ---- chh-audit contract wrapper (identical for both arms) ----
def _chh_audit_main():
    import argparse as _ap
    import csv as _csv
    import math as _math
    p = _ap.ArgumentParser()
    for k in ("--train", "--test", "--out"):
        p.add_argument(k, required=True)
    p.add_argument("--seed", type=int, required=True)
    a = p.parse_args()

    def _num(v):
        try:
            return float(v) if v != "" else _math.nan
        except ValueError:
            return _math.nan

    with open(a.train, newline="", encoding="utf-8") as f:
        rows = list(_csv.reader(f))
    head = rows[0]
    li = head.index("label")
    fi = [i for i in range(len(head)) if i != li]
    X = [[_num(r[i]) for i in fi] for r in rows[1:]]
    y = [r[li] for r in rows[1:]]
    with open(a.test, newline="", encoding="utf-8") as f:
        trows = list(_csv.reader(f))
    th = trows[0]
    ti = [th.index(head[i]) for i in fi]
    Xt = [[_num(r[i]) for i in ti] for r in trows[1:]]
    model = build_model(a.seed)  # noqa: F821  defined by the arm's code above
    model.fit(X, y)
    preds = list(model.predict(Xt))
    if len(preds) != len(Xt):
        raise SystemExit(f"arm returned {len(preds)} predictions for {len(Xt)} rows")
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f, lineterminator="\\n")
        w.writerow(["prediction"])
        for v in preds:
            w.writerow([str(v)])


if __name__ == "__main__":
    _chh_audit_main()
'''


def wrapper_hash() -> str:
    return hashlib.sha256(WRAPPER_SOURCE.encode("utf-8")).hexdigest()


def wrapper_spec_ref(store: Any, harness_id: str) -> str:
    """The `harness_hash` of a non-engine arm run (Claim Audit `chh-audit`, Court `chh-court`): the content address of
    a JSON spec holding the wrapper source and its hash, so `store.harness_spec(run.harness_hash)` resolves and the
    run's Reproducibility Bundle can be built (invariant 9). int18 stage-c: the arm runs carried the raw
    wrapper_hash(), which names no stored spec, so every arm bundle failed (FileNotFoundError, recorded as an error)."""
    return str(store.blobs.put_json({"format": "chh-arm-wrapper/1", "harness_id": harness_id,
                                     "wrapper_sha256": wrapper_hash(), "wrapper_source": WRAPPER_SOURCE}))


def compose(arm_code: bytes) -> bytes:
    """The program one arm runs: its code, a newline, then the shared wrapper."""
    return arm_code.rstrip(b"\n") + b"\n" + WRAPPER_SOURCE.encode("utf-8")


def arm_part(program: bytes) -> bytes | None:
    """The arm code of a composed program, or None if the program does not end with the exact wrapper."""
    tail = WRAPPER_SOURCE.encode("utf-8")
    return program[: -len(tail)] if program.endswith(tail) else None
