"""Child process for test_campaign_budget.py: a two-run campaign. Run 1 completes; run 2 is hard-killed by the parent
inside its step `c` (after one journalled model call)."""

import sys
from pathlib import Path

import _crash_helpers as H
from custom_harness.budget import Campaign
from custom_harness.runtime import Edge, Engine, HarnessSpec, LLMNode
from custom_harness.store import EvidenceStore

CAPS = {"tokens": 10_000}


def first_spec() -> HarnessSpec:
    return HarnessSpec(id="camp-first", entry="x", nodes={"x": LLMNode(prompt="warm up please", output_key="x"),
                                                         "y": LLMNode(prompt="and again", output_key="y")},
                       edges=[Edge(source="x", target="y")])


def first_engine(store: EvidenceStore, campaign: Campaign) -> Engine:
    return Engine(first_spec(), backends=H.backends(), store=store, campaign=campaign)


def second_engine(store: EvidenceStore, ws: str, marker: str, campaign: Campaign) -> Engine:
    return Engine(H.spec(marker), backends=H.backends(), store=store,
                  sandbox=H.LocalSandbox(Path(ws), i_understand_no_isolation=True), campaign=campaign)


if __name__ == "__main__":
    store_dir, ws, marker = sys.argv[1:4]
    store = EvidenceStore(Path(store_dir))
    camp = Campaign(store, "camp", CAPS)
    first_engine(store, camp).run("t1", {})
    second_engine(store, ws, marker, camp).run("t2", {"q": 1})
