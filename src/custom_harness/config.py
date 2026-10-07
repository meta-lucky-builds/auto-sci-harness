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
