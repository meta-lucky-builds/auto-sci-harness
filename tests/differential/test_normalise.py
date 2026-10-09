"""Fast tests of the differential diff tool itself (no Kernel run): normalisation removes only what it must, and every
behaviour change in a recording (status, code, reason, observation, label, outcome, extra call) is reported."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import normalise as N

CH = "a" * 40 + "b" * 24  # a claim hash
CH2 = "c" * 64


def _call(i: int, *, claim_hash: str = CH, ev: tuple[str, ...] = ("ev_0123456789abcdef", "ev_fedcba9876543210"),
          issued: float = 1.5, resolve_by: float = 9.0, k5: str = "subject and comparator differ (identical)",
          adapter_hash: str = "d" * 64) -> dict[str, Any]:
    return {"t": "call", "scope": "tests/x.py::t", "i": i, "api": "certify", "kwargs": {"evidence_ids": list(ev)},
            "result": {"claim_hash": claim_hash, "claim": {"resolve_by": resolve_by, "statement": "s"},
                       "label": "REJECTED", "tier": 6, "issued_at": issued, "evidence_ids": list(ev),
                       "adapters": [{"name": "ml", "hash": adapter_hash, "seq": 1}],
                       "checks": {"K3": {"status": "pass", "reasons": [], "observations": [f"{ev[0]} replayed"]},
                                  "K5": {"status": "fail", "codes": ["k5_not_paired"], "reasons": [k5]},
                                  "K9": {"status": "fail", "codes": ["k9_x"],
                                         "reasons": [f"claim {claim_hash[:12]} leaked"]}}}}


def _write(tmp: Path, name: str, recs: list[dict[str, Any]]) -> Path:
    p = tmp / name
    p.write_text("\n".join(json.dumps(r) for r in [{"t": "header"}, *recs]) + "\n", encoding="utf-8")
    return p


def test_nondeterministic_fields_normalise_away(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.jsonl", [_call(0)])
    b = _write(tmp_path, "b.jsonl", [_call(0, claim_hash=CH2, ev=("ev_1111111111111111", "ev_2222222222222222"),
                                          issued=7.0, resolve_by=11.0)])
    assert N.diff_files(a, b, raw=True) == 1  # raw: they differ
    assert N.diff_files(a, b) == 0  # every difference is a proven-nondeterministic field


def test_adapter_hash_is_masked_only_by_its_own_rule(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.jsonl", [_call(0)])
    b = _write(tmp_path, "b.jsonl", [_call(0, adapter_hash="e" * 64)])
    assert N.diff_files(a, b) == 0
    assert N.diff_files(a, b, mask_adapter_hash=False) == 1


def test_renaming_keeps_which_ids_are_equal(tmp_path: Path) -> None:
    # the same evidence id used twice is NOT the same as two different ids
    a = _write(tmp_path, "a.jsonl", [_call(0, ev=("ev_1111111111111111", "ev_2222222222222222"))])
    b = _write(tmp_path, "b.jsonl", [_call(0, ev=("ev_3333333333333333", "ev_3333333333333333"))])
    assert N.diff_files(a, b) == 1


def test_every_behaviour_change_is_reported(tmp_path: Path, capsys: Any) -> None:
    base = _call(0)
    a = _write(tmp_path, "a.jsonl", [base, {"t": "test", "scope": "tests/x.py::t", "when": "call",
                                            "outcome": "passed"}])
    changes = {
        "reason": lambda r: r["result"]["checks"]["K5"]["reasons"].__setitem__(0, "differ (the same)"),
        "status": lambda r: r["result"]["checks"]["K3"].__setitem__("status", "fail"),
        "code": lambda r: r["result"]["checks"]["K5"].__setitem__("codes", ["k5_wrong_code"]),
        "observation": lambda r: r["result"]["checks"]["K3"]["observations"].append("1.00 bits"),
        "label": lambda r: r["result"].__setitem__("label", "CERTIFIED-INTERNAL"),
        "tier": lambda r: r["result"].__setitem__("tier", 2),
        "statement": lambda r: r["result"]["claim"].__setitem__("statement", "t"),
    }
    for name, change in changes.items():
        rec = copy.deepcopy(base)
        change(rec)
        b = _write(tmp_path, f"b_{name}.jsonl", [rec, {"t": "test", "scope": "tests/x.py::t", "when": "call",
                                                       "outcome": "passed"}])
        assert N.diff_files(a, b) == 1, name
    failed = _write(tmp_path, "b_outcome.jsonl", [base, {"t": "test", "scope": "tests/x.py::t", "when": "call",
                                                         "outcome": "failed"}])
    assert N.diff_files(a, failed) == 1
    extra = _write(tmp_path, "b_extra.jsonl", [base, _call(1)])
    assert N.diff_files(a, extra) == 1
    capsys.readouterr()


def test_flaky_cells_mask_exactly_one_cell(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.jsonl", [_call(0)])
    rec = _call(0)
    rec["result"]["checks"]["K3"]["observations"] = ["noise 0.37"]
    b = _write(tmp_path, "b.jsonl", [rec])
    flaky = tmp_path / "flaky.json"
    N.explain(a, b, flaky)
    assert N.diff_files(a, b, flaky_files=[flaky]) == 0
    rec["result"]["checks"]["K5"]["reasons"] = ["changed"]
    c = _write(tmp_path, "c.jsonl", [rec])
    assert N.diff_files(a, c, flaky_files=[flaky]) == 1  # a change outside the flaky cell is still found
