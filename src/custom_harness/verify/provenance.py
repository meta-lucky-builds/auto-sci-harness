"""Feature provenance (GOAL §16.5 item 6): leak columns already in the raw data.

An ID column sorted by class, or a variable measured after the outcome, is invisible to every output check: by its
outputs a raw-data leak looks exactly like a genuinely predictive feature (code_review/upgrade_review_2026-10-07/
crossdomain-final.md, E2). Legitimacy is semantic, so the defence is semantic too:

1. **Tags.** An optional ``features.yaml`` in the task directory, declared at task registration::

       features:
         <column>: {availability: pre_outcome|post_outcome|unknown,
                    role: measurement|derived|identifier|unknown,
                    source: <free text for the human reader>}

   A tag for a column that is not a feature of train.csv (or for the target) is an error: fail closed. An untagged
   column is ``availability=unknown, role=unknown``.
2. **Refusal.** A feature tagged ``post_outcome`` or role ``identifier`` is refused outright.
3. **Screen** (train data only; ``private/`` is never opened). Per-feature mutual information with the target, an
   identifier heuristic and a train/test near-duplicate audit. The screen issues no verdict: anything it flags is
   ROUTED to a human, who rules ``legitimate`` or ``leak`` with ``chh rule-feature`` (HUMAN-only, interactive).
4. **Gate.** :func:`gate` refuses unless nothing was refused, every routed item has a current ``legitimate``
   ruling, no ruling says ``leak``, and the ruling chain verifies.

Reason codes are a closed set (:data:`REASON_CODES`); they never carry free text or data values.

Ruling store: INTERIM. Rulings live in a hash-chained append-only JSONL file,
``data_dir()/provenance/rulings.jsonl``, until the Kernel gets a ledger kind for them (requested from workstream
kstats). The chain detects an edited, reordered or deleted line, but NOT the removal of trailing lines (no anchor
outside the file); the Kernel ledger kind is the fix for that.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..config import data_dir
from .metrics import CLASSIFICATION

# ----------------------------------------------------------------------------------------------------------------
# Screen configuration: the ONLY place these thresholds live. Conservative = route more to the human; the screen
# never auto-accepts anything.
# ----------------------------------------------------------------------------------------------------------------
# A feature is "dominant" when its MI with the target exceeds DOMINANCE_RATIO x every other feature's MI.
DOMINANCE_RATIO = 2.0  # UNCALIBRATED (owner decision, GOAL §13.8)
# Denominator floor (nats) for the dominance ratio, so a lone informative feature among noise is still compared.
MI_FLOOR_NATS = 0.01  # UNCALIBRATED (owner decision, GOAL §13.8)
# A feature whose MI reaches this fraction of H(target) is routed whatever the other features do (two leaks would
# otherwise mask each other in the ratio test).
SATURATION_FRACTION = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8)
# Quantile bins for numeric features and for regression targets; categorical features keep their top
# (N_BINS - 1) levels and merge the rest, so every feature's MI has a comparable bias.
N_BINS = 10  # UNCALIBRATED (owner decision, GOAL §13.8)
# Identifier heuristic: |Spearman rho| between a numeric feature and the row index at or above this is routed.
MONOTONE_CUTOFF = 0.9  # UNCALIBRATED (owner decision, GOAL §13.8)
# Identifier heuristic: an integer-like (or non-numeric) column unique per row is routed when it has >= this many rows.
MIN_ROWS_FOR_UNIQUE_ID = 10  # UNCALIBRATED (owner decision, GOAL §13.8)
# Duplicate audit: the task is routed when more than this fraction of test_X rows equal a train row's features ...
DUPLICATE_FRACTION_MAX = 0.01  # UNCALIBRATED (owner decision, GOAL §13.8)
# ... after rounding numeric values to this many significant digits ("near" duplicates).
DUPLICATE_SIG_DIGITS = 6  # UNCALIBRATED (owner decision, GOAL §13.8)
# What happens to a feature with availability or role "unknown" (no features.yaml entry): "route" sends it to the
# human; "ignore" would let it through. Only "route" is conservative.
UNTAGGED_POLICY = "route"  # UNCALIBRATED (owner decision, GOAL §13.8)

SCREEN_CONFIG: dict[str, Any] = {
    "dominance_ratio": DOMINANCE_RATIO, "mi_floor_nats": MI_FLOOR_NATS, "saturation_fraction": SATURATION_FRACTION,
    "n_bins": N_BINS, "monotone_cutoff": MONOTONE_CUTOFF, "min_rows_for_unique_id": MIN_ROWS_FOR_UNIQUE_ID,
    "duplicate_fraction_max": DUPLICATE_FRACTION_MAX, "duplicate_sig_digits": DUPLICATE_SIG_DIGITS,
    "untagged_policy": UNTAGGED_POLICY,
}

AVAILABILITY = frozenset({"pre_outcome", "post_outcome", "unknown"})
ROLES = frozenset({"measurement", "derived", "identifier", "unknown"})
RULINGS = frozenset({"legitimate", "leak"})
ROWS_ITEM = "__rows__"  # pseudo-feature the duplicate audit routes; a ruling on it rules on the train/test overlap

# Closed reason-code set (no free text, no data values).
REFUSE_POST_OUTCOME = "post_outcome_feature"
REFUSE_IDENTIFIER = "identifier_feature"
ROUTE_DOMINANT = "dominant_feature"
ROUTE_SATURATING = "saturating_feature"
ROUTE_UNIQUE_ID = "suspected_identifier_unique"
ROUTE_MONOTONE_ID = "suspected_identifier_monotone"
ROUTE_UNTAGGED = "untagged_feature"
ROUTE_DUPLICATES = "train_test_duplicates"
GATE_REFUSED = "refused_feature"
GATE_MISSING_RULING = "missing_ruling"
GATE_LEAK_RULING = "leak_ruling"
GATE_CHAIN_TAMPERED = "ruling_chain_tampered"
GATE_REPORT_MISMATCH = "report_hash_mismatch"
REASON_CODES = frozenset({
    REFUSE_POST_OUTCOME, REFUSE_IDENTIFIER, ROUTE_DOMINANT, ROUTE_SATURATING, ROUTE_UNIQUE_ID, ROUTE_MONOTONE_ID,
    ROUTE_UNTAGGED, ROUTE_DUPLICATES, GATE_REFUSED, GATE_MISSING_RULING, GATE_LEAK_RULING, GATE_CHAIN_TAMPERED,
    GATE_REPORT_MISMATCH,
})

GENESIS = "0" * 64


class ProvenanceError(ValueError):
    """features.yaml or the task files do not match (fail closed)."""


# ----------------------------------------------------------------------------------------------------------------
# hashing
# ----------------------------------------------------------------------------------------------------------------

def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True).encode()


def _sha(obj: Any) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def data_digest(task_dir: Path) -> str:
    """sha256 over train.csv and test_X.csv bytes (each length-prefixed, so the split point is bound too)."""
    train, test = _public_paths(Path(task_dir))
    h = hashlib.sha256(b"chh-provenance-data-v1")
    for p in (train, test):
        b = p.read_bytes()
        h.update(len(b).to_bytes(8, "big"))
        h.update(b)
    return h.hexdigest()


def tags_hash(tags: Mapping[str, Mapping[str, str]]) -> str:
    return _sha({"v": 1, "tags": tags})


# ----------------------------------------------------------------------------------------------------------------
# task files
# ----------------------------------------------------------------------------------------------------------------

def _spec(task_dir: Path) -> dict[str, Any]:
    raw = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("target_column"), str):
        raise ProvenanceError("task.yaml has no target_column")
    return raw


def _public_paths(task_dir: Path) -> tuple[Path, Path]:
    spec = _spec(task_dir)
    return (task_dir / "public" / str(spec.get("train_file", "train.csv")),
            task_dir / "public" / str(spec.get("test_features_file", "test_X.csv")))


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ProvenanceError(f"{path.name} is empty")
    return rows[0], rows[1:]


def load_tags(task_dir: Path, features: Sequence[str]) -> dict[str, dict[str, str]]:
    """Resolved tags for every feature. Fails closed on a tag for a non-feature, an unknown key or an enum value
    outside the closed sets. Untagged features get availability=unknown, role=unknown."""
    path = Path(task_dir) / "features.yaml"
    declared: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != {"features"} or not isinstance(raw["features"], dict):
            raise ProvenanceError("features.yaml must be a mapping with exactly one key 'features'")
        declared = raw["features"]
    known = set(features)
    out: dict[str, dict[str, str]] = {}
    for name, tag in declared.items():
        if name not in known:
            raise ProvenanceError(f"features.yaml tags {name!r}, which is not a feature column of train.csv")
        if not isinstance(tag, dict) or not set(tag) <= {"availability", "role", "source"}:
            raise ProvenanceError(f"features.yaml: tag for {name!r} must use only availability, role, source")
        av, role = tag.get("availability", "unknown"), tag.get("role", "unknown")
        if av not in AVAILABILITY or role not in ROLES:
            raise ProvenanceError(f"features.yaml: {name!r} has availability/role outside the closed sets")
        out[name] = {"availability": av, "role": role, "source": str(tag.get("source", ""))}
    for name in features:
        out.setdefault(name, {"availability": "unknown", "role": "unknown", "source": ""})
    return out


# ----------------------------------------------------------------------------------------------------------------
# statistics (stdlib only)
# ----------------------------------------------------------------------------------------------------------------

def _num(v: str) -> float | None:
    try:
        x = float(v)
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _is_numeric(col: Sequence[str]) -> bool:
    present = [v for v in col if v.strip() != ""]
    return bool(present) and all(_num(v) is not None for v in present)


def _discretise(col: Sequence[str], numeric: bool, n_bins: int) -> list[str]:
    """Quantile bins for numeric values (values themselves when there are few distinct ones); top (n_bins - 1)
    levels plus '__other__' for categories. Missing values are their own level."""
    if numeric:
        vals = [_num(v) for v in col]
        present = sorted(x for x in vals if x is not None)
        if len(set(present)) <= n_bins:
            return ["__missing__" if x is None else repr(x) for x in vals]
        cuts = sorted({present[min(len(present) - 1, (len(present) * k) // n_bins)] for k in range(1, n_bins)})
        return ["__missing__" if x is None else f"b{bisect.bisect_right(cuts, x)}" for x in vals]
    keep = {v for v, _ in Counter(col).most_common(n_bins - 1)}
    return [v if v in keep else "__other__" for v in col]


def _entropy(labels: Sequence[str]) -> float:
    n = len(labels)
    return -sum(c / n * math.log(c / n) for c in Counter(labels).values()) if n else 0.0


def mutual_information(x: Sequence[str], y: Sequence[str]) -> float:
    """Plug-in MI (nats) of two discrete sequences with the Miller-Madow bias correction, clamped at 0."""
    n = len(x)
    if n == 0:
        return 0.0
    cx, cy, cxy = Counter(x), Counter(y), Counter(zip(x, y, strict=True))
    mi = sum(c / n * math.log(c * n / (cx[a] * cy[b])) for (a, b), c in cxy.items())
    mi -= (len(cx) - 1) * (len(cy) - 1) / (2 * n)
    return max(0.0, mi)


def _ranks(vals: Sequence[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def _pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    return 0.0 if saa == 0 or sbb == 0 else sab / math.sqrt(saa * sbb)


def row_order_spearman(col: Sequence[str]) -> float:
    """|Spearman rho| between the column's (non-missing numeric) values and their row positions."""
    pairs = [(i, x) for i, v in enumerate(col) if (x := _num(v)) is not None]
    if len(pairs) < 3:
        return 0.0
    return abs(_pearson(_ranks([float(i) for i, _ in pairs]), _ranks([x for _, x in pairs])))


def _unique_identifier_like(col: Sequence[str], numeric: bool) -> bool:
    present = [v for v in col if v.strip() != ""]
    if len(present) < MIN_ROWS_FOR_UNIQUE_ID or len(set(present)) != len(present):
        return False
    if not numeric:
        return True
    return all((x := _num(v)) is not None and x == int(x) for v in present)


def _row_key(row: Sequence[str], numeric: Sequence[bool]) -> tuple[str, ...]:
    out = []
    for v, isnum in zip(row, numeric, strict=True):
        x = _num(v) if isnum else None
        out.append(f"{x:.{DUPLICATE_SIG_DIGITS}g}" if x is not None else v.strip())
    return tuple(out)


# ----------------------------------------------------------------------------------------------------------------
# screen
# ----------------------------------------------------------------------------------------------------------------

def screen(task_dir: Path) -> dict[str, Any]:
    """Run the refusal rules and the dominance / identifier / duplicate screen on TRAIN data (plus test_X features
    for the duplicate audit). Never opens private/. Returns a JSON-able report with ``refused`` and ``routed``
    ({feature: [reason codes]}) and a ``report_hash`` over everything else. It issues no verdict."""
    task_dir = Path(task_dir)
    spec = _spec(task_dir)
    target = spec["target_column"]
    train_path, test_path = _public_paths(task_dir)
    header, rows = _read_csv(train_path)
    if target not in header:
        raise ProvenanceError("train.csv has no target column")
    if ROWS_ITEM in header:
        raise ProvenanceError(f"column name {ROWS_ITEM!r} is reserved")
    if len(set(header)) != len(header):
        raise ProvenanceError("train.csv has duplicate column names")
    if any(len(r) != len(header) for r in rows):
        raise ProvenanceError("train.csv has ragged rows")
    features = [c for c in header if c != target]
    test_header, test_rows = _read_csv(test_path)
    if test_header != features:
        raise ProvenanceError("test_X.csv columns differ from train.csv's feature columns")
    tags = load_tags(task_dir, features)

    idx = {c: header.index(c) for c in header}
    cols = {c: [r[idx[c]] for r in rows] for c in header}
    numeric = {c: _is_numeric(cols[c]) for c in features}
    regression = str(spec.get("metric", "")) not in CLASSIFICATION
    y = _discretise(cols[target], regression and _is_numeric(cols[target]), N_BINS) if regression \
        else cols[target]
    h_y = _entropy(y)

    mi = {c: mutual_information(_discretise(cols[c], numeric[c], N_BINS), y) for c in features}
    flags: dict[str, list[str]] = {c: [] for c in features}
    refused: dict[str, list[str]] = {}
    for c in features:
        if tags[c]["availability"] == "post_outcome":
            refused.setdefault(c, []).append(REFUSE_POST_OUTCOME)
        if tags[c]["role"] == "identifier":
            refused.setdefault(c, []).append(REFUSE_IDENTIFIER)
        if UNTAGGED_POLICY != "ignore" and "unknown" in (tags[c]["availability"], tags[c]["role"]):
            flags[c].append(ROUTE_UNTAGGED)
        if h_y > 0 and mi[c] >= SATURATION_FRACTION * h_y:
            flags[c].append(ROUTE_SATURATING)
        if _unique_identifier_like(cols[c], numeric[c]):
            flags[c].append(ROUTE_UNIQUE_ID)
        if numeric[c] and row_order_spearman(cols[c]) >= MONOTONE_CUTOFF:
            flags[c].append(ROUTE_MONOTONE_ID)
    if features:
        top = max(features, key=lambda c: (mi[c], c))
        others = [mi[c] for c in features if c != top]
        if mi[top] > DOMINANCE_RATIO * max(max(others, default=0.0), MI_FLOOR_NATS):
            flags[top].append(ROUTE_DOMINANT)

    num_list = [numeric[c] for c in features]
    train_keys = {_row_key([r[idx[c]] for c in features], num_list) for r in rows}
    dup = sum(_row_key(r, num_list) in train_keys for r in test_rows if len(r) == len(features))
    dup_fraction = dup / len(test_rows) if test_rows else 0.0
    routed = {c: sorted(f) for c, f in flags.items() if f and c not in refused}
    if dup_fraction > DUPLICATE_FRACTION_MAX:
        routed[ROWS_ITEM] = [ROUTE_DUPLICATES]

    body: dict[str, Any] = {
        "version": 1, "task_id": str(spec.get("id", "")), "data_digest": data_digest(task_dir),
        "tags_hash": tags_hash(tags), "config": SCREEN_CONFIG, "target_entropy_nats": round(h_y, 9),
        "features": {c: {"tags": tags[c], "mi_nats": round(mi[c], 9), "flags": sorted(flags[c])} for c in features},
        "duplicate_fraction": round(dup_fraction, 9),
        "refused": {c: sorted(v) for c, v in refused.items()}, "routed": routed,
    }
    return {**body, "report_hash": _sha(body)}


def report_body_hash(report: Mapping[str, Any]) -> str:
    return _sha({k: v for k, v in report.items() if k != "report_hash"})


# ----------------------------------------------------------------------------------------------------------------
# rulings (INTERIM hash-chained JSONL store; see module docstring)
# ----------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RulingSet:
    rulings: list[dict[str, Any]] = field(default_factory=list)
    chain_ok: bool = True


def rulings_path() -> Path:
    return data_dir() / "provenance" / "rulings.jsonl"


def _line_hash(entry: Mapping[str, Any]) -> str:
    return _sha({k: v for k, v in entry.items() if k != "hash"})


def load_rulings(path: Path | None = None) -> RulingSet:
    """Read and verify the chain. Any unparsable line, wrong ``prev`` link or wrong ``hash`` marks it tampered."""
    path = path or rulings_path()
    if not path.exists():
        return RulingSet()
    out: list[dict[str, Any]] = []
    prev = GENESIS
    ok = True
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            ok = False
            continue
        if not isinstance(entry, dict) or entry.get("prev") != prev or entry.get("hash") != _line_hash(entry):
            ok = False
        out.append(entry)
        prev = str(entry.get("hash"))
    return RulingSet(out, ok)


def verify_chain(path: Path | None = None) -> bool:
    return load_rulings(path).chain_ok


def record_ruling(task_dir: Path, feature: str, ruling: str, approver: str, note: str = "", *,
                  path: Path | None = None) -> dict[str, Any]:
    """Backend of the HUMAN-only ``chh rule-feature`` command; that command is the way a ruling is recorded.
    Binds the ruling to the CURRENT screen report of ``task_dir`` (data digest, tags hash, report hash)."""
    if not approver or not approver.strip():
        raise ProvenanceError("a ruling needs a named human approver")
    if ruling not in RULINGS:
        raise ProvenanceError("ruling must be 'legitimate' or 'leak'")
    report = screen(Path(task_dir))
    if feature not in report["routed"]:
        raise ProvenanceError(f"{feature!r} is not routed to a human (refused features cannot be ruled legitimate)")
    path = path or rulings_path()
    existing = load_rulings(path)
    if not existing.chain_ok:
        raise ProvenanceError("the ruling chain is tampered; refusing to append")
    entry: dict[str, Any] = {
        "prev": existing.rulings[-1]["hash"] if existing.rulings else GENESIS,
        "data_digest": report["data_digest"], "feature": feature, "tags_hash": report["tags_hash"],
        "report_hash": report["report_hash"], "ruling": ruling, "approver": approver.strip(), "note": note,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    entry["hash"] = _line_hash(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


# ----------------------------------------------------------------------------------------------------------------
# gate
# ----------------------------------------------------------------------------------------------------------------

def gate(report: Mapping[str, Any], rulings: RulingSet | Iterable[Mapping[str, Any]]) -> list[str]:
    """Reason codes (sorted, closed set); empty = the task's features may be used.

    Refuses when: the report's hash does not match its body; any feature was refused; the ruling chain is tampered;
    any routed item lacks a ``legitimate`` ruling bound to this report (same data digest, tags hash and report hash);
    or any ruling on a routed item of this data says ``leak``. A ``leak`` ruling counts even when the tags or the
    report changed since (re-tagging must not erase a leak finding); a ``legitimate`` ruling for other data, tags or
    report is stale and does not count."""
    rs = rulings if isinstance(rulings, RulingSet) else RulingSet([dict(r) for r in rulings])
    codes: set[str] = set()
    if report.get("report_hash") != report_body_hash(report):
        codes.add(GATE_REPORT_MISMATCH)
    if report.get("refused"):
        codes.add(GATE_REFUSED)
    if not rs.chain_ok:
        codes.add(GATE_CHAIN_TAMPERED)
    for feat in report.get("routed", {}):
        same_data = [r for r in rs.rulings
                     if r.get("feature") == feat and r.get("data_digest") == report["data_digest"]]
        if any(r.get("ruling") == "leak" for r in same_data):
            codes.add(GATE_LEAK_RULING)
        if not any(r.get("ruling") == "legitimate" and r.get("tags_hash") == report["tags_hash"]
                   and r.get("report_hash") == report["report_hash"] and str(r.get("approver", "")).strip()
                   for r in same_data):
            codes.add(GATE_MISSING_RULING)
    return sorted(codes)
