"""Certifiability preflight (GOAL §16.8, invariant 21: deny only) and the yield helpers in research/modules.py."""

import random
from pathlib import Path

import custom_harness.kernel.checks as kchecks
from custom_harness.kernel.certificate import CheckResult
from custom_harness.research.modules import agreement, headroom, signature, vote
from custom_harness.research.preflight import PreflightConfig, PublicInputs, dev_pairs, preflight
from custom_harness.research.task import TaskSpec

# Live run A (liverun1.md): 54 held-out rows; dev = 24 rows, candidate 24/24, baseline 22/24 (so 22 both right and
# 2 only-candidate-right); claimed min effect 0.02, alpha 0.05, seeds 0/1/2, family 1. The Kernel rejected it on K7.
RUN_A = PublicInputs(metric="accuracy", higher_is_better=True, n_test=54, dev_pairs=(22, 2, 0, 0), min_effect=0.02)


def test_run_a_claim_is_predicted_to_fail_k7_and_is_denied():
    r = preflight(RUN_A, PreflightConfig(n_sims=200))
    assert r.verdict == "deny" and r.p_certify is not None and r.p_certify < 0.05
    # whenever the claim's criterion holds (K6), K7 calls it trivially safe: the run-A failure, predicted
    assert r.p_k6 is not None and r.p_k6 > 0.2
    assert r.p_k7_given_k6 is not None and r.p_k7_given_k6 < 0.05
    assert r.p_k4 == 1.0  # an honest plan-following simulation passes K4


def test_preflight_runs_the_kernels_own_k7(monkeypatch):
    """Not a re-implementation: replacing the Kernel's k7_risk changes the preflight's K7 rate."""
    calls = []

    def always_pass(ctx):
        calls.append(ctx.claim_hash)
        return CheckResult(status="pass", reasons=())

    monkeypatch.setattr(kchecks, "k7_risk", always_pass)
    r = preflight(RUN_A, PreflightConfig(n_sims=50))
    assert calls and r.p_k7 == 1.0


def test_strong_claim_gets_no_objection_and_unsupported_metric_never_denies():
    strong = PublicInputs(metric="accuracy", higher_is_better=True, n_test=2000, dev_pairs=(150, 60, 5, 25),
                          min_effect=0.1)
    r = preflight(strong, PreflightConfig(n_sims=40))
    assert r.verdict == "no objection" and r.p_certify is not None and r.p_certify >= 0.5
    u = preflight(PublicInputs(metric="rmse", higher_is_better=False, n_test=100, dev_pairs=(0, 0, 0, 0),
                               min_effect=0.1))
    assert u.verdict == "unsupported" and not u.denied


def test_preflight_touches_no_real_store(tmp_path, monkeypatch):
    """The simulation never opens a store outside its own temporary scratch directory."""
    import custom_harness.research.preflight as pf
    opened: list[Path] = []
    real = pf.EvidenceStore

    def spy(root):
        opened.append(Path(root))
        return real(root)

    monkeypatch.setattr(pf, "EvidenceStore", spy)
    preflight(RUN_A, PreflightConfig(n_sims=5))
    assert opened and all("chh-preflight-" in str(p) for p in opened)


def test_dev_pairs():
    assert dev_pairs(["a", "b", "c", "d"], ["a", "b", "x", "x"], ["a", "x", "c", "x"]) == (1, 1, 1, 1)


def _task(metric="accuracy"):
    return TaskSpec(id="t", title="t", description="d", metric=metric, target_column="y")


def test_headroom_matches_the_real_racing_function():
    """Verified on 2026-10-07 against the real paired_example_interval: a perfect refinement fails dev racing for
    0 or 1 incumbent errors and passes from 2 on (the review estimated e >= 3)."""
    for n in (24, 124):
        labels = ["1"] * n
        for e, feasible in ((0, False), (1, False), (2, True), (3, True)):
            ok, why = headroom(_task(), labels, ["0"] * e + ["1"] * (n - e))
            assert ok is feasible, (n, e, why)


def test_headroom_infeasible_means_no_sampled_candidate_passes():
    """Empirical support for the computed (not proved) bound at e = 1: random candidates never pass either."""
    from custom_harness.research.modules import _dev_better
    rng = random.Random(1)
    n = 40
    labels = [rng.choice("abc") for _ in range(n)]
    inc = list(labels)
    inc[7] = "z"
    assert headroom(_task(), labels, inc)[0] is False
    for _ in range(100):
        cand = [y if rng.random() < 0.95 else "z" for y in labels]
        cand[7] = labels[7]
        assert not _dev_better(_task(), labels, cand, inc)


def test_vote_and_signatures():
    assert vote([["a", "a", "b"], ["b", "c", "b"], ["c", "c", "a"]]) == ["a", "c", "b"]
    assert agreement(["a", "b"], ["a", "c"]) == 0.5
    assert signature(["a"]) != signature(["b"])


def test_cpu_limit_is_passed_through_or_refused():
    import pytest

    from custom_harness.research.modules import cpu_exec_kwargs

    class Old:
        def exec(self, command, timeout_s=600, env=None):
            raise AssertionError

    class New:
        def exec(self, command, timeout_s=600, env=None, *, cpu_s=None):
            raise AssertionError

    assert cpu_exec_kwargs(Old(), None) == {}  # off: today's behaviour
    assert cpu_exec_kwargs(New(), 30) == {"cpu_s": 30.0}
    with pytest.raises(RuntimeError, match="cannot enforce"):
        cpu_exec_kwargs(Old(), 30)  # never silently dropped


def test_preflight_suite_cli_zero_calls(tmp_path, monkeypatch, capsys):
    import json
    import subprocess
    import sys

    from custom_harness import cli
    root = Path(__file__).resolve().parents[1]
    task = tmp_path / "suite" / "rings"
    subprocess.run([sys.executable, str(root / "examples/tasks/make_rings_task.py"), str(task), "--n-train", "100",
                    "--n-test", "60"], check=True, capture_output=True)
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    out = tmp_path / "audit.json"
    assert cli.main(["preflight-suite", str(tmp_path / "suite"), "--sims", "10", "--json", str(out)]) == 0
    rows = json.loads(out.read_text())
    assert [r["task"] for r in rows] and rows[0]["n_test"] == 60 and rows[0]["verdict"] in ("deny", "no objection")
    assert "0 model calls" in capsys.readouterr().out
