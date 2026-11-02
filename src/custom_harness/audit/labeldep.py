"""Label-dependence audit of published code (GOAL §16.10; worlds from §16.5.4 and §16.5.7).

A published ML pipeline (one Python file) is run under a fixed contract,

    {python} pipeline.py --data in/data.csv --out out/preds.csv --seed {seed}

where `data.csv` is the FULL dataset (header, an id column, feature columns, a label column) and the pipeline does
its OWN train/test split and writes `out/preds.csv` (header `row_id,prediction`, one row per test row it predicted).
The test rows are, by definition, the row_ids of the real run's output. The program is then re-run in worlds that
keep the row count, order, ids and every non-perturbed value identical:

- `real` twice (same seed). Different decisions (or a different set of predicted rows) -> INCONCLUSIVE /
  NONDETERMINISTIC. flowsb's paired McNemar fallback for nondeterministic programs (§16.5.4) is NOT implemented
  here; a nondeterministic pipeline is simply not audited.
- `flipped`: every TEST row's label is replaced by the next class in sorted order of the label set (cyclic), so
  every test label is different; training rows are untouched.
- `constant`: every test row's label is set to ONE class, the one that disagrees with the most real test labels
  (ties: sorted order).
- `transductive` (§16.5.7): the test rows are split into halves A/B by sorted row_id (alternating); the numeric
  feature values of B rows are replaced by an extreme value per column; decisions are compared on A only. This
  catches feature-only cross-split leaks (e.g. a scaler fit on train + test), which the label worlds cannot see.

The set of predicted row_ids is checked BEFORE predictions are compared: if it changes in a label world, the split
depends on the labels and the verdict is INCONCLUSIVE (SPLIT_DEPENDS_ON_LABELS), never FLAGGED; likewise for the
transductive world (SPLIT_DEPENDS_ON_FEATURES). Any split change makes the whole verdict INCONCLUSIVE (conservative:
no flag is reported from a run whose test set moved). DECISIONS are compared, not bytes: if every compared prediction
parses as a finite float, values within FLOAT_ABS_TOL are equal; otherwise predictions are compared as stripped
strings.

The result holds only a closed verdict, closed codes and counts; no label value and no free text from the program,
except `diagnostic`, a bounded stderr tail of a run that failed.

Limits (stated, not solved):
- Only the labels inside the data file are perturbed. A copy of the labels the code obtains another way (a second
  file baked into the code, a download, memorised in model weights) is invisible to this audit.
- The transductive world moves numeric features only; leaks through non-numeric columns, or through row order or
  row count, are not tested.
- Nondeterministic pipelines end INCONCLUSIVE (no McNemar fallback). No entropy pin is applied here (§16.5.1); a
  pipeline that is deterministic only under a pinned clock/urandom is reported NONDETERMINISTIC.
- A pipeline that recognises the perturbed worlds (e.g. by label statistics) and behaves only in them is not caught
  by a world-vs-world comparison; there is no stored production output to compare against for third-party code.
- CLEAN means "no dependence observed in these worlds at this seed", not "no leak".

Overlap: flowsb's `verify/noninterference.py` (branch s16-flowsb) does non-interference replay against the STORED
production output inside the Kernel's claim path, for declared copies of the truth. This module is a standalone
audit of third-party code where no production output exists; it reuses the flipped + constant world idea and adds
the transductive world. It does not import that module.

Infrastructure errors raised by the sandbox (e.g. it cannot start a process) propagate; they are not the program's
failure. Invalid audit input (missing columns, duplicate ids, ragged rows) raises ValueError.
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum

from ..sandbox.base import Sandbox, WorkspaceEscape, safe_path

# ---------------------------------------------------------------------------------------------------------------
# Parameters (GOAL §13.8). One place.
FLOAT_ABS_TOL = 1e-6  # UNCALIBRATED (owner decision, GOAL §13.8): float predictions within this are the same decision
EXTREME_SPAN_FACTOR = 10.0  # UNCALIBRATED (owner decision, GOAL §13.8): B value = max + factor*(max-min) + offset
EXTREME_OFFSET = 1000.0  # UNCALIBRATED (owner decision, GOAL §13.8)
DIAGNOSTIC_MAX_CHARS = 1500  # UNCALIBRATED (owner decision, GOAL §13.8): bound on the stderr diagnostic kept
MAX_PREDICTIONS_BYTES = 64 << 20  # UNCALIBRATED (owner decision, GOAL §13.8): larger preds.csv -> COULD_NOT_RUN
PARALLEL_RUNS = 3  # UNCALIBRATED (owner decision, GOAL §13.8): sandbox runs at once (machine budget, not policy)
# ---------------------------------------------------------------------------------------------------------------

ENTRYPOINT = "pipeline.py"
DATA_PATH = "in/data.csv"
OUT_PATH = "out/preds.csv"
OUT_HEADER = ["row_id", "prediction"]


class Verdict(StrEnum):
    FLAGGED = "flagged"
    CLEAN = "clean"
    INCONCLUSIVE = "inconclusive"
    COULD_NOT_RUN = "could_not_run"


class World(StrEnum):
    REAL = "real"
    FLIPPED = "flipped"
    CONSTANT = "constant"
    TRANSDUCTIVE = "transductive"


class Code(StrEnum):
    """Closed set (no free text can carry data out)."""

    # FLAGGED
    READS_TEST_LABELS_FLIPPED = "reads_test_labels_flipped"
    READS_TEST_LABELS_CONSTANT = "reads_test_labels_constant"
    TRANSDUCTIVE = "transductive"
    # INCONCLUSIVE
    SPLIT_DEPENDS_ON_LABELS = "split_depends_on_labels"
    SPLIT_DEPENDS_ON_FEATURES = "split_depends_on_features"
    NONDETERMINISTIC = "nondeterministic"
    SINGLE_CLASS_LABELS = "single_class_labels"  # nothing to flip: the label worlds cannot be built
    # informational (may accompany CLEAN / FLAGGED / INCONCLUSIVE): the transductive world was not run because
    # the data has no numeric feature column or fewer than 2 test rows
    TRANSDUCTIVE_NOT_CHECKED = "transductive_not_checked"
    # COULD_NOT_RUN
    CRASH = "crash"
    TIMEOUT = "timeout"
    RESOURCE_LIMIT = "resource_limit"
    OUTPUT_MISSING = "output_missing"
    OUTPUT_TOO_LARGE = "output_too_large"
    OUTPUT_MALFORMED = "output_malformed"
    WORKSPACE_ESCAPE = "workspace_escape"
    NO_PREDICTIONS = "no_predictions"
    DUPLICATE_ROW_ID = "duplicate_row_id"
    UNKNOWN_ROW_ID = "unknown_row_id"


@dataclass(frozen=True)
class WorldStats:
    """One world compared with the first real run (for World.REAL: the second real run)."""

    world: World
    ran: bool
    n_test_rows: int = 0  # rows this world's run predicted
    n_compared: int = 0  # rows whose decisions were compared (0 when the split changed)
    n_changed: int = 0  # compared rows whose DECISION differs
    n_bytes_differ: int = 0  # compared rows whose prediction string differs at all (diagnostic of float noise)
    split_changed: bool = False  # the set of predicted row_ids differs from the real run's


@dataclass(frozen=True)
class LabelDepResult:
    verdict: Verdict
    codes: tuple[Code, ...]
    worlds: tuple[WorldStats, ...]
    n_rows: int
    n_classes: int
    failed_world: World | None = None
    diagnostic: str = ""  # bounded stderr tail of the failed run (program text; the only free text here)


# ---------------------------------------------------------------------------------------------------------------
# the data table and the worlds (pure functions)


@dataclass(frozen=True)
class _Table:
    header: list[str]
    rows: list[list[str]]
    id_idx: int
    label_idx: int


def _parse_table(data_csv: bytes, id_column: str, label_column: str) -> _Table:
    try:
        text = data_csv.decode("utf-8-sig")
        records = [r for r in csv.reader(io.StringIO(text, newline="")) if r]
    except (UnicodeDecodeError, csv.Error) as e:
        raise ValueError(f"data_csv is not a readable UTF-8 CSV: {e}") from e
    if not records:
        raise ValueError("data_csv is empty")
    header, rows = records[0], records[1:]
    if id_column == label_column:
        raise ValueError("id_column and label_column must differ")
    for col in (id_column, label_column):
        if header.count(col) != 1:
            raise ValueError(f"column {col!r} must appear exactly once in the header")
    if not rows:
        raise ValueError("data_csv has no data rows")
    id_idx, label_idx = header.index(id_column), header.index(label_column)
    seen: set[str] = set()
    for n, r in enumerate(rows, start=2):
        if len(r) != len(header):
            raise ValueError(f"line {n} has {len(r)} fields, the header has {len(header)}")
        if not r[id_idx] or r[id_idx] in seen:
            raise ValueError(f"line {n}: empty or duplicate id")
        seen.add(r[id_idx])
    return _Table(header, rows, id_idx, label_idx)


def _serialize(t: _Table, rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(t.header)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _id_sort_key(ids: Sequence[str]) -> Callable[[str], tuple[int, str]]:
    """Numeric order when every id is an integer, else string order."""
    try:
        for i in ids:
            int(i)
    except ValueError:
        return lambda s: (0, s)
    return lambda s: (int(s), s)


def split_halves(test_ids: Sequence[str]) -> tuple[list[str], list[str]]:
    """Deterministic halves (A, B) of the test rows: sorted by row_id, alternating A, B, A, B, ..."""
    ordered = sorted(test_ids, key=_id_sort_key(test_ids))
    return ordered[0::2], ordered[1::2]


def _finite_float(s: str) -> float | None:
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _numeric_columns(t: _Table) -> list[int]:
    out = []
    for j in range(len(t.header)):
        if j in (t.id_idx, t.label_idx):
            continue
        if all(_finite_float(r[j]) is not None for r in t.rows):
            out.append(j)
    return out


def _extreme(values: list[str]) -> str:
    nums = [float(v) for v in values]
    hi, lo = max(nums), min(nums)
    x = hi + EXTREME_SPAN_FACTOR * (hi - lo) + EXTREME_OFFSET
    try:
        for v in values:
            int(v)
    except ValueError:
        return repr(x)
    return str(math.ceil(x))


def _world_rows(t: _Table, world: World, test_ids: Sequence[str]) -> list[list[str]]:
    rows = [list(r) for r in t.rows]
    test = set(test_ids)
    if world is World.REAL:
        return rows
    if world is World.TRANSDUCTIVE:
        b = set(split_halves(test_ids)[1])
        for j in _numeric_columns(t):
            x = _extreme([r[j] for r in t.rows])
            for r in rows:
                if r[t.id_idx] in b:
                    r[j] = x
        return rows
    classes = sorted({r[t.label_idx] for r in t.rows})
    if len(classes) < 2:
        raise ValueError("the label column has a single class; the label worlds cannot be built")
    if world is World.FLIPPED:
        nxt = {c: classes[(k + 1) % len(classes)] for k, c in enumerate(classes)}
        for r in rows:
            if r[t.id_idx] in test:
                r[t.label_idx] = nxt[r[t.label_idx]]
        return rows
    counts = {c: 0 for c in classes}  # World.CONSTANT
    for r in t.rows:
        if r[t.id_idx] in test:
            counts[r[t.label_idx]] += 1
    const = min(classes, key=lambda c: (counts[c], c))  # fewest agreements = most disagreements
    for r in rows:
        if r[t.id_idx] in test:
            r[t.label_idx] = const
    return rows


def build_world(data_csv: bytes, *, id_column: str, label_column: str, world: World,
                test_ids: Sequence[str]) -> bytes:
    """The data file the program receives in `world` (test_ids: the real run's predicted row_ids)."""
    t = _parse_table(data_csv, id_column, label_column)
    return _serialize(t, _world_rows(t, world, test_ids))


# ---------------------------------------------------------------------------------------------------------------
# running the program


@dataclass(frozen=True)
class _Run:
    preds: dict[str, str] | None
    code: Code | None = None
    diag: str = ""


def _parse_predictions(raw: bytes, known_ids: set[str]) -> tuple[dict[str, str] | None, Code | None]:
    try:
        records = [r for r in csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")) if r]
    except (UnicodeDecodeError, csv.Error):
        return None, Code.OUTPUT_MALFORMED
    if not records or records[0] != OUT_HEADER:
        return None, Code.OUTPUT_MALFORMED
    preds: dict[str, str] = {}
    for r in records[1:]:
        if len(r) != 2:
            return None, Code.OUTPUT_MALFORMED
        rid, pred = r
        if rid not in known_ids:
            return None, Code.UNKNOWN_ROW_ID
        if rid in preds:
            return None, Code.DUPLICATE_ROW_ID
        preds[rid] = pred.strip()
    if not preds:
        return None, Code.NO_PREDICTIONS
    return preds, None


def _run(sb: Sandbox, code: bytes, data: bytes, known_ids: set[str], seed: int, timeout_s: float) -> _Run:
    """One run in a fresh sandbox. Every host-side workspace path goes through safe_path."""
    ws = sb.workspace
    safe_path(ws, ENTRYPOINT).write_bytes(code)
    for d in ("in", "out"):
        safe_path(ws, d).mkdir(exist_ok=True)
    safe_path(ws, DATA_PATH).write_bytes(data)
    stale = safe_path(ws, OUT_PATH)
    if stale.exists():
        stale.unlink()
    res = sb.exec([sb.python, ENTRYPOINT, "--data", DATA_PATH, "--out", OUT_PATH, "--seed", str(seed)],
                  timeout_s=timeout_s)
    diag = (f"rc={res.returncode} timed_out={res.timed_out} "
            f"stderr_tail={res.stderr[-DIAGNOSTIC_MAX_CHARS:]!r}")[-DIAGNOSTIC_MAX_CHARS:]
    if res.timed_out:
        return _Run(None, Code.TIMEOUT, diag)
    if res.limit_exceeded:
        return _Run(None, Code.RESOURCE_LIMIT, diag)
    if res.returncode != 0:
        return _Run(None, Code.CRASH, diag)
    try:
        out = safe_path(ws, OUT_PATH)
        if not out.is_file():
            return _Run(None, Code.OUTPUT_MISSING, diag)
        if out.stat().st_size > MAX_PREDICTIONS_BYTES:
            return _Run(None, Code.OUTPUT_TOO_LARGE, diag)
        raw = out.read_bytes()
    except WorkspaceEscape:
        return _Run(None, Code.WORKSPACE_ESCAPE, diag)
    except OSError:
        return _Run(None, Code.OUTPUT_MISSING, diag)
    preds, err = _parse_predictions(raw, known_ids)
    return _Run(preds, err, diag if err else "")


def _compare(world: World, ref: dict[str, str], other: dict[str, str], ids: Sequence[str],
             tol: float) -> WorldStats:
    if set(other) != set(ref):
        return WorldStats(world, ran=True, n_test_rows=len(other), split_changed=True)
    pairs = [(ref[i], other[i]) for i in ids]
    floats = [(fa, fb) for a, b in pairs
              if (fa := _finite_float(a)) is not None and (fb := _finite_float(b)) is not None]
    if len(floats) == len(pairs):
        changed = sum(1 for fa, fb in floats if abs(fa - fb) > tol)
    else:
        changed = sum(1 for a, b in pairs if a != b)
    return WorldStats(world, ran=True, n_test_rows=len(other), n_compared=len(pairs), n_changed=changed,
                      n_bytes_differ=sum(1 for a, b in pairs if a != b))


def label_dependence_audit(code: bytes, data_csv: bytes, *, id_column: str, label_column: str,
                           sandbox_factory: Callable[[str], Sandbox], seed: int = 0, timeout_s: float = 120.0,
                           float_abs_tol: float = FLOAT_ABS_TOL,
                           parallel_runs: int = PARALLEL_RUNS) -> LabelDepResult:
    """Audit one pipeline file (`code`) on `data_csv`. Each run gets a FRESH sandbox from `sandbox_factory(name)`.
    At most 5 runs: real x2, then flipped, constant and transductive."""
    t = _parse_table(data_csv, id_column, label_column)
    known = {r[t.id_idx] for r in t.rows}
    n_rows, n_classes = len(t.rows), len({r[t.label_idx] for r in t.rows})

    def finish(verdict: Verdict, codes: Sequence[Code], worlds: Sequence[WorldStats],
               failed: World | None = None, diag: str = "") -> LabelDepResult:
        return LabelDepResult(verdict, tuple(codes), tuple(worlds), n_rows, n_classes, failed, diag)

    def run_all(jobs: list[tuple[str, bytes]]) -> list[_Run]:
        boxes = [sandbox_factory(f"labeldep-{name}") for name, _ in jobs]  # created in order, on this thread
        with ThreadPoolExecutor(max_workers=max(1, parallel_runs)) as pool:
            futs = [pool.submit(_run, sb, code, data, known, seed, timeout_s)
                    for sb, (_, data) in zip(boxes, jobs, strict=True)]
            return [f.result() for f in futs]

    if n_classes < 2:
        return finish(Verdict.INCONCLUSIVE, [Code.SINGLE_CLASS_LABELS], [])

    real_data = _serialize(t, _world_rows(t, World.REAL, []))
    r1, r2 = run_all([("real-1", real_data), ("real-2", real_data)])
    for r in (r1, r2):
        if r.preds is None:
            assert r.code is not None
            return finish(Verdict.COULD_NOT_RUN, [r.code], [WorldStats(World.REAL, ran=True)], World.REAL, r.diag)
    assert r1.preds is not None and r2.preds is not None
    ref = r1.preds
    test_ids = list(ref)
    real_stats = _compare(World.REAL, ref, r2.preds, test_ids, float_abs_tol)
    if real_stats.split_changed or real_stats.n_changed:
        return finish(Verdict.INCONCLUSIVE, [Code.NONDETERMINISTIC], [real_stats])

    a_ids, _ = split_halves(test_ids)
    transductive_ok = len(test_ids) >= 2 and bool(_numeric_columns(t))
    worlds = [World.FLIPPED, World.CONSTANT] + ([World.TRANSDUCTIVE] if transductive_ok else [])
    runs = run_all([(w.value, _serialize(t, _world_rows(t, w, test_ids))) for w in worlds])

    stats = [real_stats]
    for w, r in zip(worlds, runs, strict=True):
        if r.preds is None:
            assert r.code is not None
            return finish(Verdict.COULD_NOT_RUN, [r.code], stats + [WorldStats(w, ran=True)], w, r.diag)
        stats.append(_compare(w, ref, r.preds, a_ids if w is World.TRANSDUCTIVE else test_ids, float_abs_tol))
    if not transductive_ok:
        stats.append(WorldStats(World.TRANSDUCTIVE, ran=False))
    note = [] if transductive_ok else [Code.TRANSDUCTIVE_NOT_CHECKED]
    by = {s.world: s for s in stats}

    inconclusive = []
    if by[World.FLIPPED].split_changed or by[World.CONSTANT].split_changed:
        inconclusive.append(Code.SPLIT_DEPENDS_ON_LABELS)
    if by[World.TRANSDUCTIVE].split_changed:
        inconclusive.append(Code.SPLIT_DEPENDS_ON_FEATURES)
    if inconclusive:  # checked BEFORE predictions: a moved test set never yields a flag
        return finish(Verdict.INCONCLUSIVE, inconclusive + note, stats)

    flags = [c for w, c in ((World.FLIPPED, Code.READS_TEST_LABELS_FLIPPED),
                            (World.CONSTANT, Code.READS_TEST_LABELS_CONSTANT),
                            (World.TRANSDUCTIVE, Code.TRANSDUCTIVE)) if by[w].n_changed > 0]
    if flags:
        return finish(Verdict.FLAGGED, flags + note, stats)
    return finish(Verdict.CLEAN, note, stats)
