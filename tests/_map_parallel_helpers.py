"""Shared by test_runtime_map_parallel.py and the child process it hard-kills mid-fan-out (parallel LLM-body map)."""

import sys
import time
from pathlib import Path

from custom_harness.gateway import MockBackend
from custom_harness.runtime import Edge, Engine, HarnessSpec, LLMNode, MapNode
from custom_harness.store import EvidenceStore

ITEMS = ["x0", "x1", "x2", "x3", "x4"]
WORKERS = 4


def spec() -> HarnessSpec:
    return HarnessSpec(id="mapcrash-parallel", entry="a", nodes={
        "a": LLMNode(prompt="plan the work", output_key="plan"),
        "m": MapNode(items_key="input.items", body="w", output_key="results", reducer="collect"),
        "w": LLMNode(prompt="work on {{item}}", output_key="answer"),
        "d": LLMNode(prompt="summarise {{results}}", output_key="summary")},
        edges=[Edge(source="a", target="m"), Edge(source="m", target="d")])


def backends(marker: str | None = None):
    """The response depends on the request only. With `marker`: the call for x2 announces itself (marker file) and
    blocks until the process is killed, while the calls for the other items return at once."""

    def respond(req):
        text = req.messages[-1].content
        if marker is not None and text == "work on x2" and not Path(marker).exists():
            Path(marker).write_text("in flight")
            time.sleep(300)
        return "done: " + text[:40]
    return {"executor": MockBackend(respond)}


def engine(store_dir: str, marker: str | None, workers: int = WORKERS) -> Engine:
    return Engine(spec(), backends=backends(marker), store=EvidenceStore(Path(store_dir)), map_workers=workers,
                  allow_same_family=True)


if __name__ == "__main__":
    engine(sys.argv[1], sys.argv[2]).run("t", {"items": ITEMS})
