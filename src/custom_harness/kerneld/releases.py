"""Host side of the release ledger and the verdict embargo (GOAL §16.5.3, invariant 18).

Every transfer of target-derived information to an agent-visible surface (a prompt, the evidence export, memory,
the evolution score, a report an agent reads) goes through `Releases.emit`, which

1. records one typed release per held-out target it derives from, through `Kernel.record_release(target, channel,
   alphabet, note)` (kstats; K4 turns these into the transcript bound, and a `continuous` release makes the target
   adaptively exposed), and
2. in a recorded campaign, logs the released VALUE under a deterministic key, so that a replay of the campaign with
   fake labels can PIN every release to its recorded value (§16.5.5, `campaign.py`): if the released values are the
   only target-derived information that reached an agent, the replay is call-for-call identical.

Until kstats' `Kernel.record_release` is merged, `LedgerSink` falls back to an append-only store record of kind
`release_unledgered` with the same body (NOT hash-chained, NOT counted by K4). This is stated in the report and the
fallback disappears by itself once the Kernel method exists (it is looked up at call time).

What "target-derived" means here is decided by the caller (the engine's taint, the evolution score, the export):
those rules live next to each emission site. A flow that no emission site sees is exactly what campaign replay
detects.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ..store import EvidenceStore

# mirror of kstats' closed enums (custom_harness/kernel/releases.py: Channel, Alphabet); the Kernel re-checks them
CHANNELS = ("prompt", "export", "memory", "evolution_score", "report")
ALPHABETS = ("verdict_bit", "count", "continuous")
NOTE = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}\Z")  # a code, never a value (§16.5.8)
FALLBACK_KIND = "release_unledgered"

# certificate labels: the finite verdict alphabet (kernel/certificate.py Label values)
VERDICT_LABELS = frozenset({"CERTIFIED-VALIDATED", "CERTIFIED-INTERNAL", "CERTIFIED-EXACT", "CERTIFIED-SIMULATED",
                            "CERTIFIED-FORMAL", "SUPPORTED", "REJECTED"})


class ReleaseError(ValueError):
    pass


def note_code(*parts: object) -> str:
    """A valid note code from parts (lower-cased, invalid characters replaced, at most 80 characters)."""
    s = re.sub(r"[^a-z0-9_.:-]", "_", ":".join(str(p) for p in parts).lower())
    if not s or not s[0].isalpha():
        s = "x" + s
    return s[:80]


def check_release(channel: str, alphabet: str, note: str) -> None:
    if channel not in CHANNELS:
        raise ReleaseError(f"unknown release channel {channel!r}")
    if alphabet not in ALPHABETS:
        raise ReleaseError(f"unknown release alphabet {alphabet!r}")
    if not NOTE.match(note):
        raise ReleaseError(f"release note {note!r} is not a code ({NOTE.pattern})")


def alphabet_of(value: Any) -> str:
    """The alphabet a released value needs: verdict bits and labels are `verdict_bit`, small integers `count`,
    everything else (floats, text, structures) `continuous` (fail closed: unknown = the widest alphabet)."""
    if isinstance(value, bool) or value is None or (isinstance(value, str) and value in VERDICT_LABELS):
        return "verdict_bit"
    if isinstance(value, int):
        return "count"
    return "continuous"


class Sink(Protocol):
    def record_release(self, target_dataset_id: str, channel: str, alphabet: str, note: str) -> Any: ...


class LedgerSink:
    """Records releases through the Kernel (`Kernel.record_release`, kstats), or, while that is not merged, as
    `release_unledgered` store records after checking the target is a registered dataset (fail closed)."""

    def __init__(self, store: EvidenceStore):
        self.store = store

    def record_release(self, target_dataset_id: str, channel: str, alphabet: str, note: str) -> Any:
        from ..kernel import Kernel
        kernel = Kernel(self.store)
        fn = getattr(kernel, "record_release", None)
        if fn is not None:  # pragma: no cover - exercised once kstats' Kernel.record_release is merged
            return fn(target_dataset_id, channel, alphabet, note)
        check_release(channel, alphabet, note)
        if target_dataset_id not in kernel.vault.datasets():
            raise ReleaseError(f"release on unregistered target {target_dataset_id!r}")
        body = {"dataset_id": target_dataset_id, "channel": channel, "alphabet": alphabet, "note": note}
        self.store.append_record(FALLBACK_KIND, body)
        return body


def released(store: EvidenceStore, target_dataset_id: str | None = None) -> list[dict[str, Any]]:
    """Release bodies on a target (all targets if None): ledger `release` entries, then fallback records."""
    out = [json.loads(r[2]) for r in store.ledger_rows("release")]
    out += store.records(FALLBACK_KIND)
    return [b for b in out if target_dataset_id is None or b.get("dataset_id") == target_dataset_id]


def private_reads(store: EvidenceStore, *, after_seq: int = 0, run_id: str | None = None) -> list[str]:
    """Datasets whose PRIVATE files were read (any purpose) in ledger entries after `after_seq` (optionally only
    those naming `run_id`), sorted and de-duplicated. Kernel re-check reads carry no run id, so the engine uses the
    seq window of a step instead (conservative: a concurrent read elsewhere over-taints, never under-taints)."""
    out = set()
    for seq, _kind, body, *_ in store.ledger_rows("data_access"):
        if seq <= after_seq:
            continue
        b = json.loads(body)
        if b.get("private") and (run_id is None or b.get("run_id") == run_id):
            out.add(str(b["dataset_id"]))
    return sorted(out)


def attributed_reads(store: EvidenceStore, *, after_seq: int = 0, run_ids: Collection[str],
                     since: float | None = None) -> tuple[list[str], int]:
    """Datasets whose PRIVATE files were read in `data_access` ledger entries after `after_seq` that may have fed a
    run in `run_ids`, and the last seq examined. Per run, not per time window (a concurrent run's read, logged under
    its own run id, does not taint this run), but fail closed: a read naming NO run (Kernel re-check reads carry
    none), naming a run the store does not know, or with an unreadable body counts (an unreadable body counts as a
    read of every registered dataset). `since`: only entries logged at or after this time (seconds)."""
    out: set[str] = set()
    last = after_seq
    for seq, _at, datasets in attributed_read_log(store, after_seq=after_seq, run_ids=run_ids, since=since):
        last = max(last, seq)
        out |= datasets
    rows = store.ledger_rows("data_access")
    if rows:
        last = max(last, int(rows[-1][0]))
    return sorted(out), last


def run_targets(store: EvidenceStore, run_id: str) -> list[str]:
    """Every target attributed to a finished run (`attributed_reads` since the run started): what a release of
    anything computed from the whole run (its verdict, its export) is a release on. Replaces `private_reads(run_id=)`,
    which missed reads naming no run (Kernel re-check reads)."""
    try:
        since: float | None = store.get_run(run_id).started_at - 1.0
    except KeyError:
        since = None
    return attributed_reads(store, after_seq=0, run_ids={run_id}, since=since)[0]


def attributed_read_log(store: EvidenceStore, *, after_seq: int = 0, run_ids: Collection[str],
                        since: float | None = None) -> list[tuple[int, float, set[str]]]:
    """(seq, time, datasets) of every private read `attributed_reads` counts, in ledger order."""
    out: list[tuple[int, float, set[str]]] = []
    known: dict[str, bool] = {}
    ids = set(run_ids)
    everything: set[str] | None = None
    for seq, _kind, body, _prev, _hash, at in store.ledger_rows("data_access"):
        if seq <= after_seq or (since is not None and float(at) < since):
            continue
        try:
            b = json.loads(body)
            private, rid, ds = bool(b.get("private")), b.get("run_id"), str(b["dataset_id"])
        except (ValueError, TypeError, KeyError, AttributeError):
            if everything is None:
                from ..kernel import Kernel
                everything = set(Kernel(store).vault.datasets())
            out.append((int(seq), float(at), set(everything)))
            continue
        if not private:
            continue
        if isinstance(rid, str) and rid and rid not in ids:
            if rid not in known:
                try:
                    store.get_run(rid)
                    known[rid] = True
                except KeyError:
                    known[rid] = False
            if known[rid]:
                continue  # another, known run's own read
        out.append((int(seq), float(at), {ds}))
    return out


Mode = Literal["live", "record", "replay"]


@dataclass
class Releases:
    """Emission facade. `live`: record releases only. `record`: also log each released value under its key.
    `replay`: return the PINNED recorded value instead of the computed one; a key with no recorded value left is a
    miss (`misses`), and recorded values never asked for are `unused()` at the end; both are replay flags."""

    sink: Sink | None
    mode: Mode = "live"
    pins: dict[str, list[Any]] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    emitted: int = 0
    _used: Counter[str] = field(default_factory=Counter)

    @classmethod
    def live(cls, store: EvidenceStore) -> Releases:
        return cls(LedgerSink(store))

    def emit(self, value: Any, *, targets: Iterable[str], channel: str, alphabet: str, note: str, key: str) -> Any:
        check_release(channel, alphabet, note)
        k = f"{key}|{note}"
        n = self._used[k]
        self._used[k] += 1
        if self.mode == "replay":
            pinned = self.pins.get(k, [])
            if n < len(pinned):
                value = pinned[n]
            else:
                self.misses.append(f"{k}#{n}")
        elif self.mode == "record":
            self.log.append({"key": k, "n": n, "value": _jsonable(value), "channel": channel, "alphabet": alphabet})
        if self.sink is not None:
            for t in sorted(set(targets)):
                self.sink.record_release(t, channel, alphabet, note)
                self.emitted += 1
        return value

    def unused(self) -> list[str]:
        return sorted(f"{k}#{i}" for k, vals in self.pins.items() for i in range(self._used.get(k, 0), len(vals)))

    # ---------------------------------------------------------------- pin log on disk (host-only, never exported)
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            for rec in self.log:
                f.write(json.dumps(rec, sort_keys=True) + "\n")

    @classmethod
    def replaying(cls, path: Path, sink: Sink | None) -> Releases:
        pins: dict[str, list[Any]] = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                pins.setdefault(rec["key"], []).append(rec["value"])
        return cls(sink, mode="replay", pins=pins)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))
