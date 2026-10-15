"""Child process for test_campaign_wallclock.py: the same two-run campaign as _campaign_crash.py, opened with time caps
(summed step time wall_s and calendar time elapsed_s). Run 1 completes; run 2 is hard-killed by the parent inside its
step `c` (after one journalled model call)."""

import sys
from pathlib import Path

import _campaign_crash as C
from custom_harness.budget import Campaign
from custom_harness.store import EvidenceStore

CAPS = {"tokens": 10_000, "wall_s": 3600.0, "elapsed_s": 3600.0}
CAMPAIGN_ID = "campw"

if __name__ == "__main__":
    store_dir, ws, marker = sys.argv[1:4]
    store = EvidenceStore(Path(store_dir))
    camp = Campaign(store, CAMPAIGN_ID, CAPS)  # real clock: opened_at is time.time() in this process
    C.first_engine(store, camp).run("t1", {})
    C.second_engine(store, ws, marker, camp).run("t2", {"q": 1})
