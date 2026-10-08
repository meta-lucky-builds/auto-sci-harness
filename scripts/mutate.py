"""Stdlib AST mutation testing of the Kernel's decision code (GOAL §16.6 mutation gate). No mutmut, no download.

    python scripts/mutate.py --map            # 1. per-test line coverage of the targets (coverage contexts), cached
    python scripts/mutate.py --list           # the mutants that would run
    python scripts/mutate.py --jobs 2         # 2. run every mutant on the tests that cover its line
    python scripts/mutate.py --report         # score from the last run, with redteam/equivalent.yaml applied

Targets (GOAL §16.6): kernel/vault.py, kernel/ledger.py, and in kernel/checks.py the K2/K4/K6 code
(k2_preregistered, k4_statistics, _criterion, k6_held_out, related_targets, shared_examples, is_external).

Operators: comparison swaps (< <=, > >=, == !=, in / not in, is / is not), and/or swap, `not` removal, integer
constant +1, True/False swap, + / - swap, deletion of a statement that appends a reason / continues / raises
(replaced by `pass`), and `if` condition forced True or False. A mutant is KILLED when at least one covering test
fails (or the run times out), SURVIVED when they all pass, NO-COVER when no test executes its line.
Score = killed / (all mutants - justified equivalents). Equivalent mutants live in redteam/equivalent.yaml with a
written reason each; the mutant id is file:line:col:operator.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
KDIR = "src/custom_harness/kernel"
TARGETS: dict[str, tuple[str, ...] | None] = {  # file -> function names (None = the whole file)
    f"{KDIR}/vault.py": None,
    f"{KDIR}/ledger.py": None,
    f"{KDIR}/checks.py": ("k2_preregistered", "k4_statistics", "_criterion", "k6_held_out", "related_targets",
                          "shared_examples", "is_external"),
}
TESTS = ["tests/spec/test_spec_mutants.py", "tests/test_kernel_units.py",
         "tests/test_kernel_bundle.py", "tests/test_kernel_suite.py",
         "tests/test_kernel_exploits.py", "tests/test_kernel_integrity.py", "tests/test_kernel_sim.py",
         "tests/test_kernel_formal.py", "tests/test_kernel_quality.py", "tests/test_review_kernel.py",
         "tests/test_redteam_kernel.py", "tests/spec/test_spec_kernel.py", "tests/spec/test_spec_gate.py"]
OUT = ROOT / ".mutation"
EQUIV = ROOT / "redteam" / "equivalent.yaml"
_CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
             ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}


@dataclass(frozen=True)
class Mutant:
    file: str
    line: int
    col: int
    end_line: int
    end_col: int
    op: str
    replacement: str

    @property
    def id(self) -> str:
        return f"{self.file}:{self.line}:{self.col}:{self.op}"


def _ranges(tree: ast.Module, names: tuple[str, ...] | None) -> list[tuple[int, int]]:
    if names is None:
        return [(1, 10**9)]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out.append((node.lineno, node.end_lineno or node.lineno))
    missing = set(names) - {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    if missing:
        raise SystemExit(f"mutate: target functions not found: {sorted(missing)}")
    return out


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def mutants_of(rel: str, names: tuple[str, ...] | None) -> list[Mutant]:
    src = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    ranges = _ranges(tree, names)
    doc = _docstring_nodes(tree)
    out: list[Mutant] = []

    def add(node: ast.AST, op: str, new: ast.AST | str) -> None:
        text = new if isinstance(new, str) else ast.unparse(new)
        out.append(Mutant(rel, node.lineno, node.col_offset, node.end_lineno or node.lineno,  # type: ignore[attr-defined]
                          node.end_col_offset or 0, op, f"({text})" if not isinstance(new, str) else text))  # type: ignore[attr-defined]

    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None or not any(a <= line <= b for a, b in ranges):
            continue
        if isinstance(node, ast.Compare):
            for i, o in enumerate(node.ops):
                if type(o) in _CMP_SWAP:
                    m = ast.Compare(left=node.left, ops=[*node.ops[:i], _CMP_SWAP[type(o)](), *node.ops[i + 1:]],
                                    comparators=node.comparators)
                    add(node, f"cmp{i}:{type(o).__name__}->{_CMP_SWAP[type(o)].__name__}", m)
        elif isinstance(node, ast.BoolOp):
            add(node, "and<->or", ast.BoolOp(op=ast.Or() if isinstance(node.op, ast.And) else ast.And(),
                                             values=node.values))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            add(node, "drop-not", node.operand)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            add(node, "+<->-", ast.BinOp(left=node.left, op=ast.Sub() if isinstance(node.op, ast.Add) else ast.Add(),
                                         right=node.right))
        elif isinstance(node, ast.Constant) and id(node) not in doc:
            if isinstance(node.value, bool):
                add(node, "bool-flip", ast.Constant(not node.value))
            elif isinstance(node.value, int):
                add(node, "int+1", ast.Constant(node.value + 1))
        elif isinstance(node, ast.If):
            add(node.test, "if-True", "True")
            add(node.test, "if-False", "False")
        elif isinstance(node, (ast.Continue, ast.Raise)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr in ("append", "add")):
            add(node, "stmt->pass", "pass")
    seen, uniq = set(), []
    for m in sorted(out, key=lambda m: (m.line, m.col, m.op)):
        if m.id not in seen:
            seen.add(m.id)
            uniq.append(m)
    return uniq


def all_mutants() -> list[Mutant]:
    return [m for rel, names in TARGETS.items() for m in mutants_of(rel, names)]


def apply(src: str, m: Mutant) -> str:
    lines = src.splitlines(keepends=True)
    start = sum(len(x) for x in lines[:m.line - 1]) + len(lines[m.line - 1].encode()[:m.col].decode())
    end = sum(len(x) for x in lines[:m.end_line - 1]) + len(lines[m.end_line - 1].encode()[:m.end_col].decode())
    return src[:start] + m.replacement + src[end:]


# ------------------------------------------------------------------ coverage map
def build_map(python: str) -> None:
    OUT.mkdir(exist_ok=True)
    data = OUT / "cov.sqlite"
    env = {**os.environ, "COVERAGE_FILE": str(data), "CHH_SPEC_EXAMPLES": "10"}
    t0 = time.time()
    subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--hypothesis-seed=0",
                    "--cov=custom_harness.kernel", "--cov-context=test", "--cov-report=",
                    f"--junitxml={OUT / 'durations.xml'}", *TESTS], cwd=ROOT, env=env, check=False)
    import coverage  # test-only dependency already in the venv (pytest-cov)
    cov = coverage.Coverage(data_file=str(data))
    cov.load()
    db = cov.get_data()
    out: dict[str, dict[str, list[str]]] = {}
    for f in db.measured_files():
        rel = Path(f).resolve().as_posix()
        key = next((t for t in TARGETS if rel.endswith(t)), None)
        if key is None:
            continue
        ctx = db.contexts_by_lineno(f)
        out[key] = {str(ln): sorted({c.split("|")[0] for c in cs if c}) for ln, cs in ctx.items()}
    import xml.etree.ElementTree as ET
    dur = {f"{c.get('classname', '').replace('.', '/')}.py::{c.get('name')}": float(c.get("time", 0))
           for c in ET.parse(OUT / "durations.xml").getroot().iter("testcase")}
    (OUT / "map.json").write_text(json.dumps({"lines": out, "durations": dur, "secs": time.time() - t0}),
                                  encoding="utf-8")
    print(f"mutate: coverage map built in {time.time() - t0:.0f}s", flush=True)


KILLS: dict[str, int] = {}  # test id -> mutants it killed so far (tests that kill often run first: -x stops early)


def _covering(mp: dict, m: Mutant) -> list[str]:
    lines = mp["lines"].get(m.file, {})
    tests: set[str] = set()
    executed = False
    for ln in range(m.line, m.end_line + 1):
        executed |= str(ln) in lines
        tests |= set(lines.get(str(ln), []))
    if executed and not tests:  # ran only at import time (no test context): every test file may depend on it
        return list(TESTS)
    dur = mp["durations"]
    return sorted(tests, key=lambda t: (-KILLS.get(t, 0), dur.get(t.split("[")[0], dur.get(t, 99.0)), t))


# ------------------------------------------------------------------ running
def run_mutant(m: Mutant, tests: list[str], python: str) -> dict:
    if not tests:
        return {**asdict(m), "id": m.id, "verdict": "NO-COVER", "secs": 0.0}
    with tempfile.TemporaryDirectory(prefix="mut-") as tmp:
        work = Path(tmp)
        shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("__pycache__"))
        target = work / m.file
        target.write_text(apply(target.read_text(encoding="utf-8"), m), encoding="utf-8", newline="")
        try:
            compile(target.read_text(encoding="utf-8"), str(target), "exec")
        except SyntaxError as e:
            return {**asdict(m), "id": m.id, "verdict": "INVALID", "detail": str(e), "secs": 0.0}
        env = {**os.environ, "PYTHONPATH": str(work / "src"), "CHH_SPEC_EXAMPLES": "10"}
        t0 = time.time()
        try:
            res = subprocess.run([python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "--hypothesis-seed=0",
                                  *tests], cwd=ROOT, env=env, capture_output=True, text=True, timeout=1200,
                                 check=False)
            verdict = "SURVIVED" if res.returncode == 0 else "KILLED"
            tail = (res.stdout.strip().splitlines() or [""])[-1]
            killer = next((ln.split()[1] for ln in res.stdout.splitlines() if ln.startswith("FAILED ")), None)
        except subprocess.TimeoutExpired:
            verdict, tail, killer = "KILLED", "timeout", None
        return {**asdict(m), "id": m.id, "verdict": verdict, "detail": tail, "secs": round(time.time() - t0, 1),
                "n_tests": len(tests), "killer": killer}


def report() -> int:
    latest: dict[str, dict] = {}  # a re-run (--only) supersedes the earlier result of the same mutant
    for x in (OUT / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if x.strip():
            r = json.loads(x)
            latest[r["id"]] = r
    res = list(latest.values())
    eq = (yaml.safe_load(EQUIV.read_text(encoding="utf-8")) or {}).get("equivalent", []) if EQUIV.exists() else []
    eq_ids = {e["id"] for e in eq if str(e.get("reason", "")).strip()}
    valid = [r for r in res if r["verdict"] != "INVALID"]
    killed = [r for r in valid if r["verdict"] == "KILLED"]
    alive = [r for r in valid if r["verdict"] in ("SURVIVED", "NO-COVER") and r["id"] not in eq_ids]
    eqs = [r for r in valid if r["id"] in eq_ids and r["verdict"] != "KILLED"]
    denom = len(valid) - len(eqs)
    score = len(killed) / denom if denom else 0.0
    by_file: dict[str, list[int]] = {}
    for r in valid:
        k = by_file.setdefault(r["file"], [0, 0])
        k[0] += r["verdict"] == "KILLED"
        k[1] += r["id"] not in eq_ids or r["verdict"] == "KILLED"
    print(f"mutants: {len(res)} ({len(res) - len(valid)} invalid), killed {len(killed)}, equivalent {len(eqs)} "
          f"({len(eqs) / max(1, len(valid)):.1%}), alive {len(alive)}", flush=True)
    for f, (k, n) in by_file.items():
        print(f"  {f}: {k}/{n} = {k / max(1, n):.1%}", flush=True)
    print(f"mutation score = {score:.1%} (GOAL §16.6 target >= 90%, equivalents <= 10%)", flush=True)
    for r in alive:
        print(f"  ALIVE {r['verdict']:8s} {r['id']}  -> {r['replacement'][:70]}", flush=True)
    return 0 if score >= 0.9 and len(eqs) <= 0.1 * max(1, len(valid)) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--only", nargs="*", help="mutant ids (re-run these, appending to results)")
    ap.add_argument("--tests", nargs="*", help="with --only: run these extra tests too (e.g. a test written to "
                    "kill a survivor, not yet in the coverage map)")
    a = ap.parse_args(argv)
    if a.map:
        build_map(sys.executable)
        return 0
    if a.report:
        return report()
    ms = all_mutants()
    if a.list:
        for m in ms:
            print(m.id, "->", m.replacement[:80])
        print(f"{len(ms)} mutants", flush=True)
        return 0
    mp = json.loads((OUT / "map.json").read_text(encoding="utf-8"))
    done = set()
    results = OUT / "results.jsonl"
    if a.only:
        ms = [m for m in ms if m.id in set(a.only)]
    elif results.exists():
        done = {json.loads(x)["id"] for x in results.read_text(encoding="utf-8").splitlines() if x.strip()}
    todo = [m for m in ms if m.id not in done]
    if results.exists():
        for x in results.read_text(encoding="utf-8").splitlines():
            k = json.loads(x).get("killer") if x.strip() else None
            if k:
                KILLS[k] = KILLS.get(k, 0) + 1
    print(f"mutate: {len(todo)} mutants to run ({len(done)} already done)", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool, results.open("a", encoding="utf-8") as f:
        extra = list(a.tests or [])
        for r in pool.map(lambda m: run_mutant(m, extra + _covering(mp, m), sys.executable), todo):
            if r.get("killer"):
                KILLS[r["killer"]] = KILLS.get(r["killer"], 0) + 1
            f.write(json.dumps(r) + "\n")
            f.flush()
            print(f"{r['verdict']:9s} {r['id']} ({r.get('secs', 0)}s)", flush=True)
    return report()


if __name__ == "__main__":
    sys.exit(main())
