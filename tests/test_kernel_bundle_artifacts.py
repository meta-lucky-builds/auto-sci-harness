"""GOAL L2 large artifacts for CLAIM bundles (kernel/bundle.py): the manifest lists every blob the certificate needs
(path, bytes, sha256, kind, status bundled / available_locally / referenced, the run-bundle format), a blob over the
size cap is listed but not bundled, and reproduction REPORTS every needed blob it cannot find (never skips it)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from custom_harness.kernel import Label, SandboxReplayer
from custom_harness.kernel.bundle import build_bundle, reproduce_bundle
from custom_harness.sandbox import LocalSandbox
from custom_harness.store.run_bundle import STATUSES, check_artifacts
from kernel_world import World

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")


def _fresh(root: Path):
    return lambda: SandboxReplayer(lambda key: LocalSandbox(root / key, i_understand_no_isolation=True))


def _bundle(tmp_path: Path, large: int):
    w = World(tmp_path / "world", seed=11)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label == Label.CERTIFIED_INTERNAL
    path = tmp_path / "claim.zip"
    build_bundle(w.kernel, cert.cert_hash(), path, large_bytes=large)
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        members = set(z.namelist())
    return w, path, manifest, members


def test_every_needed_blob_is_listed_and_large_ones_are_not_bundled(tmp_path):
    w, path, manifest, members = _bundle(tmp_path, large=2000)
    arts = manifest["artifacts"]
    assert sorted(a["sha256"] for a in arts) == sorted(manifest["blobs"])
    assert all(a["status"] in STATUSES and a["required"] for a in arts)
    big = [a for a in arts if a["status"] == "available_locally"]
    assert big and all(a["bytes"] > 2000 and a["path"] not in members for a in big)
    assert all(a["path"] in members for a in arts if a["status"] == "bundled")
    assert {a["kind"] for a in arts} >= {"dataset", "output"}
    # the run-bundle checker reads the same format: everything resolves on the building machine
    rep = check_artifacts(manifest, path, blobs=w.store.blobs)
    assert rep.fatal == [] and {r["found_at"] for r in rep.rows} == {"bundle", "blob store"}, rep.problems
    store_root = tmp_path / "world"
    blobs = w.store.blobs
    # reproduction with this machine's copies of the large artifacts
    report = reproduce_bundle(path, _fresh(tmp_path / "rp1"), workdir=tmp_path / "c1",
                              artifact_source=lambda h: blobs.get(h) if blobs.has(h) else None)
    assert report.reproduced and report.missing_artifacts == [], report.problems
    # on a clean machine (the store is gone) every missing large artifact is REPORTED, and nothing reproduces
    w.close()
    shutil.rmtree(store_root)
    report = reproduce_bundle(path, _fresh(tmp_path / "rp2"), workdir=tmp_path / "c2")
    assert not report.reproduced
    assert sorted(m["sha256"] for m in report.missing_artifacts) == sorted(a["sha256"] for a in big)
    assert all(m["status"] == "available_locally" for m in report.missing_artifacts)
    assert sum("missing: not in the bundle and not found locally" in p for p in report.problems) == len(big)


def test_a_wrong_local_copy_is_refused(tmp_path):
    w, path, _, _ = _bundle(tmp_path, large=2000)
    w.close()
    report = reproduce_bundle(path, _fresh(tmp_path / "rp"), workdir=tmp_path / "c", artifact_source=lambda h: b"x")
    assert not report.reproduced and any("(local copy)" in p for p in report.problems)


def test_a_blob_missing_when_the_bundle_is_built_is_listed_as_referenced(tmp_path):
    w = World(tmp_path / "world", seed=11)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    gone = w.store.blobs._path(c.comparator.hash)
    gone.unlink()
    path = tmp_path / "claim.zip"
    build_bundle(w.kernel, cert.cert_hash(), path)
    w.close()
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
    (entry,) = [a for a in manifest["artifacts"] if a["sha256"] == c.comparator.hash]
    assert entry["status"] == "referenced" and entry["bytes"] == -1
    report = reproduce_bundle(path, _fresh(tmp_path / "rp"), workdir=tmp_path / "c")
    assert [m["sha256"] for m in report.missing_artifacts] == [c.comparator.hash] and not report.reproduced


def test_chh_reproduce_reports_missing_large_artifacts(tmp_path):
    w, path, manifest, _ = _bundle(tmp_path, large=2000)
    w.close()
    env = {**os.environ, "CHH_DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": str(ROOT / "src")}
    p = subprocess.run([sys.executable, "-m", "custom_harness.cli", "reproduce", str(path), "--dev-sandbox"],
                       capture_output=True, text=True, env=env, timeout=600)
    assert p.returncode == 1, p.stdout + p.stderr
    out = json.loads(p.stdout)
    assert out["reproduced"] is False and "required artifact missing: blobs/" in out["refused"]
    big = [a["sha256"] for a in manifest["artifacts"] if a["status"] == "available_locally"]
    assert all(h[:12] in json.dumps(out) for h in big)
