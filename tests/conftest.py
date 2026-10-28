import pytest

from custom_harness.store import EvidenceStore


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    # scripted test models share one family; runs are recorded as NOT meeting invariant 5 (test_policy.py checks
    # the enforcement itself with this override removed)
    monkeypatch.setenv("CHH_ALLOW_SAME_FAMILY", "1")
    # pytest is test mode: LocalSandbox (no isolation, trusted test code only) refuses to run without this switch.
    # Tests of the refusal itself delete it (test_sandbox_hardening.py, test_research.py, test_run_bundle.py).
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")


@pytest.fixture(autouse=True)
def _isolated_protected_paths():
    """Each test starts from the protected-path registry as it was before the test and leaves it that way.
    Every EvidenceStore registers its root process-wide, and check_mount() runs an alias-safe overlap check
    against every registered path (about 15 ms each), so without this the registry grew with every test and a
    single-process full run slowed about 10x (test_protected_registry_isolation.py; agent_worklogs/stage-c5).
    Paths registered during the test, by the test or its fixtures, stay protected for the whole test."""
    from custom_harness.sandbox import base

    saved = set(base._PROTECTED)
    yield
    base._PROTECTED.clear()
    base._PROTECTED.update(saved)


@pytest.fixture
def store(tmp_path):
    from custom_harness.kernel import Kernel
    s = EvidenceStore(tmp_path / "store")
    Kernel(s).approve_adapter("ml", approver="pytest")  # test stand-in for the human approval of the ML adapter
    yield s
    s.close()
