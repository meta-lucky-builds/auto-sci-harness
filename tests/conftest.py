import pytest

from custom_harness.store import EvidenceStore


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def store(tmp_path):
    s = EvidenceStore(tmp_path / "store")
    yield s
    s.close()
