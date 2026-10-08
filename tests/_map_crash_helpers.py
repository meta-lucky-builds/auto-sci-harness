"""Shared by test_runtime_map.py and the child process it hard-kills mid-fan-out (modules registered in both)."""

import sys
import time
from pathlib import Path

from custom_harness.gateway import ChatRequest, Message, MockBackend
from custom_harness.runtime import Edge, Engine, HarnessSpec, LLMNode, MapNode, ModuleNode, register
from custom_harness.store import EvidenceStore

ITEMS = ["x0", "x1", "x2", "x3"]


@register("mapcrash.work")
def _work(state, params, ctx):
    """One model call per branch. On the item named by params['stop'], the first time (no marker yet): announce and
    block until killed, either before the branch's model call or after it (params['when'])."""
    item, marker = state["item"], Path(params["marker"])

    def block():
        if item == params["stop"] and not marker.exists():
            marker.write_text("in flight")
            time.sleep(300)

    if params["when"] == "before":
        block()
    resp = ctx.llm("executor", ChatRequest(messages=[Message(role="user", content=f"work on {item}")]))
    if params["when"] == "after":
        block()
    return {"item": item, "answer": resp.text}


def spec(marker: str, when: str) -> HarnessSpec:
    return HarnessSpec(id=f"mapcrash-{when}", entry="a", nodes={
        "a": LLMNode(prompt="plan the work", output_key="plan"),
        "m": MapNode(items_key="input.items", body="w", output_key="results", reducer="collect"),
        "w": ModuleNode(module="mapcrash.work", params={"marker": marker, "stop": "x2", "when": when}),
        "d": LLMNode(prompt="summarise {{results}}", output_key="summary")},
        edges=[Edge(source="a", target="m"), Edge(source="m", target="d")])


def backends():
    return {"executor": MockBackend(lambda req: "done: " + req.messages[-1].content[:40])}


def engine(store_dir: str, marker: str, when: str) -> Engine:
    return Engine(spec(marker, when), backends=backends(), store=EvidenceStore(Path(store_dir)))


if __name__ == "__main__":
    engine(sys.argv[1], sys.argv[2], sys.argv[3]).run("t", {"items": ITEMS})
