"""Scorecard framework (GOAL §2, §11 M8): pinned benchmarks, re-run baselines, ablations, axes F/G/H from the store,
and a scorecard bundle reproducible with one command (`chh scorecard check`). See docs/scorecard.md."""

from .bundle import build_scorecard, check_scorecard
from .registry import (
    Capability,
    PinnedBenchmark,
    Registry,
    ScorecardError,
    content_hash,
    load_capabilities,
    load_registry,
)
from .runner import paired_compare, record_as_reported, run_ablation, run_system

__all__ = ["Capability", "PinnedBenchmark", "Registry", "ScorecardError", "build_scorecard", "check_scorecard",
           "content_hash", "load_capabilities", "load_registry", "paired_compare", "record_as_reported",
           "run_ablation", "run_system"]
