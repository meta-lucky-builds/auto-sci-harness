"""Paths and settings. Runtime data lives outside the repo (and outside OneDrive)."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Root for DB, blobs, sandboxes. Override with CHH_DATA_DIR."""
    env = os.environ.get("CHH_DATA_DIR")
    p = Path(env) if env else Path.home() / "custom-harness-data"  # home is only consulted when needed
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------- reproducibility artifacts and figures (GOAL §5 L2)
# Sizes are bytes. None of these changes claim policy; they bound what a bundle holds and what a reader accepts.
LARGE_ARTIFACT_BYTES = 64 * 2**20  # UNCALIBRATED (owner decision, GOAL §13.8): files over this are listed, not bundled
RUN_BUNDLE_MANIFEST_BYTES = 256 * 2**20  # UNCALIBRATED (owner decision, GOAL §13.8): run bundle manifest.json cap
RUN_BUNDLE_MEMBER_BYTES = 512 * 2**20  # UNCALIBRATED (owner decision, GOAL §13.8): cap on any other bundle member
RUN_BUNDLE_TOTAL_BYTES = 4 * 2**30  # UNCALIBRATED (owner decision, GOAL §13.8): cap on all members read
RUN_BUNDLE_MEMBERS = 100_000  # UNCALIBRATED (owner decision, GOAL §13.8): cap on the member count
RUN_BUNDLE_RATIO = 200.0  # UNCALIBRATED (owner decision, GOAL §13.8): max ratio, members over 8 MiB
FIGURE_MAX_BYTES = 8 * 2**20  # UNCALIBRATED (owner decision, GOAL §13.8): cap per figure file
FIGURE_RENDER_TIMEOUT_S = 120.0  # UNCALIBRATED (owner decision, GOAL §13.8): one figure re-render


# ---------------------------------------------------------------- L6.9 branch/route, L7 islands (GOAL §5)
# Branches (evolve/branches.py). A cluster is tested only with this many gate tasks: a one-task cluster would make its
# verdict bit a per-task gate value.
BRANCH_MIN_CLUSTER_TASKS = 2  # UNCALIBRATED (owner decision, GOAL §13.8)
BRANCH_MIN_UNITS = 2  # UNCALIBRATED (owner decision, GOAL §13.8): valid paired units a cluster test needs
# family-wise level over ONE candidate's cluster tests (Bonferroni: each cluster at alpha / n_tested clusters)
BRANCH_ALPHA = 0.05  # UNCALIBRATED (owner decision, GOAL §13.8)
BRANCH_MAX_EXCLUDED_FRAC = 0.25  # UNCALIBRATED (owner decision, GOAL §13.8): as EvolveConfig.max_excluded_frac
# Islands orchestrator (control/islands.py); the policy object carries these as its versioned defaults
ISLAND_STALL_WINDOW = 3  # UNCALIBRATED (owner decision, GOAL §13.8): generations without an accept = stalled
ISLAND_MORE_EVAL_SHARE = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8): share of gated rejections on the
#   (underpowered) evolve re-measurement at which a stalled island gets more evaluation instead of new edits
ISLAND_STALL_SHRINK = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8): budget weight of an island stalled on edits
ISLAND_MIN_SHARE = 0.1  # UNCALIBRATED (owner decision, GOAL §13.8): no island is starved below this budget share
ISLAND_PROMPT_MAX_SHARE = 0.4  # UNCALIBRATED (owner decision, GOAL §13.8): edit-type quota on prompt-only edits
ISLAND_MIN_EDIT_TYPES = 2  # UNCALIBRATED (owner decision, GOAL §13.8): distinct edit types per planned batch
ISLAND_POLICY_CADENCE = 5  # UNCALIBRATED (owner decision, GOAL §13.8): orchestrator rounds between policy changes
