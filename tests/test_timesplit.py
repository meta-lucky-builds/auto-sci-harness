"""Time-split benchmark (GOAL §6.4): freeze at T, register on frozen information, resolve on later windows, score,
probe for contamination, compare seed vs evolved. Offline fixtures only (no network, no model calls)."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from custom_harness.literature.http import HttpResponse
from custom_harness.literature.snapshot import SnapshotStore
from custom_harness.store.blobs import BlobStore
from custom_harness.timesplit import (
    EVOLVED_BETTER,
    NOT_SHOWN,
    DataFile,
    FakeMemorisationProbe,
    Observation,
    Registration,
    Task,
    TimesplitError,
    audit,
    compare,
    freeze_benchmark,
    load_benchmark,
    register_prediction,
    resolve,
    score,
)
from custom_harness.timesplit import config as tsconfig

T = date(2026, 1, 1)
T1 = date(2026, 4, 1)
T2 = date(2026, 7, 1)
WINDOWS = [(T, T1), (T1, T2)]
CLOCK = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
OA = "https://api.openalex.org/works"

ENTRIES = [
    ("sparse", {"title": "Sparse gradient routing for tabular models", "abstract": "We study sparse routing."}),
    ("stopping", {"title": "Calibrated early stopping", "abstract": "A rule for early stopping."}),
    ("routing2", {"title": "Another paper", "abstract": "More on Sparse   Routing."}),
]
PLANTED = ("planted", {"abstract": "Our run: zetabench   TOP score\n0.93 (preliminary)."})


def rule(field: str = "score", op: str = ">=", value: float = 0.5) -> dict:
    return {"kind": "threshold", "format": "json", "field": field, "op": op, "value": value}


def task(tid: str, phrases: list[str], window: int = 0, answer: str | None = None, **rkw) -> dict:
    return {"id": tid, "question": f"Will {tid} reach the threshold?", "rule": rule(**rkw), "window": window,
            "key_phrases": phrases, "answer": answer or f"answer-of-{tid}-xyzzy"}


BASE_TASKS = [
    task("t_route", ["sparse routing"]),
    task("t_stop", ["early stopping"]),
    task("t_fresh", ["quantum widget"]),
    task("t_leak", ["zetabench"], answer="ZetaBench top score 0.93"),
    task("t_before", ["metric a"], field="metric_a"),
    task("t_w1", ["far future"], window=1),
]
FROZEN_DATA = [DataFile(name="baseline.json", content=b'{"metric_a": 0.5}', dated=date(2025, 12, 15))]


def make_world(root: Path, tasks=None, plant: bool = True, data=None):
    snaps = SnapshotStore(root / "lit", clock=lambda: CLOCK)
    for name, body in ENTRIES + ([PLANTED] if plant else []):
        snaps.record(OA, {"search": name}, HttpResponse(status=200, text=json.dumps(body)))
    blobs = BlobStore(root / "blobs")
    bm = freeze_benchmark(snapshots=snaps, blobs=blobs, as_of=T, windows=WINDOWS,
                          tasks=BASE_TASKS if tasks is None else tasks,
                          data_files=FROZEN_DATA if data is None else data, label="fixture")
    return snaps, blobs, bm


def obs(payload: dict, dated: date, source: str = "fixture://post-T") -> Observation:
    return Observation(content=json.dumps(payload, sort_keys=True).encode(), dated=dated, source=source)


# -- freezing (G1 and the window/task checks)

def test_freeze_records_snapshot_data_and_roundtrips(tmp_path):
    snaps, blobs, bm = make_world(tmp_path)
    assert snaps.manifest(bm.literature_snapshot)["as_of"] == T.isoformat()
    assert bm.data["baseline.json"].dated == date(2025, 12, 15)
    assert load_benchmark(blobs, bm.id) == bm
    assert blobs.put_json(bm.body()) == bm.id  # content addressed


def test_g1_data_file_dated_after_T_refused(tmp_path):
    late = DataFile(name="late.json", content=b'{"score": 1}', dated=date(2026, 1, 2))
    with pytest.raises(TimesplitError, match=r"data file 'late.json' is dated 2026-01-02, after T=2026-01-01"):
        make_world(tmp_path, data=[late])
    on_t = DataFile(name="on_t.json", content=b'{"x": 1}', dated=T)  # dated == T is known at T: allowed
    _, _, bm = make_world(tmp_path / "ok", data=[on_t])
    assert "on_t.json" in bm.data


def test_task_window_must_be_declared_and_windows_well_formed(tmp_path):
    with pytest.raises(TimesplitError, match="window index 5 is not one of the declared windows"):
        make_world(tmp_path, tasks=[task("t_x", ["p"], window=5)])
    snaps = SnapshotStore(tmp_path / "lit2", clock=lambda: CLOCK)
    with pytest.raises(TimesplitError, match="first window must start at T"):
        freeze_benchmark(snapshots=snaps, blobs=BlobStore(tmp_path / "b2"), as_of=T,
                         windows=[(date(2025, 12, 1), T1)], tasks=[])
    with pytest.raises(TimesplitError, match="windows must be contiguous"):
        freeze_benchmark(snapshots=snaps, blobs=BlobStore(tmp_path / "b2"), as_of=T,
                         windows=[(T, T1), (date(2026, 5, 1), T2)], tasks=[])


def test_rule_language_is_closed():
    bad = task("t_bad", ["p"])
    bad["rule"] = {"kind": "llm_judge", "format": "json", "field": "x", "op": ">=", "value": 1}
    with pytest.raises(ValueError, match="unknown rule kind 'llm_judge'"):
        Task.model_validate(bad)
    extra = task("t_extra", ["p"])
    extra["rule"] = {**rule(), "eval": "__import__('os')"}
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        Task.model_validate(extra)


# -- registration (G2)

def test_g2_registration_after_T_and_q_below_p0_refused(tmp_path):
    _, _, bm = make_world(tmp_path)
    with pytest.raises(TimesplitError, match=r"registered on 2026-01-02, after T=2026-01-01"):
        register_prediction(bm, "t_route", "seed", 0.8, 0.2, date(2026, 1, 2))
    with pytest.raises(TimesplitError, match=r"q=0.1 < p0=0.2 refused"):
        register_prediction(bm, "t_route", "seed", 0.1, 0.2, T)
    # a hand-built Registration that skipped register_prediction is refused again at scoring
    sneaky = Registration(benchmark_id=bm.id, task_id="t_route", harness_id="seed", q=0.1, p0=0.2,
                          registered_on=T)
    a = audit(bm, *_sb(tmp_path))
    with pytest.raises(TimesplitError, match=r"q=0.1 < p0=0.2 refused"):
        score(bm, [sneaky], [], date(2026, 5, 1), a)
    ok = register_prediction(bm, "t_route", "seed", 0.8, 0.2, T)
    assert len(ok.hash) == 64 and ok.hash == register_prediction(bm, "t_route", "seed", 0.8, 0.2, T).hash


def _sb(root: Path):
    return SnapshotStore(root / "lit", clock=lambda: CLOCK), BlobStore(root / "blobs")


# -- resolution (G3, G4)

def test_g3_observation_on_or_before_T_refused(tmp_path):
    _, _, bm = make_world(tmp_path)
    with pytest.raises(TimesplitError, match=r"dated 2025-12-31 is on or before T=2026-01-01"):
        resolve(bm, "t_route", obs({"score": 0.9}, date(2025, 12, 31)))
    with pytest.raises(TimesplitError, match=r"dated 2026-01-01 is on or before T=2026-01-01"):
        resolve(bm, "t_route", obs({"score": 0.9}, T))
    with pytest.raises(TimesplitError, match=r"outside task 't_route'.s window"):
        resolve(bm, "t_route", obs({"score": 0.9}, date(2026, 4, 2)))
    assert resolve(bm, "t_route", obs({"score": 0.9}, date(2026, 1, 2))).outcome is True


def test_g3_hand_built_resolution_or_registration_refused_at_scoring(tmp_path):
    """resolve() is not the only door: a Resolution / Registration built by hand (bypassing resolve /
    register_prediction) must be refused by score() too."""
    from custom_harness.timesplit import Resolution
    from custom_harness.timesplit.rules import rule_hash
    snaps, blobs, bm = make_world(tmp_path)
    good = resolve(bm, "t_route", obs({"score": 0.9}, date(2026, 2, 1)))
    early = Resolution(**{**good.body(), "dated": date(2025, 12, 31)})  # data dated before T, forged past resolve()
    assert early.rule_hash == rule_hash(bm.task("t_route").rule) and early.dated < T  # the forgery is well formed
    preds = [register_prediction(bm, "t_route", "seed", 0.8, 0.2, T)]
    a = audit(bm, snaps, blobs, FakeMemorisationProbe())
    assert score(bm, preds, [good], date(2026, 5, 1), a).windows[0].headline.resolved == 1  # control: accepted
    with pytest.raises(TimesplitError, match=r"dated 2025-12-31 is on or before T=2026-01-01"):
        score(bm, preds, [early], date(2026, 5, 1), a)
    late = Registration(benchmark_id=bm.id, task_id="t_route", harness_id="seed", q=0.8, p0=0.2,
                        registered_on=date(2026, 1, 2))
    with pytest.raises(TimesplitError, match=r"registered on 2026-01-02, after T=2026-01-01"):
        score(bm, [late], [good], date(2026, 5, 1), a)


def test_g4_resolution_and_score_are_deterministic(tmp_path):
    _, blobs_a, bm_a = make_world(tmp_path / "a")
    _, blobs_b, bm_b = make_world(tmp_path / "b")  # independent stores, same inputs
    assert bm_a.id == bm_b.id
    o = obs({"score": 0.9}, date(2026, 2, 1))
    r1, r2, r3 = resolve(bm_a, "t_route", o), resolve(bm_a, "t_route", o), resolve(bm_b, "t_route", o)
    assert r1.record_hash == r2.record_hash == r3.record_hash
    assert r1.outcome is r2.outcome is r3.outcome is True
    assert blobs_a.put_json(r1.body()) == r1.record_hash == blobs_b.put_json(r3.body())
    preds = [register_prediction(bm_a, "t_route", "seed", 0.8, 0.2, T)]
    a = audit(bm_a, *_sb(tmp_path / "a"))
    s1 = score(bm_a, preds, [r1], date(2026, 5, 1), a)
    s2 = score(bm_a, preds, [r2], date(2026, 5, 1), audit(bm_b, *_sb(tmp_path / "b")))
    assert s1.model_dump_json() == s2.model_dump_json()


# -- scoring by hand

def test_vds_and_calibration_per_window_match_hand_computation(tmp_path):
    snaps, blobs, bm = make_world(tmp_path)
    preds = [register_prediction(bm, "t_route", "seed", 0.8, 0.2, T),   # resolves True
             register_prediction(bm, "t_stop", "seed", 0.6, 0.2, T),    # resolves False
             register_prediction(bm, "t_fresh", "seed", 0.9, 0.5, T),   # never resolved: expired miss
             register_prediction(bm, "t_w1", "seed", 0.5, 0.5, T)]      # window 1, still pending
    res = [resolve(bm, "t_route", obs({"score": 0.9}, date(2026, 2, 1))),
           resolve(bm, "t_stop", obs({"score": 0.1}, date(2026, 3, 1)))]
    sc = score(bm, preds, res, date(2026, 5, 1), audit(bm, snaps, blobs, FakeMemorisationProbe()))
    w0, w1 = sc.windows
    g_a, g_b, g_c = math.log2(0.8 / 0.2), math.log2(0.4 / 0.8), math.log2(0.1 / 0.5)
    assert (g_a, g_b) == (2.0, -1.0) and g_c < 0  # a confident miss scores negative
    assert (w0.headline.n, w0.headline.resolved, w0.headline.expired, w0.headline.pending) == (3, 2, 1, 0)
    assert w0.headline.vds == pytest.approx(g_a + g_b + g_c)
    cal = w0.headline.calibration
    assert cal["n"] == 3 and cal["brier"] == pytest.approx((0.04 + 0.36 + 0.81) / 3)
    assert cal["bins"] == [
        {"q_range": [0.6, 0.8], "n": 1, "mean_q": 0.6, "hit_rate": 0.0},
        {"q_range": [0.8, 1.0], "n": 2, "mean_q": pytest.approx(0.85), "hit_rate": 0.5}]
    # prior-art weights: t_route 2 hits (1/3), t_stop 1 hit (1/2), t_fresh 0 hits (1)
    assert w0.low_prior_art.n == 2 and w0.low_prior_art.vds == pytest.approx(g_b + g_c)
    assert w0.weighted_vds == pytest.approx(g_a / 3 + g_b / 2 + g_c)
    assert (w1.headline.n, w1.headline.pending, w1.headline.vds, w1.headline.calibration["n"]) == (1, 1, 0, 0)
    fresh = next(t for t in sc.tasks if t.task_id == "t_fresh")
    assert fresh.expired and fresh.outcome is False and fresh.gain_bits == pytest.approx(g_c)


def test_prior_art_weight_and_subset(tmp_path):
    snaps, blobs, bm = make_world(tmp_path)
    a = audit(bm, snaps, blobs)
    # "sparse routing": entry 1 abstract and entry 3 ("Sparse   Routing" after normalisation); not entry 1's title
    assert a.prior_art_hits["t_route"] == 2 and a.prior_art_weight["t_route"] == pytest.approx(1 / 3)
    assert a.prior_art_hits["t_stop"] == 1 and a.prior_art_weight["t_stop"] == 0.5
    assert a.prior_art_hits["t_fresh"] == 0 and a.prior_art_weight["t_fresh"] == 1.0
    assert all(0 < w <= 1 for w in a.prior_art_weight.values())
    assert tsconfig.LOW_PRIOR_ART_THRESHOLD == 0.5
    with pytest.raises(ValueError, match="at least one non-empty key phrase"):
        Task.model_validate(task("t_nophrase", []))


# -- contamination probes (G5)

def test_g5_answer_leak_and_resolved_before_T_flagged_and_excluded(tmp_path):
    snaps, blobs, bm = make_world(tmp_path)
    a = audit(bm, snaps, blobs, FakeMemorisationProbe({"t_stop": "model stated the outcome"}))
    bad = a.contaminated()
    assert bad["t_leak"].startswith("answer-leak: answer found in literature snapshot entry")
    assert bad["t_before"].startswith("resolved-before-T: frozen data file 'baseline.json'")
    assert bad["t_stop"].startswith("memorisation: fake-offline")
    assert set(bad) == {"t_leak", "t_before", "t_stop"}
    # without the planted body the answer-leak probe is silent
    s2, b2, bm2 = make_world(tmp_path / "clean", plant=False)
    assert "t_leak" not in audit(bm2, s2, b2).contaminated()

    preds = [register_prediction(bm, t, "seed", 0.9, 0.2, T) for t in ("t_route", "t_leak", "t_before")]
    res = [resolve(bm, t, obs({"score": 0.9, "metric_a": 0.9}, date(2026, 2, 1))) for t in ("t_route", "t_leak")]
    res.append(resolve(bm, "t_before", obs({"metric_a": 0.9}, date(2026, 2, 1))))
    w0 = score(bm, preds, res, date(2026, 5, 1), a).windows[0]
    assert w0.excluded == ("t_before", "t_leak")
    assert w0.headline.n == 1 and w0.headline.vds == pytest.approx(math.log2(0.9 / 0.2))


# -- compare (§6.4 verdict)

def _compare_world(root: Path, n: int = 12):
    tasks = [task(f"c{i:02d}", [f"phrase {i}"]) for i in range(n)]
    snaps = SnapshotStore(root / "lit", clock=lambda: CLOCK)
    snaps.record(OA, {"search": "x"}, HttpResponse(status=200, text='{"title": "unrelated"}'))
    blobs = BlobStore(root / "blobs")
    bm = freeze_benchmark(snapshots=snaps, blobs=blobs, as_of=T, windows=[(T, T1)], tasks=tasks)
    outcomes = {f"c{i:02d}": i % 4 != 0 for i in range(n)}  # 9 true, 3 false
    res = [resolve(bm, t, obs({"score": 0.9 if o else 0.1}, date(2026, 2, 1))) for t, o in outcomes.items()]
    return snaps, blobs, bm, outcomes, res


def test_compare_evolved_clearly_better(tmp_path):
    snaps, blobs, bm, outcomes, res = _compare_world(tmp_path)
    a = audit(bm, snaps, blobs, FakeMemorisationProbe())
    seed = [register_prediction(bm, t, "seed", 0.1, 0.1, T) for t in outcomes]
    evo = [register_prediction(bm, t, "evolved", 0.9 if o else 0.1, 0.1, T) for t, o in outcomes.items()]
    now = date(2026, 5, 1)
    c = compare(score(bm, seed, res, now, a), score(bm, evo, res, now, a))
    assert c.verdict == EVOLVED_BETTER and c.reasons == ()
    w = c.windows[-1]
    assert w.n_pairs == 12 and w.gain_diff is not None and w.brier_diff is not None
    assert w.gain_diff.lo > 0 and w.brier_diff.lo > 0
    assert w.gain_diff.mean == pytest.approx(9 * math.log2(9) / 12)
    assert c == compare(score(bm, seed, res, now, a), score(bm, evo, res, now, a))  # seeded: deterministic
    # same predictions but the mandatory memorisation probe not run: never a success claim
    a0 = audit(bm, snaps, blobs)
    c0 = compare(score(bm, seed, res, now, a0), score(bm, evo, res, now, a0))
    assert c0.verdict == NOT_SHOWN and "memorisation probe not run" in c0.reasons[0]


def test_compare_equal_harnesses_not_shown(tmp_path):
    snaps, blobs, bm, outcomes, res = _compare_world(tmp_path)
    a = audit(bm, snaps, blobs, FakeMemorisationProbe())
    one = [register_prediction(bm, t, "seed", 0.7, 0.1, T) for t in outcomes]
    two = [register_prediction(bm, t, "evolved", 0.7, 0.1, T) for t in outcomes]
    now = date(2026, 5, 1)
    c = compare(score(bm, one, res, now, a), score(bm, two, res, now, a))
    assert c.verdict == NOT_SHOWN
    assert "gain-difference CI lower bound is not > 0" in c.reasons
    assert "Brier-difference CI lower bound is not > 0" in c.reasons


def test_cli_compare_feeds_scorecard_axis_e(tmp_path, monkeypatch):
    from custom_harness.cli import main
    from custom_harness.scorecard import build_scorecard, check_scorecard
    from custom_harness.scorecard.axes import numbers_without_sources
    from custom_harness.store import EvidenceStore
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    snaps, blobs, bm, outcomes, res = _compare_world(tmp_path)
    now = date(2026, 5, 1)
    for probe, tag in ((FakeMemorisationProbe(), "ok"), (None, "noprobe")):
        a = audit(bm, snaps, blobs, probe)
        seed = score(bm, [register_prediction(bm, t, "seed", 0.1, 0.1, T) for t in outcomes], res, now, a)
        evo = score(bm, [register_prediction(bm, t, "evolved", 0.9 if o else 0.1, 0.1, T)
                         for t, o in outcomes.items()], res, now, a)
        (tmp_path / f"seed_{tag}.json").write_text(seed.model_dump_json(), encoding="utf-8")
        (tmp_path / f"evo_{tag}.json").write_text(evo.model_dump_json(), encoding="utf-8")
    ok, bad = tmp_path / "cmp_ok.json", tmp_path / "cmp_noprobe.json"
    assert main(["timesplit", "compare", str(tmp_path / "seed_ok.json"), str(tmp_path / "evo_ok.json"),
                 "--out", str(ok)]) == 0
    assert main(["timesplit", "compare", str(tmp_path / "seed_noprobe.json"), str(tmp_path / "evo_noprobe.json"),
                 "--out", str(bad)]) == 1
    assert json.loads(ok.read_text(encoding="utf-8"))["verdict"] == EVOLVED_BETTER
    store = EvidenceStore(tmp_path / "store")
    card = build_scorecard(store, tmp_path / "card", timesplit=ok)
    e = card["axes"]["E"]
    assert e["status"] == "pass" and e["benchmarks"][0]["verdict"] == EVOLVED_BETTER
    assert e["benchmarks"][0]["evolved_minus_seed_last_window"]["sources"][0].startswith("input:")
    assert numbers_without_sources(card) == [] and check_scorecard(tmp_path / "card", store)["reproduced"]
    card2 = build_scorecard(store, tmp_path / "card2", timesplit=bad)
    assert card2["axes"]["E"]["status"] == "inconclusive" and "axis E: inconclusive" in card2["not_shown"]
    assert any("memorisation probe not run" in r for r in card2["axes"]["E"]["benchmarks"][0]["reasons"])
    store.close()
