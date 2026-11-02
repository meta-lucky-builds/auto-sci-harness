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
FUTURE_FORMAT = "chh-court-future/1"
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


def future_protocol(project: str, n_test: int) -> str:
    """What a future-data claim freezes: kerneld/ingest.future_heldout takes only the data this names."""
    return (f"{FUTURE_FORMAT} project={project} n_test={n_test}: held-out truth registered by kerneld after this claim was "
            f"fixed and anchored (GOAL 18.6); both arms through one contract")


def future_files(raw: bytes, label_column: str, n_test: int, lab: bytes) -> SealedPart:
    """A future target from the file a person hands over. Exactly the promised rows, the Lab's columns, and no row the
    Lab already has: such a row's label would not be fresh. SealSpecError never names a data value."""
    try:
        table = [r for r in csv.reader(io.StringIO(raw.decode("utf-8"))) if r]
    except UnicodeDecodeError:
        raise SealSpecError("the data is not UTF-8") from None
    if not table or label_column not in table[0]:
        raise SealSpecError("the label column is not in the header")
    head, rows = table[0], table[1:]
    li = head.index(label_column)
    features = [c for j, c in enumerate(head) if j != li]
    lab_table = [r for r in csv.reader(io.StringIO(lab.decode("utf-8"))) if r]
    lab_li = lab_table[0].index("label")
    if features != [c for j, c in enumerate(lab_table[0]) if j != lab_li]:
        raise SealSpecError("the columns differ from the Lab's data")
    if len(rows) != n_test or any(len(r) != len(head) for r in rows):
        raise SealSpecError("the file does not have the promised number of whole rows")

    def x(r: list[str], at: int) -> tuple[str, ...]:
        return tuple(v for j, v in enumerate(r) if j != at)

    if {x(r, li) for r in rows} & {x(r, lab_li) for r in lab_table[1:]}:
        raise SealSpecError("a row of this data is in the Lab's part, so its label is not fresh")
    return SealedPart(_csv([features, *[list(x(r, li)) for r in rows]]), _csv([["label"], *[[r[li]] for r in rows]]),
                      len(rows))


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
