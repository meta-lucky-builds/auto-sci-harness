import pytest

from custom_harness.store import EvidenceStore


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    # scripted test models share one family; runs are recorded as NOT meeting invariant 5 (test_policy.py checks
    # the enforcement itself with this override removed)
    monkeypatch.setenv("CHH_ALLOW_SAME_FAMILY", "1")


@pytest.fixture
def store(tmp_path):
    from custom_harness.kernel import Kernel
    s = EvidenceStore(tmp_path / "store")
    Kernel(s).approve_adapter("ml", approver="pytest")  # test stand-in for the human approval of the ML adapter
    yield s
    s.close()
