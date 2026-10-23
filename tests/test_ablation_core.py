"""The ablation-switch mechanism (custom_harness/ablation.py; GOAL §2.1, invariant 12) and its safety design.

An ablation must never be a production backdoor:
- only the scorecard runner's entry point (`scorecard.runner.ablation_set`) makes a non-empty AblationSet; no env var,
  config default or CLI flag switches one on;
- an ablated run is marked ABLATED in its run record, every trace step and its run bundle;
- the run-bound Kernel refuses pre-registration / evidence / certification for it, the memory store refuses memory and
  skill writes from it, evolution never accepts an edit while ablated or on an ablated run (the Kernel's own refusal
  of evidence from ablated runs is in tests/test_kernel_ablation.py, inside the Kernel coverage gate);
- every switch name is in the closed registry; capabilities.yaml and the registry agree.

The mechanism tests use a probe switch registered by monkeypatch (`probe`), so they do not depend on any real
capability's switch."""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import zipfile
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

import custom_harness.ablation as A
import custom_harness.scorecard.registry as G
import custom_harness.scorecard.runner as R
from custom_harness.ablation import NO_ABLATION, AblationError, AblationSet, Switch
from custom_harness.records import Run, RunStatus, StepKind, TraceStep
from custom_harness.runtime import END, Edge, Engine, HarnessSpec, ModuleNode, register
from custom_harness.scorecard.registry import DEFAULT_CAPABILITIES, load_capabilities
from custom_harness.scorecard.runner import ablation_set

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "custom_harness"
PROBE = "probe_off"


@pytest.fixture
def probe(monkeypatch):
    """Register a test-only switch `probe_off` for capability `probe-capability` in every module that reads the
    closed registry."""
    sw = Switch(PROBE, "probe-capability", "nothing (test probe)", "tests/test_ablation_core.py::probe")
    reg = MappingProxyType({**A.SWITCHES, PROBE: sw})
    for mod in (A, R, G):
        monkeypatch.setattr(mod, "SWITCHES", reg)
    return sw


@register("test.ablation_probe")
def _probe_module(state, params, ctx):
    """Records what the engine threaded into the module context."""
    return {"seen": list(ctx.ablation.names), "n": state.get("n", 0) + 1}


def _spec() -> HarnessSpec:
    return HarnessSpec(id="abl", entry="a", nodes={"a": ModuleNode(module="test.ablation_probe"),
                                                   "b": ModuleNode(module="test.ablation_probe")},
                       edges=[Edge(source="a", target="b"), Edge(source="b", target=END)])


# ===================================================================== the set and its entry point
def test_unknown_switch_is_refused_everywhere(probe):
    with pytest.raises(AblationError, match="unknown"):
        AblationSet(["no_such_switch"])
    with pytest.raises(R.ScorecardError, match="closed registry"):
        ablation_set({"no_such_switch"})
    with pytest.raises(AblationError, match="unknown"):
        NO_ABLATION.off("no_such_switch")  # a typo in a hook never silently reads "on"
    assert NO_ABLATION.off(PROBE) is False


def test_a_nonempty_set_is_made_only_by_the_scorecard_runner_entry_point(probe):
    with pytest.raises(AblationError, match="entry point"):
        AblationSet([PROBE])  # from this (test) module: refused
    s = ablation_set({PROBE})
    assert s.off(PROBE) and s.active and s.names == (PROBE,) and list(s) == [PROBE] and PROBE in s
    assert s.marker() == {"ABLATED": True, "disabled": [PROBE]} and NO_ABLATION.marker() == {}
    assert AblationSet() == NO_ABLATION and not NO_ABLATION  # the empty set is production, constructible anywhere
    with pytest.raises(AttributeError):
        s._off = frozenset()  # type: ignore[misc]


def test_no_env_var_config_default_or_cli_flag_switches_ablation_on():
    """Static: the only constructor call with switches is the runner's entry point; nothing outside the scorecard
    calls the entry point; the ablation module reads no environment; no CLI or config file mentions ablation."""
    offenders = []
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name == "AblationSet" and (node.args or node.keywords) and rel != "scorecard/runner.py":
                    offenders.append(f"{rel}:{node.lineno} AblationSet(...) with switches")
                if name == "ablation_set" and not rel.startswith("scorecard/"):
                    offenders.append(f"{rel}:{node.lineno} calls the ablation entry point")
                # s4-wiring: the evolve driver and the islands driver TAKE an AblationSet (multi-generation arms),
                # but no src module hands them one: only a scorecard System (given the runner's set) does
                if name in ("evolve", "evolve_islands") and any(k.arg == "ablation" for k in node.keywords):
                    offenders.append(f"{rel}:{node.lineno} passes an ablation to {name}()")
    assert offenders == []
    import inspect

    from custom_harness.ablation import NO_ABLATION
    from custom_harness.evolve.driver import evolve
    from custom_harness.evolve.search import evolve_islands
    for fn in (evolve, evolve_islands):  # production default: nothing switched off
        assert inspect.signature(fn).parameters["ablation"].default is NO_ABLATION
    src = (SRC / "ablation.py").read_text(encoding="utf-8")
    names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    assert not ({"os", "environ", "getenv"} & (names | attrs)), "the ablation module reads no environment"
    for rel in ("cli.py", "config.py", "orchestration/cli.py", "studio/cli.py", "audit/cli.py", "literature/cli.py",
                "timesplit/cli.py", "scorecard/cli.py", "scorecard/config.py", "evolve/cli.py"):
        # (evolve/driver.py left this list in s4-wiring: it now threads an AblationSet it is GIVEN, checked above)
        text = (SRC / rel).read_text(encoding="utf-8").lower()
        assert "ablat" not in text and "--disable" not in text, rel
    env_reads = [p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")
                 if re.search(r"(environ|getenv)[^\n]*ABLAT", p.read_text(encoding="utf-8"), re.I)]
    assert env_reads == []


# ===================================================================== the ABLATED marker
def test_ablated_run_is_marked_in_run_record_trace_and_bundle(store, probe):
    on = Engine(_spec(), backends={}, store=store).run("t", {})
    off = Engine(_spec(), backends={}, store=store, ablation=ablation_set({PROBE})).run("t", {})
    assert on.status == off.status == RunStatus.COMPLETED
    assert on.state["seen"] == [] and off.state["seen"] == [PROBE]  # threaded into every module context
    assert store.get_run(off.run_id).ablated == (PROBE,) and store.run_ablated(off.run_id) == (PROBE,)
    assert store.get_run(on.run_id).ablated == ()
    assert [s.ablated for s in store.trace(off.run_id)] == [(PROBE,), (PROBE,)]
    assert [s.ablated for s in store.trace(on.run_id)] == [(), ()]
    # production bytes are unchanged: the field is absent from a production run's and step's JSON
    assert "ablated" not in json.loads(store.get_run(on.run_id).model_dump_json())
    assert all("ablated" not in json.loads(s.model_dump_json()) for s in store.trace(on.run_id))
    bundles = {r["run_id"]: r for r in store.records("run_bundle")}
    with zipfile.ZipFile(bundles[off.run_id]["path"]) as z:
        m = json.loads(z.read("manifest.json"))
    assert m["ABLATED"] == {"disabled": [PROBE]} and m["run"]["ablated"] == [PROBE]
    assert all(s["ablated"] == [PROBE] for s in m["trace"])
    with zipfile.ZipFile(bundles[on.run_id]["path"]) as z:
        m = json.loads(z.read("manifest.json"))
    assert "ABLATED" not in m and "ablated" not in m["run"]


def test_the_store_stamps_the_marker_from_the_run_record_not_from_the_writer(store):
    prod = Run(harness_id="h", harness_hash="h", task_id="t")
    abl = Run(harness_id="h", harness_hash="h", task_id="t", ablated=("x",))
    store.add_run(prod)
    store.add_run(abl)
    for run, claimed in ((prod, ("forged",)), (abl, ())):
        store.add_step(TraceStep(run_id=run.id, index=0, node_id="n", kind=StepKind.MODULE, input={}, output={},
                                 started_at=0.0, ended_at=0.0, ablated=claimed))
    assert store.trace(prod.id)[0].ablated == ()  # a production step cannot claim the marker
    assert store.trace(abl.id)[0].ablated == ("x",)  # an ablated run's step cannot drop it


def test_resume_and_fork_refuse_a_different_ablation(store, probe):
    off = Engine(_spec(), backends={}, store=store, ablation=ablation_set({PROBE})).run("t", {})
    on = Engine(_spec(), backends={}, store=store).run("t", {})
    with pytest.raises(ValueError, match="cannot be forked"):
        Engine(_spec(), backends={}, store=store).fork(off.run_id, 0)  # no laundering into production
    with pytest.raises(ValueError, match="cannot be forked"):
        Engine(_spec(), backends={}, store=store, ablation=ablation_set({PROBE})).fork(on.run_id, 0)
    child = Engine(_spec(), backends={}, store=store, ablation=ablation_set({PROBE})).fork(off.run_id, 0)
    assert store.get_run(child.run_id).ablated == (PROBE,)  # same ablation: allowed, still marked
    assert Engine(_spec(), backends={}, store=store).fork(on.run_id, 0).status == RunStatus.COMPLETED
    store.set_run_status(off.run_id, RunStatus.INFRA_FAILED, "simulated outage")
    with pytest.raises(ValueError, match="cannot be resumed"):
        Engine(_spec(), backends={}, store=store).resume(off.run_id)
    res = Engine(_spec(), backends={}, store=store, ablation=ablation_set({PROBE})).resume(off.run_id)
    assert res.status == RunStatus.COMPLETED  # positive control: the same ablation resumes


def test_engine_refuses_a_non_ablationset(store):
    with pytest.raises(TypeError, match="AblationSet"):
        Engine(_spec(), backends={}, store=store, ablation=frozenset({"x"}))  # type: ignore[arg-type]


# ===================================================================== refusals
def test_run_bound_kernel_refuses_every_claim_making_call_for_an_ablated_run(store):
    from custom_harness.kernel import KernelError
    from custom_harness.studio.kill import RunGuardedKernel
    abl = Run(harness_id="h", harness_hash="h", task_id="t", ablated=("x",))
    prod = Run(harness_id="h", harness_hash="h", task_id="t")
    store.add_run(abl)
    store.add_run(prod)
    k = RunGuardedKernel(store, run_id=abl.id)
    for call in (lambda: k.preregister(None), lambda: k.record_evaluation(None),  # type: ignore[arg-type]
                 lambda: k.record_certificate(None, "a"), lambda: k.certify(None, [])):  # type: ignore[arg-type]
        with pytest.raises(KernelError, match="ABLATED"):
            call()
    # positive control: the production run's Kernel gets past the guard (and fails later on the bogus argument)
    with pytest.raises(Exception) as e:
        RunGuardedKernel(store, run_id=prod.id).preregister(None)  # type: ignore[arg-type]
    assert "ABLATED" not in str(e.value)


def _proposal():
    from custom_harness.memory.model import Proposal
    return Proposal.model_validate({"kind": "positive", "title": "t", "text": "a fact", "evidence": ["a" * 64]})


def test_memory_store_refuses_memory_and_skill_writes_from_an_ablated_run(store):
    from custom_harness.memory import MemoryRefused, MemoryStore
    from custom_harness.studio.kill import RunGuardedKernel
    abl = Run(harness_id="h", harness_hash="h", task_id="t", ablated=("x",))
    prod = Run(harness_id="h", harness_hash="h", task_id="t")
    store.add_run(abl)
    store.add_run(prod)
    good = MemoryStore(RunGuardedKernel(store, run_id=prod.id), namespace="abl")
    eid = good.propose(_proposal(), writer="test")  # positive control: a production run's write is stored
    assert eid in {r["entry_id"] for r in good.log() if r["type"] == "propose"}
    bad = MemoryStore(RunGuardedKernel(store, run_id=abl.id), namespace="abl2")
    with pytest.raises(MemoryRefused, match="ABLATED"):
        bad.propose(_proposal(), writer="test")
    with pytest.raises(MemoryRefused, match="ABLATED"):
        bad.admit(eid)
    with pytest.raises(MemoryRefused, match="ABLATED"):
        bad.record_outcome(eid, "b" * 64)
    assert bad.log() == []  # nothing was written
    assert bad.query(reader="run:x") == []  # reads stay allowed


# ===================================================================== evolution never accepts from an ablation
def _evolve_world(tmp_path, ablation=NO_ABLATION, run_id=""):
    from custom_harness.evolve import EvolveConfig
    from custom_harness.gateway import MockBackend
    from custom_harness.store import EvidenceStore
    from test_evolve import seed_spec
    from test_evolve_hardening import _GateTable, _label_tasks
    from test_lofo import ONE_EDIT

    class Table(_GateTable):
        def outcome(self, spec, unit):
            o = super().outcome(spec, unit)
            return dataclasses.replace(o, run_id=run_id) if run_id else o

    tasks = _label_tasks(tmp_path)
    parent = seed_spec()
    st = EvidenceStore(tmp_path / "store")
    ev = Table(st, tasks, parent.spec_hash(), "CERTIFIED-VALIDATED", 0.0, 100)
    ev.ablation = ablation
    return st, parent, tasks, ev, MockBackend([ONE_EDIT]), EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1)


def test_an_ablated_generation_never_accepts_and_keeps_its_records_apart(tmp_path, probe):
    from custom_harness.evolve import evolve_generation
    st, parent, tasks, ev, prop, cfg = _evolve_world(tmp_path / "on")
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=prop, store=st, cfg=cfg)
    assert res.accepted, res.decision  # positive control: the same world accepts in production
    st.close()
    abl = ablation_set({PROBE})
    st, parent, tasks, ev, prop, cfg = _evolve_world(tmp_path / "off", abl)
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=prop, store=st, cfg=cfg)
    assert not res.accepted and res.new_parent.spec_hash() == parent.spec_hash()
    assert res.decision.startswith("not accepted: ABLATED")
    final = [c for c in res.candidates if c["status"] == "ablated_not_accepted"]
    assert len(final) == 1 and final[0]["ablated_would_accept"] is True and final[0]["disabled"] == [PROBE]
    # no production reader (novelty gate, proposer ledger, calibration, generation ledger) sees an ablated record
    for kind in ("lineage", "harness_edit", "generation_stats"):
        assert st.records(kind) == [] and st.records(f"{kind}:ABLATED"), kind
    assert all(r["ABLATED"] is True for r in st.records("lineage:ABLATED"))
    st.close()


def test_an_acceptance_resting_on_an_ablated_run_is_refused_even_in_production(tmp_path):
    from custom_harness.evolve import evolve_generation
    st, parent, tasks, ev, prop, cfg = _evolve_world(tmp_path / "w", run_id="run_marked")
    st.add_run(Run(id="run_marked", harness_id="h", harness_hash="h", task_id="t", ablated=("x",)))
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=prop, store=st, cfg=cfg)
    assert not res.accepted
    rec = next(c for c in res.candidates if c["status"] == "ablated_not_accepted")
    assert rec["ablated_runs"] == ["run_marked"] and rec["ablated_would_accept"] is True
    st.close()


def test_generation_and_evaluator_must_share_one_ablation(tmp_path, probe):
    from custom_harness.evolve import evolve_generation
    st, parent, tasks, ev, prop, cfg = _evolve_world(tmp_path / "w")
    with pytest.raises(ValueError, match="differs from its evaluator"):
        evolve_generation(parent, tasks, evaluator=ev, proposer=prop, store=st, cfg=cfg,
                          ablation=ablation_set({PROBE}))
    st.close()


def test_an_ablated_evaluator_keeps_durable_outcomes_apart(tmp_path, probe):
    from custom_harness.evolve import Evaluator
    from custom_harness.store import EvidenceStore
    st = EvidenceStore(tmp_path / "s")
    prod = Evaluator({}, st, lambda k: None, [], durable_key="k")
    abl = Evaluator({}, st, lambda k: None, [], durable_key="k", ablation=ablation_set({PROBE}))
    assert prod.durable_key == "k" and abl.durable_key == f"k|ABLATED:{PROBE}"
    st.close()


# ===================================================================== the closed registry and capabilities.yaml
def test_registry_and_capabilities_yaml_agree():
    caps = load_capabilities()
    ids = {c.id for c in caps}
    assert len(caps) == 37
    named = {c.ablation.switch: c.id for c in caps if c.ablation is not None and c.ablation.switch is not None}
    for name, sw in A.SWITCHES.items():
        assert sw.name == name and sw.capability in ids, name
        assert named.get(name) == sw.capability, f"switch {name} is not named by its row {sw.capability}"
        path, _, fn = sw.test.partition("::")
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        assert fn in {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}, sw.test
        assert sw.turns_off.strip()
    for name, cid in named.items():
        assert name in A.SWITCHES, f"row {cid} names {name!r}, which is not in the closed registry"
    for c in caps:  # every row has a record now: a switch or the exact reason it has none
        assert c.ablation is not None, c.id
    raw = yaml.safe_load(DEFAULT_CAPABILITIES.read_text(encoding="utf-8"))["capabilities"]
    assert [r["id"] for r in raw] == [c.id for c in caps]
