"""Two-speed Kernel check (GOAL §16.7, invariant 22).

The Kernel CORE (the §10 invariants, the §4.3 K0-K9 pass conditions, and the Kernel source with its parameter list
blanked out) changes only (a) in a scheduled window after a 7-day cooling-off, or (b) at once when the change only
tightens checks. The PARAMETER LIST (`KernelConfig` defaults in kernel/checks.py) changes on evidence and is not part
of the core fingerprint.

    python scripts/governance.py                     # fail unless the current core fingerprint has a valid record
    python scripts/governance.py --fingerprint       # print the current fingerprint
    python scripts/governance.py --record --kind window --proposed 2026-10-07 --approver NAME --note "..."
    python scripts/governance.py --record --kind tighten --approver NAME --note "..."

Records live in `redteam/kernel_core_changes.yaml`. What this check CAN enforce: no core change reaches the gate
without a record bound to its exact fingerprint; a `window` record is at least 7 days after its proposal date; every
record names an approver. What it CANNOT enforce (stated, not solved): that the approver is a human, and that a
`tighten` change really only tightens. The museum and the mutation gate are the evidence for the latter; the
human-held check (docs/governance.md) is the only answer to the former.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KERNEL = "src/custom_harness/kernel"
RECORDS = "redteam/kernel_core_changes.yaml"
COOLING_DAYS = 7
PARAMETER_CLASS = "KernelConfig"


def _goal_sections(text: str) -> str:
    """GOAL §10 (invariants) and the §4.3 table (K0-K9 pass conditions)."""
    inv = re.search(r"^## 10\. .*?(?=^## 11\. )", text, re.S | re.M)
    k = re.search(r"^### 4\.3 .*?(?=^### 4\.4 )", text, re.S | re.M)
    if not inv or not k:
        raise SystemExit("governance: GOAL.md §10 or §4.3 not found (fail closed)")
    return inv.group(0) + k.group(0)


def _strip_parameters(src: str) -> str:
    """Blank the default values of the parameter list so that changing a parameter does not change the core."""
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == PARAMETER_CLASS:
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and stmt.value is not None and stmt.end_lineno is not None:
                    for i in range(stmt.lineno - 1, stmt.end_lineno):
                        lines[i] = ""
                    lines[stmt.lineno - 1] = f"{ast.unparse(stmt.target)}: {ast.unparse(stmt.annotation)} = <param>"
    return "\n".join(lines)


def fingerprint(root: Path = ROOT) -> str:
    h = hashlib.sha256()
    h.update(_goal_sections((root / "GOAL.md").read_text(encoding="utf-8")).encode())
    for f in sorted((root / KERNEL).glob("*.py")):
        h.update(f.name.encode() + b"\0" + _strip_parameters(f.read_text(encoding="utf-8")).encode() + b"\0")
    return h.hexdigest()


def problems(root: Path = ROOT, today: dt.date | None = None) -> list[str]:
    path = root / RECORDS
    recs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("changes", []) if path.exists() else []
    fp = fingerprint(root)
    mine = [r for r in recs if r.get("fingerprint") == fp]
    if not mine:
        return [f"the Kernel core changed (fingerprint {fp[:16]}) with no record in {RECORDS}: a core change needs "
                f"a scheduled-window record ({COOLING_DAYS}-day cooling-off) or a tighten-only record "
                f"(`python scripts/governance.py --record ...`)"]
    out = []
    for r in mine:
        if not str(r.get("approver", "")).strip():
            out.append(f"record {fp[:16]}: names no approver")
        if r.get("kind") == "window":
            try:
                proposed = dt.date.fromisoformat(str(r["proposed"]))
                merged = dt.date.fromisoformat(str(r["recorded"]))
            except (KeyError, ValueError):
                out.append(f"record {fp[:16]}: a window record needs ISO dates 'proposed' and 'recorded'")
                continue
            if (merged - proposed).days < COOLING_DAYS:
                out.append(f"record {fp[:16]}: recorded {merged} is less than {COOLING_DAYS} days after the "
                           f"proposal {proposed} (cooling-off)")
            if today is not None and merged > today:
                out.append(f"record {fp[:16]}: recorded in the future ({merged})")
        elif r.get("kind") == "baseline" and recs.index(r) != 0:
            out.append(f"record {fp[:16]}: only the first record may be the 'baseline'")
        elif r.get("kind") not in ("tighten", "baseline"):
            out.append(f"record {fp[:16]}: kind {r.get('kind')!r} is neither 'window' nor 'tighten'")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fingerprint", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--kind", choices=("window", "tighten"))
    ap.add_argument("--proposed")
    ap.add_argument("--approver", default="")
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    if a.fingerprint:
        print(fingerprint())
        return 0
    if a.record:
        if not a.kind or not a.approver.strip():
            print("governance: --record needs --kind and --approver", flush=True)
            return 1
        path = ROOT / RECORDS
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
        doc = doc or {"changes": []}
        rec = {"fingerprint": fingerprint(), "kind": a.kind, "approver": a.approver, "note": a.note,
               "recorded": dt.date.today().isoformat()}
        if a.proposed:
            rec["proposed"] = a.proposed
        doc["changes"].append(rec)
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="")
        print(f"governance: recorded {rec}", flush=True)
        return 0
    found = problems(today=dt.date.today())
    for p in found:
        print(f"governance: {p}", flush=True)
    print(f"governance: {'FAILED' if found else 'OK'} (core fingerprint {fingerprint()[:16]})", flush=True)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
