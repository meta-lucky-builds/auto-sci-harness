"""Human-held check scaffold CLI (GOAL invariant 22; docs/human_check.md). Output never includes check content.

    python scripts/human_check.py --status        # NOT MET until a human registers a check
    python scripts/human_check.py --verify        # hashes only (registry + redteam/human_checks.lock)
    python scripts/human_check.py --run           # verify, run each check (output discarded), ledger the result
    python scripts/human_check.py --check-ledger  # redteam/human_check_runs.jsonl is an intact hash chain
    python scripts/human_check.py --gate          # the check.py step (non-failing while nothing is registered)

Options: --repo-root PATH (default: this repository), --timeout SECONDS (default 1800, --run only).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The venv's editable install points at the MAIN checkout; use THIS repository's source.
sys.path.insert(0, str(ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    from custom_harness.assure import humancheck as hc

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    for flag in ("--status", "--verify", "--run", "--check-ledger", "--gate"):
        g.add_argument(flag, action="store_true")
    ap.add_argument("--repo-root", type=Path, default=ROOT)
    ap.add_argument("--timeout", type=float, default=hc.DEFAULT_TIMEOUT_S)
    a = ap.parse_args(argv)
    root = a.repo_root

    if a.check_ledger:
        ok, msg = hc.check_chain(root)
        print(msg)
        return 0 if ok else 1
    if a.gate:
        ok, lines = hc.gate(root)
        print("\n".join(lines))
        return 0 if ok else 1
    if a.status:
        st, notes = hc.status(root)
        print(f"human-held check: {st}")
        print("\n".join(notes))
        return 0 if st in ("LAST-RUN-PASSED", "REGISTERED-NOT-RUN", "NOT MET") else 1
    rep = hc.run_checks(root, timeout_s=a.timeout) if a.run else hc.verify_hashes(root)
    print(f"human-held check: {rep.status}")
    for p in rep.problems:
        print(f"  {p}")
    if rep.status == "NOT MET":
        print(hc.NOT_MET_LINE)
    return 0 if rep.status in ("OK", "NOT MET") else 1


if __name__ == "__main__":
    sys.exit(main())
