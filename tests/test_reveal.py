"""L6.11 temporal-reveal mode for streams (GOAL §5): labels sealed outside every evolver mount, a watermark rule,
undated = unrevealed, every reveal ledgered. No model calls."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from custom_harness.evolve.reveal import REVEAL_KIND, LabelItem, RevealRefused, TemporalReveal, label_time
from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.kerneld.releases import released

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC).timestamp()
FUTURE_LABEL = "label-from-2026-09-30-FUTURE"


def _stream(store, tmp_path, **kw):
    Kernel(store).vault.register_dataset("stream-target", DatasetRole.TEST, public={"x.csv": b"x\n1\n"},
                                         private={"y.csv": b"y\n1\n"})
    mounts = [tmp_path / "export", tmp_path / "sandbox"]
    for m in mounts:
        m.mkdir(parents=True, exist_ok=True)
    r = TemporalReveal(store, "s1", "stream-target", tmp_path / "host" / "sealed", mounts=mounts,
                       clock=lambda: NOW, **kw)
    r.seal([LabelItem("old", 1, "2026-09-01"),
            LabelItem("edge-day", 2, "2026-09-15"),  # date-only: counts as the END of that day
            LabelItem("tz", 3, "2026-09-15T10:00:00+02:00"),  # 08:00 UTC
            LabelItem("late", FUTURE_LABEL, "2026-09-30"),
            LabelItem("undated", 5, None),
            LabelItem("naive", 6, "2026-09-01T10:00:00"),  # no timezone: undated
            LabelItem("garbage", 7, "last tuesday")])
    return r


def _ids(rows):
    return sorted(r["item_id"] for r in rows)


def test_nothing_is_visible_before_a_watermark_and_after_it_only_dated_labels_at_or_before_it(store, tmp_path):
    r = _stream(store, tmp_path)
    assert r.visible() == []  # no watermark: nothing
    r.advance("2026-09-15T09:00:00+00:00")
    assert _ids(r.visible()) == ["old", "tz"]  # edge-day ends at 23:59:59 UTC, after the watermark
    r.advance("2026-09-16")
    assert _ids(r.visible()) == ["edge-day", "old", "tz"]
    r.advance("2026-10-08")  # the latest watermark the clock allows
    vis = r.visible()
    assert _ids(vis) == ["edge-day", "late", "old", "tz"]
    assert not {"undated", "naive", "garbage"} & set(_ids(vis))  # undated is never visible, at any watermark


def test_a_label_dated_after_the_watermark_is_never_visible_or_written(store, tmp_path):
    r = _stream(store, tmp_path)
    r.advance("2026-09-29T23:59:59+00:00")
    dest = r.write_revealed(tmp_path / "export" / "labels.jsonl")
    assert "late" not in _ids(r.visible())
    text = dest.read_text(encoding="utf-8")
    assert '"old"' in text and FUTURE_LABEL not in text  # positive control + the future label is absent
    for m in r.mounts:  # nothing under any mount holds it
        for f in m.rglob("*"):
            if f.is_file():
                assert FUTURE_LABEL not in f.read_text(encoding="utf-8", errors="replace")
    with pytest.raises(RevealRefused, match="future"):
        r.advance("2026-10-09")  # the watermark cannot pass the clock
    with pytest.raises(RevealRefused, match="forward"):
        r.advance("2026-09-01")
    assert "late" not in _ids(r.visible())


def test_undated_labels_are_never_visible_even_at_the_latest_watermark(store, tmp_path):
    r = _stream(store, tmp_path)
    r.advance("2026-10-08T12:00:00+00:00")
    assert not {"undated", "naive", "garbage"} & set(_ids(r.visible()))
    assert label_time(None) is None and label_time("2026-09-01T10:00:00") is None and label_time("") is None


def test_each_reveal_is_ledgered_once_per_item(store, tmp_path):
    r = _stream(store, tmp_path)
    r.advance("2026-09-15T09:00:00+00:00")
    r.visible()
    r.visible()  # the same items again: no new reveal
    rel = [b for b in released(store, "stream-target")]
    assert len(rel) == 2 and {(b["channel"], b["alphabet"], b["note"]) for b in rel} == {("export", "count",
                                                                                          "reveal:s1")}
    recs = store.records(REVEAL_KIND)
    assert sorted(x["item_id"] for x in recs) == ["old", "tz"] and all(x["watermark"].startswith("2026-09-15T09")
                                                                      for x in recs)
    r.advance("2026-10-08")
    r.visible()
    assert len(released(store, "stream-target")) == 4  # + edge-day, late (the continuous text label: alphabet)
    assert {b["alphabet"] for b in released(store, "stream-target")} == {"count", "continuous"}


def test_a_reveal_that_cannot_be_ledgered_returns_nothing(store, tmp_path):
    class Broken:
        def record_release(self, *a):
            raise RuntimeError("ledger down")

    r = _stream(store, tmp_path, sink=Broken())
    r.advance("2026-09-15T09:00:00+00:00")
    with pytest.raises(RuntimeError, match="ledger down"):
        r.visible()
    assert store.records(REVEAL_KIND) == []


def test_the_seal_must_lie_outside_every_evolver_mount(store, tmp_path):
    mount = tmp_path / "export"
    for root in (mount / "sealed", mount, tmp_path):  # inside a mount, equal to it, or containing it
        with pytest.raises(RevealRefused, match="overlap an evolver mount"):
            TemporalReveal(store, "s", "t", root, mounts=[mount])
    with pytest.raises(RevealRefused, match="mounts must be named"):
        TemporalReveal(store, "s", "t", tmp_path / "host", mounts=[])
    r = _stream(store, tmp_path)
    with pytest.raises(RevealRefused, match="outside the seal"):
        r.write_revealed(r.root / "copy.jsonl")
    with pytest.raises(RevealRefused, match="evolver mount"):
        r.write_revealed(tmp_path / "elsewhere" / "x.jsonl")
    with pytest.raises(RevealRefused, match="already sealed"):
        r.seal([LabelItem("late", 9, "2026-01-01")])  # re-dating a sealed item would reveal it early
