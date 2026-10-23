"""L4 multi-agent research mode ("station", GOAL §9 The Station row; docs/multi_agent_mode.md).

Several research-loop agents work one problem or a family of problems in parallel. They coordinate ONLY through a
shared, append-only archive (`archive.StationArchive`, an L5 memory-store namespace). A deterministic orchestrator
(`mode.StationMode`) schedules their turns, harvests each turn's Kernel certificates from the ledger window of that
turn, publishes them through the L5 gate, renders other agents' entries to each agent as quoted data with ledgered
releases, detects stalled agents with the one stall rule (`evolve.generations.stall_evidence`) and shrinks their
budget only through the downgrade-only gate (`control.downgrade.DowngradeGate`).

Modules:
- `archive`  publication rules, taint, cross-agent rendering with releases, revocation
- `mode`     config, deterministic schedule, budgets, stagnation lanes, loop labels, the hash-chained mode log
- `runner`   the agent-turn protocol and the Engine-backed runner (research spec patched for the station)
- `cli`      `chh research multi ...`
"""

from .archive import (
    ARCHIVE_KINDS,
    PubCode,
    Publication,
    RenderResult,
    StationArchive,
    TurnWindow,
)
from .mode import (
    AgentSpec,
    BudgetSpec,
    ModeConfig,
    ProblemSpec,
    StationError,
    StationInfraError,
    StationMode,
)
from .runner import AgentRunner, EngineRunner, TurnOutcome, TurnRequest, station_spec

__all__ = ["ARCHIVE_KINDS", "AgentRunner", "AgentSpec", "BudgetSpec", "EngineRunner", "ModeConfig", "ProblemSpec",
           "PubCode", "Publication", "RenderResult", "StationArchive", "StationError", "StationInfraError",
           "StationMode", "TurnOutcome", "TurnRequest", "TurnWindow", "station_spec"]
