"""Paged recall wired into the research loop's debug path (GOAL L4 "Context management"; research/recall.py,
examples/research_v2.yaml: fix -> fix_recall -> fix_paged; research_v1 unchanged).

End to end on the real research_v2 spec with a scripted (mock) LLM and trusted stdlib scripts: a failing script writes
a long stderr log; run_candidate keeps it as `run_log`; the fix prompt renders it through the log reducer, which
archives it behind a 16-hex handle. The fixer asks for a page of that handle; fix_recall serves exactly that page,
charges it and traces it, and fix_paged writes the fix with the page in view. Refusals end to end: a forged handle,
another run's handle, a log carrying a canary (private) and a log archived after a private read of the run
(target-derived) are all refused, and a refused request makes no further model call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.kernel.vault import DatasetRole
from custom_harness.records import RunStatus
from custom_harness.research import recall as RC
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_research import KNN_RADIUS, critic_answers, rings_task  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[1]
SPEC_V1 = ROOT / "examples/research_v1.yaml"
SPEC_V2 = ROOT / "examples/research_v2.yaml"
HANDLE = re.compile(r"archived as ([0-9a-f]{16})")
FORGED = "0123456789abcdef"


def loud(tag: str, extra: str = "") -> str:
    """A script that fails after ~75k characters of stderr (longer than the context budget: archived)."""
    return (f"import sys\n# {tag}\nfor i in range(1500):\n"
            f"    print(f'note {{i:04d}}: solver diagnostic line {tag} {extra}', file=sys.stderr)\n"
            f"raise SystemExit('boom: ValueError: the real cause of {tag}')\n")


def _spec(path: Path) -> HarnessSpec:
    return HarnessSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class Script:
    """Scripted LLM for research_v2. `fixes` = what each successive `fix` call returns: (code, recall request or None,
    where a request is a function of the fix prompt). `on_implement` runs when the implement call is made."""

    def __init__(self, solutions: list[tuple[str, str]], fixes, on_implement=None):
        self.solutions, self.fixes, self.on_implement = solutions, list(fixes), on_implement
        self.fix_prompts: list[str] = []
        self.paged_prompts: list[str] = []

    def __call__(self, req):
        text = req.messages[-1].content
        if "Answer every critic question" in text:
            return critic_answers(text)
        if "Propose 5 DISTINCT hypotheses" in text:
            return json.dumps({"hypotheses": [{"id": hid, "idea": f"idea {hid}", "rationale": "r"}
                                              for hid, _ in self.solutions]})
        if "Implement EACH of these hypotheses" in text:
            if self.on_implement is not None:
                self.on_implement()
            return json.dumps({"solutions": [{"id": hid, "code": code, "notes": "n"} for hid, code in self.solutions]})
        if "Ask at most 5 short questions" in text:
            return json.dumps({"questions": ["What would falsify the radius hypothesis?"]})
        if "A script failed and you drafted a fix" in text:
            self.paged_prompts.append(text)
            return json.dumps({"code": KNN_RADIUS, "notes": "fixed with the recalled page in view"})
        if "This script failed" in text:
            self.fix_prompts.append(text)
            code, ask = self.fixes.pop(0) if self.fixes else (loud(f"later{len(self.fix_prompts)}"), None)
            out = {"code": code, "notes": "draft"}
            if ask is not None:
                out["recall"] = ask(text)
            return json.dumps(out)
        if "This script works and is the best so far" in text:
            return json.dumps({"code": KNN_RADIUS, "notes": "no real change"})
        if "Re-implement the method described below from scratch" in text:
            return json.dumps({"code": KNN_RADIUS, "notes": "replica"})
        raise AssertionError(f"unexpected prompt: {text[:200]}")


def _run(store, tmp_path, script, monkeypatch, name="rings"):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    llm = MockBackend(script)
    eng = Engine(_spec(SPEC_V2), backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm},
                 store=store, sandbox=LocalSandbox(tmp_path / f"ws-{name}", i_understand_no_isolation=True),
                 allow_same_family=True)
    return eng, llm


def _recall_steps(store, run_id):
    return [s for s in store.trace(run_id) if s.node_id == "fix_recall"]


def _own(text: str) -> dict:
    return {"handle": HANDLE.search(text).group(1), "page": 2}


# ======================================================================================== configuration
def test_recall_is_on_in_v2_and_off_in_v1():
    v1, v2 = _spec(SPEC_V1), _spec(SPEC_V2)
    assert not any(getattr(n, "module", None) == RC.RECALL_MODULE for n in v1.nodes.values())
    assert "run_log_chars" not in v1.nodes["run"].params and "{{run_error|log}}" in v1.nodes["fix"].prompt
    assert v2.nodes["fix_recall"].module == RC.RECALL_MODULE and v2.nodes["fix_recall"].params["agent"] == "fix"
    p = v2.nodes["fix_recall"].params
    # every recall cap holds in the wiring: page size, per-run count, and the page fits the context budget
    assert p["page_chars"] <= RC.MAX_PAGE_CHARS and p["max_recalls"] <= RC.MAX_RECALLS_PER_RUN
    assert p["page_chars"] + 300 <= v2.context.max_value_chars
    from custom_harness.research.modules import RUN_LOG_MAX_CHARS
    assert v2.nodes["run"].params["run_log_chars"] <= RUN_LOG_MAX_CHARS
    assert -(-RUN_LOG_MAX_CHARS // p["page_chars"]) <= RC.MAX_PAGES  # every page of the longest log is addressable


# ======================================================================================== served + forged
def test_a_fix_step_recalls_a_page_of_its_failed_runs_archived_log(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    script = Script([("h_loud", loud("first")), ("h_radius", KNN_RADIUS)],
                    fixes=[(loud("first", "draft1"), lambda t: {"handle": FORGED, "page": 1}),  # forged: refused
                           (loud("first", "draft2"), _own)])  # its own handle, page 2: served
    eng, llm = _run(store, tmp_path, script, monkeypatch)
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    steps = _recall_steps(store, res.run_id)
    assert [s.output["recall_log"]["code"] for s in steps] == ["not_visible", None]
    # the forged request was refused, charged nothing and made no further model call
    assert steps[0].output["recalled"]["refused"] == "not_visible" and not steps[0].cost.prompt_tokens
    served = steps[1].output["recall_log"]
    assert served["served"] is True and served["page"] == 2 and served["chars"] == 4000
    # exactly the right page of the log the fixer was shown (archived by the engine from ITS prompt)
    fix_step = [s for s in store.trace(res.run_id) if s.node_id == "fix"][1]
    assert served["digest"] in fix_step.output["archived"]
    full = store.blobs.get(served["digest"]).decode("utf-8")
    assert "the real cause of first" in full and len(full) > 20_000
    page = steps[1].output["recalled"]["text"]
    assert "\n".join(ln[2:] for ln in page.splitlines()[2:-1]) == full[4000:8000]
    # charged (estimated prompt tokens on the recall step, counted by the run budget) and traced
    assert steps[1].cost.prompt_tokens == 1000 and steps[1].cost.estimated
    # one fix_paged call, and it saw the page; the draft that asked was kept until then
    assert len(script.paged_prompts) == 1 and page in script.paged_prompts[0]
    assert res.state["best"]["dev_score"] is not None
    assert sum("A script failed and you drafted a fix" in r.messages[-1].content for r in llm.requests) == 1


# ======================================================================================== refusals end to end
def test_private_and_other_run_handles_are_refused_end_to_end(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    first = Script([("h_loud", loud("first")), ("h_radius", KNN_RADIUS)], fixes=[(loud("first", "d"), _own)])
    eng, _ = _run(store, tmp_path, first, monkeypatch, name="one")
    r1 = eng.run("rings", {"task_dir": str(rings_task)})
    other_handle = HANDLE.search(first.fix_prompts[0]).group(1)
    canary = (rings_task / "private" / "canary.txt").read_text(encoding="utf-8").strip()
    second = Script([("h_canary", loud("second", canary)), ("h_b", loud("b"))],
                    fixes=[(loud("second", canary + " d1"), _own),  # its own log carries the canary: private
                           (loud("second", canary + " d2"), lambda t: {"handle": other_handle, "page": 1})])
    eng, _ = _run(store, tmp_path, second, monkeypatch, name="two")
    r2 = eng.run("rings", {"task_dir": str(rings_task)})
    assert r1.status == RunStatus.COMPLETED and r2.status == RunStatus.COMPLETED, (r1.detail, r2.detail)
    assert other_handle not in "".join(second.fix_prompts[:2])  # run 1's handle was never shown to run 2
    codes = [s.output["recall_log"]["code"] for s in _recall_steps(store, r2.run_id)]
    assert codes == ["private", "not_visible"]
    assert second.paged_prompts == []  # no page reached a prompt
    for s in _recall_steps(store, r2.run_id):
        assert canary not in json.dumps(s.output) and not s.cost.prompt_tokens


def test_a_log_archived_after_a_private_read_of_the_run_is_target_derived(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    def private_read():  # a private (held-out) read of THIS run before the failing log is rendered
        kernel = Kernel(store)
        run_id = store.list_runs(1)[0].id
        test = [d for d, rec in kernel.vault.datasets().items() if rec.role == DatasetRole.TEST]
        kernel.vault.read(test[0], "test_y.csv", purpose="leakscan", run_id=run_id)

    script = Script([("h_loud", loud("third")), ("h_b", loud("b"))], fixes=[(loud("third", "d"), _own)],
                    on_implement=private_read)
    eng, _ = _run(store, tmp_path, script, monkeypatch)
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    steps = _recall_steps(store, res.run_id)
    assert [s.output["recall_log"]["code"] for s in steps] == ["target_derived"]
    assert steps[0].output["recalled"]["refused"] == "target_derived" and script.paged_prompts == []
