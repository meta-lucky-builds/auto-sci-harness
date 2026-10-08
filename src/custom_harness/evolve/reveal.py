"""Temporal-reveal mode for streams (GOAL §5 L6.11; Adaptive Auto-Harness): labels of a stream are SEALED outside
every evolver mount and become visible only once a watermark has passed their date.

Rules (all fail closed):
  - Seal: the labels live in a host-only JSONL file under `root`; `root` must not be inside, nor contain, any
    evolver mount (the evidence export, a proposer sandbox, a task directory). Checked when the seal is opened and
    again before every write of revealed labels.
  - Watermark: a label is visible iff it is DATED and its date is at or before the watermark. A date-only label
    ("2026-10-08") counts as the END of that day (it may have been produced at any time that day); a date-only
    watermark counts as the START of its day. A datetime without a timezone, an unparsable date or no date at all is
    UNDATED, and undated means unrevealed, whatever the watermark.
  - The watermark only moves forward, never past the clock (`now`), and each move is a store record.
  - Each reveal is ledgered: the first time an item becomes visible, one Kernel `release` entry (channel `export`,
    alphabet of the label value: kerneld/releases.alphabet_of) on the stream's registered target dataset, plus a
    `reveal` store record naming the item, its date and the watermark that released it. A reveal that cannot be
    ledgered raises (nothing is returned).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any

from ..kerneld.releases import LedgerSink, Sink, alphabet_of, note_code
from ..store import EvidenceStore

REVEAL_KIND = "reveal"
WATERMARK_KIND = "reveal_watermark"
LABELS_FILE = "labels.jsonl"


class RevealRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class LabelItem:
    item_id: str
    label: Any
    date: str | None  # ISO date or tz-aware ISO datetime; None / unparsable / naive = undated


def label_time(value: Any) -> datetime | None:
    """The latest instant a label's date allows, in UTC; None = undated (never revealed)."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        if len(v) == 10:
            d = date.fromisoformat(v)
            return datetime.combine(d, dtime.max, tzinfo=UTC)
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None  # a naive datetime is ambiguous by up to a day: undated
    return dt.astimezone(UTC)


def watermark_time(value: str) -> datetime:
    """The earliest instant a watermark allows, in UTC (refused if not a date or a tz-aware datetime)."""
    v = (value or "").strip()
    try:
        if len(v) == 10:
            return datetime.combine(date.fromisoformat(v), dtime.min, tzinfo=UTC)
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise RevealRefused(f"watermark {value!r} is not an ISO date or datetime") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise RevealRefused("a watermark datetime needs a timezone")
    return dt.astimezone(UTC)


def _inside(a: Path, b: Path) -> bool:
    return a == b or b in a.parents


class TemporalReveal:
    """One stream's sealed labels. `mounts`: every directory an evolver (proposer, export, sandbox, task) can see.
    `sink`: where reveals are ledgered (default: the Kernel release ledger of `store`)."""

    def __init__(self, store: EvidenceStore, stream_id: str, target_dataset_id: str, root: Path, *,
                 mounts: Iterable[Path], clock: Callable[[], float] = time.time, sink: Sink | None = None):
        if not stream_id or not target_dataset_id:
            raise RevealRefused("a stream needs an id and a registered target dataset")
        self.store, self.stream_id, self.target = store, stream_id, target_dataset_id
        self.root = Path(root).resolve()
        self.mounts = [Path(m).resolve() for m in mounts]
        if not self.mounts:
            raise RevealRefused("the evolver mounts must be named (an empty list cannot be checked)")
        self.clock = clock
        self.sink = sink if sink is not None else LedgerSink(store)
        self._check_seal()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ the seal
    def _check_seal(self) -> None:
        for m in self.mounts:
            if _inside(self.root, m) or _inside(m, self.root):
                raise RevealRefused(f"sealed labels at {self.root} overlap an evolver mount {m} (must be outside "
                                    "every mount)")

    @property
    def _file(self) -> Path:
        return self.root / LABELS_FILE

    def seal(self, items: Iterable[LabelItem]) -> int:
        """Append labels to the seal. An item id already sealed is refused (a re-dated copy would reveal early)."""
        self._check_seal()
        have = {r["item_id"] for r in self._read()}
        new = []
        for it in items:
            if not isinstance(it.item_id, str) or not it.item_id or it.item_id in have:
                raise RevealRefused(f"item id {it.item_id!r} is empty or already sealed")
            have.add(it.item_id)
            new.append({"item_id": it.item_id, "label": it.label, "date": it.date})
        with open(self._file, "a", encoding="utf-8", newline="") as f:
            for r in new:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        return len(new)

    def _read(self) -> list[dict[str, Any]]:
        if not self._file.exists():
            return []
        return [json.loads(ln) for ln in self._file.read_text(encoding="utf-8").splitlines() if ln.strip()]

    # ------------------------------------------------------------------ the watermark
    def watermark(self) -> datetime | None:
        recs = [r for r in self.store.records(WATERMARK_KIND) if r.get("stream") == self.stream_id]
        return watermark_time(recs[-1]["watermark"]) if recs else None

    def advance(self, value: str) -> datetime:
        wm = watermark_time(value)
        now = datetime.fromtimestamp(self.clock(), tz=UTC)
        if wm > now:
            raise RevealRefused(f"watermark {value} is in the future (now {now.isoformat()})")
        cur = self.watermark()
        if cur is not None and wm < cur:
            raise RevealRefused("the watermark only moves forward")
        self.store.append_record(WATERMARK_KIND, {"stream": self.stream_id, "watermark": wm.isoformat(),
                                                  "at": self.clock()})
        return wm

    # ------------------------------------------------------------------ reveals
    def _revealed_before(self) -> set[str]:
        return {r["item_id"] for r in self.store.records(REVEAL_KIND) if r.get("stream") == self.stream_id}

    def visible(self) -> list[dict[str, Any]]:
        """Every label dated at or before the watermark (none without a watermark). Each item's first reveal is
        ledgered before it is returned."""
        wm = self.watermark()
        if wm is None:
            return []
        out, before = [], self._revealed_before()
        for r in self._read():
            t = label_time(r.get("date"))
            if t is None or t > wm:
                continue
            if r["item_id"] not in before:
                self.sink.record_release(self.target, "export", alphabet_of(r["label"]),
                                         note_code("reveal", self.stream_id))
                self.store.append_record(REVEAL_KIND, {"stream": self.stream_id, "item_id": r["item_id"],
                                                       "date": r["date"], "watermark": wm.isoformat(),
                                                       "target": self.target, "at": self.clock()})
                before.add(r["item_id"])
            out.append({"item_id": r["item_id"], "label": r["label"], "date": r["date"]})
        return out

    def write_revealed(self, dest: Path) -> Path:
        """Write the visible labels to `dest` (a file inside an evolver mount); refuses a destination in the seal."""
        self._check_seal()
        dest = Path(dest).resolve()
        if _inside(dest, self.root):
            raise RevealRefused("revealed labels are written outside the seal")
        if not any(_inside(dest, m) for m in self.mounts):
            raise RevealRefused("revealed labels go to an evolver mount (name it in `mounts`)")
        rows = self.visible()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + f".tmp{os.getpid()}")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        os.replace(tmp, dest)
        return dest


__all__ = ["LabelItem", "RevealRefused", "TemporalReveal", "label_time", "watermark_time"]
