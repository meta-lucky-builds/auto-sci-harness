"""L8 trajectory export for training (GOAL §5 L8; data side only, nothing is trained here).

`select_for_training` keeps a trajectory only if ALL hold, and records every reason it fails (closed `Reason`s):
- its outcome is in `config.SFT_OUTCOMES` (SFT format filtered by outcome; SciDER exports failed runs too);
- it is certified by BOTH declared, disjoint verifier sets A and B (`trajectory.certified_by`);
- every step that carries provenance, and every assistant step (which must carry it), is licence-permitted
  (`licence.LicencePolicy.permits`, invariant 11, fail closed);
- it has at least one assistant message (otherwise there is nothing to learn).
Limit: a user/system/tool step WITHOUT provenance is treated as harness-authored and not licence-checked; if a model
wrote it (e.g. a tool that calls a model), its caller must attach the provenance or it escapes the filter.

`export_trajectories` writes the kept trajectories as SFT rows, one JSON object per line in `sft.jsonl`, OpenAI-style
`messages` (the SciDER row shape), PLUS the tags SciDER lacks: outcome, trajectory digest, the A/B set digests and the
per-row sources (provider/mode/model/backend). No truncation of tool output. `manifest.json` records the pair digest,
the licence-policy digest, the outcome filter and the exclusion counts per reason. The export is written beside its
destination and renamed into place; an existing destination is refused (`DEST_EXISTS`).

A weight update itself stays a human decision (tiers.DEFAULT_POLICY[WEIGHT_UPDATE] is None). The sealed scores used by
the coupled-Goodhart indicator are not part of this export.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import config as C
from .licence import LicencePolicy
from .trajectory import DeclaredPair, ExportRefused, Trajectory, Verdict, certified_by

__all__ = ["ExportRefused", "ExportResult", "Reason", "Selection", "export_trajectories", "select_for_training",
           "sft_row"]


class Reason(StrEnum):
    OUTCOME = "OUTCOME"
    NOT_CERTIFIED_A = "NOT_CERTIFIED_A"
    NOT_CERTIFIED_B = "NOT_CERTIFIED_B"
    LICENCE = "LICENCE"
    NO_ASSISTANT = "NO_ASSISTANT"


@dataclass
class Selection:
    kept: list[Trajectory] = field(default_factory=list)
    excluded: dict[str, list[Reason]] = field(default_factory=dict)


def select_for_training(trajectories: Sequence[Trajectory], verdicts: Iterable[Verdict], pair: DeclaredPair,
                        licence: LicencePolicy, *, outcomes: frozenset[str] = C.SFT_OUTCOMES) -> Selection:
    if not isinstance(pair, DeclaredPair):
        raise ExportRefused("SET_INVALID", "verifier sets must be declared with declare_disjoint")
    DeclaredPair(pair.a, pair.b)  # re-checked at use: disjointness and the tier floor
    vs = list(verdicts)
    sel = Selection()
    seen: set[str] = set()
    for t in trajectories:
        if t.id in seen:
            raise ExportRefused("BAD_TRAJECTORY", f"trajectory id {t.id!r} appears twice")
        seen.add(t.id)
        reasons: list[Reason] = []
        if t.outcome not in outcomes:
            reasons.append(Reason.OUTCOME)
        if not certified_by(t, pair.a, vs):
            reasons.append(Reason.NOT_CERTIFIED_A)
        if not certified_by(t, pair.b, vs):
            reasons.append(Reason.NOT_CERTIFIED_B)
        if any((s.role == "assistant" or s.provenance is not None) and not licence.permits(s.provenance)
               for s in t.steps):
            reasons.append(Reason.LICENCE)
        if not any(s.role == "assistant" for s in t.steps):
            reasons.append(Reason.NO_ASSISTANT)
        if reasons:
            sel.excluded[t.id] = reasons
        else:
            sel.kept.append(t)
    return sel


def sft_row(t: Trajectory, pair: DeclaredPair) -> dict[str, Any]:
    sources: list[dict[str, str]] = []
    for s in t.steps:
        if s.provenance is not None:
            src = {"provider": s.provenance.provider, "mode": s.provenance.mode.value, "model": s.provenance.model,
                   "backend": s.provenance.backend}
            if src not in sources:
                sources.append(src)
    return {"id": t.id, "task_id": t.task_id, "outcome": t.outcome, "trajectory_digest": t.digest(),
            "messages": [{"role": s.role, "content": s.content} for s in t.steps],
            "verifier_sets": {"A": pair.a.digest(), "B": pair.b.digest()}, "sources": sources}


@dataclass
class ExportResult:
    rows: list[dict[str, Any]]
    excluded: dict[str, list[Reason]]
    manifest: dict[str, Any]
    path: Path


def export_trajectories(trajectories: Sequence[Trajectory], verdicts: Iterable[Verdict], pair: DeclaredPair,
                        licence: LicencePolicy, dest: Path | str, *,
                        outcomes: frozenset[str] = C.SFT_OUTCOMES) -> ExportResult:
    dest = Path(dest)
    if dest.exists():
        raise ExportRefused("DEST_EXISTS", f"{dest} exists; an export is never overwritten")
    sel = select_for_training(trajectories, verdicts, pair, licence, outcomes=outcomes)
    rows = [sft_row(t, pair) for t in sel.kept]
    counts = Counter(r.value for rs in sel.excluded.values() for r in rs)
    manifest = {"format": "chh-l8-sft/1", "pair": pair.digest(), "A": pair.a.digest(), "B": pair.b.digest(),
                "licence_policy": licence.digest(), "outcomes": sorted(outcomes),
                "tier_floor": C.EXPORT_TIER_FLOOR.name, "kept": len(rows), "excluded": dict(sorted(counts.items())),
                "excluded_ids": {k: [r.value for r in v] for k, v in sorted(sel.excluded.items())}}
    tmp = dest.parent / f".{dest.name}.tmp-{uuid.uuid4().hex[:10]}"
    tmp.mkdir(parents=True)
    try:
        with open(tmp / "sft.jsonl", "w", encoding="utf-8", newline="") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
        with open(tmp / "manifest.json", "w", encoding="utf-8", newline="") as f:
            json.dump(manifest, f, sort_keys=True, indent=1)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return ExportResult(rows, sel.excluded, manifest, dest)
