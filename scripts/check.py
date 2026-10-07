"""One-command quality gate: lint, types, strict Kernel types, full tests, 100% Kernel branch coverage.

    python scripts/check.py            # everything (about 10 minutes; includes real-Docker tests)
    python scripts/check.py --fast     # lint + types + registry/governance checks + Kernel tests with the coverage gate
    python scripts/check.py --self-test  # prove the gate goes red on a planted failing Kernel test (GOAL §16.6)

The Kernel test list is DERIVED from `tests/test_kernel_*.py` (GOAL §16.6: a hand-kept list held merge-conflict
markers during the 2026-10-07 merge, and a Kernel test file missing from it silently left the coverage gate).
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
KERNEL_TEST_GLOB = "test_kernel_*.py"
CONFLICT = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.MULTILINE)


class GateError(RuntimeError):
    pass


def kernel_tests(root: Path = ROOT) -> list[str]:
    """The Kernel test files, derived from the tree. Fails (GateError) unless the set is non-empty, every file parses,
    defines at least one test function and holds no merge-conflict marker."""
    files = sorted((root / "tests").glob(KERNEL_TEST_GLOB))
    if not files:
        raise GateError(f"no tests/{KERNEL_TEST_GLOB} found: the Kernel coverage gate would test nothing")
    out = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if CONFLICT.search(text):
            raise GateError(f"{f.relative_to(root).as_posix()}: merge-conflict marker")
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            raise GateError(f"{f.relative_to(root).as_posix()}: does not parse ({e})") from e
        if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
                   for n in tree.body):
            raise GateError(f"{f.relative_to(root).as_posix()}: defines no test function")
        out.append(f.relative_to(root).as_posix())
    return out


def conflict_markers(root: Path = ROOT) -> list[str]:
    """Files under src/, tests/ and scripts/ holding a merge-conflict marker (the same mistake, anywhere)."""
    bad = []
    for sub in ("src", "tests", "scripts"):
        for f in sorted((root / sub).rglob("*")):
            if f.suffix in (".py", ".yaml", ".yml", ".toml", ".cfg") and f.is_file() and \
                    CONFLICT.search(f.read_text(encoding="utf-8", errors="replace")):
                bad.append(f.relative_to(root).as_posix())
    return bad


def kernel_step(root: Path = ROOT) -> list[str]:
    return [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *kernel_tests(root), "--cov=custom_harness.kernel",
            "--cov-branch", "--cov-report=term-missing", "--cov-fail-under=100"]


def run(name: str, cmd: list[str], cwd: Path = ROOT) -> bool:
    print(f"== {name}: {' '.join(cmd)}", flush=True)
    ok = subprocess.run(cmd, cwd=cwd, check=False).returncode == 0
    print(f"== {name}: {'OK' if ok else 'FAILED'}", flush=True)
    return ok


def self_test() -> int:
    """Copy the tree, plant a failing Kernel test file, and require the derived list to include it and the Kernel
    step to fail on it; plant a conflict marker and require the derivation to refuse."""
    with tempfile.TemporaryDirectory(prefix="gate-selftest-") as tmp:
        work = Path(tmp)
        for sub in ("src", "tests", "scripts"):
            shutil.copytree(ROOT / sub, work / sub, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "pyproject.toml", work / "pyproject.toml")
        planted = work / "tests" / "test_kernel_zz_planted_canary.py"
        planted.write_text("def test_planted_failure():\n    assert False, 'planted by check.py --self-test'\n",
                           encoding="utf-8", newline="")
        listed = kernel_tests(work)
        if "tests/test_kernel_zz_planted_canary.py" not in listed:
            print("self-test FAILED: the planted Kernel test file is not in the derived list", flush=True)
            return 1
        res = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                              "tests/test_kernel_zz_planted_canary.py"], cwd=work, capture_output=True, text=True,
                             check=False, env={**os.environ, "PYTHONPATH": str(work / "src")})
        if res.returncode == 0 or "1 failed" not in res.stdout:
            print(f"self-test FAILED: the planted test did not fail the step:\n{res.stdout[-500:]}", flush=True)
            return 1
        (work / "tests" / "test_kernel_quality.py").write_text(
            "<<<<<<< HEAD\n" + (ROOT / "tests" / "test_kernel_quality.py").read_text(encoding="utf-8"),
            encoding="utf-8", newline="")
        try:
            kernel_tests(work)
        except GateError as e:
            print(f"self-test OK: planted failure fails the Kernel step; conflict marker refused ({e})", flush=True)
            return 0
        print("self-test FAILED: a merge-conflict marker in a Kernel test file was not refused", flush=True)
        return 1


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    fast = "--fast" in sys.argv
    try:
        ktests = kernel_tests()
    except GateError as e:
        print(f"== kernel test list: FAILED ({e})", flush=True)
        return 1
    print(f"== kernel test list (derived from tests/{KERNEL_TEST_GLOB}): {len(ktests)} files", flush=True)
    markers = conflict_markers()
    print(f"== conflict markers: {'FAILED ' + str(markers) if markers else 'OK'}", flush=True)
    steps = [
        ("lint", [PY, "-m", "ruff", "check", "src", "tests", "scripts"]),
        ("types", [PY, "-m", "mypy", "src"]),
        ("kernel strict types", [PY, "-m", "mypy", "--strict", "src/custom_harness/kernel"]),
        ("red-team registry", [PY, "scripts/museum.py", "--check-only"]),
        ("governance (two-speed Kernel)", [PY, "scripts/governance.py"]),
        ("kernel tests + 100% branch coverage", kernel_step()),
    ]
    if not fast:
        steps.append(("all tests", [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"]))
    results = [run(n, c) for n, c in steps]
    return 0 if all(results) and not markers else 1


if __name__ == "__main__":
    sys.exit(main())
