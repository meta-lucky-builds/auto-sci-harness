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

# ---------------------------------------------- relative judgment: tournament + judge panel (GOAL §5 L4.5, L3)
# The tournament only orders hypotheses for trial (a T1/T2 metric decides downstream); the judge panel's evidence is
# capped at T5 and may only downgrade. None of these values grants anything.
BT_RIDGE = 0.01  # UNCALIBRATED (owner decision, GOAL §13.8): prior precision on log-strengths (keeps fits finite)
BT_CI_LEVEL = 0.95  # UNCALIBRATED (owner decision, GOAL §13.8): level of the reported Bradley-Terry intervals
POSITION_BIAS_ALPHA = 0.01  # UNCALIBRATED (owner decision, GOAL §13.8): exact-binomial level, position bias
PANEL_MIN_FAMILIES = 3  # GOAL §5 L3 ("judge panel: >= 3 families"); not an owner-free choice
PANEL_MIN_LABELS = 50  # UNCALIBRATED (owner decision, GOAL §13.8): human-labelled pairs to validate a panel
PANEL_MIN_AGREEMENT = 0.75  # UNCALIBRATED (owner decision, GOAL §13.8): panel vs human agreement floor
PANEL_MIN_KAPPA = 0.5  # UNCALIBRATED (owner decision, GOAL §13.8): Cohen's kappa floor (panel vs human)
PANEL_MAX_ECE = 0.10  # UNCALIBRATED (owner decision, GOAL §13.8): expected calibration error ceiling
PANEL_CALIBRATION_BINS = 10  # UNCALIBRATED (owner decision, GOAL §13.8): equal-width bins for the ECE

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
