"""Shared by test_taint_flow.py and the child process it hard-kills (modules must be registered in both).

Run `held` reads a private file (its output `score` is target-derived), `wait` blocks until killed, `ask` renders
`{{score}}` into a prompt."""

import sys
import time
from pathlib import Path

from custom_harness.gateway import MockBackend
from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.runtime import Edge, Engine, HarnessSpec, LLMNode, ModuleNode, register
from custom_harness.store import EvidenceStore

TARGET = "tk-held"


@register("taintk.held")
def _held(state, params, ctx):
    vault = ctx.kernel().vault
    if TARGET not in vault.datasets():
        vault.register_dataset(TARGET, DatasetRole.TEST, public={"x.csv": b"x\n1\n"}, private={"y.csv": b"y\n1\n"})
    labels = vault.read(TARGET, "y.csv", purpose="leakscan", run_id=ctx.run_id).decode().split()[1:]
    return {"score": f"held-out mean {sum(map(float, labels)) / len(labels):.4f}"}


@register("taintk.wait")
def _wait(state, params, ctx):
    marker = Path(params["marker"])
    if not marker.exists():
        marker.write_text("in flight")
        time.sleep(300)
    return {"waited": True}


def spec(marker: str) -> HarnessSpec:
    return HarnessSpec(id="taintk", entry="held", nodes={
        "held": ModuleNode(module="taintk.held"),
        "wait": ModuleNode(module="taintk.wait", params={"marker": marker}),
        "ask": LLMNode(prompt="Result so far: {{score}}", output_key="answer")},
        edges=[Edge(source="held", target="wait"), Edge(source="wait", target="ask")])


def backends():
    return {"executor": MockBackend(lambda req: "ok")}


def engine(store_dir: str, marker: str) -> Engine:
    store = EvidenceStore(Path(store_dir))
    Kernel(store).approve_adapter("ml", approver="pytest")
    return Engine(spec(marker), backends=backends(), store=store)


if __name__ == "__main__":
    engine(*sys.argv[1:3]).run("t", {})
