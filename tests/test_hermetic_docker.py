"""Real-Docker checks of the entropy pin and CPU-time limits (skipped without Docker or the candidate image).
Few and short on purpose (machine budget)."""

from __future__ import annotations

import pytest

from custom_harness.sandbox.base import LIMIT_RETURNCODE
from custom_harness.sandbox.docker import CandidateImageMissing, DockerSandbox, DockerUnavailable
from custom_harness.sandbox.hermetic import EntropyPin


def _box(path):
    try:
        return DockerSandbox(path)
    except (DockerUnavailable, CandidateImageMissing) as e:
        pytest.skip(f"no Docker sandbox: {e}")


PROBE = """
import os, random, numpy as np, torch
from sklearn.ensemble import RandomForestClassifier
X = np.random.rand(60, 3); y = (X[:, 0] > .5).astype(int)
print(os.urandom(4).hex(), random.random(), np.random.default_rng().random(), float(torch.rand(1)),
      RandomForestClassifier(n_estimators=3).fit(X, y).predict_proba(X[:5])[:, 1].tolist(), hash("a"))
"""


def test_docker_pin_makes_numpy_sklearn_torch_deterministic(tmp_path):
    outs = {}
    for name, pin in (("u1", None), ("u2", None), ("p1", EntropyPin.for_run("x")), ("p2", EntropyPin.for_run("x"))):
        box = _box(tmp_path / name)
        (box.workspace / "p.py").write_text(PROBE, encoding="utf-8")
        box.pin_entropy(pin)
        r = box.exec(["python", "p.py"], timeout_s=600)
        assert r.returncode == 0, r.stderr
        outs[name] = r.stdout
    assert outs["u1"] != outs["u2"]  # the control: unpinned honest code is nondeterministic today
    assert outs["p1"] == outs["p2"]


def test_docker_cpu_limit_counts_every_process_and_charges_sleep(tmp_path):
    box = _box(tmp_path / "busy")
    # the work happens in two CHILD processes: per-process limits would miss it, the cgroup counter does not
    (box.workspace / "p.py").write_text(
        "import subprocess, sys\n"
        "c = 'x=0\\nwhile True: x+=1'\n"
        "ps = [subprocess.Popen([sys.executable, '-c', c]) for _ in range(2)]\n"
        "[p.wait() for p in ps]\n", encoding="utf-8")
    r = box.exec(["python", "p.py"], timeout_s=120, cpu_s=2.0)
    assert r.cpu_exceeded and r.returncode == LIMIT_RETURNCODE, r.stderr
    assert r.cpu_s is not None and r.cpu_s >= 2.0
    box2 = _box(tmp_path / "sleep")
    (box2.workspace / "s.py").write_text("import time; time.sleep(60)", encoding="utf-8")
    r2 = box2.exec(["python", "s.py"], timeout_s=5, cpu_s=30.0)
    assert r2.timed_out and not r2.cpu_exceeded and r2.cpu_s is not None and r2.cpu_s < 5
    assert r2.contended is not None, r2.stderr
    box3 = _box(tmp_path / "ok")
    (box3.workspace / "o.py").write_text("print('fine')", encoding="utf-8")
    r3 = box3.exec(["python", "o.py"], timeout_s=120, cpu_s=30.0)
    assert r3.returncode == 0 and r3.stdout.strip() == "fine" and r3.cpu_s is not None, r3.stderr
