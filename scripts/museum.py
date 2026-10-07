"""Loophole museum (GOAL §16.6, invariant 19): every patch-shaped red-team finding has a patch that RE-INTRODUCES it,
and at least one of the finding's tests must fail on the patched code.

    python scripts/museum.py                 # apply every museum patch in a scratch copy and run its tests
    python scripts/museum.py --only K7-gap   # one or more findings
    python scripts/museum.py --check-only    # static registry check only (fast; part of `check.py --fast`)
    python scripts/museum.py --jobs 2        # run two patched copies at once
    python scripts/museum.py --control       # run every finding's tests on the UNPATCHED copy (must all pass)
    python scripts/museum.py --registry F    # another registry file (e.g. a fragment being written)

The registry is `redteam/findings.yaml`; patches are `redteam/museum/<id>.patch` (git unified diffs against the repo
root). For each patched finding the runner copies the tracked tree to a scratch directory, applies the patch there
(never in the checkout), and runs the finding's tests with PYTHONPATH pointing at the copy. Outcomes:
  KILLED    at least one test failed: the tests still catch the loophole (required)
  SURVIVED  every test passed on the re-introduced loophole: the tests stopped attacking (gate failure)
  BROKEN    the patch no longer applies, or the tests could not be collected (gate failure: repair the patch)
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "redteam/findings.yaml"
CAPABILITIES = ("A0", "A1", "A2", "A3", "A4", "A5")
STATUSES = ("fixed", "open", "accepted")
TIMEOUT_S = 900


@dataclass
class Outcome:
    finding: str
    verdict: str  # KILLED / SURVIVED / BROKEN
    detail: str


def load(root: Path = ROOT, registry: str = REGISTRY) -> dict[str, Any]:
    with (root / registry).open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict) or not isinstance(doc.get("findings"), list):
        raise SystemExit(f"{registry}: expected a mapping with a 'findings' list")
    return doc


def _test_functions(path: Path) -> set[str]:
    """Names of module-level test functions and Test*-class methods (as 'Class::method')."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            out.add(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            out |= {f"{node.name}::{m.name}" for m in node.body
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name.startswith("test")}
    return out


def dangling_test_id(test_id: str, root: Path = ROOT) -> str | None:
    """None if `tests/x.py::name[param]` names an existing test function, else why not."""
    if "::" not in test_id:
        return f"{test_id}: not a node id (file::test)"
    file, name = test_id.split("::", 1)
    path = root / file
    if not path.is_file():
        return f"{test_id}: file {file} does not exist"
    base = name.split("[", 1)[0]
    if base not in _test_functions(path):
        return f"{test_id}: no test function {base!r} in {file}"
    return None


def check_registry(root: Path = ROOT, registry: str = REGISTRY) -> list[str]:
    """Static problems with the registry (empty = consistent). Cheap: no test is run."""
    problems: list[str] = []
    doc = load(root, registry)
    seen: set[str] = set()
    for i, f in enumerate(doc["findings"]):
        fid = str(f.get("id", f"#{i}"))
        if fid in seen:
            problems.append(f"{fid}: duplicate id")
        seen.add(fid)
        for key in ("invariant", "capability", "tests", "status"):
            if key not in f:
                problems.append(f"{fid}: missing {key!r}")
        if f.get("capability") not in CAPABILITIES:
            problems.append(f"{fid}: capability {f.get('capability')!r} not in {CAPABILITIES}")
        if f.get("status") not in STATUSES:
            problems.append(f"{fid}: status {f.get('status')!r} not in {STATUSES}")
        tests = f.get("tests") or []
        if f.get("status") == "fixed" and not tests:
            problems.append(f"{fid}: a fixed finding needs at least one test")
        for t in tests:
            why = dangling_test_id(str(t), root)
            if why:
                problems.append(f"{fid}: dangling test id {why}")
        patch, reason = f.get("museum"), f.get("not_patch_shaped")
        if f.get("status") == "fixed" and bool(patch) == bool(reason):
            problems.append(f"{fid}: a fixed finding needs exactly one of 'museum' (a patch) or 'not_patch_shaped' "
                            f"(the reason)")
        if patch and not (root / str(patch)).is_file():
            problems.append(f"{fid}: museum patch {patch} does not exist")
    if registry != REGISTRY:  # a fragment: patches of other fragments are not its business
        return problems
    on_disk = {p.stem for p in (root / "redteam" / "museum").glob("*.patch")}
    named = {Path(str(f["museum"])).stem for f in doc["findings"] if f.get("museum")}
    problems += [f"redteam/museum/{s}.patch is not referenced by any finding" for s in sorted(on_disk - named)]
    return problems


def _copy_tree(root: Path, dest: Path) -> None:
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, check=True,
                           capture_output=True, text=True).stdout.splitlines()
    for rel in files:
        src = root / rel
        if src.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def run_one(root: Path, finding: dict[str, Any], python: str, control: bool = False) -> Outcome:
    fid = str(finding["id"])
    patch = (root / str(finding["museum"])).resolve()
    with tempfile.TemporaryDirectory(prefix=f"museum-{fid}-") as tmp:
        work = Path(tmp) / "tree"
        _copy_tree(root, work)
        applied = subprocess.run(["git", "apply", "--check" if control else "--whitespace=nowarn", str(patch)],
                                 cwd=work, capture_output=True, text=True)
        if applied.returncode != 0:
            return Outcome(fid, "BROKEN", f"patch does not apply: {applied.stderr.strip()[:300]}")
        junit = Path(tmp) / "junit.xml"
        env = {**os.environ, "PYTHONPATH": str(work / "src")}
        try:
            res = subprocess.run([python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", f"--junitxml={junit}",
                                  *[str(t) for t in finding["tests"]]], cwd=work, env=env, capture_output=True,
                                 text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return Outcome(fid, "KILLED", f"tests timed out after {TIMEOUT_S}s on the patched code")
        tail = (res.stdout.strip().splitlines() or ["(no output)"])[-1]
        if res.returncode not in (0, 1) or not junit.exists():
            return Outcome(fid, "BROKEN", f"pytest exit {res.returncode}: {tail}")
        suite = ET.parse(junit).getroot()
        cases = list(suite.iter("testcase"))
        failed = [c.get("name", "?") for c in cases if c.find("failure") is not None or c.find("error") is not None]
        if not cases:
            return Outcome(fid, "BROKEN", "no test ran")
        if control:  # unpatched: every test must pass, or a KILLED verdict would prove nothing
            return Outcome(fid, "CONTROL-FAIL" if failed else "CONTROL-OK",
                           f"{len(failed)} of {len(cases)} test(s) failed on the unpatched code ({tail})")
        if failed:
            return Outcome(fid, "KILLED", f"{len(failed)} test(s) failed, first: {failed[0]} ({tail})")
        return Outcome(fid, "SURVIVED", f"all {len(cases)} test(s) passed on the re-introduced loophole ({tail})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--control", action="store_true")
    ap.add_argument("--registry", default=REGISTRY)
    args = ap.parse_args(argv)
    problems = check_registry(ROOT, args.registry)
    for p in problems:
        print(f"registry: {p}", flush=True)
    if problems and (args.check_only or args.only is None):  # a targeted --only run reports, then proceeds
        print(f"museum: registry FAILED ({len(problems)} problem(s))", flush=True)
        return 1
    doc = load(ROOT, args.registry)
    patched = [f for f in doc["findings"] if f.get("museum")]
    if args.check_only:
        print(f"museum: registry OK ({len(doc['findings'])} findings, {len(patched)} museum patches)", flush=True)
        return 0
    if args.only is not None:
        unknown = set(args.only) - {str(f["id"]) for f in patched}
        if unknown:
            print(f"museum: no museum patch for {sorted(unknown)}", flush=True)
            return 1
        patched = [f for f in patched if str(f["id"]) in args.only]
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        outcomes = list(pool.map(lambda f: run_one(ROOT, f, sys.executable, args.control), patched))
    for o in outcomes:
        print(f"{o.verdict:9s} {o.finding}: {o.detail}", flush=True)
    want = "CONTROL-OK" if args.control else "KILLED"
    bad = [o for o in outcomes if o.verdict != want]
    print(f"museum: {len(outcomes) - len(bad)}/{len(outcomes)} {want}"
          + (f"; NOT killed: {[o.finding for o in bad]}" if bad else ""), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
