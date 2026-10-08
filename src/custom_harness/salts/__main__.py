"""HUMAN command: reveal a sealed salt campaign on or after its reveal date.

    python -m custom_harness.salts reveal SEALED_FILE --by NAME

Checks the sealed plant list against the commitment in the Evidence store's ledger, ledgers the reveal, and prints
the plant list. Refused before the reveal date (taken from the system clock; there is no override).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys


def main(argv: list[str] | None = None) -> int:
    from ..config import data_dir
    from ..kernel import Kernel
    from ..store import EvidenceStore
    from .generate import SaltError
    from .plan import reveal

    p = argparse.ArgumentParser(prog="python -m custom_harness.salts")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reveal", help="HUMAN: open a sealed salt campaign on/after its reveal date")
    r.add_argument("sealed")
    r.add_argument("--by", required=True, help="the human revealing (recorded in the ledger)")
    a = p.parse_args(argv)
    store = EvidenceStore(data_dir() / "store")
    try:
        revealed = reveal(a.sealed, Kernel(store).vault, human=a.by, today=dt.date.today())
    except SaltError as e:
        raise SystemExit(f"refused: {e}") from None
    finally:
        store.close()
    print(json.dumps({"campaign_hash": revealed.campaign_hash, "plant_list": list(revealed.plant_list)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
