"""Publication-style figure assets for LAB figures (OpenResearch orx-figures, MIT; assets/openresearch/NOTICE).

`vendor_into(workspace)` copies the style module, the offline chart template and the TikZ preamble into a sandbox
workspace, so a Lab figure script can `from orx_figstyle import ...` there (the assets' own advice: vendor the file
next to the figure scripts so the figure stays reproducible). The assets are never imported by the harness itself
(they need matplotlib/numpy, which only the sandbox image has).

Court figures do NOT use these assets: a Court figure is rendered by the trusted SVG renderer (figures/svg.py) from
certified evidence and re-rendered by figures/audit.py (invariant 8). OR only asks its figure scripts for "real
evidence" and does not check it (PORTMAP §5). Built, NOT test-verified.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..sandbox.base import safe_path

ASSET_DIR = Path(__file__).parent / "assets" / "openresearch"
ASSETS = ("orx_figstyle.py", "orx_chart.py", "orx-chart.html", "orx-tikz-preamble.tex", "NOTICE")


def vendor_into(workspace: Path, subdir: str = "figstyle") -> list[Path]:
    """Copy the assets (with their MIT NOTICE) into `workspace/subdir`; returns the written paths."""
    dest = safe_path(Path(workspace), subdir)
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in ASSETS:
        target = safe_path(Path(workspace), f"{subdir}/{name}")
        shutil.copyfile(ASSET_DIR / name, target)
        out.append(target)
    return out
