"""Calibrated K3 tolerance for research claims (GOAL §5 L3 "Calibrated tolerances"; `verify/tolerance.py`).

`research.claim` with param `calibrate_tolerance: true` calls `claim_tolerance_for(...)` before it builds the claim.
Trusted code (this module; the claim node is protected from harness edits) measures the candidate program's noise
floor on PUBLIC development data only, through the same k-fold dev evaluation the loop already uses
(`modules._dev_predict`, every fold in a fresh sandbox, the program's --seed fixed):

  reference   one dev evaluation at seed 0;
  nominal     n more dev evaluations at seed 0 (same inputs, fresh sandboxes);
  perturbed   n dev evaluations at seed 0 with each fold's TRAINING rows shuffled (a harmless perturbation: the
              validation rows and their order, hence the label alignment, are unchanged).

The tolerance is `tolerance.claim_tolerance(DATA, floor)`: the split-conformal bound over these sources at the
owner-set miss rate (`l3_config.TOL_CLAIM_MISS_RATE`), refused above the Kernel's K1 ceiling. Without the param, or
when any re-run fails or calibration refuses, the tolerance is 0.0 (identical predictions on replay: today's
behaviour, the strictest K3). Nothing here reads a held-out label; the floor's record (dev scores only) goes to
state for audit, and `make_claim` freezes the value into the pre-registered claim (invariant 17).

Assumption (stated): a held-out replay's deviation is exchangeable with the dev re-runs' deviations (same program,
same environment, different rows). A program that is deliberately noisy on dev to widen its tolerance gains at most
the Kernel's own K1 ceiling (`max_reproduce_tolerance`), which a hand-set tolerance could already declare.
Cost: (1 + 2n) dev evaluations, n = `tolerance.min_runs(miss_rate)` (19 at the default 0.10).
"""

from __future__ import annotations

import random
from typing import Any

from ..kernel.claim import EvidenceClass
from ..runtime.modules import ModuleContext
from ..verify import l3_config as L3
from ..verify import tolerance as TOL


def _shuffled_rows(data: bytes, seed: int) -> bytes:
    """The same CSV with its data rows (not the header) in another order; line terminators kept."""
    lines = data.splitlines(keepends=True)
    if len(lines) < 3:
        return data
    header, rows = lines[0], lines[1:]
    term = b"\r\n" if rows[0].endswith(b"\r\n") else b"\n"
    last_open = not rows[-1].endswith((b"\n", b"\r"))
    if last_open:
        rows[-1] += term
    random.Random(seed).shuffle(rows)
    if last_open:
        rows[-1] = rows[-1][: -len(term)]
    return header + b"".join(rows)


def measure_dev_noise_floor(state: dict[str, Any], ctx: ModuleContext, code: bytes, n: int,
                            purpose: str = L3.TOL_CLAIM_PURPOSE) -> TOL.NoiseFloor:
    from .modules import _dev_labels, _dev_predict, _score, _task  # the loop's own dev evaluation
    task, labels = _task(state), _dev_labels(state, ctx)

    def score(st: dict[str, Any], name: str) -> float | None:
        ctx.budget.check()  # BudgetExceeded propagates: the run's caps bound the calibration too
        preds, _ = _dev_predict(ctx, st, code, name, seed=0)
        return _score(task, labels, preds)

    def rerun(source: str, i: int) -> float | None:
        if source == "nominal":
            return score(state, f"noise-nominal-{i}")
        if source == "perturbed":
            folds = [{**f, "train_ref": ctx.store.blobs.put(_shuffled_rows(ctx.store.blobs.get(f["train_ref"]),
                                                                           1000 * i + k))}
                     for k, f in enumerate(state["dev_folds"])]
            return score({**state, "dev_folds": folds}, f"noise-perturbed-{i}")
        raise TOL.ToleranceError(f"source {source!r} cannot be measured on dev data")

    return TOL.measure(rerun, score(state, "noise-reference"), purpose=purpose, n=n)


def claim_tolerance_for(state: dict[str, Any], params: dict[str, Any], ctx: ModuleContext,
                        code: bytes) -> tuple[float, dict[str, Any]]:
    """(the tolerance to freeze into the claim, a record for state). Only `calibrate_tolerance` is read from params;
    the miss rate and sources are owner config (l3_config)."""
    if params.get("calibrate_tolerance") is not True:
        r = TOL.claim_tolerance(EvidenceClass.DATA, None)
        return float(r.value or 0.0), {"source": r.source, "value": r.value, "reasons": list(r.reasons)}
    n = TOL.min_runs(L3.TOL_CLAIM_MISS_RATE)
    try:
        floor = measure_dev_noise_floor(state, ctx, code, n)
    except TOL.ToleranceError as e:
        r = TOL.claim_tolerance(EvidenceClass.DATA, None)
        return float(r.value or 0.0), {"source": r.source, "value": r.value,
                                       "reasons": [*r.reasons, f"noise floor not measured: {e}"]}
    r = TOL.claim_tolerance(EvidenceClass.DATA, floor)
    rec: dict[str, Any] = {"source": r.source, "value": r.value, "reasons": list(r.reasons),
                           "miss_rate": L3.TOL_CLAIM_MISS_RATE, "purpose": L3.TOL_CLAIM_PURPOSE, "runs_per_source": n,
                           "floor": {"reference": floor.reference, "nominal": list(floor.nominal),
                                     "perturbed": list(floor.perturbed)}}
    if r.value is None:  # refused for the class: stay exact
        return 0.0, rec
    return float(r.value), rec


__all__ = ["claim_tolerance_for", "measure_dev_noise_floor"]
