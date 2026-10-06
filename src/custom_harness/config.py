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
