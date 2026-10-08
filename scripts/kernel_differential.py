"""Differential test of the Evidence Kernel: does a refactor change any verdict or reason? (docs/kernel_differential.md)

Run from the TREE UNDER TEST as working directory (locked_run.py sets cwd and PYTHONPATH=<tree>/src), with this
script and its helpers taken from the HARNESS tree (this checkout), so OLD and NEW are recorded by the same code:

    locked_run.py <TREE> <LOG> script <HARNESS>/scripts/kernel_differential.py suite --out old_suite.jsonl
    locked_run.py <TREE> <LOG> script <HARNESS>/scripts/kernel_differential.py histories --out old_hist.jsonl --n 40
    python <HARNESS>/scripts/kernel_differential.py diff old_suite.jsonl new_suite.jsonl   # exit 1 on any difference

Subcommands:
  suite      run the Kernel tests (tests/test_kernel_*.py of the tree under test, unchanged) with the recorder plugin
  histories  build N seeded random histories through the public Kernel API and record every Kernel decision
  diff       normalise both recordings (rules in tests/differential/normalise.py) and print every difference
  explain    for two RAW recordings of the SAME commit: which normalisation rule removes which raw difference
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent
DIFFERENTIAL = HARNESS / "tests" / "differential"


def _check_tree() -> str:
    """The Kernel under test must be imported from <cwd>/src (the PYTHONPATH trap: the venv's editable install points
    at the main checkout)."""
    import custom_harness

    got = Path(custom_harness.__file__).resolve()
    want = (Path.cwd() / "src" / "custom_harness").resolve()
    if want not in got.parents:
        sys.exit(f"kernel_differential: custom_harness imported from {got}, not from the tree under test {want}; "
                 f"run through locked_run.py (it sets PYTHONPATH=<tree>/src)")
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "src"], capture_output=True, text=True,
                           check=False).stdout.strip()
    return f"{head}{' (src modified)' if dirty else ''}"


def cmd_suite(a: argparse.Namespace) -> int:
    head = _check_tree()
    sys.path.insert(0, str(DIFFERENTIAL))
    import kernel_recorder
    import pytest

    files = sorted(glob.glob("tests/test_kernel_*.py"))
    if a.k:
        files = [f for f in files if any(Path(f).stem == s or Path(f).name == s for s in a.k)]
    os.environ[kernel_recorder.ENV] = str(Path(a.out).resolve())
    print(f"kernel_differential suite: tree HEAD {head}; files {files}", flush=True)
    args = ["-p", "no:cacheprovider", "-o", "faulthandler_timeout=900", "-q", "-rfE", *files, *a.pytest_args]
    return int(pytest.main(args, plugins=[kernel_recorder]))


def cmd_histories(a: argparse.Namespace) -> int:
    head = _check_tree()
    sys.path.insert(0, str(DIFFERENTIAL))
    sys.path.insert(1, str(HARNESS / "tests"))  # kernel_world.py of the HARNESS tree: identical for OLD and NEW
    import histories

    return histories.main(out=Path(a.out), n=a.n, seed=a.seed, head=head, only=a.only)


def cmd_diff(a: argparse.Namespace) -> int:
    sys.path.insert(0, str(DIFFERENTIAL))
    import normalise

    return normalise.diff_files(Path(a.old), Path(a.new), raw=a.raw, intersect=a.intersect, limit=a.limit,
                                mask_adapter_hash=not a.keep_adapter_hash,
                                flaky_files=[Path(f) for f in a.flaky or []])


def cmd_explain(a: argparse.Namespace) -> int:
    sys.path.insert(0, str(DIFFERENTIAL))
    import normalise

    return normalise.explain(Path(a.run1), Path(a.run2), Path(a.flaky_out) if a.flaky_out else None)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("suite")
    s.add_argument("--out", required=True)
    s.add_argument("-k", action="append", help="only this test file (stem or name); repeatable (demo subsets)")
    s.add_argument("pytest_args", nargs="*")
    h = sub.add_parser("histories")
    h.add_argument("--out", required=True)
    h.add_argument("--n", type=int, default=40)
    h.add_argument("--seed", type=int, default=20261008)
    h.add_argument("--only", action="append", help="only histories of this kind (repeatable)")
    d = sub.add_parser("diff")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--raw", action="store_true", help="no normalisation at all")
    d.add_argument("--keep-adapter-hash", action="store_true", help="do not mask adapters[].hash")
    d.add_argument("--intersect", action="store_true",
                   help="compare only scopes present on both sides (for a demo on a subset; never for the real check)")
    d.add_argument("--limit", type=int, default=0, help="print at most this many differences (0 = all)")
    d.add_argument("--flaky", action="append", help="mask the cells listed by `explain --flaky-out` (repeatable)")
    e = sub.add_parser("explain")
    e.add_argument("run1")
    e.add_argument("run2")
    e.add_argument("--flaky-out", help="write the cells still differing after all rules (to pass to diff --flaky)")
    a = p.parse_args()
    return {"suite": cmd_suite, "histories": cmd_histories, "diff": cmd_diff, "explain": cmd_explain}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
