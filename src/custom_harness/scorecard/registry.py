"""Pinned-benchmark registry and claimed-capability registry (GOAL §2, §11 M8, invariant 12).

A pinned benchmark is (name, version, axis, licence, content hash, item directory). Its content hash is the SHA-256 of
the sorted list of (relative path, SHA-256 of bytes) of every file in its directory, so any change to any item, or an
added or removed file, changes the pin. Nothing is run on a benchmark whose bytes do not match its pin (fail closed).
The shipped registry (`benchmarks.yaml`) is EMPTY: every real benchmark (Terminal-Bench 2.x, SWE-bench Verified, GAIA,
MLE-bench, ResearchBench, ...) needs a download, which is an owner decision. Tests use fixture registries.

The capability registry (`capabilities.yaml`) lists every capability the coverage contract (GOAL §9) claims, each with
its `ablation` record (`AblationRecord`): either `switch` = the name of a switch in the closed registry
(custom_harness.ablation.SWITCHES), with `not_covered` listing any part of the row the switch does not turn off and
why; or `not_ablatable` = the exact reason no switch exists. A row counts as having a switch ONLY when its switch is in
the closed registry for that row and `not_covered` is empty (`switch_fact`). A capability without such a switch, or
with no recorded ablation run, is reported as an invariant-12 gap on the scorecard; it is never left out.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ..ablation import SWITCHES

HERE = Path(__file__).resolve().parent
DEFAULT_BENCHMARKS = HERE / "benchmarks.yaml"
DEFAULT_CAPABILITIES = HERE / "capabilities.yaml"
AXES = ("A", "B", "C", "D", "E", "F", "G", "H")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")


class ScorecardError(ValueError):
    pass


class PinnedBenchmark(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    version: str
    axis: Literal["A", "B", "C", "D", "E", "F", "G", "H"]
    licence: str
    content_hash: str
    path: str  # item directory, relative to the registry file
    metric: str = "pass_rate"
    higher_is_better: bool = True

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


class AblationRecord(BaseModel):
    """Exactly one of: `switch` (a name in the closed registry; `not_covered` = row parts it does not turn off, each
    with its reason) or `not_ablatable` (the exact reason no switch exists)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    switch: str | None = None
    not_covered: dict[str, str] = {}
    not_ablatable: str | None = None

    @model_validator(mode="after")
    def _one(self) -> AblationRecord:
        if (self.switch is None) == (self.not_ablatable is None):
            raise ValueError("an ablation record names exactly one of `switch` or `not_ablatable`")
        if self.not_ablatable is not None and (not self.not_ablatable.strip() or self.not_covered):
            raise ValueError("`not_ablatable` needs a non-empty reason and no `not_covered`")
        if any(not str(v).strip() for v in self.not_covered.values()):
            raise ValueError("every `not_covered` part needs its reason")
        return self


class Capability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    source: str  # the surveyed system it comes from (GOAL §9)
    capability: str
    home: str
    # None = no record yet (an invariant-12 gap); a bare string is read as {switch: <string>}
    ablation: AblationRecord | None = None

    @field_validator("ablation", mode="before")
    @classmethod
    def _bare_switch(cls, v: Any) -> Any:
        return {"switch": v} if isinstance(v, str) else v


def switch_fact(cap_id: str, ablation: Any) -> dict[str, Any]:
    """What the scorecard needs about one row's ablation, as plain JSON. `has_switch` is True ONLY when the row names
    a switch that is in the closed registry FOR THIS ROW, with a test, and nothing of the row is left uncovered.
    A malformed record is reported as such (never as a switch)."""
    try:
        rec = Capability.model_validate({"id": cap_id, "source": "", "capability": "", "home": "",
                                         "ablation": ablation}).ablation
    except ValueError as e:
        return {"has_switch": False, "switch": None, "why": f"malformed ablation record: {str(e).splitlines()[0]}"}
    if rec is None:
        return {"has_switch": False, "switch": None, "why": "no ablation switch"}
    if rec.not_ablatable is not None:
        return {"has_switch": False, "switch": None, "why": f"not ablatable: {rec.not_ablatable}"}
    sw = SWITCHES.get(str(rec.switch))
    if sw is None or sw.capability != cap_id:
        return {"has_switch": False, "switch": rec.switch,
                "why": f"switch {rec.switch!r} is not in the closed registry for this capability"}
    if not sw.test.strip():
        return {"has_switch": False, "switch": rec.switch, "why": f"switch {rec.switch!r} names no test"}
    if rec.not_covered:
        return {"has_switch": False, "switch": rec.switch, "test": sw.test, "not_covered": dict(rec.not_covered),
                "why": f"partial switch: {rec.switch!r} does not turn off {sorted(rec.not_covered)}"}
    return {"has_switch": True, "switch": rec.switch, "test": sw.test}


def content_hash(directory: Path) -> str:
    d = Path(directory)
    if not d.is_dir():
        raise ScorecardError(f"benchmark directory {d} does not exist")
    files = sorted(p for p in d.rglob("*") if p.is_file())
    if not files:
        raise ScorecardError(f"benchmark directory {d} is empty")
    listing = [[p.relative_to(d).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()] for p in files]
    return hashlib.sha256(repr(listing).encode()).hexdigest()


class Registry:
    def __init__(self, path: Path, benchmarks: tuple[PinnedBenchmark, ...]):
        self.path, self.benchmarks = Path(path), benchmarks

    def get(self, key: str) -> PinnedBenchmark:
        for b in self.benchmarks:
            if b.key == key:
                return b
        raise ScorecardError(f"benchmark {key!r} is not in the registry {self.path}")

    def directory(self, bench: PinnedBenchmark) -> Path:
        return (self.path.parent / bench.path).resolve()

    def verify(self, bench: PinnedBenchmark) -> Path:
        """The benchmark's directory, after checking its bytes against the pin (refused on any difference)."""
        d = self.directory(bench)
        got = content_hash(d)
        if got != bench.content_hash:
            raise ScorecardError(f"{bench.key}: content hash {got[:16]} does not match the pin "
                                 f"{bench.content_hash[:16]} (the benchmark changed; re-pin it as a new version)")
        return d


def _load_yaml(path: Path, key: str) -> list[Any]:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ScorecardError(f"cannot read {path}: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get(key), list):
        raise ScorecardError(f"{path}: expected a mapping with a list '{key}'")
    return doc[key]


def load_registry(path: Path = DEFAULT_BENCHMARKS) -> Registry:
    rows = _load_yaml(path, "benchmarks")
    out: list[PinnedBenchmark] = []
    for row in rows:
        try:
            b = PinnedBenchmark.model_validate(row)
        except ValueError as e:
            raise ScorecardError(f"{path}: invalid benchmark entry {row!r}: {e}") from e
        if not b.licence.strip():
            raise ScorecardError(f"{path}: {b.key} has no licence (a benchmark is pinned only with its licence)")
        if not _HEX64.match(b.content_hash):
            raise ScorecardError(f"{path}: {b.key} content_hash is not a SHA-256 hex digest")
        if any(x.key == b.key for x in out):
            raise ScorecardError(f"{path}: duplicate benchmark {b.key}")
        out.append(b)
    return Registry(Path(path), tuple(out))


def load_capabilities(path: Path = DEFAULT_CAPABILITIES) -> tuple[Capability, ...]:
    rows = _load_yaml(path, "capabilities")
    out: list[Capability] = []
    for row in rows:
        try:
            c = Capability.model_validate(row)
        except ValueError as e:
            raise ScorecardError(f"{path}: invalid capability entry {row!r}: {e}") from e
        if any(x.id == c.id for x in out):
            raise ScorecardError(f"{path}: duplicate capability {c.id}")
        out.append(c)
    return tuple(out)
