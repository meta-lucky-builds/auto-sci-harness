"""The process-wide protected-path registry (sandbox.base._PROTECTED) must not carry one test's paths into the next.

Every EvidenceStore(root) registers root, and check_mount() runs an alias-safe overlap check (stat/samefile on
ancestors) against EVERY registered path, about 15 ms per path on this machine. Without per-test isolation the
registry grew with every test in a single pytest process, so each sandbox creation got slower as the session went on:
the full suite in one process ran about 10x slower than the same files one per process (stage-c5 investigation,
agent_worklogs/stage-c5). tests/conftest.py restores the registry after each test.

The check is run in a separate inner pytest session that uses this repository's tests/conftest.py, so the order of
its tests is fixed and the test also runs alone.
"""

import os
import subprocess
import sys
from pathlib import Path

INNER = '''
import os
from pathlib import Path

import pytest

from custom_harness.sandbox.base import ProtectedMount, _norm, check_mount, protected_paths
from custom_harness.store import EvidenceStore

MARK = Path(os.environ["C5_MARK"])


def test_1_a_store_registers_its_root(tmp_path):
    root = tmp_path / "st"
    s = EvidenceStore(root)
    try:
        assert _norm(root) in protected_paths()
    finally:
        s.close()
    MARK.write_text(str(root), encoding="utf-8")


def test_2_the_previous_tests_store_is_no_longer_registered():
    earlier = Path(MARK.read_text(encoding="utf-8"))
    assert earlier.exists()  # pytest keeps the directory; only the registration must be gone
    assert _norm(earlier) not in protected_paths()


def test_3_a_store_made_in_this_test_is_still_protected(tmp_path):
    root = tmp_path / "st"
    s = EvidenceStore(root)
    try:
        with pytest.raises(ProtectedMount):
            check_mount(root / "inside")
        with pytest.raises(ProtectedMount):
            check_mount(tmp_path)
    finally:
        s.close()
'''


def test_protected_paths_registered_in_one_test_do_not_leak_into_the_next(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    conftest = Path(__file__).with_name("conftest.py")
    (inner / "conftest.py").write_text(conftest.read_text(encoding="utf-8"), encoding="utf-8")
    (inner / "test_inner.py").write_text(INNER, encoding="utf-8")
    env = {**os.environ, "C5_MARK": str(tmp_path / "mark.txt")}
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
                        f"--rootdir={inner}", f"--basetemp={tmp_path / 'bt'}", str(inner / "test_inner.py")],
                       cwd=inner, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0 and "3 passed" in r.stdout, r.stdout[-4000:] + r.stderr[-2000:]
