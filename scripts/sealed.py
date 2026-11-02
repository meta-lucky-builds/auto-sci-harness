"""Dev and sealed halves of the verifier's own validation sets (GOAL §16.6, invariant 20).

    python scripts/sealed.py --list museum --half dev          # item ids of one half
    python scripts/sealed.py --look museum --by NAME --reason "milestone M3 check"   # ledgered look, then run it
    python scripts/sealed.py --check                           # the look ledger is an intact hash chain (gate)

Sets:
  claim-suite    the 51 scenarios of tests/test_kernel_suite.py (by scenario name)
  museum         the findings with a museum patch (redteam/findings.yaml)
  honest-corpus  the honest corpus (GOAL §16.6; built by scripts/honest_corpus.py): item ids from the committed
                 manifest redteam/honest_corpus/manifest.json. Its halves are assigned per PAIR (an honest item and
                 its planted-fault counterpart share a half), so the manifest's `half` field decides; the sealed
                 items' bundles, ledgers and expected verdicts live outside the repository (custom-harness-data/assure)

Membership is a pure function of the item id (the pair id for the honest corpus) and a fixed salt
(sha256(salt | set | id), low bit), so an item's half never changes and new items are assigned on arrival. Every look
at a sealed half is appended to `redteam/sealed_looks.jsonl` BEFORE the items run (who, why, which items, the git
HEAD), and its result after; each line carries the hash of the previous line, and `--check` (in check.py --fast)
fails on a broken chain. The ledger code lives in custom_harness/assure/looks.py (the resweep screen uses it too).

Honest limits (stated, not solved):
- Sealing here is ACCOUNTING, not secrecy (owner decision 2026-10-08): the sealed items are files on this machine (in
  the repository for the claim suite and the museum; under custom-harness-data for the honest corpus), readable by
  any agent, and the honest corpus's sealed half can be regenerated from the committed builder. Real secrecy needs
  the human-held check (docs/governance.md, scripts/human_check.py).
- The routine gate still runs every claim-suite scenario and every museum patch: removing the sealed halves from
  the gate would weaken an existing check, which this workstream may not do. Activating the seal (gate runs the dev
  half only) is an owner decision (needs owner).
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # this checkout's code, never another checkout's editable install
from custom_harness.assure import looks as _looks  # noqa: E402

SALT = _looks.SALT
LOOKS = "redteam/sealed_looks.jsonl"
CORPUS_MANIFEST = "redteam/honest_corpus/manifest.json"
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
        path = root / CORPUS_MANIFEST
        if not path.exists():
            return []
        return sorted(str(it["id"]) for it in json.loads(path.read_text(encoding="utf-8"))["items"])
    raise SystemExit(f"sealed: unknown set {name!r}; sets: {SETS}")


def half_of(name: str, item: str) -> str:
    return _looks.half_of(name, item)


def split(name: str, root: Path = ROOT) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"dev": [], "sealed": []}
    if name == "honest-corpus":  # halves per pair, recorded in the manifest (tests check it against half_of)
        path = root / CORPUS_MANIFEST
        if path.exists():
            for it in json.loads(path.read_text(encoding="utf-8"))["items"]:
                out[str(it["half"])].append(str(it["id"]))
        return {h: sorted(v) for h, v in out.items()}
    for it in items(name, root):
        out[half_of(name, it)].append(it)
    return out


def check_chain(root: Path = ROOT) -> list[str]:
    return _looks.check_chain(root / LOOKS, LOOKS)


def append(rec: dict, root: Path = ROOT) -> None:
    _looks.append(root / LOOKS, rec)


def look(name: str, by: str, reason: str, root: Path = ROOT, run: bool = True) -> int:
    if not by.strip() or not reason.strip():
        print("sealed: a look needs --by and --reason", flush=True)
        return 1
    problems = check_chain(root)
    if problems:
        print(f"sealed: refusing to look: {problems}", flush=True)
        return 1
    if name == "honest-corpus" and run:  # its command ledgers its own look (with the items it actually reads)
        cmd = [sys.executable, "scripts/honest_corpus.py", "--check", "--half", "sealed", "--by", by, "--reason",
               reason, "--looks", str(root / LOOKS)]
        res = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
        tail = (res.stdout.strip().splitlines() or [""])[-1]
        print(tail, flush=True)
        return res.returncode
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
