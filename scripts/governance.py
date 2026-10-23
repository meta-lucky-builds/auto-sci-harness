"""Two-speed Kernel check (GOAL §16.7, invariant 22).

The Kernel CORE (the §10 invariants, the §4.3 K0-K9 pass conditions, and the Kernel source with its parameter list
blanked out) changes only (a) in a scheduled window after a 7-day cooling-off, or (b) at once when the change only
tightens checks. The PARAMETER LIST (`KernelConfig` defaults in kernel/checks.py) changes on evidence and is not part
of the core fingerprint.

    python scripts/governance.py                     # fail unless the current core fingerprint has a valid record
    python scripts/governance.py --fingerprint       # print the current fingerprint
    python scripts/governance.py --record --kind window --proposed 2026-10-07 --approver NAME --note "..."
    python scripts/governance.py --record --kind tighten --approver NAME --note "..."
    python scripts/governance.py --record --kind delegated --approver NAME --delegated-by OWNER --basis "GOAL §..." \
        --change "..." [--change "..."] --note "..."

A `delegated` record (s3-kernel suggestion, added by s4-kernel) is an owner-DELEGATED core change made without the
cooling-off: it satisfies the current fingerprint's requirement but always states `cooling_off_observed: false`, who
delegated (`delegated_by`), the `basis` and the `changes`, so it is never mistaken for a tightening or a window (the
earlier tighten-plus-deviation pattern classed such changes wrongly first). The owner reviews it and may replace it by
a window record.

Records live in `redteam/kernel_core_changes.yaml`. A `deviation` record (appended, never edited in place)
corrects an earlier misclassified record and states whether the cooling-off was observed; it never satisfies the
current core's requirement. What this check CAN enforce: no core change reaches the gate
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


def _deviation_problems(recs: list[dict]) -> list[str]:
    """`deviation` records (owner decision 2026-10-08): an appended correction of an EARLIER record that misclassified
    a core change (history is never rewritten). Required: `corrects` = the fingerprint of an earlier non-deviation
    record, a non-empty `changes` list, `cooling_off_observed` as a bool (False = a recorded deviation from §16.7),
    a `basis` and an `approver`."""
    out = []
    for i, r in enumerate(recs):
        if r.get("kind") != "deviation":
            continue
        tag = f"deviation record #{i}"
        earlier = {e.get("fingerprint") for e in recs[:i] if e.get("kind") != "deviation"}
        if r.get("corrects") not in earlier:
            out.append(f"{tag}: 'corrects' must name the fingerprint of an earlier record")
        changes = r.get("changes")
        if not isinstance(changes, list) or not changes or not all(isinstance(c, str) and c.strip() for c in changes):
            out.append(f"{tag}: 'changes' must be a non-empty list of descriptions")
        if not isinstance(r.get("cooling_off_observed"), bool):
            out.append(f"{tag}: 'cooling_off_observed' must be true or false")
        for key in ("basis", "approver"):
            if not str(r.get(key, "")).strip():
                out.append(f"{tag}: names no {key}")
    return out


def _delegated_problems(r: dict, fp: str) -> list[str]:
    """A `delegated` record names who delegated, a basis and a non-empty list of changes, and admits the cooling-off
    was not observed (it cannot claim otherwise)."""
    out = [f"record {fp[:16]}: a delegated record names no {k}" for k in ("delegated_by", "basis")
           if not str(r.get(k, "")).strip()]
    changes = r.get("changes")
    if not isinstance(changes, list) or not changes or not all(isinstance(c, str) and c.strip() for c in changes):
        out.append(f"record {fp[:16]}: a delegated record needs a non-empty 'changes' list")
    if r.get("cooling_off_observed") is not False:
        out.append(f"record {fp[:16]}: a delegated record must state cooling_off_observed: false")
    return out


def problems(root: Path = ROOT, today: dt.date | None = None) -> list[str]:
    path = root / RECORDS
    recs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("changes", []) if path.exists() else []
    fp = fingerprint(root)
    out = _deviation_problems(recs)
    # a deviation record annotates an EARLIER record; it never stands in for the record the current core needs
    mine = [r for r in recs if r.get("fingerprint") == fp and r.get("kind") != "deviation"]
    if not mine:
        return out + [f"the Kernel core changed (fingerprint {fp[:16]}) with no record in {RECORDS}: a core change "
                      f"needs a scheduled-window record ({COOLING_DAYS}-day cooling-off) or a tighten-only record "
                      f"(`python scripts/governance.py --record ...`)"]
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
        elif r.get("kind") == "delegated":
            out += _delegated_problems(r, fp)
        elif r.get("kind") not in ("tighten", "baseline"):
            out.append(f"record {fp[:16]}: kind {r.get('kind')!r} is not 'window', 'tighten' or 'delegated'")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fingerprint", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--kind", choices=("window", "tighten", "delegated"))
    ap.add_argument("--delegated-by", default="")
    ap.add_argument("--basis", default="")
    ap.add_argument("--change", action="append", default=[])
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
        if a.kind == "delegated":
            rec |= {"delegated_by": a.delegated_by, "basis": a.basis, "changes": a.change,
                    "cooling_off_observed": False}
            bad = _delegated_problems(rec, rec["fingerprint"])
            if bad:
                print("governance: " + "; ".join(bad), flush=True)
                return 1
        if path.exists() and doc["changes"]:  # append only: earlier records keep their bytes (history not rewritten)
            with path.open("a", encoding="utf-8", newline="") as f:
                f.write(yaml.safe_dump([rec], sort_keys=False, width=120))
        else:
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
