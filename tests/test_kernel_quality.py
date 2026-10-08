"""Structural guarantees of the Evidence Kernel (GOAL §4.1, §5, invariant 3):
no model call can be reached from Kernel code, the Kernel stays small, and it type-checks strictly."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KERNEL = ROOT / "src" / "custom_harness" / "kernel"
FORBIDDEN = ("custom_harness.gateway", "custom_harness.runtime", "custom_harness.research", "custom_harness.evolve",
             "custom_harness.cli", "httpx", "openai", "anthropic")


def test_no_model_call_is_reachable_from_the_kernel():
    """Import the Kernel (and everything it uses) in a clean interpreter, then list every loaded module:
    nothing that can talk to a model may be among them, directly or transitively."""
    code = ("import sys, json, custom_harness.kernel, custom_harness.kernel.bundle, "
            "custom_harness.kernel.predictions, custom_harness.kernel.checklist; "
            "print(json.dumps(sorted(sys.modules)))")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                       env={"PYTHONPATH": str(ROOT / "src"), **{k: v for k, v in __import__("os").environ.items()
                                                                 if k != "PYTHONPATH"}})
    loaded = json.loads(p.stdout)
    bad = [m for m in loaded if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)]
    assert bad == [], f"Kernel import closure reaches model-capable code: {bad}"


def test_kernel_is_small():
    """GOAL §4.1: a small trusted core (target < 4,000 lines of code, excluding blanks and comment lines). A ratchet:
    raised from 3,000 on 2026-10-08 by an owner decision (§16 features; core split (§16.6) deferred); lowering it
    again is the goal, raising it needs another owner decision."""
    loc = 0
    for f in KERNEL.glob("*.py"):
        for line in f.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                loc += 1
    assert loc < 4000, loc


@pytest.mark.parametrize("tool", ["mypy", "ruff"])
def test_static_gates(tool):
    if tool == "mypy":
        cmd = [sys.executable, "-m", "mypy", "--strict", str(KERNEL)]
    else:
        cmd = [sys.executable, "-m", "ruff", "check", str(ROOT / "src"), str(ROOT / "tests")]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=600)
    except FileNotFoundError:  # pragma: no cover
        pytest.skip(f"{tool} not installed")
    if "No module named" in p.stderr:
        pytest.skip(f"{tool} not installed in this environment")
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-2000:]
