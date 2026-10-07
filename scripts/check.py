"""One-command quality gate: lint, types, strict Kernel types, full tests, 100% Kernel branch coverage.

    python scripts/check.py            # everything (about 10 minutes; includes real-Docker tests)
    python scripts/check.py --fast     # lint + types + Kernel tests with the coverage gate
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
KERNEL_TESTS = ["tests/test_kernel_units.py", "tests/test_kernel_bundle.py", "tests/test_kernel_suite.py",
                "tests/test_kernel_quality.py", "tests/test_kernel_exploits.py",
                "tests/test_kernel_integrity.py", "tests/test_kernel_sim.py"]


def run(name: str, cmd: list[str]) -> bool:
    print(f"== {name}: {' '.join(cmd)}", flush=True)
    ok = subprocess.run(cmd, cwd=ROOT, check=False).returncode == 0
    print(f"== {name}: {'OK' if ok else 'FAILED'}", flush=True)
    return ok


def main() -> int:
    fast = "--fast" in sys.argv
    steps = [
        ("lint", [PY, "-m", "ruff", "check", "src", "tests", "scripts"]),
        ("types", [PY, "-m", "mypy", "src"]),
        ("kernel strict types", [PY, "-m", "mypy", "--strict", "src/custom_harness/kernel"]),
        ("kernel tests + 100% branch coverage", [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *KERNEL_TESTS,
                                                 "--cov=custom_harness.kernel", "--cov-branch",
                                                 "--cov-report=term-missing", "--cov-fail-under=100"]),
    ]
    if not fast:
        steps.append(("all tests", [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"]))
    results = [run(n, c) for n, c in steps]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
