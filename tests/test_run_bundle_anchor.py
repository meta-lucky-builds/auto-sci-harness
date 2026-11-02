"""Run-bundle attacks found by an adversarial review (2026-10-08): a manifest is self-attested unless it is anchored,
so a rewritten manifest can drop a required artifact; duplicate zip members show the checker other bytes than an
extractor; malformed bundles must be refused cleanly (fail closed, no traceback)."""

import argparse
import json
import zipfile

import pytest

from custom_harness.cli import cmd_rerun
from custom_harness.store import EvidenceStore
from test_run_bundle_artifacts import _rewrite_manifest, _run, task  # noqa: F401


def _args(path, **kw):
    return argparse.Namespace(bundle=str(path), role=["executor=mock"], allow_same_family=True, task_dir=None,
                              docker=False, dev_sandbox=True, artifact_root=[], **kw)


def _weights(tmp_path):
    w = tmp_path / "pretrained.pt"
    w.write_bytes(b"W" * 100)
    return w


@pytest.fixture
def other_store(tmp_path):
    s = EvidenceStore(tmp_path / "other-machine")
    yield s
    s.close()


def test_attack_unanchored_manifest_with_a_required_artifact_removed(store, other_store, tmp_path, task):  # noqa: F811
    w = _weights(tmp_path)
    _, path = _run(store, tmp_path, task, [{"path": str(w), "kind": "weights", "required": True}])
    w.unlink()
    _rewrite_manifest(path, lambda m: m.__setitem__("artifacts", [a for a in m["artifacts"]
                                                                  if a["path"] != str(w)]))
    n = len(other_store.list_runs(limit=50))
    with pytest.raises(SystemExit, match="not anchored"):
        cmd_rerun(_args(path), other_store)  # another machine: no record of this run
    assert len(other_store.list_runs(limit=50)) == n


def test_an_unanchored_bundle_runs_with_its_published_manifest_hash(store, other_store, tmp_path, task, capsys):  # noqa: F811
    from custom_harness.store.run_bundle import read_run_bundle_bytes
    _, path = _run(store, tmp_path, task, [])
    sha = read_run_bundle_bytes(path)[1]
    with pytest.raises(SystemExit, match="differs from the hash given"):
        cmd_rerun(_args(path, manifest_sha256="0" * 64), other_store)
    capsys.readouterr()
    assert cmd_rerun(_args(path, manifest_sha256=sha), other_store) in (0, 1)
    assert json.loads(capsys.readouterr().out)["manifest_anchor"] == "matches the given --manifest-sha256"
    assert cmd_rerun(_args(path, allow_unanchored=True), other_store) in (0, 1)
    assert "NOT anchored" in json.loads(capsys.readouterr().out)["manifest_anchor"]


def test_attack_artifact_list_inconsistent_with_task_files(store, tmp_path, task):  # noqa: F811
    from custom_harness.store.run_bundle import check_artifacts, read_run_bundle
    _, path = _run(store, tmp_path, task, [])
    m = read_run_bundle(path)
    m["artifacts"] = [a for a in m["artifacts"] if not a["path"].startswith("task/")]
    assert m["task_files"]  # the attack is in place: task data recorded but no longer listed
    assert any("not listed" in f for f in check_artifacts(m, path, blobs=None).fatal)


def test_attack_duplicate_zip_members_are_refused(store, tmp_path, task):  # noqa: F811
    from custom_harness.store.run_bundle import RunBundleError, read_run_bundle
    _, path = _run(store, tmp_path, task, [])
    with zipfile.ZipFile(path) as z:
        members = [(n, z.read(n)) for n in z.namelist()]
    with pytest.warns(UserWarning, match="Duplicate name"), zipfile.ZipFile(path, "w") as z:
        for n, b in [("manifest.json", b"{}"), *members]:
            z.writestr(n, b)
    with zipfile.ZipFile(path) as z:  # the attack is in place: two members named manifest.json
        assert sum(i.filename == "manifest.json" for i in z.infolist()) == 2
    with pytest.raises(RunBundleError, match="duplicate member"):
        read_run_bundle(path)


def test_corrupt_member_is_a_clean_refusal(store, tmp_path, task):  # noqa: F811
    _, path = _run(store, tmp_path, task, [])
    raw = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("manifest.json")
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    raw[start + 5] ^= 0xFF  # one byte of manifest.json's compressed data
    path.write_bytes(bytes(raw))
    with pytest.raises(SystemExit, match="run bundle refused"):
        cmd_rerun(_args(path), store)


def test_malformed_artifact_entry_is_a_clean_refusal(store, tmp_path, task):  # noqa: F811
    from custom_harness.store.run_bundle import RunBundleError, check_artifacts
    m = {"format": "chh-run-bundle/1", "artifacts": [
        {"path": "x", "bytes": 1, "sha256": "a" * 64, "status": "referenced", "kind": "weights"}]}  # no "required"
    with pytest.raises(RunBundleError, match="malformed artifact entry"):
        check_artifacts(m, None, blobs=None)
