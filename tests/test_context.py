"""Context management (SoL-Pi ObservationPack; GOAL L4): nothing truncated is lost, reduced logs quote
verbatim."""

from __future__ import annotations

from custom_harness.gateway import MockBackend
from custom_harness.runtime import Engine, HarnessSpec, LLMNode
from custom_harness.runtime.template import reduce_log, render, truncate


def test_truncation_archives_the_full_value():
    store = {}

    def archive(text):
        store["h"] = text
        return "f" * 64
    out = truncate("A" * 100 + "B" * 100, 40, 0.5, archive)
    assert out.startswith("A" * 20) and out.endswith("B" * 20) and "archived as ffffffffffffffff" in out
    assert store["h"] == "A" * 100 + "B" * 100
    assert truncate("short", 40) == "short"


def test_log_reducer_keeps_error_lines_verbatim_and_the_tail():
    lines = [f"step {i} ok" for i in range(200)]
    lines[50] = 'File "solution.py", line 12, in fit'
    lines[51] = "ValueError: could not convert string to float: 'red'"
    text = "\n".join(lines)
    out = reduce_log(text, 600)
    body = out.split("\n", 1)[1]
    kept = [ln for ln in body.splitlines() if not ln.startswith("...[")]
    assert all(ln in lines for ln in kept)  # every kept line is a verbatim original line
    assert lines[51] in kept and lines[199] in kept and "step 10 ok" not in kept
    assert out.startswith("[log reduced to") and len(out) <= 700
    assert reduce_log("tiny", 600) == "tiny"


def test_log_reducer_falls_back_to_raw_head_tail_when_reduction_is_too_long():
    noisy = "\n".join(f"Error {i}: " + "x" * 50 for i in range(500))
    out = reduce_log(noisy, 300)
    assert "chars omitted" in out and not out.startswith("[log reduced")


def test_engine_records_archived_handles(store):
    spec = HarnessSpec(id="ctx", entry="a", nodes={"a": LLMNode(prompt="{{input.big}} / {{input.log|log}}",
                                                                output_key="o")},
                       context={"max_value_chars": 100, "head_fraction": 0.5})
    res = Engine(spec, backends={"executor": MockBackend(["ok"])}, store=store).run(
        "t", {"big": "z" * 1000, "log": "\n".join(["line"] * 300 + ["RuntimeError: boom"])})
    step = store.trace(res.run_id)[0]
    assert step.output["archived"] and all(store.blobs.has(h) for h in step.output["archived"])
    assert store.blobs.get(step.output["archived"][0]).decode() == "z" * 1000
    assert "RuntimeError: boom" in step.output["messages"][-1]["content"]
    assert render("{{x}}", {"x": "y"}) == "y"
