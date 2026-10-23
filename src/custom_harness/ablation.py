"""Ablation switches (GOAL §2.1 "appears in an ablation", invariant 12). One mechanism, one closed registry.

A capability claimed in the coverage contract (GOAL §9, `scorecard/capabilities.yaml`) is ablated by re-running the
system with that capability switched off (`scorecard.runner.run_ablation`). This module is that switch mechanism:

- `SWITCHES` is the CLOSED registry: one switch per capability row that has a real, tested switch. A name not in it
  is refused everywhere (`AblationSet.off` raises), so a typo can never silently ablate nothing.
- `AblationSet` is the typed set of switched-off capabilities that the code paths a scorecard `System.run` drives
  receive (`Engine(ablation=...)`, `ModuleContext.ablation`, `Evaluator(ablation=...)`,
  `evolve_generation(ablation=...)` ...). `NO_ABLATION` (empty) is the default everywhere.

Safety design (owner-delegated decision, 2026-10-08): an ablation is NEVER a production backdoor.
- A non-empty AblationSet can only be made by `scorecard.runner.ablation_set` (the scorecard runner's ablation entry
  point): the constructor refuses any other caller module. There is no environment variable, config default or CLI
  flag that makes one (tests/test_ablation_core.py checks the source tree for each).
- An ablated run is marked ABLATED: its run record (`Run.ablated`), every trace step (`TraceStep.ablated`, stamped by
  the store) and its run bundle manifest (`ABLATED`).
- Nothing an ablated run produces is trusted: the Kernel refuses to certify on evidence produced by an ablated run;
  the run-bound Kernel a module gets refuses pre-registration, evidence and certification; the memory store refuses
  every write (memory and skills) from an ablated run; evolution never accepts an edit while ablated or on an
  ablated run, and an ablated generation's records go to separate `<kind>:ABLATED` record kinds; the scorecard lists
  ablated runs apart and never counts them on the production axes. All of these fail closed.
  Not enforced (stated): L8 weights export takes trajectories as inputs and is not wired to runs yet, so it has no
  run marker to check.

Integrity mechanisms (Kernel checks, sandbox isolation, the leak/canary audit) have NO switch here: a production path
that skips a Kernel check is never added. Their contribution is measured inside the evaluation harness only (the
planted-exploit museum compares a check against a simulated check-off arm built in the test layer); their rows in
capabilities.yaml are `not_ablatable` with that reason, and the scorecard keeps them as invariant-12 gaps.

Honest limits: Python cannot make a constructor truly private. The caller-module check stops accidental or casual
construction and keeps every ablation entry point greppable (one function); trusted in-process code could forge it,
as it could forge any store row (runtime/modules.py says the same of modules).

This module is a leaf: it imports nothing from the rest of the package (the memory package and the Kernel may use
the run marker without loading runtime, research or evolve code).
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# the ONLY module allowed to construct a non-empty AblationSet (the scorecard runner's ablation entry point)
ENTRY_POINT_MODULE = "custom_harness.scorecard.runner"
# the duplicate (novelty) gate's switch name, named once here so the acceptance code (evolve/loop.py) refers to it
# without carrying the word (emergence.check_novelty_isolation scans acceptance paths; this registry is not one)
DUPLICATE_GATE_SWITCH = "novelty-gate"
MARKER = "ABLATED"


class AblationError(ValueError):
    """An unknown switch, or an AblationSet made outside the scorecard runner's entry point."""


class AblationRefused(RuntimeError):
    """Something an ablated run produced was offered where only production output is accepted (fail closed)."""


@dataclass(frozen=True)
class Switch:
    name: str
    capability: str  # the capabilities.yaml row id it turns off
    turns_off: str  # exactly what is off when the switch is set (the code path)
    test: str  # the test that shows behaviour changes when off and is unchanged when on ("path::function")


def _registry(*switches: Switch) -> Mapping[str, Switch]:
    out: dict[str, Switch] = {}
    for s in switches:
        if s.name in out or any(x.capability == s.capability for x in out.values()):
            raise AblationError(f"duplicate switch or capability in the registry: {s.name} / {s.capability}")
        out[s.name] = s
    return MappingProxyType(out)


# The closed registry, by layer. Add a switch ONLY together with its code path and its test (docs/ablation.md).
_CORE: tuple[Switch, ...] = (
)

_T_EVOLVE = "tests/test_ablation_switches_evolve.py::"
_EVOLVE: tuple[Switch, ...] = (  # L6 / L7 (evolve/, control/)
    Switch(name="proposer-history", capability="meta-harness",
           turns_off="evolve/loop.py evolve_generation: the proposer gets no history. The PAST EDITS ledger "
                     "(proposer_view of every harness_edit record) is empty, and a ProposerAgent does not run its "
                     "agentic session over the evidence-export filesystem (propose_from_export): its backend is "
                     "called once, one-shot (propose.propose), with the current diagnosis only; the diagnosis' "
                     "first-divergence references are the parent's own cached runs only (no earlier candidate's "
                     "runs)",
           test=_T_EVOLVE + "test_proposer_history_switch"),
    Switch(name="contract-hop-edits", capability="rhi",
           turns_off="evolve/loop.py evolve_generation: edits of type `contract` or `hop` (edits.EditType) are "
                     "dropped before any screen, check or run (lineage status `ablated_edit_type`, edit withheld). "
                     "The proposer is still offered these types (evolve/propose.py schema and prompt): their edit "
                     "slots are lost, not redirected",
           test=_T_EVOLVE + "test_contract_hop_edits_switch"),
    Switch(name="cost-term", capability="rrsi",
           turns_off="evolve/loop.py evolve_generation: the acceptance cost term (selection.cost_bound) no longer "
                     "rejects; it is still computed and recorded under cost.ablated.measured_passed",
           test=_T_EVOLVE + "test_cost_term_switch"),
    Switch(name="causal-signatures", capability="self-harness",
           turns_off="evolve/loop.py evolve_generation: the diagnosis carries no first-divergence causal "
                     "signatures (digest divergences / no_reference emptied and unlinked set to 0 right after "
                     "propose.diagnose; the leak screen still runs), so the "
                     "proposer sees none and cannot cite them (claims.check_claims)",
           test=_T_EVOLVE + "test_causal_signatures_switch"),
    Switch(name="minibatch-prefilter", capability="autosaddler",
           turns_off="evolve/loop.py evolve_generation: no smoke run pre-filter (L6.7, loop._smoke_failure is not "
                     "run) and no successive-halving early elimination in the race (race_rounds forced to 1: every "
                     "raced candidate is evaluated on every evolve unit before the best is picked, under the batch and "
                     "per_completion allocators; the voi allocator's knowledge-gradient stopping is unchanged)",
           test=_T_EVOLVE + "test_minibatch_prefilter_switch"),
    Switch(name="orchestrator-reallocation", capability="milo",
           turns_off="control/islands.py Orchestrator.step: a stalled island on the harness_edit lever keeps its "
                     "budget weight (no stall_shrink) and its mutator mix apart from the prompt-only quota cap (no "
                     "diversification); decisions are logged under `orchestrator_decision:ABLATED`. Reachability: "
                     "evolve/search.py evolve_islands (`chh evolve --islands`, opt-in) builds the Orchestrator; an "
                     "ablation arm reaches the switch through evolve_islands(ablation=...)",
           test=_T_EVOLVE + "test_orchestrator_reallocation_switch"),
    Switch(name="novelty-gate", capability="harnessx",
           turns_off="evolve/loop.py evolve_generation: the novelty gate (rejected_signatures) is empty, so an edit "
                     "identical to a previously rejected one is evaluated instead of `rejected_novelty`",
           test=_T_EVOLVE + "test_novelty_gate_switch"),
    Switch(name="observation-pack", capability="sol-pi",
           turns_off="runtime/engine.py -> runtime/template.render: LLM prompts get no archive of over-long "
                     "values (no blob handle) and no verified-quote log reducer (`|log` is plain head/tail "
                     "truncation); tool commands (which never archive) get no `|log` reducer. The `_private` "
                     "refusal and prompt-release accounting are unchanged",
           test=_T_EVOLVE + "test_observation_pack_switch"),
    Switch(name="lever-choice", capability="sia",
           turns_off="control/islands.py Orchestrator.step: the `more_evaluation` lever is never chosen; a stalled "
                     "island always takes the harness_edit lever (no extra seed). Reachability: evolve/search.py "
                     "evolve_islands (`chh evolve --islands`, opt-in) builds the Orchestrator; an ablation arm "
                     "reaches the switch through evolve_islands(ablation=...)",
           test=_T_EVOLVE + "test_lever_choice_switch"),
    Switch(name="voi-allocation", capability="mda",
           turns_off="evolve/loop.py evolve_generation: with race_allocator='voi', the race runs as the batch race "
                     "(selection.race) instead of control/voi.voi_race. A no-op under the default "
                     "race_allocator='batch': an ablation of this row must run with race_allocator='voi' "
                     "(opt-in: `chh evolve --race-allocator voi`)",
           test=_T_EVOLVE + "test_voi_allocation_switch"),
    Switch(name="per-completion-race", capability="openresearch-alphaxiv",
           turns_off="evolve/loop.py evolve_generation: with race_allocator='per_completion', the race runs as the "
                     "sequential batch race (selection.race, race_slots=1) instead of the completion-driven "
                     "allocator (control/evalrace.race_evaluator). A no-op under the default race_allocator='batch': "
                     "an ablation of this row must run with race_allocator='per_completion' "
                     "(opt-in: `chh evolve --race-allocator per_completion`)",
           test=_T_EVOLVE + "test_per_completion_race_switch"),
)

_T = "tests/test_ablation_switches_research.py::"
_RESEARCH: tuple[Switch, ...] = (  # L3 / L4 / L5 and the rest (research/, memory wiring, verify/, literature/ ...)
    Switch(name="cross_task_memory", capability="evoscientist",
           turns_off="research/memory_wiring.py recall_memory: the memory store is not opened; memory.context is the "
                     "empty marker, so no cross-task finding, verified failure or dead-end reaches the prompt",
           test=_T + "test_cross_task_memory_switch_renders_no_memory"),
    Switch(name="skills_paged_recall", capability="evomaster",
           turns_off="research/memory_wiring.py recall_memory withholds the skill kinds (playbook, method) from the "
                     "query; research/recall.py recall refuses every page request (code 'ablated'; research.recall is "
                     "in no shipped spec, so that half is reached only by a spec with a research.recall node)",
           test=_T + "test_skills_paged_recall_switch_withholds_skills_and_refuses_pages"),
    Switch(name="playbook_dead_ends", capability="auto-recsys",
           turns_off="research/memory_wiring.py recall_memory withholds playbook and dead-end entries from the query",
           test=_T + "test_playbook_dead_ends_switch_withholds_playbooks_and_dead_ends"),
    Switch(name="method_library", capability="mm-agent",
           turns_off="research/memory_wiring.py recall_memory withholds method-library entries from the query",
           test=_T + "test_method_library_switch_withholds_methods"),
    Switch(name="data_profile_kway_rank", capability="scider",
           turns_off="research/modules.py profile_data computes no profile (an 'ablated' marker instead); "
                     "research/tournament.py tournament_module (in specs with a research.tournament node, e.g. "
                     "examples/research_v1.yaml, or research_v2 with the opt-in research.admit_rank "
                     "params.tournament; as shipped research_v2 ranks by execution) makes no judge call and keeps "
                     "the proposer's order",
           test=_T + "test_data_profile_kway_rank_switch_skips_profile_and_ranking"),
    Switch(name="btl_tournament", capability="robin",
           turns_off="research/tournament.py tournament_module: no judge call, no Bradley-Terry fit; the queue keeps "
                     "the proposer's order (in specs with a research.tournament node, e.g. examples/research_v1.yaml, "
                     "or under "
                     "research_v2.yaml only with the opt-in research.admit_rank params.tournament; as shipped, "
                     "research_v2 ranks by execution and this switch changes nothing there)",
           test=_T + "test_btl_tournament_switch_keeps_the_proposer_order"),
    Switch(name="experiment_tree_repair_improve", capability="ai-scientist-v1-v2",
           turns_off="research/modules.py run_candidate never routes to `fix` (a failed node is abandoned, no "
                     "repair) and next_hypothesis opens no improve round: each hypothesis is one draft run",
           test=_T + "test_experiment_tree_switch_removes_repairs_and_improve_rounds"),
    Switch(name="multi_source_retrieval", capability="feynman",
           turns_off="literature/client.py LiteratureClient.search asks only the first source of its order (no "
                     "fallback across sources); lookups are unchanged. Reached from `chh lit-search` (production, all "
                     "sources) and, for an ablation arm, by a scorecard System that builds live_client / "
                     "offline_client(ablation=...); no harness run path calls search",
           test=_T + "test_multi_source_retrieval_switch_disables_source_fallback"),
    Switch(name="reward_variance_selection", capability="aworld-rl",
           turns_off="weights/selection.py select_by_reward_variance ignores the variance: every task in task-id "
                     "order, none excluded. Archive only (owner decision 2026-10-14: no model training, L8 out of "
                     "scope): kept as the L8 export archive's task selection, reachable from no run by design; the "
                     "row stays an invariant-12 gap",
           test=_T + "test_reward_variance_selection_switch_selects_uniformly"),
)

# The Station (multi-agent research mode, s4-station). Its own group: tests/test_ablation_switches_research.py pins
# _RESEARCH to the switches whose tests live in that file; this one's test lives with the station tests
# (tests/test_station_mode.py pins _STATION the same way). int18 stage-c: s4-station had added it to _RESEARCH.
_STATION: tuple[Switch, ...] = (
    Switch(name="station_coordination", capability="the-station",
           turns_off="research/station/mode.py StationMode: no agent is shown the shared archive (its archive context "
                     "is the switched-off marker), nothing is published to it (every certificate of a turn is "
                     "recorded as code 'ablated') and no stagnation lane or stall shrink is applied: N independent "
                     "research loops on the same schedule, seeds and budgets. Library level: no scorecard System "
                     "drives a station mode yet, and the CLI never passes an ablation",
           test="tests/test_station_mode.py::test_station_coordination_switch_runs_independent_agents"),
)

SWITCHES: Mapping[str, Switch] = _registry(*_CORE, *_EVOLVE, *_RESEARCH, *_STATION)


class AblationSet:
    """An immutable set of switched-off capabilities. Empty = production (`NO_ABLATION`)."""

    __slots__ = ("_off",)
    _off: frozenset[str]

    def __init__(self, off: Iterable[str] = ()) -> None:
        names = frozenset(str(x) for x in off)
        unknown = sorted(names - set(SWITCHES))
        if unknown:
            raise AblationError(f"unknown ablation switch(es) {unknown}: the registry is closed "
                                f"(custom_harness.ablation.SWITCHES)")
        if names:
            caller = sys._getframe(1).f_globals.get("__name__")
            if caller != ENTRY_POINT_MODULE:
                raise AblationError(f"an ablation can only be switched on through the scorecard runner's entry point "
                                    f"({ENTRY_POINT_MODULE}.ablation_set), not from {caller!r}")
        object.__setattr__(self, "_off", names)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("AblationSet is immutable")

    def off(self, switch: str) -> bool:
        """True when `switch` is switched off in this run. An unknown name raises (never a silent no-op)."""
        if switch not in SWITCHES:
            raise AblationError(f"unknown ablation switch {switch!r}")
        return switch in self._off

    @property
    def active(self) -> bool:
        return bool(self._off)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._off))

    def marker(self) -> dict[str, Any]:
        """What an ablated run's records carry (empty for production)."""
        return {MARKER: True, "disabled": list(self.names)} if self._off else {}

    def __iter__(self) -> Iterator[str]:
        return iter(self.names)

    def __len__(self) -> int:
        return len(self._off)

    def __bool__(self) -> bool:
        return bool(self._off)

    def __contains__(self, item: object) -> bool:
        return item in self._off

    def __eq__(self, other: object) -> bool:
        return isinstance(other, AblationSet) and other._off == self._off

    def __hash__(self) -> int:
        return hash(self._off)

    def __repr__(self) -> str:
        return f"AblationSet({list(self.names)})"


NO_ABLATION = AblationSet()


def run_ablated(store: Any, run_id: str | None) -> tuple[str, ...]:
    """The switches an existing run was ablated with; () for a production run or a run id with no record (a run is
    only ever ablated through `Engine(ablation=...)`, which writes the marker into the run record itself)."""
    if not run_id:
        return ()
    try:
        run = store.get_run(run_id)
    except KeyError:
        return ()
    return tuple(getattr(run, "ablated", ()) or ())


def refuse_if_ablated(store: Any, run_id: str | None, what: str) -> None:
    """Raise AblationRefused when `run_id` is an ablated run (fail closed)."""
    off = run_ablated(store, run_id)
    if off:
        raise AblationRefused(f"{what} refused: run {run_id} is {MARKER} (switched off: {', '.join(off)}); nothing an "
                              f"ablated run produces is persisted, certified or accepted")


__all__ = ["DUPLICATE_GATE_SWITCH", "ENTRY_POINT_MODULE", "MARKER", "NO_ABLATION", "SWITCHES", "AblationError",
           "AblationRefused", "AblationSet", "Switch", "refuse_if_ablated", "run_ablated"]
