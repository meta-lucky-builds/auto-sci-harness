"""Target-derived taint follows values through copies, across resume and fork, and per run (GOAL §16.5.3,
invariant 18; design note docs/taint.md).

Reproductions written FIRST, on c9823b8 (where each failed):
  (a) a module copies a target-derived value into a new state key; a later prompt renders the copy: no release was
      emitted and the K4 count did not change;
  (b) a hard kill (or an infra failure, or a fork) followed by a resume in a fresh engine lost the taint, and the same
      leak happened;
  (c) a private read made by a CONCURRENT run (another thread, its own run id) inside this run's step window tainted
      this run's output (sequence-window over-taint, timing dependent).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import _taint_crash_helpers as K
from _kerneld_standin import kerneld_vault
from custom_harness.errors import InfraError
from custom_harness.gateway import ChatRequest, Message, MockBackend
from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.kerneld.releases import released
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec, register
from custom_harness.runtime.engine import RetryPolicy
from custom_harness.store import EvidenceStore

HERE = Path(__file__).resolve().parent
TARGET = "tf-held"


def _ensure(vault, ds: str = TARGET) -> None:
    if ds not in vault.datasets():
        vault.register_dataset(ds, DatasetRole.TEST, public={"x.csv": b"x\n1\n"},
                               private={"y.csv": b"y\n1\n0\n1\n1\n"})


def _held_mean(ctx, ds: str = TARGET, run_id: object = "self") -> float:
    rid = ctx.run_id if run_id == "self" else run_id
    with kerneld_vault(ctx) as vault:  # kerneld's private ingest + logged read (tests/_kerneld_standin.py)
        _ensure(vault, ds)
        labels = vault.read(ds, "y.csv", purpose="leakscan", run_id=rid).decode().split()[1:]
    return sum(map(float, labels)) / len(labels)


@register("tf.held")
def _held(state, params, ctx):
    """Reads the target's private labels: everything it writes is target-derived."""
    return {"raw": {"mean": _held_mean(ctx)}, "route": "high" if _held_mean(ctx) > 0.5 else "low"}


@register("tf.copy")
def _copy(state, params, ctx):
    """No private read: copies the target-derived number into a fresh key."""
    return {"copied": state["raw"]["mean"]}


@register("tf.format")
def _format(state, params, ctx):
    return {"text": f"the model scored {state['raw']['mean']:.3f} on the test set"}


@register("tf.split")
def _split(state, params, ctx):
    digits = f"{state['raw']['mean']:.4f}"
    return {"part_a": digits[:3], "part_b": digits[3:]}


@register("tf.public")
def _public(state, params, ctx):
    return {"box": {"note": "public"}}


@register("tf.mutate")
def _mutate(state, params, ctx):
    """Writes the target-derived value INTO an existing untainted key, in place, and returns nothing."""
    state["box"]["note"] = f"{state['raw']['mean']}"
    return {}


@register("tf.bulk")
def _bulk(state, params, ctx):
    """Reads the whole state in bulk (no per-key access) and derives a value from it."""
    snapshot = {**state}
    return {"digest": str(sorted(snapshot.items(), key=lambda kv: kv[0]))[-400:]}


@register("tf.module_prompt")
def _module_prompt(state, params, ctx):
    """Renders the target-derived value into a prompt the module builds itself."""
    resp = ctx.llm("executor", ChatRequest(messages=[Message(role="user", content=f"score={state['copied']}")]))
    return {"module_answer": resp.text}


@register("tf.trace_reader")
def _trace_reader(state, params, ctx):
    """No state read at all: reads the run's own trace from the store (the held step's output is in it)."""
    steps = ctx.store.trace(ctx.run_id)
    return {"from_trace": str([s.output.get("raw") for s in steps])}


@register("tf.item_body")
def _item_body(state, params, ctx):
    """A fan-out branch body that mixes a target-derived key into each (public) item."""
    return {"v": f"{state['item']}:{state['raw']['mean']}"}


def _spec(nodes: dict, order: list[str], **extra) -> HarnessSpec:
    from itertools import pairwise
    return HarnessSpec.model_validate({"id": "tf", "entry": order[0], "nodes": nodes,
                                       "edges": [{"source": a, "target": b} for a, b in pairwise(order)], **extra})


def _prompt_releases(store, target: str = TARGET) -> list[tuple[str, str]]:
    return [(r["alphabet"], r["note"]) for r in released(store, target) if r["channel"] == "prompt"]


def _k4_count(store, target: str = TARGET) -> int:
    """What K4 counts: ledger `release` entries on the target's family (Kernel.releases)."""
    return len(Kernel(store).releases(target))


def _run(store, nodes, order, llm=None, **extra):
    llm = llm or MockBackend(lambda req: "ok")
    res = Engine(_spec(nodes, order, **extra), backends={"executor": llm}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    return res, llm


HELD = {"kind": "module", "module": "tf.held"}


def _ask(prompt: str) -> dict:
    return {"kind": "llm", "prompt": prompt, "output_key": "answer"}


# ------------------------------------------------------------------------------------------ (a) copies
def test_a_copy_into_a_new_key_rendered_later_is_a_release_and_k4_counts_it(store):
    res, llm = _run(store, {"held": HELD, "copy": {"kind": "module", "module": "tf.copy"},
                            "ask": _ask("Score: {{copied}}")}, ["held", "copy", "ask"])
    assert str(res.state["copied"]) in llm.requests[0].messages[-1].content  # the value really reached the prompt
    assert _prompt_releases(store) == [("continuous", "prompt:ask:copied")]
    assert _k4_count(store) == 1


@pytest.mark.parametrize("module,prompt,key", [
    ("tf.format", "Summary: {{text}}", "text"),
    ("tf.split", "Parts: {{part_b}}", "part_b"),
])
def test_a_derived_values_in_new_keys_are_released(store, module, prompt, key):
    _run(store, {"held": HELD, "derive": {"kind": "module", "module": module}, "ask": _ask(prompt)},
         ["held", "derive", "ask"])
    assert _prompt_releases(store) == [("continuous", f"prompt:ask:{key}")]


def test_a_in_place_mutation_of_an_untainted_key_is_released(store):
    res, _ = _run(store, {"pub": {"kind": "module", "module": "tf.public"}, "held": HELD,
                          "mutate": {"kind": "module", "module": "tf.mutate"}, "ask": _ask("Note: {{box.note}}")},
                  ["pub", "held", "mutate", "ask"])
    assert res.state["box"]["note"] != "public"
    assert _prompt_releases(store) == [("continuous", "prompt:ask:box.note")]


def test_a_bulk_state_read_is_released(store):
    _run(store, {"held": HELD, "bulk": {"kind": "module", "module": "tf.bulk"}, "ask": _ask("D: {{digest}}")},
         ["held", "bulk", "ask"])
    assert _prompt_releases(store) == [("continuous", "prompt:ask:digest")]


def test_a_module_built_prompt_carrying_a_tainted_value_is_a_release(store):
    res, llm = _run(store, {"held": HELD, "copy": {"kind": "module", "module": "tf.copy"},
                            "mp": {"kind": "module", "module": "tf.module_prompt"}}, ["held", "copy", "mp"])
    assert str(res.state["copied"]) in llm.requests[0].messages[-1].content
    assert [n for _a, n in _prompt_releases(store)] == ["module_llm:mp:copied"]


def test_a_value_read_back_from_the_store_is_released(store):
    _run(store, {"held": HELD, "tr": {"kind": "module", "module": "tf.trace_reader"}, "ask": _ask("T: {{from_trace}}")},
         ["held", "tr", "ask"])
    assert _prompt_releases(store) == [("continuous", "prompt:ask:from_trace")]


def test_a_fan_out_branch_mixing_a_tainted_key_is_released(store):
    nodes = {"held": HELD, "items": {"kind": "module", "module": "tf.listing"},
             "fan": {"kind": "map", "items_key": "listing", "body": "body", "output_key": "fanned",
                     "reducer": "collect"},
             "body": {"kind": "module", "module": "tf.item_body"}, "ask": _ask("F: {{fanned}}")}
    _run(store, nodes, ["held", "items", "fan", "ask"])
    assert _prompt_releases(store) == [("continuous", "prompt:ask:fanned")]


@register("tf.listing")
def _listing(state, params, ctx):
    return {"listing": ["a", "b"]}


def test_a_routing_on_a_tainted_key_is_released_when_a_later_prompt_reveals_it(store):
    """Implicit flow: which prompt runs depends on the target (the route), although the prompt renders nothing
    tainted. One release per tainted decision, at the first agent-visible surface after it."""
    nodes = {"held": HELD, "hi": _ask("The run went the high way."), "lo": _ask("The run went the low way.")}
    spec = HarnessSpec.model_validate({"id": "tf", "entry": "held", "nodes": nodes, "edges": [
        {"source": "held", "target": "hi", "when": {"key": "route", "op": "eq", "value": "high"}},
        {"source": "held", "target": "lo"}]})
    res = Engine(spec, backends={"executor": MockBackend(lambda r: "ok")}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert _prompt_releases(store) == [("verdict_bit", "control:held:hi")]


def test_untainted_flows_release_nothing(store):
    """Control: copies of public values, and a prompt rendering only public keys, release nothing."""
    _run(store, {"pub": {"kind": "module", "module": "tf.public"}, "held": HELD,
                 "ask": _ask("Note: {{box.note}}")}, ["pub", "held", "ask"])
    assert _prompt_releases(store) == []


# ------------------------------------------------------------------------------------------ (b) resume, fork
def test_b_hard_kill_then_resume_keeps_the_label(tmp_path):
    store_dir, marker = tmp_path / "store", tmp_path / "marker"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.Popen([sys.executable, str(HERE / "_taint_crash_helpers.py"), str(store_dir), str(marker)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        while not marker.exists():
            assert proc.poll() is None, proc.stderr.read().decode()
            assert time.time() < deadline, "child never reached the in-flight step"
            time.sleep(0.05)
        time.sleep(0.2)
    finally:
        proc.kill()  # TerminateProcess on Windows / SIGKILL on POSIX: no cleanup code runs
        proc.wait()
    store = EvidenceStore(store_dir)
    run = store.list_runs(1)[0]
    assert store.run_status(run.id)[0] == RunStatus.RUNNING
    assert [s.node_id for s in store.trace(run.id)] == ["held"]  # `wait` was in flight
    res = Engine(K.spec(str(marker)), backends=K.backends(), store=store).resume(run.id)
    assert res.status == RunStatus.COMPLETED, res.detail
    assert _prompt_releases(store, K.TARGET) == [("continuous", "prompt:ask:score")]
    store.close()


_flaky = {"n": 0}


@register("tf.flaky")
def _flaky_mod(state, params, ctx):
    _flaky["n"] += 1
    if _flaky["n"] == 1:
        raise InfraError("docker daemon went away", transient=False)
    return {"after": True}


def test_b_infra_failure_then_resume_in_a_fresh_engine_keeps_the_label(store):
    _flaky["n"] = 0
    spec = _spec({"held": HELD, "copy": {"kind": "module", "module": "tf.copy"},
                  "flaky": {"kind": "module", "module": "tf.flaky"}, "ask": _ask("Score: {{copied}}")},
                 ["held", "copy", "flaky", "ask"])
    first = Engine(spec, backends={"executor": MockBackend(lambda r: "ok")}, store=store,
                   retry=RetryPolicy(max_attempts=1, backoff_s=0.0)).run("t", {})
    assert first.status == RunStatus.INFRA_FAILED
    done = Engine(spec, backends={"executor": MockBackend(lambda r: "ok")}, store=store).resume(first.run_id)
    assert done.status == RunStatus.COMPLETED, done.detail
    assert _prompt_releases(store) == [("continuous", "prompt:ask:copied")]


def test_b_fork_in_a_fresh_engine_keeps_the_label(store):
    spec = _spec({"held": HELD, "copy": {"kind": "module", "module": "tf.copy"}, "ask": _ask("Score: {{copied}}")},
                 ["held", "copy", "ask"])
    first = Engine(spec, backends={"executor": MockBackend(lambda r: "ok")}, store=store).run("t", {})
    assert first.status == RunStatus.COMPLETED
    before = len(_prompt_releases(store))
    child = Engine(spec, backends={"executor": MockBackend(lambda r: "ok")}, store=store).fork(first.run_id, 1)
    assert child.status == RunStatus.COMPLETED, child.detail
    assert len(_prompt_releases(store)) == before + 1  # the child's prompt renders the parent's tainted copy


# ------------------------------------------------------------------------------------------ (c) concurrent runs
_gates: dict[str, threading.Event] = {}


@register("tf.slow_public")
def _slow_public(state, params, ctx):
    _gates["a_in_step"].set()
    assert _gates["b_read"].wait(20), "run B never read"
    return {"note": "public"}


@register("tf.concurrent_reader")
def _concurrent_reader(state, params, ctx):
    assert _gates["a_in_step"].wait(20), "run A never started its step"
    _held_mean(ctx, "tf-other")  # a private read of ANOTHER target, logged under run B's own id
    _gates["b_read"].set()
    return {"b": True}


def test_c_a_concurrent_runs_private_read_does_not_taint_this_run(store):
    _gates.update(a_in_step=threading.Event(), b_read=threading.Event())
    spec_a = _spec({"slow": {"kind": "module", "module": "tf.slow_public"}, "ask": _ask("Note: {{note}}")},
                   ["slow", "ask"])
    spec_b = _spec({"reader": {"kind": "module", "module": "tf.concurrent_reader"}}, ["reader"])
    out: dict = {}

    def go(name, spec):
        out[name] = Engine(spec, backends={"executor": MockBackend(lambda r: "ok")}, store=store).run(name, {})
    threads = [threading.Thread(target=go, args=(n, s)) for n, s in (("a", spec_a), ("b", spec_b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert out["a"].status == RunStatus.COMPLETED and out["b"].status == RunStatus.COMPLETED
    # the attack surface is live: B's private read really landed inside A's step window
    rows = [r for r in store.ledger_rows("data_access") if '"tf-other"' in r[2]]
    assert len(rows) == 1
    assert _prompt_releases(store, "tf-other") == [], "run B's read tainted run A's prompt"


@register("tf.anonymous_read")
def _anonymous_read(state, params, ctx):
    _held_mean(ctx, run_id=params.get("run_id"))  # a read naming no run (like a Kernel re-check) or an unknown one
    return {"note": "public"}


@pytest.mark.parametrize("run_id", [None, "no-such-run"])
def test_c_a_private_read_without_a_known_run_id_still_taints_fail_closed(store, run_id):
    _run(store, {"anon": {"kind": "module", "module": "tf.anonymous_read", "params": {"run_id": run_id}},
                 "ask": _ask("Note: {{note}}")}, ["anon", "ask"])
    assert _prompt_releases(store) == [("continuous", "prompt:ask:note")]


# ------------------------------------------------------------------------------------------ the evolver's views
EXPORT_LABELS = b"y\n1\n0\n0\n1\n1\n1\n0\n"  # mean 4/7 = 0.5714285714285714: a distinctive held-out value


@register("tf.export_held")
def _export_held(state, params, ctx):
    with kerneld_vault(ctx) as vault:  # kerneld's private ingest + logged read (tests/_kerneld_standin.py)
        if "tfx-held" not in vault.datasets():
            vault.register_dataset("tfx-held", DatasetRole.TEST, public={"x.csv": b"x\n1\n"},
                                   private={"y.csv": EXPORT_LABELS})
        labels = vault.read("tfx-held", "y.csv", purpose="leakscan", run_id=ctx.run_id).decode().split()[1:]
    return {"raw": {"mean": sum(map(float, labels)) / len(labels)}}


@register("tf.export_copy")
def _export_copy(state, params, ctx):
    mean = state["raw"]["mean"]
    if params.get("fail"):
        raise ValueError(f"majority class is {'POSITIVE' if mean > 0.5 else 'NEGATIVE'}")
    return {"summary": f"held-out mean {mean}"}


def _evolve_run(store, tmp_path, nodes, order, **edges_extra):
    from custom_harness.evolve.evaluate import SuiteTask, run_harness
    from custom_harness.sandbox import LocalSandbox
    task = SuiteTask("tfx", tmp_path / "taskdir", "evolve", family="A")
    task.task_dir.mkdir(parents=True, exist_ok=True)
    out = run_harness(_spec(nodes, order, **edges_extra), task, 0, backends={}, store=store,
                      sandbox_factory=lambda k: LocalSandbox(tmp_path / k, i_understand_no_isolation=True))
    return out, task


def test_export_withholds_a_copied_held_out_value(store, tmp_path):
    from custom_harness.evolve.export import export_evidence
    out, task = _evolve_run(store, tmp_path, {"held": {"kind": "module", "module": "tf.export_held"},
                                               "copy": {"kind": "module", "module": "tf.export_copy"}},
                            ["held", "copy"])
    assert out.status == "completed"
    res = export_evidence(store, tmp_path / "exp", runs=[out], tasks=[task])
    text = "".join(p.read_text(encoding="utf-8") for p in res.path.rglob("*.json"))
    assert "traces/" in " ".join(res.files)  # the trace really was exported
    assert "0.5714285714" not in text, "a copied continuous held-out value reached the evolver's export"


def test_failure_signatures_withhold_target_derived_error_text(store, tmp_path):
    from custom_harness.evolve.evaluate import failure_signatures
    nodes = {"held": {"kind": "module", "module": "tf.export_held"},
             "copy": {"kind": "module", "module": "tf.export_copy", "params": {"fail": True}}}
    out, _task = _evolve_run(store, tmp_path, nodes, ["held", "copy"])
    sigs = failure_signatures(store, out.run_id)
    assert sigs, "the run did fail"
    assert not any("POSITIVE" in s or "NEGATIVE" in s for s in sigs), sigs


@register("tf.recheck_read")
def _recheck_read(state, params, ctx):
    """A private read that names no run (like a Kernel re-check read)."""
    with kerneld_vault(ctx) as vault:  # kerneld's private ingest + logged read (tests/_kerneld_standin.py)
        _ensure(vault, "tfr-held")
        vault.read("tfr-held", "y.csv", purpose="leakscan")
    return {"certificate": {"label": "CERTIFIED-INTERNAL"}}


def test_run_level_releases_count_reads_that_name_no_run(store, tmp_path):
    """The export's verdict release (and the evolution score's) is on every target the run read, including reads
    logged without a run id; `private_reads(run_id=...)` missed those."""
    from custom_harness.evolve.export import export_evidence
    out, task = _evolve_run(store, tmp_path, {"r": {"kind": "module", "module": "tf.recheck_read"}}, ["r"])
    export_evidence(store, tmp_path / "exp", runs=[out], tasks=[task])
    assert [(r["channel"], r["note"]) for r in released(store, "tfr-held")] == [
        ("evolution_score", "evolve.score:verdict"), ("export", "export.run:verdict")]


def test_attributed_reads_rule(store):
    """Unit rule: a read naming this run, no run or an unknown run counts; another known run's own read does not."""
    from custom_harness.kerneld.releases import attributed_reads
    from custom_harness.records import Run
    vault = Kernel(store).vault
    for ds in ("u-mine", "u-none", "u-unknown", "u-other"):
        _ensure(vault, ds)
    other = Run(harness_id="h", harness_hash="x", task_id="t")
    store.add_run(other)
    vault.read("u-mine", "y.csv", purpose="leakscan", run_id="run-me")
    vault.read("u-none", "y.csv", purpose="leakscan")
    vault.read("u-unknown", "y.csv", purpose="leakscan", run_id="run-nobody-knows")
    vault.read("u-other", "y.csv", purpose="leakscan", run_id=other.id)
    vault.read_public("u-other", "x.csv")
    targets, last = attributed_reads(store, after_seq=0, run_ids={"run-me"})
    assert targets == ["u-mine", "u-none", "u-unknown"]
    assert last == store.ledger_rows("data_access")[-1][0]


# ------------------------------------------------------------------------------------------ recall (b-research rule)
def test_recall_refuses_a_handle_whose_value_a_fresh_engine_fork_inherited_tainted(store):
    """Paged recall refuses a handle if its archive provenance carries a target (the engine's labels) OR the prompt
    that archived it ran after a private read of THIS run (research/evidence_refs.run_private_reads). Neither rule
    contains the other: here the read happened in the PARENT run, before the fork, so the time rule does not fire;
    only the label restored from the fork checkpoint refuses it. On c9823b8 a fork in a fresh engine served it."""
    from test_redteam_critic_recall import _ask, _handle, _refused, _rmock, _rspec
    spec = _rspec(["seed", "held", "agent", "recall", "reader"])
    parent = Engine(spec, backends={"executor": _rmock(_ask(_handle))}, store=store).run("t", {})
    assert parent.status == RunStatus.COMPLETED, parent.detail
    held_step = next(s.index for s in store.trace(parent.run_id) if s.node_id == "held")
    child = Engine(spec, backends={"executor": _rmock(_ask(_handle))}, store=store).fork(parent.run_id, held_step)
    assert child.status == RunStatus.COMPLETED, child.detail
    assert child.state["big"].startswith("held-out derived value")
    assert _refused(child) == "target_derived", child.state["recalled"]
