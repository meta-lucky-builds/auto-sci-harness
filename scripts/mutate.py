"""Stdlib AST mutation testing of the Kernel's decision code (GOAL §16.6 mutation gate). No mutmut, no download.

    python scripts/mutate.py --map            # 1. per-test line coverage of the targets (coverage contexts), cached
    python scripts/mutate.py --list           # the mutants that would run
    python scripts/mutate.py --jobs 2         # 2. run every mutant on the tests that cover its line
    python scripts/mutate.py --report         # score from the last run, with redteam/equivalent.yaml applied
    python scripts/mutate.py --out DIR --budget-s 1500 --jobs 1   # one resumable slice of at most ~25 min

Targets (GOAL §16.6): kernel/vault.py, kernel/ledger.py, and in kernel/checks.py the K2/K4/K6 code
(k2_preregistered, k4_statistics, k6_held_out, related_targets, shared_examples, is_external), and since the Kernel 1.3
core/adapter split kernel/ml.py statistics, criterion (was checks._criterion) and plan_code_divergence.
Since the §16 Kernel 1.2 work, K4 and K6 also decide through helpers and two new files; these are targets too
(`GROWN`, reported apart so the original score stays comparable): registration, family, releases_on,
registration_level, fixed_terms, fixed, target_level in checks.py, plan_code_divergence (now in ml.py), and
kernel/alpha.py and kernel/releases.py whole.

The test list is the hand list below UNION the gate's derived Kernel test list (`check.kernel_tests()`,
tests/test_kernel_*.py): a Kernel test file that is missing from a hand list would silently leave the mutation map
(the same lesson as the gate's derived list, GOAL §16.6).

Slicing (s3-assure, 2026-10-08): `--out` keeps the map and the results outside the checkout so slices run from any
snapshot of the same Kernel code; `--budget-s` stops starting new mutants after that many seconds (a running
mutant finishes), and the next slice resumes from results.jsonl. The map records the hash of the target files and
refuses to run against other Kernel code.

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
import hashlib
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
LEGACY_CHECKS = ("k2_preregistered", "k4_statistics", "k6_held_out", "related_targets",
                 "shared_examples", "is_external")
GROWN_CHECKS = ("registration", "family", "releases_on", "registration_level", "fixed_terms", "fixed",
                "target_level", "plan_code_divergence")
# Kernel 1.3 core/adapter split (s3-kernel, docs/kernel_core_adapter_split.md): K4's statistics, `_criterion` (now
# `criterion`) and plan_code_divergence moved verbatim from checks.py to the ML adapter's procedures, kernel/ml.py.
# int17 merge: the targets follow the code (otherwise _ranges refuses the missing names and no mutant runs).
ML_LEGACY = ("statistics", "criterion")
TARGETS: dict[str, tuple[str, ...] | None] = {  # file -> function names (None = the whole file)
    f"{KDIR}/vault.py": None,
    f"{KDIR}/ledger.py": None,
    f"{KDIR}/checks.py": (*LEGACY_CHECKS, *(n for n in GROWN_CHECKS if n != "plan_code_divergence")),
    f"{KDIR}/ml.py": (*ML_LEGACY, "plan_code_divergence"),
    f"{KDIR}/alpha.py": None,
    f"{KDIR}/releases.py": None,
}
GROWN_FILES = (f"{KDIR}/alpha.py", f"{KDIR}/releases.py")
HAND_TESTS = ["tests/spec/test_spec_mutants.py", "tests/test_kernel_units.py",
         "tests/test_kernel_bundle.py", "tests/test_kernel_suite.py",
         "tests/test_kernel_exploits.py", "tests/test_kernel_integrity.py", "tests/test_kernel_sim.py",
         "tests/test_kernel_formal.py", "tests/test_kernel_quality.py", "tests/test_review_kernel.py",
         "tests/test_redteam_kernel.py", "tests/spec/test_spec_kernel.py", "tests/spec/test_spec_gate.py"]


def derived_tests(root: Path = ROOT) -> list[str]:
    """HAND_TESTS plus every Kernel test file the gate derives (check.kernel_tests), sorted, without duplicates."""
    sys.path.insert(0, str(root / "scripts"))
    try:
        from check import kernel_tests
    finally:
        sys.path.pop(0)
    return sorted(set(HAND_TESTS) | set(kernel_tests(root)))


TESTS = derived_tests()
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
    grown: bool = False  # a target added after the 2026-10-07 baseline (GROWN_CHECKS / GROWN_FILES)

    @property
    def id(self) -> str:
        return f"{self.file}:{self.line}:{self.col}:{self.op}"


def _ranges(tree: ast.Module, names: tuple[str, ...] | None) -> list[tuple[int, int, str]]:
    if names is None:
        return [(1, 10**9, "")]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out.append((node.lineno, node.end_lineno or node.lineno, node.name))
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
        line = node.lineno  # type: ignore[attr-defined]
        func = next((f for a, b, f in ranges if a <= line <= b), "")
        grown = rel in GROWN_FILES or func in GROWN_CHECKS
        out.append(Mutant(rel, node.lineno, node.col_offset, node.end_lineno or node.lineno,  # type: ignore[attr-defined]
                          node.end_col_offset or 0, op, f"({text})" if not isinstance(new, str) else text,  # type: ignore[attr-defined]
                          grown))

    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line is None or not any(a <= line <= b for a, b, _ in ranges):
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
def code_hash(root: Path = ROOT) -> str:
    """sha256 over the target files' bytes: a map or a result set belongs to exactly this Kernel code."""
    h = hashlib.sha256()
    for rel in sorted(TARGETS):
        h.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return h.hexdigest()


def build_map(python: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
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
    (OUT / "map.json").write_text(json.dumps({"lines": out, "durations": dur, "secs": time.time() - t0,
                                              "code_hash": code_hash(), "tests": TESTS}), encoding="utf-8")
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
    current = {m.id: m for m in all_mutants()}
    latest: dict[str, dict] = {}  # a re-run (--only) supersedes the earlier result of the same mutant
    for x in (OUT / "results.jsonl").read_text(encoding="utf-8").splitlines():
        if x.strip():
            r = json.loads(x)
            if r["id"] in current:  # a result for a mutant that no longer exists (other code) never counts
                latest[r["id"]] = {**r, "grown": current[r["id"]].grown}
    res = list(latest.values())
    missing = len(current) - len(res)
    if missing:
        print(f"mutate: INCOMPLETE: {missing} of {len(current)} mutants have no result yet (score below is partial)",
              flush=True)
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
    for name, grown in (("original targets (2026-10-07 set)", False),
                        ("grown targets (K4/K6 helpers, alpha.py, releases.py)", True)):
        part = [r for r in valid if bool(r.get("grown")) == grown]
        k = sum(r["verdict"] == "KILLED" for r in part)
        n = sum(r["id"] not in eq_ids or r["verdict"] == "KILLED" for r in part)
        print(f"  {name}: {k}/{n} = {k / max(1, n):.1%}", flush=True)
    print(f"mutation score = {score:.1%} (GOAL §16.6 target >= 90%, equivalents <= 10%)", flush=True)
    for r in alive:
        print(f"  ALIVE {r['verdict']:8s} {r['id']}  -> {r['replacement'][:70]}", flush=True)
    return 0 if score >= 0.9 and len(eqs) <= 0.1 * max(1, len(valid)) and not missing else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--only", nargs="*", help="mutant ids (re-run these, appending to results)")
    ap.add_argument("--tests", nargs="*", help="with --only: run these extra tests too (e.g. a test written to "
                    "kill a survivor, not yet in the coverage map)")
    ap.add_argument("--out", help="map and results directory (default: <checkout>/.mutation)")
    ap.add_argument("--budget-s", type=float, default=None, help="start no new mutant after this many seconds")
    a = ap.parse_args(argv)
    global OUT
    if a.out:
        OUT = Path(a.out).resolve()
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
    if mp.get("code_hash") != code_hash():
        print(f"mutate: the coverage map in {OUT} was built for other Kernel code (code_hash "
              f"{str(mp.get('code_hash'))[:12]} != {code_hash()[:12]}): rebuild it with --map", flush=True)
        return 1
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
    t_start = time.time()
    extra = list(a.tests or [])

    def one(m: Mutant) -> dict | None:
        if a.budget_s is not None and time.time() - t_start > a.budget_s:
            return None  # budget spent: the next slice resumes here
        return run_mutant(m, extra + _covering(mp, m), sys.executable)

    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool, results.open("a", encoding="utf-8") as f:
        for r in pool.map(one, todo):
            if r is None:
                continue
            if r.get("killer"):
                KILLS[r["killer"]] = KILLS.get(r["killer"], 0) + 1
            f.write(json.dumps(r) + "\n")
            f.flush()
            print(f"{r['verdict']:9s} {r['id']} ({r.get('secs', 0)}s)", flush=True)
    print(f"mutate: slice ended after {time.time() - t_start:.0f}s", flush=True)
    return report()


if __name__ == "__main__":
    sys.exit(main())
