"""Campaign-scope cassette replay: the completeness check of the release log (GOAL §16.5.5).

Record a campaign once (real labels): every model call goes into a cassette and every release's value into a pin
log (`Releases(mode="record")`). Replay it in a FRESH store with FAKE labels (flipped / constant), the model calls
served from the cassette and every release pinned to its recorded value. If the ledgered releases are the only
target-derived information that reached an agent, the replay is call-for-call identical: every prompt hits the
cassette and every decision repeats. Any cassette miss, decision change, unpinned release or unused pin is a FLAG:
held-out information reached an agent outside the release log. Zero model calls.

Cassette format: one JSONL record per model call, `{"key", "request", "response"}`, key = sha256 of the canonical
request (messages, json_schema, temperature, max_tokens, model). This MIRRORS yield's cassette format (§16.8; not yet
posted when this was written); swap `request_key`/`load_cassette` for yield's once merged. Replayed responses carry
`replayed=True` in their provenance-side marker list (`ReplayBackend.served`); they are never evidence (inv. 21).

Limits (stated): complete only relative to the perturbed roots (the private label files are the only thing faked)
and to byte-deterministic prompts; it detects a flow only if the fake labels change the leaked value.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..gateway import Backend, BackendError, ChatRequest, ChatResponse
from ..records import AccessMode, Cost


def request_key(req: ChatRequest) -> str:
    body = {"messages": [m.model_dump() for m in req.messages], "json_schema": req.json_schema,
            "temperature": req.temperature, "max_tokens": req.max_tokens, "model": req.model}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RecordingBackend(Backend):
    def __init__(self, inner: Backend, path: Path):
        self.inner, self.path = inner, Path(path)
        self.name, self.provider, self.mode = inner.name, inner.provider, inner.mode
        self.training_on_outputs = inner.training_on_outputs
        self.family = getattr(inner, "family", None)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def complete(self, req: ChatRequest) -> ChatResponse:
        resp = self.inner.complete(req)
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps({"key": request_key(req), "request": req.model_dump(mode="json"),
                                "response": {"text": resp.text}}, sort_keys=True) + "\n")
        return resp


def load_cassette(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out.setdefault(rec["key"], []).append(rec["response"]["text"])
    return out


class ReplayBackend(Backend):
    """Serves recorded responses in order per request key. A miss is recorded and fails the call (never live)."""

    name, provider, mode = "replay", "replay", AccessMode.MOCK
    training_on_outputs = "allowed"

    def __init__(self, cassette: dict[str, list[str]], misses: list[str], family: str | None = None):
        self.cassette, self.misses, self.family = cassette, misses, family
        self.used: dict[str, int] = {}
        self.served = 0

    def complete(self, req: ChatRequest) -> ChatResponse:
        k = request_key(req)
        n = self.used.get(k, 0)
        texts = self.cassette.get(k, [])
        if n >= len(texts):
            self.misses.append(f"{k[:16]}#{n}: {req.messages[-1].content[:120]!r}")
            raise BackendError("cassette miss (replay never falls back to a live call)", infra=False)
        self.used[k] = n + 1
        self.served += 1
        return ChatResponse(text=texts[n], cost=Cost(estimated=True), provenance=self.provenance("replayed"))


def fake_labels(task_dir: Path, out_dir: Path, how: str = "flip", label_files: tuple[str, ...] = ()) -> Path:
    """Copy a task, replacing every truth CSV's label column (first column after the header): `flip` maps each
    label to the next distinct label value (cyclic), `constant` sets all labels to the least frequent value.

    The truth is read from `truth_layout.truth_files(task_dir)` (s4-integrity: a suite task's truth is NOT inside
    task_dir) and written to `truth_layout.truth_dir(out_dir)`; every other truth file (canary, records) is copied
    unchanged. Both refuse (TruthLayoutError) a truth location inside an agent-readable root: an `out_dir` whose
    truth dir would be agent-readable is refused, never written."""
    from ..truth_layout import truth_dir, truth_files
    sources = truth_files(task_dir)  # before anything is written: a refused layout leaves nothing behind
    dest = truth_dir(out_dir)
    shutil.copytree(task_dir, out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    for rel, src in sources.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not rel.endswith(".csv") or "/" in rel or (label_files and rel not in label_files):
            shutil.copyfile(src, target)
            continue
        rows = list(csv.reader(io.StringIO(src.read_text(encoding="utf-8"))))
        vals = [r[0] for r in rows[1:] if r]
        uniq = sorted(set(vals))
        if how == "flip":
            nxt = {v: uniq[(i + 1) % len(uniq)] for i, v in enumerate(uniq)} if len(uniq) > 1 else {}
            new = [nxt.get(v, v + "_x") for v in vals]
        else:
            rare = min(uniq, key=vals.count) if uniq else ""
            new = [rare for _ in vals]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(rows[0] if rows else ["label"])
        for v in new:
            w.writerow([v])
        target.write_text(buf.getvalue(), encoding="utf-8", newline="")
    return out_dir


@dataclass
class ReplayReport:
    cassette_misses: list[str] = field(default_factory=list)
    release_misses: list[str] = field(default_factory=list)
    unused_pins: list[str] = field(default_factory=list)
    decision_changes: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.cassette_misses or self.release_misses or self.unused_pins or self.decision_changes)


def compare_decisions(recorded: list[Any], replayed: list[Any]) -> list[str]:
    out = []
    for i in range(max(len(recorded), len(replayed))):
        a = recorded[i] if i < len(recorded) else None
        b = replayed[i] if i < len(replayed) else None
        if json.dumps(a, sort_keys=True, default=str) != json.dumps(b, sort_keys=True, default=str):
            out.append(f"decision {i}: recorded {a!r} replayed {b!r}"[:300])
    return out


def replay_report(recorded_decisions: list[Any], run: Callable[[], list[Any]], misses: list[str],
                  releases: Any) -> ReplayReport:
    """Run the replay (`run` returns its decision list) and collect every flag."""
    decisions = run()
    return ReplayReport(cassette_misses=list(misses), release_misses=list(releases.misses),
                        unused_pins=releases.unused(), decision_changes=compare_decisions(recorded_decisions,
                                                                                         decisions))
