"""Label-dependence audit of published code (GOAL §16.10; worlds from §16.5.4 and §16.5.7).

Every leaky fixture is an ATTACK test: it asserts the attack happened (decisions changed in the world that is meant
to expose it) and that the other worlds did NOT fire where they must not, so a fixture that silently stopped leaking
fails here instead of passing as CLEAN."""

from __future__ import annotations

import csv
import io
import json
import random
from dataclasses import asdict
from pathlib import Path

import pytest

from custom_harness.audit.labeldep import (
    Code,
    LabelDepResult,
    Verdict,
    World,
    build_world,
    label_dependence_audit,
    split_halves,
)
from custom_harness.sandbox import LocalSandbox

FIXTURES = Path(__file__).parent / "fixtures" / "audit" / "pipelines"
LABELS = ("lab_A", "lab_B", "lab_C")


def make_data(n: int = 160, seed: int = 7) -> bytes:
    """id, a categorical feature (30 levels, ~5 rows each: target encoding of a level moves with its own rows'
    labels), three numeric features on very different scales, and an imbalanced 3-class label (lab_C is rare)."""
    rng = random.Random(seed)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["id", "cat", "x1", "x2", "x3", "label"])
    for i in range(n):
        cat = rng.randrange(30)
        x1, x2, x3 = rng.random(), rng.random() * 100, rng.gauss(0, 5)
        z = x1 + x2 / 100 + (cat % 3) * 0.4 + rng.gauss(0, 0.3)
        label = "lab_C" if rng.random() < 0.15 else ("lab_A" if z < 1.3 else "lab_B")
        w.writerow([100 + i, f"c{cat:02d}", f"{x1:.4f}", f"{x2:.3f}", f"{x3:.3f}", label])
    return buf.getvalue().encode()


DATA = make_data()


def _factory(tmp_path: Path):
    n = [0]

    def make(name: str) -> LocalSandbox:
        n[0] += 1
        return LocalSandbox(tmp_path / "boxes" / f"{n[0]:02d}-{name}", i_understand_no_isolation=True)
    return make


def audit(tmp_path: Path, fixture: str, data: bytes = DATA, **kw) -> LabelDepResult:
    code = (FIXTURES / f"{fixture}.py").read_bytes()
    return label_dependence_audit(code, data, id_column="id", label_column="label",
                                  sandbox_factory=_factory(tmp_path), seed=3, timeout_s=60.0, **kw)


def world(r: LabelDepResult, w: World):
    (s,) = [s for s in r.worlds if s.world == w]
    return s


def _no_label_values(r: LabelDepResult) -> None:
    d = asdict(r)
    d.pop("diagnostic")
    text = json.dumps(d, default=str)
    for lab in LABELS:
        assert lab not in text


# ---------------------------------------------------------------- planted leaks: FLAGGED, and the attack happened

def test_leaky_target_encoding_is_flagged_by_the_label_worlds(tmp_path):
    r = audit(tmp_path, "leaky_target_encoding")
    assert r.verdict is Verdict.FLAGGED
    assert Code.READS_TEST_LABELS_FLIPPED in r.codes
    flipped = world(r, World.FLIPPED)
    assert flipped.ran and not flipped.split_changed
    assert flipped.n_compared == flipped.n_test_rows == 48  # 30% of 160, the same rows as the real run
    assert flipped.n_changed > 0  # the attack happened: test labels moved the decisions
    _no_label_values(r)


def test_leaky_scaler_is_flagged_only_by_the_transductive_world(tmp_path):
    r = audit(tmp_path, "leaky_scaler")
    assert r.verdict is Verdict.FLAGGED
    assert r.codes == (Code.TRANSDUCTIVE,)
    # label worlds are blind to a feature-only leak (asserted, so the transductive world is what caught it)
    assert world(r, World.FLIPPED).n_changed == 0
    assert world(r, World.CONSTANT).n_changed == 0
    t = world(r, World.TRANSDUCTIVE)
    assert t.ran and not t.split_changed
    assert t.n_compared == 24  # half A of the 48 test rows
    assert t.n_changed > 0  # the attack happened: other test rows' features moved A's decisions


def test_leaky_oversampling_is_flagged(tmp_path):
    r = audit(tmp_path, "leaky_oversample")
    assert r.verdict is Verdict.FLAGGED
    assert Code.READS_TEST_LABELS_FLIPPED in r.codes
    assert Code.TRANSDUCTIVE not in r.codes
    assert not world(r, World.FLIPPED).split_changed
    assert world(r, World.FLIPPED).n_changed > 0


def test_leaky_float_scores_shift_beyond_tolerance_are_flagged(tmp_path):
    r = audit(tmp_path, "float_leak")
    assert r.verdict is Verdict.FLAGGED
    assert Code.READS_TEST_LABELS_FLIPPED in r.codes
    assert world(r, World.FLIPPED).n_changed > 0


# ---------------------------------------------------------------- honest versions: CLEAN, every world really ran

@pytest.mark.parametrize("fixture", ["honest_target_encoding", "honest_scaler", "honest_oversample"])
def test_honest_pipelines_are_clean(tmp_path, fixture):
    r = audit(tmp_path, fixture)
    assert r.verdict is Verdict.CLEAN, r
    assert r.codes == ()
    for w in (World.FLIPPED, World.CONSTANT, World.TRANSDUCTIVE):
        s = world(r, w)
        assert s.ran and not s.split_changed and s.n_changed == 0
        assert s.n_compared == (24 if w is World.TRANSDUCTIVE else 48)
    _no_label_values(r)


def test_float_noise_below_tolerance_is_not_flagged(tmp_path):
    r = audit(tmp_path, "float_noise")
    assert r.verdict is Verdict.CLEAN
    flipped = world(r, World.FLIPPED)
    assert flipped.n_bytes_differ > 0  # the bytes DID change between worlds ...
    assert flipped.n_changed == 0  # ... but no decision did


def test_float_noise_is_flagged_with_zero_tolerance(tmp_path):
    """The tolerance is what keeps float_noise CLEAN (with it at 0 the same pipeline is flagged)."""
    r = audit(tmp_path, "float_noise", float_abs_tol=0.0)
    assert r.verdict is Verdict.FLAGGED
    assert Code.READS_TEST_LABELS_FLIPPED in r.codes


# ---------------------------------------------------------------- label-dependent split: never FLAGGED

@pytest.mark.parametrize("fixture", ["stratified_honest", "stratified_leaky"])
def test_label_stratified_split_is_inconclusive_never_flagged(tmp_path, fixture):
    r = audit(tmp_path, fixture)
    assert r.verdict is Verdict.INCONCLUSIVE
    assert r.verdict is not Verdict.FLAGGED
    assert Code.SPLIT_DEPENDS_ON_LABELS in r.codes
    assert not {Code.READS_TEST_LABELS_FLIPPED, Code.READS_TEST_LABELS_CONSTANT} & set(r.codes)
    assert world(r, World.FLIPPED).split_changed


# ---------------------------------------------------------------- nondeterminism and broken programs

def test_nondeterministic_pipeline_is_inconclusive(tmp_path):
    r = audit(tmp_path, "nondeterministic")
    assert r.verdict is Verdict.INCONCLUSIVE
    assert r.codes == (Code.NONDETERMINISTIC,)


def test_crashing_pipeline_could_not_run(tmp_path):
    r = audit(tmp_path, "crash")
    assert r.verdict is Verdict.COULD_NOT_RUN
    assert r.codes == (Code.CRASH,)
    assert r.failed_world is World.REAL
    assert "boom" in r.diagnostic and len(r.diagnostic) <= 2000


@pytest.mark.parametrize(("fixture", "code"), [("unknown_ids", Code.UNKNOWN_ROW_ID),
                                               ("duplicate_ids", Code.DUPLICATE_ROW_ID)])
def test_bad_row_ids_could_not_run(tmp_path, fixture, code):
    r = audit(tmp_path, fixture)
    assert r.verdict is Verdict.COULD_NOT_RUN
    assert r.codes == (code,)


def test_malformed_output_could_not_run(tmp_path):
    code = (b"import sys\nout = sys.argv[sys.argv.index('--out') + 1]\n"
            b"open(out, 'w').write('id,pred\\n100,x\\n')\n")
    r = label_dependence_audit(code, DATA, id_column="id", label_column="label",
                               sandbox_factory=_factory(tmp_path), timeout_s=60.0)
    assert r.verdict is Verdict.COULD_NOT_RUN
    assert r.codes == (Code.OUTPUT_MALFORMED,)


def test_missing_output_could_not_run(tmp_path):
    r = label_dependence_audit(b"pass\n", DATA, id_column="id", label_column="label",
                               sandbox_factory=_factory(tmp_path), timeout_s=60.0)
    assert r.verdict is Verdict.COULD_NOT_RUN
    assert r.codes == (Code.OUTPUT_MISSING,)


def test_timeout_could_not_run(tmp_path):
    r = label_dependence_audit(b"import time\ntime.sleep(30)\n", DATA, id_column="id", label_column="label",
                               sandbox_factory=_factory(tmp_path), timeout_s=1.0)
    assert r.verdict is Verdict.COULD_NOT_RUN
    assert r.codes == (Code.TIMEOUT,)


def test_bad_audit_input_raises(tmp_path):
    with pytest.raises(ValueError):
        label_dependence_audit(b"pass\n", DATA, id_column="nope", label_column="label",
                               sandbox_factory=_factory(tmp_path))
    dup = DATA + b"100,c00,0.1,1.0,1.0,lab_A\n"
    with pytest.raises(ValueError):
        label_dependence_audit(b"pass\n", dup, id_column="id", label_column="label",
                               sandbox_factory=_factory(tmp_path))


# ---------------------------------------------------------------- the worlds themselves (pure functions)

def _rows(b: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(b.decode())))


def test_flipped_world_changes_every_test_label_and_nothing_else():
    real = _rows(DATA)
    test_ids = [r["id"] for r in real[::3]]
    out = _rows(build_world(DATA, id_column="id", label_column="label", world=World.FLIPPED, test_ids=test_ids))
    assert len(out) == len(real)
    nxt = {"lab_A": "lab_B", "lab_B": "lab_C", "lab_C": "lab_A"}
    for a, b in zip(real, out, strict=True):
        assert {k: v for k, v in a.items() if k != "label"} == {k: v for k, v in b.items() if k != "label"}
        assert b["label"] == (nxt[a["label"]] if a["id"] in test_ids else a["label"])


def test_constant_world_uses_the_class_that_disagrees_most():
    real = _rows(DATA)
    test_ids = [r["id"] for r in real[:60]]
    counts = {lab: sum(1 for r in real[:60] if r["label"] == lab) for lab in LABELS}
    expect = min(LABELS, key=lambda c: (counts[c], c))
    out = _rows(build_world(DATA, id_column="id", label_column="label", world=World.CONSTANT, test_ids=test_ids))
    for a, b in zip(real, out, strict=True):
        assert b["label"] == (expect if a["id"] in test_ids else a["label"])
        assert {k: v for k, v in a.items() if k != "label"} == {k: v for k, v in b.items() if k != "label"}


def test_transductive_world_moves_numeric_features_of_half_b_only():
    real = _rows(DATA)
    test_ids = [r["id"] for r in real[5:45]]
    a_ids, b_ids = split_halves(test_ids)
    assert len(a_ids) == len(b_ids) == 20 and not set(a_ids) & set(b_ids)
    assert sorted(a_ids + b_ids, key=int) == sorted(test_ids, key=int)
    out = _rows(build_world(DATA, id_column="id", label_column="label", world=World.TRANSDUCTIVE,
                            test_ids=test_ids))
    x1_max = max(float(r["x1"]) for r in real)
    for a, b in zip(real, out, strict=True):
        assert (a["id"], a["cat"], a["label"]) == (b["id"], b["cat"], b["label"])  # non-numeric untouched
        if a["id"] in b_ids:
            assert all(float(b[f]) > float(a[f]) + 1000 for f in ("x1", "x2", "x3"))
            assert float(b["x1"]) > x1_max + 1000
        else:
            assert (a["x1"], a["x2"], a["x3"]) == (b["x1"], b["x2"], b["x3"])
