"""GOAL §16.3: one commitment primitive `draw(purpose, *, prereg_hash, beacon, secret)` with a per-purpose input
table, and the grep check that the human secret appears nowhere agents can read."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path

import pytest

from custom_harness import commitment as C

H = hashlib.sha256(b"frozen claim").hexdigest()
SECRET = b"offline-human-secret-0123456789abcdef"
ROOT = Path(__file__).resolve().parents[1]


def test_draw_is_hmac_of_purpose_hash_and_beacon_and_rerunnable():
    v = C.draw("heldout_mint", prereg_hash=H, beacon="drand:4242", secret=None)
    msg = json.dumps(["chh-draw/1", "heldout_mint", H, "drand:4242"], separators=(",", ":")).encode()
    assert v == int.from_bytes(hmac.new(b"chh-draw/1", msg, hashlib.sha256).digest(), "big")  # known answer
    assert v == C.draw("heldout_mint", prereg_hash=H, beacon="drand:4242", secret=None)  # anyone can re-run it
    others = {C.draw("seed", prereg_hash=H, beacon="drand:4242", secret=None),
              C.draw("heldout_mint", prereg_hash="0" * 64, beacon="drand:4242", secret=None),
              C.draw("heldout_mint", prereg_hash=H, beacon="drand:4243", secret=None)}
    assert v not in others and len(others) == 3  # bound to purpose, frozen hash and beacon


def test_per_purpose_table_fixes_the_inputs():
    beacon_only = {p for p, s in C.PURPOSES.items() if s.inputs == "beacon"}
    assert C.PURPOSES["salt_assignment"].inputs == "secret+beacon"
    assert beacon_only == set(C.PURPOSES) - {"salt_assignment"}
    assert all(C.PURPOSES[p].frozen_before_draw for p in beacon_only)
    assert {"heldout_subsample", "seed", "target", "gate_tasks", "heldout_mint", "check_params"} <= beacon_only
    for p in beacon_only:  # a secret would make a public draw unverifiable
        with pytest.raises(C.DrawError, match="beacon-only"):
            C.draw(p, prereg_hash=H, beacon="b", secret=SECRET)
    with pytest.raises(C.DrawError, match="human secret"):
        C.draw("salt_assignment", prereg_hash=H, beacon="b", secret=None)
    with pytest.raises(C.DrawError, match="human secret"):
        C.draw("salt_assignment", prereg_hash=H, beacon="b", secret=b"short")
    a = C.draw("salt_assignment", prereg_hash=H, beacon="b", secret=SECRET)
    assert a != C.draw("salt_assignment", prereg_hash=H, beacon="b", secret=SECRET + b"x")  # the secret matters


@pytest.mark.parametrize("kwargs, match", [
    ({"purpose": "nope", "prereg_hash": H, "beacon": "b"}, "unknown draw purpose"),
    ({"purpose": "seed", "prereg_hash": "ABC", "beacon": "b"}, "prereg_hash"),
    ({"purpose": "seed", "prereg_hash": H.upper(), "beacon": "b"}, "prereg_hash"),
    ({"purpose": "seed", "prereg_hash": H, "beacon": None}, "beacon"),
    ({"purpose": "seed", "prereg_hash": H, "beacon": "  "}, "beacon"),
])
def test_draw_fails_closed(kwargs, match):
    purpose = kwargs.pop("purpose")
    with pytest.raises(C.DrawError, match=match):
        C.draw(purpose, secret=None, **kwargs)


def test_draw_module_fetches_nothing():
    """The beacon is an explicit input (drand vs dice is an owner decision): no network module is imported."""
    src = Path(C.__file__).read_text(encoding="utf-8")
    for mod in ("urllib", "http", "socket", "requests", "httpx"):
        assert f"import {mod}" not in src and f"from {mod}" not in src, mod


@pytest.mark.parametrize("form", ["raw", "hex", "HEX", "b64", "b64-nopad", "urlsafe"])
def test_secret_leak_scan_finds_the_secret_in_any_common_encoding(tmp_path, form):
    enc = {"raw": SECRET, "hex": SECRET.hex().encode(), "HEX": SECRET.hex().upper().encode(),
           "b64": base64.b64encode(SECRET), "b64-nopad": base64.b64encode(SECRET).rstrip(b"="),
           "urlsafe": base64.urlsafe_b64encode(SECRET)}[form]
    (tmp_path / "agent" / "deep" / "er").mkdir(parents=True)
    (tmp_path / "agent" / "clean.txt").write_bytes(b"nothing here")
    (tmp_path / "agent" / "deep" / "er" / "notes.md").write_bytes(b"prefix " + enc + b" suffix")
    problems = C.secret_leak_scan(SECRET, [tmp_path / "agent"])
    assert len(problems) == 1 and "notes.md" in problems[0]  # the attack (a planted copy) was found
    assert all(SECRET.decode() not in p and SECRET.hex() not in p for p in problems)  # messages never echo it


def test_secret_leak_scan_flags_the_secret_file_inside_a_root_and_passes_clean_roots(tmp_path):
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "x.txt").write_text("clean")
    sf = tmp_path / "agent" / "secret.bin"
    sf.write_bytes(SECRET)
    problems = C.secret_leak_scan(SECRET, [tmp_path / "agent"], secret_file=sf)
    assert any("inside agent-readable root" in p for p in problems) and any("contains" in p for p in problems)
    sf.unlink()
    assert C.secret_leak_scan(SECRET, [tmp_path / "agent"], secret_file=tmp_path / "vault" / "s.bin") == []
    assert C.secret_leak_scan(b"short", [tmp_path / "agent"]) != []  # too short to scan: fail closed


def test_default_roots_include_scratchpads_and_env_override(tmp_path, monkeypatch):
    import tempfile

    (Path(tempfile.gettempdir()) / "claude").mkdir(exist_ok=True)
    roots = C.default_agent_readable_roots()
    assert ROOT.resolve() in [r.resolve() for r in roots]
    assert Path(tempfile.gettempdir()) / "claude" in roots
    monkeypatch.setenv(C.AGENT_READABLE_ENV, str(tmp_path / "a") + __import__("os").pathsep + str(tmp_path / "b"))
    assert C.default_agent_readable_roots() == [tmp_path / "a", tmp_path / "b"]


def test_check_secret_script_exits_nonzero_on_a_leak(tmp_path):
    (tmp_path / "vault").mkdir()
    (tmp_path / "agent").mkdir()
    (tmp_path / "vault" / "s.bin").write_bytes(SECRET)
    env = {**__import__("os").environ, C.SECRET_FILE_ENV: str(tmp_path / "vault" / "s.bin"),
           C.AGENT_READABLE_ENV: str(tmp_path / "agent"), "PYTHONPATH": str(ROOT / "src")}
    script = [sys.executable, str(ROOT / "scripts" / "check_secret.py")]
    ok = subprocess.run(script, env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    (tmp_path / "agent" / "leak.log").write_bytes(SECRET.hex().encode())
    bad = subprocess.run(script, env=env, capture_output=True, text=True)
    assert bad.returncode == 1 and "leak.log" in bad.stdout and SECRET.hex() not in bad.stdout + bad.stderr
    env.pop(C.SECRET_FILE_ENV)
    none = subprocess.run(script, env=env, capture_output=True, text=True)
    assert none.returncode == 2  # no secret configured: refuse, never report clean
