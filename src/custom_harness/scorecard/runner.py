"""Baseline re-run runner, ablation runner and paired comparison (GOAL §2.3, invariant 12).

Every comparison on the scorecard is between RE-RUNS recorded in the store: our system and each baseline are run by
`run_system` on the same pinned benchmark bytes (the pin is checked first), and each run is appended as a
`scorecard_run` record with its per-item scores in a blob. A number a baseline's authors reported, without a re-run,
is recorded separately by `record_as_reported` as a `scorecard_as_reported` record; the scorecard shows it under
"as reported", and uses it for a verdict only when no re-run of that baseline exists, labelled as such (GOAL §2.3).

`run_ablation` re-runs our system with one capability switched off (`disabled`) on the same items: invariant 12's
ablation. `ablation_set` is THE entry point that switches capabilities off (custom_harness/ablation.py): the only
place a non-empty AblationSet can be made. The system receives it as `disabled` and threads it into the code it
drives (`Engine(ablation=...)`, `Evaluator(ablation=...)`, ...); every run made under it is marked ABLATED, and
nothing an ablated run produces is certified, persisted or accepted.

Comparisons are paired over the identical item set (refused otherwise), with a seeded paired-bootstrap percentile
interval of the mean difference, so a verdict is reproducible byte for byte.
"""

from __future__ import annotations

import json
import math
import random
import time
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from ..ablation import NO_ABLATION, SWITCHES, AblationSet
from ..store.evidence import EvidenceStore
from . import config as C
from .registry import PinnedBenchmark, Registry, ScorecardError

RUN_KIND = "scorecard_run"
AS_REPORTED_KIND = "scorecard_as_reported"
Role = Literal["ours", "baseline", "ablation"]


class System(Protocol):
    """A system under test. `run` returns one finite score per item id (e.g. 1.0 pass / 0.0 fail). `disabled` is the
    AblationSet to thread into the code it drives (NO_ABLATION for a production or baseline run)."""

    name: str
    version: str  # a content hash or pinned version of the system (code + config)

    def run(self, items: list[dict[str, Any]], disabled: AblationSet) -> Mapping[str, float]: ...


def ablation_set(switches: frozenset[str] | set[str] | tuple[str, ...]) -> AblationSet:
    """The scorecard runner's ablation entry point: the ONLY way to switch capabilities off. Every name must be in
    the closed registry (custom_harness.ablation.SWITCHES)."""
    unknown = sorted(set(switches) - set(SWITCHES))
    if unknown:
        raise ScorecardError(f"unknown ablation switch(es) {unknown}: not in the closed registry")
    return AblationSet(switches)


def load_items(registry: Registry, bench: PinnedBenchmark) -> list[dict[str, Any]]:
    d = registry.verify(bench)
    path = d / "items.jsonl"
    if not path.is_file():
        raise ScorecardError(f"{bench.key}: no items.jsonl in {d}")
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    ids = [str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id") is not None]
    if len(ids) != len(items) or len(set(ids)) != len(ids):
        raise ScorecardError(f"{bench.key}: every item needs a unique 'id'")
    return items


def run_system(store: EvidenceStore, registry: Registry, bench: PinnedBenchmark, system: System, role: Role,
               disabled: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Run `system` on the pinned benchmark and append the result as a scorecard_run record (returned)."""
    if role == "ablation" and not disabled:
        raise ScorecardError("an ablation run must disable at least one capability")
    if role != "ablation" and disabled:
        raise ScorecardError("only an ablation run may disable capabilities")
    items = load_items(registry, bench)
    raw = dict(system.run(items, ablation_set(frozenset(disabled)) if disabled else NO_ABLATION))
    want = {str(i["id"]) for i in items}
    if set(raw) != want:
        raise ScorecardError(f"{system.name} on {bench.key}: scores for {len(set(raw) & want)}/{len(want)} items, "
                             f"{len(set(raw) - want)} unknown ids (every item must be scored exactly once)")
    scores = {k: float(v) for k, v in sorted(raw.items())}
    if not all(math.isfinite(v) for v in scores.values()):
        raise ScorecardError(f"{system.name} on {bench.key}: non-finite score")
    body = {"benchmark": bench.key, "content_hash": bench.content_hash, "axis": bench.axis, "metric": bench.metric,
            "higher_is_better": bench.higher_is_better, "system": system.name, "system_version": system.version,
            "role": role, "disabled": sorted(disabled), "results_blob": store.blobs.put_json(scores),
            "n": len(scores), "mean": sum(scores.values()) / len(scores), "at": time.time()}
    store.append_record(RUN_KIND, body)
    return body


def record_as_reported(store: EvidenceStore, bench: PinnedBenchmark, system: str, value: float, source: str,
                       note: str = "") -> dict[str, Any]:
    """A baseline number taken from its authors' report (NOT re-run). `source` must say where it was reported."""
    if not source.strip():
        raise ScorecardError("an as-reported number needs its source (paper, table, page)")
    if not math.isfinite(value):
        raise ScorecardError("as-reported value must be finite")
    body = {"benchmark": bench.key, "content_hash": bench.content_hash, "system": system, "value": float(value),
            "source": source, "note": note, "label": "as reported (not re-run)", "at": time.time()}
    store.append_record(AS_REPORTED_KIND, body)
    return body


def run_ablation(store: EvidenceStore, registry: Registry, bench: PinnedBenchmark, system: System,
                 switch: str) -> dict[str, Any]:
    return run_system(store, registry, bench, system, "ablation", frozenset({switch}))


def paired_compare(a: Mapping[str, float], b: Mapping[str, float], higher_is_better: bool = True) -> dict[str, Any]:
    """Mean of (a - b) over the identical item set, oriented so that positive = a is better, with a seeded paired
    bootstrap percentile interval. verdict: 'win' (interval above 0), 'lose' (below 0), else 'inconclusive'."""
    if set(a) != set(b):
        raise ScorecardError("a paired comparison needs the identical item set on both sides")
    if not a:
        raise ScorecardError("a comparison needs at least one item")
    keys = sorted(a)
    sign = 1.0 if higher_is_better else -1.0
    d = [sign * (float(a[k]) - float(b[k])) for k in keys]
    n = len(d)
    rng = random.Random(C.BOOT_SEED)
    means = sorted(sum(d[rng.randrange(n)] for _ in range(n)) / n for _ in range(C.N_BOOT))
    tail = (1 - C.CONFIDENCE) / 2
    lo = means[max(0, math.floor(tail * C.N_BOOT))]
    hi = means[min(C.N_BOOT - 1, math.ceil((1 - tail) * C.N_BOOT) - 1)]
    verdict = "win" if lo > 0 else "lose" if hi < 0 else "inconclusive"
    return {"diff": sum(d) / n, "ci": [lo, hi], "n": n, "confidence": C.CONFIDENCE, "n_boot": C.N_BOOT,
            "seed": C.BOOT_SEED, "verdict": verdict}
