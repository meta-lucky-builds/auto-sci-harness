"""Axis G shows unknown USD as "unknown", never as a 0 sum (s3-report finding; s4-integrity item 7).

Fail-first reasoning (NOT run: owner rule): at 82719e1 axis_g summed float(r["cost"]["usd"]) over runs, so a subscription
(byos) run recorded at usd 0.0 made the total $0 (and a facts record with usd None raised TypeError); facts._run
recorded usd 0.0 for unpriced calls.
"""

from __future__ import annotations

from custom_harness.scorecard.axes import axis_g


def _run(ref: str, usd: float | None) -> dict:
    return {"ref": ref, "cost": {"usd": usd, "gpu_hours": 0.0, "quota_units": 1.0, "tokens": 10, "estimated": False}}


def test_unknown_usd_is_shown_as_unknown() -> None:
    g = axis_g({"runs": [_run("r1", None), _run("r2", 0.25)], "certificates": []})
    assert g["total_cost"]["value"]["usd"] == "unknown"
    assert g["total_cost"]["runs_usd_unknown"] == ["r1"]
    assert g["total_cost"]["value"]["tokens"] == 20


def test_known_usd_still_sums() -> None:
    g = axis_g({"runs": [_run("r1", 0.5), _run("r2", 0.25)], "certificates": []})
    assert g["total_cost"]["value"]["usd"] == 0.75
    assert g["total_cost"]["runs_usd_unknown"] == []


def test_cost_per_certified_claim_keeps_unknown() -> None:
    cert = {"ref": "c1", "label": "CERTIFIED-T1", "latest_for_claim": True, "retracted": False}
    g = axis_g({"runs": [_run("r1", None)], "certificates": [cert]})
    assert g["cost_per_certified_claim"]["value"]["usd"] == "unknown"
    assert g["cost_per_certified_claim"]["value"]["quota_units"] == 1.0
