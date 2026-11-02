"""GOAL §16.5.1 entropy pin and §16.8 CPU-time limits (LocalSandbox: trusted test programs only).

The real-Docker counterpart (numpy/sklearn/torch under the pin, cgroup CPU accounting) is in
test_hermetic_docker.py and is skipped without Docker."""

from __future__ import annotations

import json
import sys

import pytest

from custom_harness.sandbox.base import LIMIT_RETURNCODE
from custom_harness.sandbox.hermetic import SUPERVISOR_MARK, EntropyPin, split_record
from custom_harness.sandbox.local import LocalSandbox

ENTROPY_PROG = """
import os, random, secrets, uuid
print(os.urandom(8).hex(), random.random(), random.Random().random(), secrets.token_hex(8), uuid.uuid4(),
      hash("abc"), "".join(sorted({"x", "y", "z", "w"}, key=lambda s: hash(s))), random.SystemRandom().random())
"""


def _run(tmp_path, name, prog, pin=None, **kw):
    box = LocalSandbox(tmp_path / name, i_understand_no_isolation=True)
    (box.workspace / "p.py").write_text(prog, encoding="utf-8")
    box.pin_entropy(pin)
    return box.exec([sys.executable, "p.py"], **kw)


def test_unpinned_entropy_differs_between_runs(tmp_path):
    """The control: without the pin, two honest runs of the same program differ (the problem being fixed)."""
    a, b = (_run(tmp_path, f"u{i}", ENTROPY_PROG) for i in range(2))
    assert a.returncode == b.returncode == 0, a.stderr
    assert a.stdout != b.stdout
    assert a.entropy_pin is None


def test_pinned_entropy_is_byte_identical_and_seed_dependent(tmp_path):
    pin = EntropyPin.for_run("claim", "subject", 0)
    a, b = (_run(tmp_path, f"p{i}", ENTROPY_PROG, pin) for i in range(2))
    assert a.returncode == 0, a.stderr
    assert a.stdout == b.stdout  # urandom, random, Random(), secrets, uuid4, hash seed, SystemRandom: all pinned
    assert a.entropy_pin == pin.digest
    c = _run(tmp_path, "p2", ENTROPY_PROG, EntropyPin.for_run("claim", "subject", 1))
    assert c.stdout != a.stdout  # another run identity gets another stream


def test_pin_overrides_caller_env(tmp_path):
    pin = EntropyPin.for_run("x")
    r = _run(tmp_path, "e", "import os; print(os.environ['PYTHONHASHSEED'], os.environ['OMP_NUM_THREADS'])", pin,
             env={"PYTHONHASHSEED": "random", "OMP_NUM_THREADS": "8"})
    assert r.stdout.split() == [pin.env("s")["PYTHONHASHSEED"], "1"]


BUSY = "x = 0\nwhile True:\n    x += 1\n"


def test_cpu_limit_charges_and_kills_a_busy_program(tmp_path):
    r = _run(tmp_path, "busy", BUSY, cpu_s=1.0, timeout_s=60)
    assert r.cpu_exceeded and r.returncode == LIMIT_RETURNCODE
    assert r.cpu_s is not None and r.cpu_s >= 1.0
    assert not r.timed_out and "CPU time exceeded" in r.stderr


def test_sleeping_program_hits_the_wall_backstop_and_is_witnessed(tmp_path):
    r = _run(tmp_path, "sleep", "import time; time.sleep(30)", cpu_s=20.0, timeout_s=2.0)
    assert r.timed_out and r.returncode == -1 and not r.cpu_exceeded
    assert r.cpu_s is not None and r.cpu_s < 2.0
    assert r.contended is not None  # the witness ran and returned a verdict
    assert "contention witness" in r.stderr


def test_normal_program_passes_with_its_cpu_charged(tmp_path):
    r = _run(tmp_path, "ok", "import sys; print('hi'); sys.exit(3)", cpu_s=30.0, timeout_s=60)
    assert r.returncode == 3 and r.stdout.strip() == "hi" and not r.cpu_exceeded and r.cpu_s is not None
    assert SUPERVISOR_MARK not in r.stderr  # the record is consumed, not shown


def test_forged_supervisor_record_does_not_save_a_busy_program(tmp_path):
    fake = SUPERVISOR_MARK + json.dumps({"rc": 0, "cpu_s": 0.0, "cpu_exceeded": False, "wall_exceeded": False,
                                         "wall_s": 0.1, "mode": "cgroup", "nonce": "guess"})
    prog = f"import sys\nsys.stderr.write({fake!r} + '\\n'); sys.stderr.flush()\n" + BUSY
    r = _run(tmp_path, "forge", prog, cpu_s=1.0, timeout_s=60)
    assert r.cpu_exceeded and r.returncode == LIMIT_RETURNCODE


def test_forged_record_without_genuine_one_is_the_programs_failure():
    """A record with the wrong nonce, or one followed by more output, is not trusted."""
    rec = {"rc": 0, "cpu_s": 0.0, "cpu_exceeded": False, "wall_exceeded": False, "wall_s": 0.1, "mode": "x"}
    _, got = split_record("a\n" + SUPERVISOR_MARK + json.dumps(rec) + "\nmore output\n")
    assert got is None
    _, got = split_record("a\n" + SUPERVISOR_MARK + "{not json\n")
    assert got is None


@pytest.mark.parametrize("cpu_s", [None])
def test_no_cpu_limit_keeps_todays_behaviour(tmp_path, cpu_s):
    r = _run(tmp_path, "plain", "print(1)", cpu_s=cpu_s)
    assert r.returncode == 0 and r.cpu_s is None and r.contended is None and r.entropy_pin is None
