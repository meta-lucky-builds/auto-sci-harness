"""L4.1 Problem Formulator (GOAL §5 L4 stage 1; research/formulator.py): dev-only anomaly scan through a declared cell
grammar, permutation-max look-elsewhere correction, typed candidates ranked by tier x value x feasibility, and no
held-out read."""

from __future__ import annotations

import csv
import io
import math
import random
import statistics

import pytest

from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.kernel.adapters import approve
from custom_harness.research.formulator import (
    CandidateProblem,
    CellGrammar,
    FeatureRule,
    FormulatorError,
    check_in_grammar,
    enumerate_cells,
    formulate,
    formulate_from_vault,
)
from custom_harness.store import EvidenceStore
from custom_harness.tiers import Tier

HEADER = ["g", "x1", "x2", "x3", "y"]
GRAMMAR = CellGrammar(target="y", features=(FeatureRule(column="g", kind="categorical"),
                                             FeatureRule(column="x1", kind="numeric", bins=4),
                                             FeatureRule(column="x2", kind="numeric", bins=4),
                                             FeatureRule(column="x3", kind="numeric", bins=4)),
                      min_cell=20, alpha=0.05, permutations=199, seed=0)


def table(n: int, seed: int, shift: float = 0.0) -> list[list[str]]:
    """Pure noise; with shift > 0 the mean of y is raised by `shift` where x2 > 0.75 (planted dev anomaly)."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        g = rng.choice("abcd")
        x1, x2, x3 = rng.random(), rng.random(), rng.random()
        y = rng.gauss(0, 1) + (shift if x2 > 0.75 else 0.0)
        rows.append([g, f"{x1:.6f}", f"{x2:.6f}", f"{x3:.6f}", f"{y:.6f}"])
    return rows


def csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode()


# ---------------------------------------------------------------- planted anomaly
def test_planted_dev_anomaly_is_found_and_ranked_first():
    rows = table(400, seed=1, shift=0.8)
    inside = [float(r[4]) for r in rows if float(r[2]) > 0.75]
    outside = [float(r[4]) for r in rows if float(r[2]) <= 0.75]
    assert statistics.mean(inside) - statistics.mean(outside) > 0.5  # the plant is really in the dev data
    res = formulate(GRAMMAR, HEADER, rows)
    assert res.candidates, "the planted anomaly was not found"
    top = res.candidates[0]
    assert isinstance(top, CandidateProblem) and top.status == "hypothesis"
    assert top.kind == "subgroup_shift" and top.response == "target"
    assert [c.column for c in top.conditions] == ["x2"] and top.conditions[0].hi is None  # the top x2 quartile
    assert 0.70 < top.conditions[0].lo < 0.80
    assert top.p_scan <= GRAMMAR.alpha and top.effect > 0.5
    assert top.scan_size == res.scan_size == len(enumerate_cells(GRAMMAR, HEADER, rows)[0])
    assert res.threshold is not None and abs(top.z) > res.threshold
    # every reported candidate survived the scan-wide null, and the null is the stated size
    assert len(res.max_null) == GRAMMAR.permutations
    assert all(c.p_scan <= GRAMMAR.alpha for c in res.candidates)


# ---------------------------------------------------------------- look-elsewhere control on pure noise
def _binom_upper(n: int, p: float, tail: float) -> int:
    """Smallest k with P(X >= k) <= tail for X ~ Binomial(n, p)."""
    pmf = [math.comb(n, k) * p**k * (1 - p) ** (n - k) for k in range(n + 1)]
    for k in range(n + 1):
        if sum(pmf[k:]) <= tail:
            return k
    return n + 1


def test_pure_noise_yields_at_most_alpha_false_candidates_and_the_scan_really_had_multiplicity():
    alpha, sims = 0.1, 200
    g = GRAMMAR.model_copy(update={"alpha": alpha, "permutations": 99})
    z_local = statistics.NormalDist().inv_cdf(1 - alpha / 2)
    any_false, any_local, false_total = 0, 0, 0
    for s in range(sims):
        rows = table(200, seed=1000 + s)
        res = formulate(g.model_copy(update={"seed": s}), HEADER, rows)
        false_total += len(res.candidates)
        any_false += bool(res.candidates)
        # the attack: the same cells judged one at a time (no look-elsewhere correction)
        ys = [float(r[4]) for r in rows]
        var, tot, n = statistics.pvariance(ys), sum(ys), len(ys)
        cells, _ = enumerate_cells(g, HEADER, rows)
        zs = []
        for c in cells:
            m, sm = len(c.members), sum(ys[i] for i in c.members)
            zs.append((sm / m - (tot - sm) / (n - m)) / math.sqrt(var * (1 / m + 1 / (n - m))))
        any_local += any(abs(z) > z_local for z in zs)
    # integrity: the chance that a pure-noise scan yields any candidate is <= alpha (binomial tail 0.001)
    assert any_false < _binom_upper(sims, alpha, 0.001), (any_false, sims)
    # the scan was a real multiple-comparison attack: uncorrected, most noise datasets yield a "finding"
    assert any_local >= 0.5 * sims, any_local
    assert false_total <= 2 * alpha * sims


def test_baseline_error_pocket_is_an_open_problem_in_accuracy_units():
    rng = random.Random(7)
    rows, preds = [], []
    for _ in range(400):
        gv = rng.choice("abcd")
        y = rng.choice("01")
        wrong = rng.random() < (0.45 if gv == "c" else 0.1)
        rows.append([gv, f"{rng.random():.6f}", f"{rng.random():.6f}", f"{rng.random():.6f}", y])
        preds.append(("1" if y == "0" else "0") if wrong else y)
    g = GRAMMAR.model_copy(update={"responses": ("baseline_error",)})
    res = formulate(g, HEADER, rows, baseline_predictions=preds, tier=Tier.T2_STATISTICAL)
    top = res.candidates[0]
    assert top.kind == "baseline_error_pocket" and top.conditions[0].level == "c" and top.effect > 0.2
    f = top.n_cell / top.n_dev
    assert top.value == pytest.approx(f * top.effect)  # the dev accuracy gained if the pocket matched the rest
    assert top.priority == pytest.approx(top.tier_weight * top.value * top.feasibility) and top.priority > 0
    with pytest.raises(FormulatorError):  # baseline_error without aligned predictions is refused
        formulate(g, HEADER, rows, baseline_predictions=preds[:-1])


# ---------------------------------------------------------------- the grammar is declared and binding
def test_grammar_is_declared_and_out_of_grammar_cells_are_refused():
    rows = table(800, seed=3)
    with pytest.raises(ValueError):  # cells may not be built from the response
        CellGrammar(target="y", features=(FeatureRule(column="y", kind="numeric"),))
    with pytest.raises(FormulatorError):  # an undeclared column in the data cannot be scanned
        formulate(CellGrammar(target="y", features=(FeatureRule(column="zz", kind="categorical"),)), HEADER, rows)
    cells, _ = enumerate_cells(GRAMMAR, HEADER, rows)
    assert check_in_grammar(cells[0].conditions, GRAMMAR, HEADER, rows)
    # a hand-picked cut point and an undeclared column are not cells of the grammar
    assert not check_in_grammar([{"column": "x2", "op": "bin", "lo": 0.9, "hi": None}], GRAMMAR, HEADER, rows)
    assert not check_in_grammar([{"column": "y", "op": "bin", "lo": 1.0, "hi": None}], GRAMMAR, HEADER, rows)
    # a conjunction is a cell only if the grammar declares max_conditions=2
    pair = cells[0].conditions + cells[-1].conditions
    assert not check_in_grammar(pair, GRAMMAR, HEADER, rows)
    assert check_in_grammar(pair, GRAMMAR.model_copy(update={"max_conditions": 2}), HEADER, rows)
    # the digest names the declaration: any change of the grammar changes it
    assert GRAMMAR.digest() == CellGrammar.model_validate(GRAMMAR.model_dump()).digest()
    assert GRAMMAR.digest() != GRAMMAR.model_copy(update={"min_cell": 21}).digest()
    # model_copy skips pydantic validation: the scan re-validates, so a copied-in response column is still refused
    with pytest.raises(ValueError):
        formulate(GRAMMAR.model_copy(update={"features": (FeatureRule(column="y", kind="numeric"),)}), HEADER, rows)


def test_ranking_uses_the_best_available_verifier_tier():
    rows = table(400, seed=1, shift=0.8)
    t2 = formulate(GRAMMAR, HEADER, rows, tier=Tier.T2_STATISTICAL).candidates[0]
    t6 = formulate(GRAMMAR, HEADER, rows, tier=Tier.T6_NONE).candidates[0]
    assert t2.priority > 0 and t6.priority == 0 and t2.value == t6.value and t2.feasibility == t6.feasibility
    assert 0 < t2.feasibility <= 1 and 0 < t2.value <= 1


# ---------------------------------------------------------------- held-out data are never read
@pytest.fixture
def vault_world(tmp_path):
    store = EvidenceStore(tmp_path / "store")
    kernel = Kernel(store)
    train = table(400, seed=1, shift=0.8)
    test = table(300, seed=2, shift=0.8)
    test_x = csv_bytes(HEADER[:-1], [r[:-1] for r in test])
    test_y = csv_bytes(["y"], [[r[-1]] for r in test])
    kernel.vault.register_dataset("d/train", DatasetRole.DEV, public={"train.csv": csv_bytes(HEADER, train)},
                                  private={})
    kernel.vault.register_dataset("d/test", DatasetRole.TEST, public={"test_X.csv": test_x},
                                  private={"test_y.csv": test_y})
    held = {f.blob for f in kernel.vault.dataset("d/test").files.values()}
    fetched: list[str] = []
    real_get = store.blobs.get

    def spy(ref, *a, **k):
        fetched.append(ref)
        return real_get(ref, *a, **k)

    store.blobs.get = spy
    return kernel, held, fetched, test_x


def test_formulator_never_reads_held_out_data(vault_world):
    kernel, held, fetched, test_x = vault_world
    approve(kernel.ledger, "ml", "test-human")
    res = formulate_from_vault(kernel, "d/train", "train.csv", GRAMMAR, metric="rmse")
    assert res.candidates and res.candidates[0].tier == int(Tier.T2_STATISTICAL)
    assert res.candidates[0].dev_dataset_id == "d/train"
    # the vault log shows no read of held-out data, and no held-out byte was fetched from the store at all
    assert [e for e in kernel.vault.accesses() if e.body["dataset_id"] == "d/test"] == []
    assert fetched and not (set(fetched) & held)
    # the grammar was committed to the ledger before the scan
    arts = [e for e in kernel.ledger.entries("register_artifact") if e.body["kind"] == "formulator-grammar"]
    assert [e.seq for e in arts] == [res.grammar_seq] and arts[0].body["artifact_id"].endswith(res.grammar_digest[:16])
    # attacks: pointing it at held-out data is refused before any byte is fetched
    n_before = len(fetched)
    with pytest.raises(FormulatorError, match="dev data only"):
        formulate_from_vault(kernel, "d/test", "test_X.csv", GRAMMAR)
    kernel.vault.register_dataset("d/relabelled", DatasetRole.DEV, public={"x.csv": test_x}, private={})
    with pytest.raises(FormulatorError, match="held-out"):
        formulate_from_vault(kernel, "d/relabelled", "x.csv", GRAMMAR)
    kernel.vault.register_dataset("d/privdev", DatasetRole.DEV, public={},
                                  private={"p.csv": csv_bytes(HEADER, table(100, seed=9))})
    with pytest.raises(FormulatorError, match="private"):
        formulate_from_vault(kernel, "d/privdev", "p.csv", GRAMMAR)
    assert not (set(fetched[n_before:]) & held)
    assert [e for e in kernel.vault.accesses() if e.body["dataset_id"] == "d/test"] == []
