"""HUMAN command: reveal a sealed salt campaign on or after its reveal date.

    python -m custom_harness.salts reveal SEALED_FILE --by NAME

Checks the sealed plant list against the commitment in the Evidence store's ledger, ledgers the reveal, and prints
the plant list. Refused before the reveal date (taken from the system clock; there is no override).
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    """Parses its own arguments, then runs `chh salts-reveal` (the main CLI is the one place that opens the store:
    tests/test_kerneld_boundary.py)."""
    from ..cli import main as chh

    p = argparse.ArgumentParser(prog="python -m custom_harness.salts")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reveal", help="HUMAN: open a sealed salt campaign on/after its reveal date")
    r.add_argument("sealed")
    r.add_argument("--by", required=True, help="the human revealing (recorded in the ledger)")
    a = p.parse_args(argv)
    return chh(["salts-reveal", a.sealed, "--by", a.by])


if __name__ == "__main__":
    sys.exit(main())
