"""Contamination probes and prior-art weights (GOAL §6.4: contamination probes and a novelty-weighted subset are
mandatory).

`audit()` runs, per task:
  a. answer-leak: the normalised answer (casefold, whitespace collapsed) occurs in a frozen literature snapshot entry
     (raw body, or the string leaves of a JSON body) or in a frozen data file -> contaminated;
  b. resolved-before-T: a frozen data file (all dated <= T by G1) already determines the task under its own rule ->
     contaminated (the answer was knowable at T);
  c. memorisation: a MemorisationProbe (a model asked about the outcome before T). Only an offline fake ships here;
     a live probe needs the owner (model budget). When none is given the audit records that it did not run, and
     compare() then never reports success.
and computes the prior-art weight w = 1 / (1 + hits), hits = number of snapshot entries whose normalised text
contains any of the task's normalised key phrases. This w is §6.4's "novelty weight"; tasks with
w >= config.LOW_PRIOR_ART_THRESHOLD form §6.4's "novelty-weighted subset".
Flagged tasks are excluded from the headline score and listed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from ..literature.http import LiteratureError
from ..literature.snapshot import SnapshotStore
from ..store.blobs import BlobStore
from .benchmark import Benchmark, Task, TimesplitError, _norm
from .rules import RuleUndetermined, evaluate

ANSWER_LEAK = "answer-leak"
RESOLVED_BEFORE_T = "resolved-before-T"
MEMORISATION = "memorisation"


class ProbeFlag(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    task_id: str
    probe: str
    reason: str


class MemorisationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    flagged: bool
    reason: str


class MemorisationProbe(Protocol):
    name: str

    def probe(self, task: Task, as_of: date) -> MemorisationResult: ...


class FakeMemorisationProbe:
    """Offline stand-in for tests: flags exactly the tasks it is told the model already 'knows'."""

    name = "fake-offline"

    def __init__(self, known: Mapping[str, str] | None = None):
        self.known = dict(known or {})

    def probe(self, task: Task, as_of: date) -> MemorisationResult:
        if task.id in self.known:
            return MemorisationResult(flagged=True, reason=self.known[task.id])
        return MemorisationResult(flagged=False, reason="model did not state the outcome")


class Audit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    benchmark_id: str
    flags: tuple[ProbeFlag, ...]
    prior_art_hits: dict[str, int]
    prior_art_weight: dict[str, float]
    memorisation_probe: str | None  # None = not run (a live probe needs the owner)

    def contaminated(self) -> dict[str, str]:
        """task id -> first reason (sorted flags), for every flagged task."""
        out: dict[str, str] = {}
        for f in self.flags:
            out.setdefault(f.task_id, f"{f.probe}: {f.reason}")
        return out

    @property
    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def _leaves(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _leaves(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _leaves(v)]
    return []


def _snapshot_texts(benchmark: Benchmark, snapshots: SnapshotStore) -> list[tuple[str, str]]:
    """(request key, normalised searchable text) for every entry of the benchmark's literature snapshot."""
    m = snapshots.manifest(benchmark.literature_snapshot)
    if m.get("as_of") != benchmark.as_of.isoformat():
        raise TimesplitError(f"literature snapshot {benchmark.literature_snapshot[:12]} is as of {m.get('as_of')}, "
                             f"not T={benchmark.as_of}")
    out: list[tuple[str, str]] = []
    for key, digest in sorted(m["entries"].items()):
        try:
            text = snapshots.load(digest, key).text
        except LiteratureError as e:
            raise TimesplitError(f"literature snapshot entry {key[:12]} unreadable: {e}") from e
        parts = [text]
        try:
            parts.extend(_leaves(json.loads(text)))
        except ValueError:
            pass
        out.append((key, " \x00 ".join(_norm(p) for p in parts)))
    return out


def audit(benchmark: Benchmark, snapshots: SnapshotStore, blobs: BlobStore,
          memorisation: MemorisationProbe | None = None) -> Audit:
    lit = _snapshot_texts(benchmark, snapshots)
    data = [(name, e, blobs.get(e.sha256)) for name, e in sorted(benchmark.data.items())]
    data_text = [(name, _norm(raw.decode("utf-8", errors="replace"))) for name, _, raw in data]
    flags: list[ProbeFlag] = []
    hits: dict[str, int] = {}
    weight: dict[str, float] = {}
    for task in sorted(benchmark.tasks, key=lambda t: t.id):
        answer = _norm(task.answer)
        for key, text in lit:
            if answer in text:
                flags.append(ProbeFlag(task_id=task.id, probe=ANSWER_LEAK,
                                       reason=f"answer found in literature snapshot entry {key[:12]}"))
                break
        for name, text in data_text:
            if answer in text:
                flags.append(ProbeFlag(task_id=task.id, probe=ANSWER_LEAK,
                                       reason=f"answer found in frozen data file {name!r}"))
                break
        for name, entry, raw in data:
            try:
                outcome = evaluate(task.rule, raw)
            except RuleUndetermined:
                continue
            flags.append(ProbeFlag(task_id=task.id, probe=RESOLVED_BEFORE_T,
                                   reason=f"frozen data file {name!r} (dated {entry.dated}) already resolves it "
                                          f"(outcome={outcome})"))
            break
        if memorisation is not None:
            r = memorisation.probe(task, benchmark.as_of)
            if r.flagged:
                flags.append(ProbeFlag(task_id=task.id, probe=MEMORISATION, reason=f"{memorisation.name}: {r.reason}"))
        phrases = [_norm(p) for p in task.key_phrases]
        h = sum(1 for _, text in lit if any(p in text for p in phrases))
        hits[task.id] = h
        weight[task.id] = 1.0 / (1.0 + h)
    return Audit(benchmark_id=benchmark.id, flags=tuple(flags), prior_art_hits=hits, prior_art_weight=weight,
                 memorisation_probe=None if memorisation is None else memorisation.name)
