"""Dev and sealed halves of the verifier's own validation sets (GOAL §16.6, invariant 20).

    python scripts/sealed.py --list museum --half dev          # item ids of one half
    python scripts/sealed.py --look museum --by NAME --reason "milestone M3 check"   # ledgered look, then run it
    python scripts/sealed.py --check                           # the look ledger is an intact hash chain (gate)

Sets:
  claim-suite    the 51 scenarios of tests/test_kernel_suite.py (by scenario name)
  museum         the findings with a museum patch (redteam/findings.yaml)
  honest-corpus  NOT BUILT: no honest-claim corpus exists yet (placeholder; the split rule is ready for it)

Membership is a pure function of the item id and a fixed salt (sha256(salt | set | id), low bit), so an item's half
never changes and new items are assigned on arrival. Every look at a sealed half is appended to
`redteam/sealed_looks.jsonl` BEFORE the items run (who, why, which items, the git HEAD), and its result after;
each line carries the hash of the previous line, and `--check` (in check.py --fast) fails on a broken chain.

Honest limits (stated, not solved):
- Sealing here is ACCOUNTING, not secrecy: the sealed items are files in the repository, readable by any agent. Real
  secrecy needs the human-held check (docs/governance.md).
- The routine gate still runs every claim-suite scenario and every museum patch: removing the sealed halves from
  the gate would weaken an existing check, which this workstream may not do. Activating the seal (gate runs the dev
  half only) is an owner decision (needs owner).
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SALT = "chh-validation-split-2026-10-07"
LOOKS = "redteam/sealed_looks.jsonl"
SETS = ("claim-suite", "museum", "honest-corpus")


def items(name: str, root: Path = ROOT) -> list[str]:
    if name == "claim-suite":
        tree = ast.parse((root / "tests/test_kernel_suite.py").read_text(encoding="utf-8"))
        return sorted(n.name for n in tree.body if isinstance(n, ast.FunctionDef) and any(
            isinstance(d, ast.Call) and getattr(d.func, "id", "") == "scenario" for d in n.decorator_list))
    if name == "museum":
        doc = yaml.safe_load((root / "redteam/findings.yaml").read_text(encoding="utf-8"))
        return sorted(str(f["id"]) for f in doc["findings"] if f.get("museum"))
    if name == "honest-corpus":
        return []
    raise SystemExit(f"sealed: unknown set {name!r}; sets: {SETS}")


def half_of(name: str, item: str) -> str:
    return "sealed" if hashlib.sha256(f"{SALT}|{name}|{item}".encode()).digest()[0] & 1 else "dev"


def split(name: str, root: Path = ROOT) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"dev": [], "sealed": []}
    for it in items(name, root):
        out[half_of(name, it)].append(it)
    return out


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()


def check_chain(root: Path = ROOT) -> list[str]:
    path = root / LOOKS
    if not path.exists():
        return []
    problems, prev = [], "genesis"
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{LOOKS}:{i}: not JSON")
            break
        if rec.get("prev") != prev:
            problems.append(f"{LOOKS}:{i}: chain broken (an earlier look was edited or deleted)")
        if rec.get("kind") == "look" and not (str(rec.get("by", "")).strip() and str(rec.get("reason", "")).strip()):
            problems.append(f"{LOOKS}:{i}: a look must name who looked and why")
        prev = _line_hash(line)
    return problems


def append(rec: dict, root: Path = ROOT) -> None:
    path = root / LOOKS
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rec = {**rec, "prev": _line_hash(lines[-1]) if lines else "genesis"}
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def look(name: str, by: str, reason: str, root: Path = ROOT, run: bool = True) -> int:
    if not by.strip() or not reason.strip():
        print("sealed: a look needs --by and --reason", flush=True)
        return 1
    problems = check_chain(root)
    if problems:
        print(f"sealed: refusing to look: {problems}", flush=True)
        return 1
    sealed = split(name, root)["sealed"]
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                          check=False).stdout.strip()
    append({"kind": "look", "set": name, "items": sealed, "by": by, "reason": reason, "head": head,
            "at": dt.datetime.now(dt.UTC).isoformat()}, root)
    if not run or not sealed:
        append({"kind": "result", "set": name, "ran": False, "n": len(sealed)}, root)
        return 0
    if name == "museum":
        cmd = [sys.executable, "scripts/museum.py", "--only", *sealed]
    else:
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_kernel_suite.py", "-k",
               " or ".join(sealed)]
    res = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    tail = (res.stdout.strip().splitlines() or [""])[-1]
    append({"kind": "result", "set": name, "ran": True, "returncode": res.returncode, "summary": tail}, root)
    print(tail, flush=True)
    return res.returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", choices=SETS)
    ap.add_argument("--half", choices=("dev", "sealed"), default="dev")
    ap.add_argument("--look", choices=SETS)
    ap.add_argument("--by", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.list:
        print("\n".join(split(a.list)[a.half]))
        return 0
    if a.look:
        return look(a.look, a.by, a.reason)
    problems = check_chain()
    for p in problems:
        print(f"sealed: {p}", flush=True)
    sizes = {s: {h: len(v) for h, v in split(s).items()} for s in SETS}
    print(f"sealed: look ledger {'FAILED' if problems else 'OK'}; halves {sizes}", flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
