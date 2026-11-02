"""Seal at project start (GOAL 18.6, UPGRADES N7): the keyed split of a researcher's dataset, computed inside kerneld.

This module is pure. `seal_files` turns the raw CSV into the Lab's part and one or two sealed parts. Which rows are
sealed comes from `commitment.draw("seal_split", <sha256 of the data>, <beacon>)`, so nobody chooses them and anyone
holding the data and the beacon can re-run the split. A sealed row whose features also occur in the Lab's part or an
earlier sealed part is dropped: its label would not be fresh. kerneld/ingest.sealed_split registers the result.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
from dataclasses import dataclass

SEALED_FORMAT = "chh-court-sealed/1"
MAX_PARTS = 2  # the second part is for RE-VALIDATED (GOAL 18.2)
MIN_PART_ROWS = 2


class SealSpecError(ValueError):
    pass


@dataclass(frozen=True)
class SealedPart:
    test_x: bytes  # features only
    test_y: bytes  # PRIVATE: the "label" column
    n: int


@dataclass(frozen=True)
class SealedFiles:
    lab: bytes  # what the Lab receives, with its "label" column
    parts: tuple[SealedPart, ...]
    n_lab: int
    data_sha256: str


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


def seal_files(raw: bytes, project: str, label_column: str, n_parts: int, rows_per_part: int, beacon: str
               ) -> SealedFiles:
    """Deterministic in its arguments. SealSpecError names what is wrong with the request, never a data value."""
    from ..commitment import as_key, draw
    if not 1 <= n_parts <= MAX_PARTS or rows_per_part < MIN_PART_ROWS:
        raise SealSpecError(f"1 to {MAX_PARTS} parts of at least {MIN_PART_ROWS} rows")
    try:
        table = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
    except UnicodeDecodeError:
        raise SealSpecError("the data is not UTF-8") from None
    if not table or label_column not in table[0]:
        raise SealSpecError("the label column is not in the header")
    head = table[0]
    li = head.index(label_column)
    if "label" in head and head[li] != "label":
        raise SealSpecError("another column is already named 'label'")
    rows = [r for r in table[1:] if r]
    if any(len(r) != len(head) for r in rows):
        raise SealSpecError("a row does not have one value per column")
    if len(rows) <= n_parts * rows_per_part:
        raise SealSpecError("too few rows: the Lab would get nothing")
    data_sha = hashlib.sha256(raw).hexdigest()
    key = as_key(draw("seal_split", prereg_hash=data_sha, beacon=beacon, secret=None))
    order = sorted(range(len(rows)), key=lambda i: hmac.new(key, f"{project}:{i}".encode(), "sha256").digest())

    def x(r: list[str]) -> tuple[str, ...]:
        return tuple(v for j, v in enumerate(r) if j != li)

    sealed_n = n_parts * rows_per_part
    lab_rows = [rows[i] for i in order[sealed_n:]]
    seen = {x(r) for r in lab_rows}
    features = [c for j, c in enumerate(head) if j != li]
    parts = []
    for p in range(n_parts):
        kept = []
        for i in order[p * rows_per_part: (p + 1) * rows_per_part]:
            if x(rows[i]) not in seen:
                seen.add(x(rows[i]))
                kept.append(rows[i])
        if len(kept) < MIN_PART_ROWS:
            raise SealSpecError("a sealed part is empty once rows repeated in the Lab's part are dropped")
        parts.append(SealedPart(_csv([features, *[list(x(r)) for r in kept]]),
                                _csv([["label"], *[[r[li]] for r in kept]]), len(kept)))
    header = ["label" if j == li else c for j, c in enumerate(head)]
    return SealedFiles(_csv([header, *lab_rows]), tuple(parts), len(lab_rows), data_sha)
