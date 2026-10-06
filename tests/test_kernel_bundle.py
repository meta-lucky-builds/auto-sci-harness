"""Reproducibility Bundle + `chh reproduce` (M1 exit: "`chh reproduce` works"; GOAL §7: "chh reproduce on a
clean machine"). The original store is DELETED before reproducing, so only the bundle's bytes are used."""

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
from custom_harness.kernel.bundle import BundleError, build_bundle, reproduce_bundle
from custom_harness.sandbox import LocalSandbox
from kernel_world import World

ROOT = Path(__file__).resolve().parents[1]


def _fresh_replayer(root: Path):
    return lambda: SandboxReplayer(lambda key: LocalSandbox(root / key, i_understand_no_isolation=True))


def _certified(tmp_path: Path, good: str = "good"):
    w = World(tmp_path / "world", seed=11)
    c = w.claim(w.artifact(good), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    return w, cert


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")


def test_bundle_reproduces_on_a_clean_store(tmp_path):
    w, cert = _certified(tmp_path)
    assert cert.label == Label.CERTIFIED_INTERNAL
    bundle = tmp_path / "out" / "claim.zip"
    bundle_hash = build_bundle(w.kernel, cert.cert_hash(), bundle)
    assert any(e.body["bundle_hash"] == bundle_hash for e in w.kernel.ledger.entries("bundle"))
    w.close()
    shutil.rmtree(tmp_path / "world")  # nothing but the bundle survives
    report = reproduce_bundle(bundle, _fresh_replayer(tmp_path / "replay"), workdir=tmp_path / "clean")
    assert report.reproduced, report.problems
    assert report.bundle_hash == bundle_hash and report.reproduced_label == "CERTIFIED-INTERNAL"
    assert report.certificate is not None and report.certificate.checks["K3"].status == "pass"


def test_rejected_certificate_reproduces_as_rejected(tmp_path):
    w, cert = _certified(tmp_path, good="nondet")  # irreproducible subject -> K3 fails
    assert cert.label == Label.REJECTED and "K3" in cert.failed()
    bundle = tmp_path / "b.zip"
    build_bundle(w.kernel, cert.cert_hash(), bundle)
    w.close()
    report = reproduce_bundle(bundle, _fresh_replayer(tmp_path / "replay"))
    assert report.reproduced_label == "REJECTED"
    assert report.certificate is not None and "K3" in report.certificate.failed()


def _rewrite(bundle: Path, out: Path, edit) -> None:
    with zipfile.ZipFile(bundle) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.namelist():
            zout.writestr(item, edit(item, zin.read(item)))


def test_tampered_bundle_is_not_reproduced(tmp_path):
    w, cert = _certified(tmp_path)
    bundle = tmp_path / "b.zip"
    build_bundle(w.kernel, cert.cert_hash(), bundle)
    code_blob = cert.claim["subject"]["hash"]
    w.close()

    tampered = tmp_path / "tampered_code.zip"
    _rewrite(bundle, tampered, lambda name, data: b"print('swapped')" if name == f"blobs/{code_blob}" else data)
    report = reproduce_bundle(tampered, _fresh_replayer(tmp_path / "r1"))
    assert not report.reproduced and any("tampered" in p for p in report.problems)

    def bump_seq(name, data):
        if name != "manifest.json":
            return data
        m = json.loads(data)
        m["ledger"][0][2] = m["ledger"][0][2].replace('"dev"', '"test"')  # edit a ledger entry body
        return json.dumps(m).encode()
    edited = tmp_path / "tampered_ledger.zip"
    _rewrite(bundle, edited, bump_seq)
    report = reproduce_bundle(edited, _fresh_replayer(tmp_path / "r2"))
    assert not report.reproduced and any("does not match its hash" in p for p in report.problems)

    wrong = tmp_path / "wrong.zip"
    _rewrite(bundle, wrong, lambda n, d: json.dumps({**json.loads(d), "format": "x"}).encode()
             if n == "manifest.json" else d)
    with pytest.raises(BundleError):
        reproduce_bundle(wrong, _fresh_replayer(tmp_path / "r3"))


def test_chh_reproduce_cli(tmp_path):
    w, cert = _certified(tmp_path)
    bundle = tmp_path / "b.zip"
    build_bundle(w.kernel, cert.cert_hash(), bundle)
    w.close()
    env = {**os.environ, "CHH_DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": str(ROOT / "src")}
    p = subprocess.run([sys.executable, "-m", "custom_harness.cli", "reproduce", str(bundle), "--dev-sandbox"],
                       capture_output=True, text=True, env=env, timeout=600)
    assert p.returncode == 0, p.stdout + p.stderr
    out = json.loads(p.stdout)
    assert out["reproduced"] is True and out["reproduced_label"] == "CERTIFIED-INTERNAL"
