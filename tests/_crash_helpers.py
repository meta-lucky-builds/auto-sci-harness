"""Shared by test_resume.py and the child process it hard-kills (modules must be registered in both)."""

import sys
import time
from pathlib import Path

from custom_harness.gateway import MockBackend
from custom_harness.runtime import Edge, Engine, HarnessSpec, LLMNode, ModuleNode, register
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore


@register("crash.write")
def _write(state, params, ctx):
    (ctx.sandbox.workspace / "f.txt").write_text("step-a")
    return {"wrote": True}


@register("crash.wait")
def _wait(state, params, ctx):
    """First time: announce, then block until killed. After a resume the marker exists: continue."""
    marker = Path(params["marker"])
    if not marker.exists():
        marker.write_text("in flight")
        time.sleep(300)
    return {"seen": (ctx.sandbox.workspace / "f.txt").read_text(), "junk_present":
            (ctx.sandbox.workspace / "junk.txt").exists()}


def spec(marker: str) -> HarnessSpec:
    return HarnessSpec(id="crashy", entry="a", nodes={
        "a": ModuleNode(module="crash.write"),
        "b": LLMNode(prompt="first question", output_key="b"),
        "c": ModuleNode(module="crash.wait", params={"marker": marker}),
        "d": LLMNode(prompt="second question", output_key="d")},
        edges=[Edge(source="a", target="b"), Edge(source="b", target="c"), Edge(source="c", target="d")])


def backends():
    return {"executor": MockBackend(lambda req: "answer to " + req.messages[-1].content)}


def engine(store_dir: str, ws: str, marker: str) -> Engine:
    return Engine(spec(marker), backends=backends(), store=EvidenceStore(Path(store_dir)),
                  sandbox=LocalSandbox(Path(ws), i_understand_no_isolation=True))


if __name__ == "__main__":
    engine(*sys.argv[1:4]).run("t", {"q": 1})
