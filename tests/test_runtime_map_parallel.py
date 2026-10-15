"""Opt-in parallel MapNode fan-out (Engine(map_workers=N), runtime/engine.py _ParallelBranches): outputs, persisted
branches, labels and errors equal the sequential run's for every worker count; worker threads write only their own
cost-journal rows; a hard kill mid-map resumes exactly (every model call charged once); labels stay per branch.

Fail-first (stated, not run in the no-test round): at the parent commit `Engine` has no `map_workers` argument, so
every test here fails with TypeError before any assertion; `test_workers_write_only_their_journal_rows` and the
determinism test also fail against a naive parallel loop that commits in completion order (records and releases
out of item order) or calls `_attempt` from workers (store/ledger writes off the engine thread)."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import _map_parallel_helpers as H
from custom_harness.budget import Budget
from custom_harness.errors import InfraError
from custom_harness.gateway import Backend, ChatRequest, ChatResponse, MockBackend
from custom_harness.records import AccessMode, Cost, RunStatus
from custom_harness.runtime import Engine, HarnessSpec, LLMNode, MapNode
from custom_harness.runtime.engine import RetryPolicy, _branch_id, approval_item_hash
from custom_harness.store import EvidenceStore

HERE = Path(__file__).resolve().parent
ITEMS = [f"i{k}" for k in range(6)]


def _tokens(c: Cost) -> int:
    return c.prompt_tokens + c.completion_tokens


def _slow_first(req: ChatRequest) -> str:
    """Response depends on the request only; EARLIER items answer LATER, so completion order is reversed."""
    text = req.messages[-1].content
    k = int(text.rsplit("i", 1)[-1])
    time.sleep(0.02 * (len(ITEMS) - k))
    return text.upper()


def _spec(body: LLMNode | None = None) -> HarnessSpec:
    return HarnessSpec(id="pmap", entry="m", nodes={
        "m": MapNode(items_key="input.items", body="b", output_key="out", reducer="collect"),
        "b": body or LLMNode(prompt="say {{item}}", output_key="said")})


class _Rec:
    """Release recorder (engine.releases): which thread emitted, for which targets."""

    def __init__(self):
        self.calls = []

    def emit(self, value, **kw):
        self.calls.append((threading.current_thread().name, sorted(kw["targets"])))


def _run(tmp_path: Path, workers: int, items=ITEMS, responder=_slow_first, body=None, **kw):
    store = EvidenceStore(tmp_path / f"store-{workers}-{len(list(tmp_path.iterdir()))}")
    writes: list[tuple[str, str]] = []
    orig = store.append_record

    def spy(kind, body_, _o=orig):
        writes.append((threading.current_thread().name, kind))
        return _o(kind, body_)
    store.append_record = spy
    eng = Engine(_spec(body), backends={"executor": MockBackend(responder)}, store=store, map_workers=workers, **kw)
    eng.taint["input"] = {"target-1"}
    eng.releases = _Rec()
    res = eng.run("t", {"items": list(items)})
    return store, eng, res, writes


def _view(store: EvidenceStore, res) -> dict:
    """Everything that must not depend on the worker count."""
    [row] = store.trace(res.run_id)
    recs = store.records_where("map_branch", "run_id", res.run_id)
    inst = row.output["instance"]
    # the detail quotes a branch id prefix, which depends on the run id (instance): normalised
    detail = re.sub(r"\([0-9a-f]{12}\)", "(id)", res.detail)
    return {"status": res.status, "out": res.state.get("out"), "detail": detail,
            "records": [(r["index"], r["item_hash"], r["output_ref"], r["error"], r["taint"]) for r in recs],
            "ids_ok": [r["branch_id"] for r in recs] == [
                _branch_id(inst, "m", r["index"], approval_item_hash(ITEMS[r["index"]])) for r in recs],
            "branches": [(b["index"], b.get("output_ref"), b.get("error")) for b in row.output["branches"]],
            "value": row.output.get("value")}


# ------------------------------------------------------------------------------------------- determinism
def test_outputs_records_and_labels_equal_across_worker_counts(tmp_path):
    views, labels, releases = {}, {}, {}
    for w in (1, 2, 4, 8):
        store, eng, res, _ = _run(tmp_path, w)
        assert res.status == RunStatus.COMPLETED, res.detail
        views[w] = _view(store, res)
        labels[w] = sorted(eng.taint["out"])
        releases[w] = [t for _, t in eng.releases.calls]
        store.close()
    assert views[1]["out"] == [f"SAY {i.upper()}" for i in ITEMS]
    assert views[1]["ids_ok"]
    for w in (2, 4, 8):
        assert views[w] == views[1], w
        assert labels[w] == labels[1] == ["target-1"]
        assert releases[w] == releases[1]  # one release per branch prompt, in item order


def test_parallel_is_opt_in_and_validated(tmp_path):
    store, eng, _res, writes = _run(tmp_path, 1)
    assert eng.map_workers == 1
    assert not [n for n, _ in writes if n.startswith("chh-map")]  # default: no worker thread at all
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="map_workers"):
            Engine(_spec(), backends={"executor": MockBackend(_slow_first)}, store=store, map_workers=bad)
    store.close()


def test_workers_write_only_their_journal_rows(tmp_path):
    store, eng, res, writes = _run(tmp_path, 4)
    assert res.status == RunStatus.COMPLETED
    worker = [k for n, k in writes if n.startswith("chh-map")]
    assert worker and set(worker) == {"cost_journal"}  # single writer: everything else on the engine thread
    assert len(worker) == len(ITEMS)
    assert {n for n, _ in eng.releases.calls} == {threading.main_thread().name}  # releases: engine thread only
    # every journal row of a branch names its branch and execution nonce, which the persisted record carries
    recs = {r["nonce"]: r for r in store.records_where("map_branch", "run_id", res.run_id)}
    rows = [j for j in store.records_where("cost_journal", "run_id", res.run_id)]
    assert {j["nonce"] for j in rows} == set(recs) and all(recs[j["nonce"]]["branch_id"] == j["branch"]
                                                            for j in rows)
    store.close()


def test_a_failing_branch_gives_the_sequential_error_and_charges_every_call_once(tmp_path):
    body = LLMNode(prompt="say {{item}}", output_key="said", output_schema={"type": "object"}, retries=0)

    def resp(req):  # i1 breaks its JSON contract; the others answer JSON
        text = req.messages[-1].content
        return "not json" if text.endswith("i1") else json.dumps({"t": text})
    seq_store, _, seq, _ = _run(tmp_path, 1, responder=resp, body=body)
    par_store, _, par, _ = _run(tmp_path, 4, responder=resp, body=body)
    assert seq.status == par.status == RunStatus.FAILED
    sv, pv = _view(seq_store, seq), _view(par_store, par)
    assert sv["detail"] == pv["detail"] and "ContractError" in pv["detail"]
    assert pv["branches"] == sv["branches"]  # same committed branches, same error, nothing reduced
    assert pv["records"][:2] == sv["records"]  # the sequential records are a prefix of the parallel ones
    # spend: the step charges exactly what its branches' model calls journalled (later branches the parallel run
    # had started are charged too: listed as discarded)
    [prow] = par_store.trace(par.run_id)
    journal = sum((Cost.model_validate(j["cost"]) for j in par_store.records_where("cost_journal", "run_id",
                                                                                  par.run_id)), Cost())
    assert _tokens(prow.cost) == _tokens(journal)
    assert all(d["index"] > 1 for d in prow.output.get("discarded_branches", []))
    seq_store.close()
    par_store.close()


def test_budget_stop_is_the_sequential_stop(tmp_path):
    spec_budget = {"tokens": 7}  # each branch costs 2 + 2 tokens ("say iK" / "SAY IK")
    seq_store, _, seq, _ = _run(tmp_path, 1, budget=Budget(limits=dict(spec_budget)))
    par_store, _, par, _ = _run(tmp_path, 4, budget=Budget(limits=dict(spec_budget)))
    assert seq.status == par.status == RunStatus.BUDGET_EXCEEDED
    [srow], [prow] = seq_store.trace(seq.run_id), par_store.trace(par.run_id)
    assert [b["index"] for b in prow.output["branches"]] == [b["index"] for b in srow.output["branches"]]
    # the parallel run may have overshot by at most workers-1 branches, all of them charged and listed
    assert len(prow.output.get("discarded_branches", [])) <= 3
    journal = sum((Cost.model_validate(j["cost"]) for j in par_store.records_where("cost_journal", "run_id",
                                                                                  par.run_id)), Cost())
    assert _tokens(prow.cost) == _tokens(journal)
    seq_store.close()
    par_store.close()


class _FlakyOnce(Backend):
    """Transient outage on the first call for item i2 only."""
    name, provider, mode, training_on_outputs = "flaky", "mock", AccessMode.MOCK, "allowed"

    def __init__(self):
        self.failed = False
        self.lock = threading.Lock()

    def complete(self, req: ChatRequest) -> ChatResponse:
        text = req.messages[-1].content
        with self.lock:
            fail = text.endswith("i2") and not self.failed
            self.failed = self.failed or fail
        if fail:
            raise InfraError("gateway timeout", transient=True)
        return ChatResponse(text=text.upper(), cost=Cost(prompt_tokens=2, completion_tokens=2),
                            provenance=self.provenance("m"))


def test_transient_retry_releases_like_the_sequential_path(tmp_path):
    def run(w):
        store = EvidenceStore(tmp_path / f"flaky-{w}")
        eng = Engine(_spec(), backends={"executor": _FlakyOnce()}, store=store, map_workers=w,
                     retry=RetryPolicy(max_attempts=3, backoff_s=0.0))
        eng.taint["input"] = {"target-1"}
        eng.releases = _Rec()
        res = eng.run("t", {"items": ITEMS})
        out = (res.status, res.state.get("out"), len(eng.releases.calls))
        store.close()
        return out
    seq, par = run(1), run(4)
    assert seq == par
    assert seq[2] == len(ITEMS) + 1  # the retried branch re-renders (re-releases) its prompt once


# ------------------------------------------------------------------------------------------- taint per branch
def test_labels_are_per_branch_and_never_cross(tmp_path):
    """Each branch record carries what THAT branch rendered (its item, labelled like the list) and nothing else: a
    labelled run key no branch renders (`_seed` here) enters no branch record and not the map output; equal for every
    worker count (labels are computed on the engine thread at prepare and commit, never by a worker)."""
    body = LLMNode(prompt="say {{item.text}} {{item.extra}}", output_key="said")
    items = [{"text": f"i{k}", "extra": ""} for k in range(4)]

    def resp(req):
        return req.messages[-1].content.upper()
    out = {}
    for w in (1, 4):
        store = EvidenceStore(tmp_path / f"lab-{w}")
        eng = Engine(_spec(body), backends={"executor": MockBackend(resp)}, store=store, map_workers=w)
        eng.taint["input"] = {"target-items"}
        eng.taint["_seed"] = {"target-unrendered"}
        eng.releases = _Rec()
        res = eng.run("t", {"items": items})
        assert res.status == RunStatus.COMPLETED, res.detail
        recs = store.records_where("map_branch", "run_id", res.run_id)
        out[w] = ([r["taint"] for r in recs], sorted(eng.taint["out"]), [t for _, t in eng.releases.calls])
        store.close()
    assert out[1] == out[4]
    assert out[4][0] == [["target-items"]] * 4  # each branch: the items' label, nothing more
    assert "target-unrendered" not in out[4][1]


# ------------------------------------------------------------------------------------------- crash and resume
def test_kill9_mid_parallel_map_then_resume_charges_every_call_once(tmp_path):
    store_dir, marker = tmp_path / "store", tmp_path / "marker"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.Popen([sys.executable, str(HERE / "_map_parallel_helpers.py"), str(store_dir), str(marker)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        while not marker.exists():
            assert proc.poll() is None, proc.stderr.read().decode()
            assert time.time() < deadline, "child never reached the in-flight branch"
            time.sleep(0.05)
        time.sleep(0.5)  # let the other in-flight branches (x3, x4) finish their calls: journalled, not committed
    finally:
        proc.kill()  # TerminateProcess / SIGKILL: no cleanup runs
        proc.wait()
    store = EvidenceStore(store_dir)
    run = store.list_runs(1)[0]
    assert store.run_status(run.id)[0] == RunStatus.RUNNING
    before = store.records_where("map_branch", "run_id", run.id)
    assert [r["index"] for r in before] == [0, 1]  # committed in item order: x2 blocked, so nothing past it

    eng = Engine(H.spec(), backends=H.backends(None), store=store, map_workers=H.WORKERS)
    res = eng.resume(run.id)
    assert res.status == RunStatus.COMPLETED, res.detail
    trace = store.trace(run.id)
    assert [s.node_id for s in trace] == ["a", "m", "d"]
    m = trace[1].output
    assert m["reused_branches"] == 2 and [b["reused"] for b in m["branches"]] == [True, True, False, False, False]
    assert [b["branch_id"] for b in m["branches"][:2]] == [r["branch_id"] for r in before]
    assert res.state["results"] == [f"done: work on {i}" for i in H.ITEMS]
    # exact: every model call ever made for this run (both processes, journalled write-ahead) is charged exactly once
    journal = sum((Cost.model_validate(j["cost"]) for j in store.records_where("cost_journal", "run_id", run.id)),
                  Cost())
    recorded = sum((s.cost for s in trace), Cost())
    assert _tokens(recorded) == _tokens(journal)
    assert _tokens(eng.budget.spent) == _tokens(recorded)
    # the finished-but-uncommitted calls of the killed process (x3, x4 when they finished) were recovered as lost
    assert 0 <= m["recovered_lost_calls"] <= 2
    store.close()


# ------------------------------------------------------------------------------------------- run context in workers
def test_a_parallel_branch_call_runs_in_the_run_context(tmp_path):
    """Review finding (s4-wiring__review): ThreadPoolExecutor does not copy contextvars, so a worker saw no active run
    and no read-capture lists; a private read or nested run made in-process during the call would have been
    attributed differently from the sequential path (fail open). Each branch now runs in a copy of the engine
    thread's context. Fail-first: before the fix the parallel run records None for every branch."""
    from custom_harness.runtime import engine as E
    seen: dict[int, list] = {1: [], 4: []}

    def run(w):
        def resp(req):
            seen[w].append((E._ACTIVE_RUN.get(), len(E._CAPTURE.get())))
            return req.messages[-1].content.upper()
        store = EvidenceStore(tmp_path / f"ctx-{w}")
        res = Engine(_spec(), backends={"executor": MockBackend(resp)}, store=store, map_workers=w).run(
            "t", {"items": ITEMS})
        store.close()
        return res.run_id
    rid1, rid4 = run(1), run(4)
    assert seen[1] == [(rid1, 1)] * len(ITEMS)
    assert seen[4] == [(rid4, 1)] * len(ITEMS)


class _BadJsonThenDown(Backend):
    """Call 1: invalid JSON (spent, journalled); call 2 (the contract retry): a non-transient provider failure."""
    name, provider, mode, training_on_outputs = "flaky2", "mock", AccessMode.MOCK, "allowed"

    def __init__(self):
        self.n = 0

    def complete(self, req: ChatRequest) -> ChatResponse:
        self.n += 1
        if self.n == 1:
            return ChatResponse(text="not json", cost=Cost(prompt_tokens=3, completion_tokens=2),
                                provenance=self.provenance("m"))
        from custom_harness.gateway import BackendError
        raise BackendError("provider down", transient=False)


@pytest.mark.parametrize("workers", [1, 4])
def test_a_contract_retry_call_spent_before_a_provider_failure_is_charged(tmp_path, workers):
    """Generalised from the review (pre-existing on both paths): `_call_llm` dropped the cost of an earlier
    contract-retry call when a later call of the same attempt raised; its nonce was still marked charged, so no
    resume recovered it. Fail-first: before the fix the step cost was 0 while the journal held 5 tokens."""
    body = LLMNode(prompt="say {{item}}", output_key="said", output_schema={"type": "object"}, retries=1)
    store = EvidenceStore(tmp_path / f"partial-{workers}")
    res = Engine(_spec(body), backends={"executor": _BadJsonThenDown()}, store=store, map_workers=workers,
                 retry=RetryPolicy(max_attempts=1)).run("t", {"items": ["i0"]})
    [row] = store.trace(res.run_id)
    journal = sum((Cost.model_validate(j["cost"]) for j in store.records_where("cost_journal", "run_id",
                                                                              res.run_id)), Cost())
    assert _tokens(journal) == 5 and _tokens(row.cost) == 5
    store.close()
