"""Ablation switches of the EVOLVE / CONTROL layer (custom_harness/ablation.py `_EVOLVE`, invariant 12).

Each test shows, on the same scripted world, that the capability's behaviour CHANGES when its switch is off, and is
UNCHANGED (equal to NO_ABLATION) when a different switch is off: so the switch is not a no-op and does not leak into
other code paths. Scores come from a fixed table (no harness run, no model call); the proposer is a MockBackend.
Every ablated generation is a measurement arm: it never accepts, its released bit is `ablated_would_accept`.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from custom_harness.ablation import _EVOLVE, MARKER, NO_ABLATION, SWITCHES, AblationSet
from custom_harness.control import voi as voi_mod
from custom_harness.control.islands import DECISION_KIND, Orchestrator
from custom_harness.evolve import Evaluator, EvolveConfig, RunOutcome, SuiteTask, evolve_generation
from custom_harness.evolve import loop as loop_mod
from custom_harness.evolve.edits import Edit, EditOp, EditType
from custom_harness.evolve.loop import edit_signature
from custom_harness.evolve.proposer_agent import AgentProposal, ProposerAgent
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.records import Cost
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.runtime.spec import LLMNode
from custom_harness.scorecard.registry import load_capabilities, switch_fact
from custom_harness.scorecard.runner import ablation_set
from custom_harness.store import EvidenceStore
from test_evolve import PROPOSALS, seed_spec
from test_islands import POLICY, _islands, _script

EDIT0 = PROPOSALS["edits"][0]  # max_fixes 0 -> 2: a valid, honest control-flow edit
CFG = {"seeds": (0, 1), "n_edits": 1, "race_rounds": 1}
OTHER = "novelty-gate"  # an unrelated switch: the control arm (ablated, but not this capability)


def _proposal(*edits: dict) -> str:
    return json.dumps({"edits": list(edits)})


class _Table(Evaluator):
    """Candidate +1 (slightly varied) on every unit, parent 0. Gate cost of the candidate and the status of its
    smoke unit are set per world. No harness is run."""

    def __init__(self, store, tasks, parent_hash, *, ablation: AblationSet = NO_ABLATION, cand_gate_cost=100,
                 cand_smoke_status="completed"):
        super().__init__({}, store, lambda k: None, tasks, ablation=ablation)
        self.parent_hash, self.cand_gate_cost, self.cand_smoke_status = parent_hash, cand_gate_cost, \
            cand_smoke_status

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            self.calls += 1
            cand = spec.spec_hash() != self.parent_hash
            gate = self.tasks[unit[0]].split == "gate"
            score = 1.0 + 0.001 * (int(unit[0][1:]) + 3 * (unit[1] % 1000)) if cand else 0.0
            cost = Cost(prompt_tokens=self.cand_gate_cost if (cand and gate) else 100)
            status = self.cand_smoke_status if (cand and unit == ("e0", 0)) else "completed"
            self.cache[key] = RunOutcome(unit[0], "", unit[1], score, False, status, cost, [])
        return self.cache[key]


def _tasks(tmp_path: Path) -> list[SuiteTask]:
    """Family A: 3 evolve + 4 gate tasks; family H (held out): 2 gate tasks."""
    tasks = [SuiteTask(f"e{i}", tmp_path, "evolve", family="A") for i in range(3)]
    tasks += [SuiteTask(f"g{i}", tmp_path, "gate", family="A") for i in range(4)]
    return tasks + [SuiteTask(f"h{i}", tmp_path, "gate", family="H") for i in range(2)]


def _store(tmp_path: Path, name: str) -> EvidenceStore:
    s = EvidenceStore(tmp_path / name)
    Kernel(s).approve_adapter("ml", approver="pytest")
    return s


def _gen(tmp_path, name, *, off=(), proposer=None, cfg=None, seed_records=(), cached=(), **world):
    """One generation in a fresh store under `off` (empty = production). `cached`: (cache key, outcome) pairs the
    evaluator already holds (runs of earlier generations). Returns (result, evaluator, proposer, store records
    helper)."""
    abl = ablation_set(set(off)) if off else NO_ABLATION
    st = _store(tmp_path, name)
    for kind, body in seed_records:
        st.append_record(kind, body)
    tasks = _tasks(tmp_path)
    parent = seed_spec()
    ev = _Table(st, tasks, parent.spec_hash(), ablation=abl, **world)
    for key, o in cached:
        ev.cache[key] = o
    proposer = proposer if proposer is not None else MockBackend([_proposal(EDIT0)])
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=proposer, store=st,
                            cfg=cfg or EvolveConfig(**CFG), ablation=abl)
    out = {"res": res, "ev": ev, "proposer": proposer, "lineage": st.records("lineage" + (f":{MARKER}" if abl else ""))}
    st.close()
    return out


def _final(res) -> dict:
    """The generation's gated record (accepted / rejected_gate / ablated_not_accepted)."""
    gated = [c for c in res.candidates if c["status"] in ("accepted", "rejected_gate", "ablated_not_accepted")]
    assert len(gated) == 1, [c["status"] for c in res.candidates]
    return gated[0]


def _would_accept(res) -> bool:
    """Production: the accept bit. Ablated arm: the released bit it kept as the measurement."""
    rec = _final(res)
    return bool(rec["ablated_would_accept"]) if rec.get(MARKER) else res.accepted


def _prompt(backend: MockBackend) -> str:
    return "\n".join(m.content for m in backend.requests[0].messages)


# ===================================================================== registry: every switch has its row and test
def test_evolve_switches_are_registered_with_their_row_and_test():
    caps = {c.id: c for c in load_capabilities()}
    here = {n for n, f in globals().items() if n.startswith("test_") and inspect.isfunction(f)}
    assert _EVOLVE, "no evolve switch registered"
    for sw in _EVOLVE:
        assert SWITCHES[sw.name] is sw and sw.capability in caps
        path, fn = sw.test.split("::")
        assert path == "tests/test_ablation_switches_evolve.py" and fn in here, sw.test
        rec = caps[sw.capability].ablation
        assert rec is not None and rec.switch == sw.name, sw.capability
        fact = switch_fact(sw.capability, rec.model_dump(exclude_defaults=True))
        # a row is a full switch only with nothing left uncovered; a partial one stays an invariant-12 gap
        assert fact["has_switch"] == (not rec.not_covered), fact


def test_switch_names_are_closed():
    with pytest.raises(ValueError, match="unknown"):
        ablation_set({"proposer-histroy"})  # a typo never ablates nothing silently
    with pytest.raises(ValueError, match="entry point"):
        AblationSet({"proposer-history"})  # only the scorecard runner's entry point makes one


# ===================================================================== meta-harness: proposer-history
def test_proposer_history_switch(tmp_path, monkeypatch):
    """Off: the proposer sees no PAST EDITS ledger, and a ProposerAgent runs no export session (one-shot call)."""
    past = [("harness_edit", {"parent": "p" * 64, "type": "prompt_text", "rationale": "HISTORY-MARKER-7f3",
                              "accepted": False, "decision": "evolve: no gain"})]
    on = _gen(tmp_path, "on", seed_records=past)
    off = _gen(tmp_path, "off", off={"proposer-history"}, seed_records=past)
    ctl = _gen(tmp_path, "ctl", off={OTHER}, seed_records=past)
    assert "HISTORY-MARKER-7f3" in _prompt(on["proposer"]) and "HISTORY-MARKER-7f3" in _prompt(ctl["proposer"])
    assert "HISTORY-MARKER-7f3" not in _prompt(off["proposer"])
    assert _prompt(on["proposer"]) == _prompt(ctl["proposer"])  # unchanged when this switch is not set

    # the diagnosis' first-divergence references: production compares against every cached evolve run, earlier
    # candidates' runs included; with the switch off only the parent's own runs are used (no history through them)
    earlier = (("c" * 64, "e0", 0), RunOutcome("e0", "EARLIER-CANDIDATE-RUN", 0, 0.5, False, "completed", Cost(), []))
    refs: dict[str, list[str]] = {}
    real_diagnose = loop_mod.diagnose

    def spy_diagnose(parent_outcomes, store, *, references, evolve_ids):
        refs[current] = [o.run_id for o in references]
        return real_diagnose(parent_outcomes, store, evolve_ids=evolve_ids,
                             references=[o for o in references if o.run_id != "EARLIER-CANDIDATE-RUN"])
    monkeypatch.setattr(loop_mod, "diagnose", spy_diagnose)
    for current, off_set in (("on", ()), ("off", {"proposer-history"}), ("ctl", {OTHER})):
        _gen(tmp_path, f"refs-{current}", off=off_set, cached=[earlier])
    assert "EARLIER-CANDIDATE-RUN" in refs["on"] and refs["ctl"] == refs["on"]
    assert "EARLIER-CANDIDATE-RUN" not in refs["off"] and len(refs["off"]) == len(refs["on"]) - 1
    monkeypatch.undo()

    sessions: list[str] = []

    def spy(self, spec, digest, ledger, **kw):  # stands in for the export session (no sandbox, no model)
        sessions.append(spec.spec_hash())
        return AgentProposal([], [], None, "spy: no edit", [], frozenset())
    monkeypatch.setattr(ProposerAgent, "propose_from_export", spy)

    def agent():
        return ProposerAgent(MockBackend([_proposal(EDIT0)]), tmp_path / "exports", lambda k: None)
    a_on = _gen(tmp_path, "a-on", proposer=agent())
    assert sessions and a_on["res"].decision.startswith("proposer declined: spy")
    assert a_on["proposer"].backend.requests == []  # production: the agentic session, not a one-shot call
    n = len(sessions)
    a_off = _gen(tmp_path, "a-off", off={"proposer-history"}, proposer=agent())
    assert len(sessions) == n  # no export session at all
    req = a_off["proposer"].backend.requests
    assert len(req) == 1 and "EVIDENCE EXPORT" not in _prompt(a_off["proposer"].backend)
    assert _final(a_off["res"])["status"] == "ablated_not_accepted" and _would_accept(a_off["res"])
    a_ctl = _gen(tmp_path, "a-ctl", off={OTHER}, proposer=agent())
    assert len(sessions) == n + 1 and a_ctl["proposer"].backend.requests == []


# ===================================================================== rhi: contract-hop-edits
@pytest.mark.parametrize("etype", ["contract", "hop"])
def test_contract_hop_edits_switch(tmp_path, etype):
    """Off: a contract/hop edit is dropped before any check or run; other edit types are untouched."""
    edit = {**EDIT0, "type": etype}
    on = _gen(tmp_path, "on", proposer=MockBackend([_proposal(edit)]))
    assert on["res"].accepted and _final(on["res"])["edit"]["type"] == etype
    off = _gen(tmp_path, "off", off={"contract-hop-edits"}, proposer=MockBackend([_proposal(edit)]))
    assert [c["status"] for c in off["res"].candidates] == ["ablated_edit_type"]
    assert off["res"].decision == "no valid candidates"
    assert off["ev"].calls == 6  # the parent's evolve units only: the edit was never run
    assert off["lineage"][0]["edit"]["withheld"] is True  # unscreened text is never stored
    ctl = _gen(tmp_path, "ctl", off={OTHER}, proposer=MockBackend([_proposal(edit)]))
    assert _would_accept(ctl["res"]) and ctl["ev"].calls == on["ev"].calls
    # another edit type is not affected by the switch
    other = _gen(tmp_path, "other", off={"contract-hop-edits"})
    assert _final(other["res"])["edit"]["type"] == "control_flow" and _would_accept(other["res"])


# ===================================================================== rrsi: cost-term
def test_cost_term_switch(tmp_path):
    """A candidate 10x the parent's cost on the gate: rejected on cost in production; with the cost term off it
    would have been accepted (the cost is still measured and recorded)."""
    on = _gen(tmp_path, "on", cand_gate_cost=1000)
    assert not on["res"].accepted and _final(on["res"])["rejected_on"] == "cost"
    off = _gen(tmp_path, "off", off={"cost-term"}, cand_gate_cost=1000)
    rec = _final(off["res"])
    assert rec["ablated_would_accept"] is True and rec["cost"]["passed"] is True
    assert rec["cost"]["ablated"] == {"switch": "cost-term", "measured_passed": False}
    assert rec["cost"]["candidate_total"] == _final(on["res"])["cost"]["candidate_total"]
    ctl = _gen(tmp_path, "ctl", off={OTHER}, cand_gate_cost=1000)
    assert _would_accept(ctl["res"]) is False and _final(ctl["res"])["cost"] == _final(on["res"])["cost"]
    cheap = _gen(tmp_path, "cheap", off={"cost-term"})  # a cheap candidate: same verdict either way
    assert _would_accept(cheap["res"]) is True


# ===================================================================== self-harness: causal-signatures
DIGEST = {"n_runs": 6, "n_excluded_infra": 0, "n_excluded_split": 0, "mean_score": 0.0, "verified_rate": 0.0,
          "divergences": [{"id": "D0", "task": "e0", "step": 3, "cause": "route:fix->END", "criticality": "fatal",
                           "mechanism": "route", "evidence": []}],
          "no_reference": [], "unlinked": 0, "clusters": []}


def test_causal_signatures_switch(tmp_path, monkeypatch):
    """Off: the diagnosis shows no first-divergence signature, so an edit citing one is unsupported."""
    monkeypatch.setattr(loop_mod, "diagnose", lambda *a, **k: json.loads(json.dumps(DIGEST)))
    cites = {**EDIT0, "evidence": ["D0"]}

    def run(name, off=()):
        return _gen(tmp_path, name, off=off, proposer=MockBackend([_proposal(cites)]))
    on, off, ctl = run("on"), run("off", {"causal-signatures"}), run("ctl", {OTHER})
    assert '"id": "D0"' in _prompt(on["proposer"]) and on["res"].accepted
    assert _prompt(ctl["proposer"]) == _prompt(on["proposer"]) and _would_accept(ctl["res"])
    assert '"id": "D0"' not in _prompt(off["proposer"]) and '"divergences": []' in _prompt(off["proposer"])
    rec = off["res"].candidates[0]
    assert rec["status"] == "rejected_claims" and "resolve to nothing" in rec["reason"]


# ===================================================================== autosaddler: minibatch-prefilter
def test_minibatch_prefilter_switch(tmp_path):
    """Off: (1) no smoke run: a candidate whose smoke unit breaks is raced instead of rejected_smoke; (2) no
    successive-halving elimination: every raced candidate is evaluated on every evolve unit."""
    on = _gen(tmp_path, "on", cand_smoke_status="failed")
    assert [c["status"] for c in on["res"].candidates] == ["rejected_smoke"]
    off = _gen(tmp_path, "off", off={"minibatch-prefilter"}, cand_smoke_status="failed")
    assert "rejected_smoke" not in [c["status"] for c in off["res"].candidates]
    assert _final(off["res"])["status"] == "ablated_not_accepted"
    ctl = _gen(tmp_path, "ctl", off={OTHER}, cand_smoke_status="failed")
    assert [c["status"] for c in ctl["res"].candidates] == ["rejected_smoke"]

    two = MockBackend([_proposal(EDIT0, PROPOSALS["edits"][1])])
    cfg = EvolveConfig(seeds=(0, 1), n_edits=2, race_rounds=2)

    def raced(r):
        return [c for c in r["res"].candidates if c["status"] == "raced"]
    h_on = _gen(tmp_path, "h-on", proposer=two, cfg=cfg)
    assert len(raced(h_on)) == 2 and any(c["race"]["eliminated_round"] == 0 for c in raced(h_on))
    h_off = _gen(tmp_path, "h-off", off={"minibatch-prefilter"},
                 proposer=MockBackend([_proposal(EDIT0, PROPOSALS["edits"][1])]), cfg=cfg)
    assert len(raced(h_off)) == 2
    assert all(c["race"]["eliminated_round"] is None and c["race"]["n_units"] == 6 for c in raced(h_off))
    h_ctl = _gen(tmp_path, "h-ctl", off={OTHER}, proposer=MockBackend([_proposal(EDIT0, PROPOSALS["edits"][1])]),
                 cfg=cfg)
    assert [c["race"] for c in raced(h_ctl)] == [c["race"] for c in raced(h_on)]


# ===================================================================== harnessx: novelty-gate
def test_novelty_gate_switch(tmp_path):
    """Off: an edit identical to a previously REJECTED one is evaluated instead of refused unevaluated."""
    sig = edit_signature(Edit(type=EditType.CONTROL_FLOW, rationale="x",
                              ops=[EditOp(op="replace", path="/nodes/run/params/max_fixes", value=2)]))
    past = [("lineage", {"parent": "p" * 64, "status": "rejected_gate", "signature": sig})]
    on = _gen(tmp_path, "on", seed_records=past)
    assert [c["status"] for c in on["res"].candidates] == ["rejected_novelty"] and on["ev"].calls == 6
    off = _gen(tmp_path, "off", off={"novelty-gate"}, seed_records=past)
    assert _final(off["res"])["signature"] == sig and _would_accept(off["res"]) and off["ev"].calls > 6
    ctl = _gen(tmp_path, "ctl", off={"cost-term"}, seed_records=past)
    assert [c["status"] for c in ctl["res"].candidates] == ["rejected_novelty"] and ctl["ev"].calls == 6


# ===================================================================== mda: voi-allocation
def test_voi_allocation_switch(tmp_path, monkeypatch):
    """Off: with race_allocator='voi' the race runs as the batch race (control/voi.voi_race is not called)."""
    calls: list[int] = []
    real = voi_mod.voi_race

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(voi_mod, "voi_race", spy)
    cfg = EvolveConfig(seeds=(0, 1), n_edits=2, race_rounds=2, race_allocator="voi")

    def run(name, off=()):
        return _gen(tmp_path, name, off=off, proposer=MockBackend([_proposal(EDIT0, PROPOSALS["edits"][1])]),
                    cfg=cfg)
    on = run("on")
    assert len(calls) == 1
    off = run("off", {"voi-allocation"})
    assert len(calls) == 1  # the VOI allocator did not run
    raced_off = [c["race"] for c in off["res"].candidates if c["status"] == "raced"]
    assert len(raced_off) == 2 and any(r["eliminated_round"] == 0 for r in raced_off)  # the batch race's halving
    ctl = run("ctl", {OTHER})
    assert len(calls) == 2
    assert [c["race"] for c in ctl["res"].candidates if c["status"] == "raced"] == \
        [c["race"] for c in on["res"].candidates if c["status"] == "raced"]


# ===================================================================== openresearch-alphaxiv: per-completion-race
def test_per_completion_race_switch(tmp_path, monkeypatch):
    """Off: with race_allocator='per_completion' the race runs as the sequential batch race (the completion-driven
    allocator control/evalrace.race_evaluator is not called)."""
    from custom_harness.control import evalrace
    from custom_harness.evolve.selection import race
    calls: list[int] = []

    def spy(candidates, units, evaluator, parent_scores, *, slots, rounds, keep=1, timeout_s=3600.0):
        # stands in for the threaded allocator (the scripted table evaluator has no begin/finish): records the call
        # and decides with the same race over the same scores
        calls.append(slots)
        return race(list(candidates), units, lambda h, u: evaluator.score(candidates[h], u), parent_scores,
                    rounds=rounds, keep=keep), None, None
    monkeypatch.setattr(evalrace, "race_evaluator", spy)
    cfg = EvolveConfig(seeds=(0, 1), n_edits=2, race_rounds=2, race_allocator="per_completion", race_slots=2)

    def run(name, off=()):
        return _gen(tmp_path, name, off=off, proposer=MockBackend([_proposal(EDIT0, PROPOSALS["edits"][1])]),
                    cfg=cfg)

    def raced(r):
        return [c["race"] for c in r["res"].candidates if c["status"] == "raced"]
    on = run("on")
    assert calls == [2]  # production: the per-completion allocator with its 2 slots
    off = run("off", {"per-completion-race"})
    assert calls == [2]  # the allocator did not run
    assert len(raced(off)) == 2 and raced(off) == raced(on)  # the batch race decides the same way on these scores
    ctl = run("ctl", {OTHER})
    assert calls == [2, 2] and raced(ctl) == raced(on)
    batch = _gen(tmp_path, "batch", off={"per-completion-race"},
                 proposer=MockBackend([_proposal(EDIT0, PROPOSALS["edits"][1])]),
                 cfg=EvolveConfig(seeds=(0, 1), n_edits=2, race_rounds=2))
    assert calls == [2, 2] and raced(batch) == raced(on)  # under the default batch allocator: a no-op


# ===================================================================== milo / sia: the L7 orchestrator
def _orch(tmp_path, name, off=()):
    st = _store(tmp_path, name)
    orch = Orchestrator(_islands(), POLICY, total_budget=30, store=st,
                        ablation=ablation_set(set(off)) if off else NO_ABLATION)
    _script(orch)
    d = {x["island"]: x for x in orch.step()}
    kinds = (len(st.records(DECISION_KIND)), len(st.records(f"{DECISION_KIND}:{MARKER}")))
    st.close()
    return d, orch, kinds


def test_orchestrator_reallocation_switch(tmp_path):
    """Off: a stalled island on the harness_edit lever keeps its budget weight and its mutator mix."""
    on, _, k_on = _orch(tmp_path, "on")
    assert on["promptish"]["stalled"] and on["promptish"]["lever"] == "harness_edit"
    assert on["promptish"]["budget"] < on["good"]["budget"]  # shrunk (precondition: reallocation happened)
    off, _, k_off = _orch(tmp_path, "off", {"orchestrator-reallocation"})
    assert off["promptish"]["lever"] == "harness_edit" and "switched off" in off["promptish"]["why"]
    assert off["promptish"]["budget"] >= off["good"]["budget"]  # not shrunk
    # the mix is not diversified (the edit types it never used are not opened); only the prompt quota cap applies
    assert set(off["promptish"]["mutator_mix"]) == {"prompt_text", "contract"}
    assert set(on["promptish"]["mutator_mix"]) > {"prompt_text", "contract"}
    assert off["noisy"]["lever"] == "more_evaluation"  # the lever choice is another switch's
    assert k_on == (3, 0)  # production decisions in the production kind only
    assert k_off == (0, 3)  # ablated decisions only under <kind>:ABLATED
    # control: another switch leaves this island's own reallocation as in production (its budget SHARE also depends
    # on the other islands' weights, which the lever switch changes, so the budget itself is not compared)
    ctl, _, _ = _orch(tmp_path, "ctl", {"lever-choice"})
    own = ("stalled", "lever", "why", "mutator_mix", "n_seeds", "evidence")
    assert {k: ctl["promptish"][k] for k in own} == {k: on["promptish"][k] for k in own}
    assert ctl["promptish"]["budget"] < ctl["good"]["budget"]  # still shrunk


def test_lever_choice_switch(tmp_path):
    """Off: the more_evaluation lever is never chosen; a stalled, underpowered island takes harness_edit."""
    on, o_on, _ = _orch(tmp_path, "on")
    assert on["noisy"]["lever"] == "more_evaluation" and o_on.state["noisy"].n_seeds == 2
    off, o_off, _ = _orch(tmp_path, "off", {"lever-choice"})
    assert off["noisy"]["lever"] == "harness_edit" and o_off.state["noisy"].n_seeds == 1
    ctl, o_ctl, _ = _orch(tmp_path, "ctl", {"orchestrator-reallocation"})
    assert ctl["noisy"]["lever"] == "more_evaluation" and o_ctl.state["noisy"].n_seeds == 2


# ===================================================================== sol-pi: observation-pack
def _ctx_run(tmp_path, name, off=()):
    st = _store(tmp_path, name)
    spec = HarnessSpec(id="ctx", entry="a", nodes={"a": LLMNode(prompt="{{input.big}} / {{input.log|log}}",
                                                                output_key="o")},
                       context={"max_value_chars": 400, "head_fraction": 0.5})
    eng = Engine(spec, backends={"executor": MockBackend(["ok"])}, store=st,
                 ablation=ablation_set(set(off)) if off else NO_ABLATION)
    res = eng.run("t", {"big": "z" * 1000, "log": "\n".join(["line"] * 300 + ["RuntimeError: boom"])})
    step = st.trace(res.run_id)[0]
    out = {"archived": list(step.output["archived"]), "content": step.output["messages"][-1]["content"]}
    st.close()
    return out


def test_observation_pack_switch(tmp_path):
    """Off: no archived handle for an over-long value and no verified-quote log reducer (plain head/tail)."""
    on = _ctx_run(tmp_path, "on")
    assert on["archived"] and "archived as" in on["content"] and "[log reduced" in on["content"]
    off = _ctx_run(tmp_path, "off", {"observation-pack"})
    assert off["archived"] == [] and "archived as" not in off["content"] and "[log reduced" not in off["content"]
    assert "chars omitted" in off["content"]  # still bounded: plain truncation, not the raw value
    assert "z" * 1000 not in off["content"] and "z" * 200 in off["content"]
    ctl = _ctx_run(tmp_path, "ctl", {OTHER})
    assert ctl == on
