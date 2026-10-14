"""Human-held check scaffold (GOAL invariant 22): hash-verified, output-blind, ledgered, NOT MET without a check.

All checks here are DUMMY scripts written by the tests into tmp dirs. They say nothing about invariant 22, which
needs a human-supplied check.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from custom_harness.assure import humancheck as hc

ROOT = Path(__file__).resolve().parents[2]
SECRET = "SECRET-MARKER-9f3a1c-do-not-leak"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Env:
    def __init__(self, tmp: Path):
        self.repo = tmp / "repo"
        self.data = tmp / "data"
        (self.repo / "redteam").mkdir(parents=True)
        (self.data / "human" / "checks").mkdir(parents=True)
        self.cdir = self.data / "human" / "checks"
        self.side = tmp / "side_effect.txt"

    def add(self, cid: str, body: str, *, lock: bool = True, registry: bool = True, write: bool = True) -> str:
        data = body.encode()
        sha = _sha(data)
        fname = f"{cid}.py"
        if write:
            (self.cdir / fname).write_bytes(data)
        if registry:
            reg_p = self.cdir / "registry.json"
            reg = json.loads(reg_p.read_text()) if reg_p.exists() else {"checks": []}
            reg["checks"].append({"id": cid, "file": fname, "sha256": sha, "registered_by": "t",
                                  "registered": "2026-01-01"})
            reg_p.write_text(json.dumps(reg))
        if lock:
            lp = self.repo / hc.LOCK
            cur = json.loads(lp.read_text()) if lp.exists() else {}
            cur[cid] = sha
            lp.write_text(json.dumps(cur))
        return sha

    def ledger(self) -> str:
        p = self.repo / hc.LEDGER
        return p.read_text() if p.exists() else ""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    return Env(tmp_path)


def _script(env: Env, code: int = 0, extra: str = "") -> str:
    return (f"import sys\nprint({SECRET!r})\nsys.stderr.write({SECRET!r})\n{extra}\nsys.exit({code})\n")


def test_no_check_registered_is_not_met(env):
    (env.repo / hc.LOCK).write_text("{}")
    assert hc.verify_hashes(env.repo).status == "NOT MET"
    st, notes = hc.status(env.repo)
    assert st == "NOT MET" and hc.NOT_MET_LINE in notes
    ok, lines = hc.gate(env.repo)
    assert ok and lines == [hc.NOT_MET_LINE]
    assert hc.run_checks(env.repo).status == "NOT MET" and env.ledger() == ""


def test_no_lock_file_and_no_registry_is_still_not_met(env):
    assert hc.verify_hashes(env.repo).status == "NOT MET"


def test_pass_case_and_ledger(env, capfd):
    sha = env.add("m3-a", _script(env, 0))
    assert hc.verify_hashes(env.repo).status == "OK"
    rep = hc.run_checks(env.repo, timeout_s=60)
    assert rep.status == "OK", rep.problems
    rec = json.loads(env.ledger().strip())
    assert rec["id"] == "m3-a" and rec["sha256"] == sha and rec["result"] == "pass" and rec["prev"] == "genesis"
    assert rec["at"] and rec["head"]
    assert hc.status(env.repo)[0] == "LAST-RUN-PASSED"
    out = capfd.readouterr()
    assert SECRET not in out.out + out.err
    assert hc.check_chain(env.repo)[0]


def test_fail_case_exit_1(env):
    env.add("m3-a", _script(env, 1))
    rep = hc.run_checks(env.repo, timeout_s=60)
    assert rep.status == "FAIL"
    rec = json.loads(env.ledger().strip())
    assert rec["result"] == "fail" and rec["code"] == "exit-1"
    assert hc.status(env.repo)[0] == "FAIL" and hc.gate(env.repo)[0] is False


def test_timeout_is_fail(env):
    env.add("slow", "import time\ntime.sleep(60)\n")
    rep = hc.run_checks(env.repo, timeout_s=1)
    assert rep.status == "FAIL"
    assert json.loads(env.ledger().strip())["code"] == "timeout"


def test_tampered_file_fails_and_is_not_executed(env):
    env.add("m3-a", _script(env, 0))
    (env.cdir / "m3-a.py").write_bytes(f"open({str(env.side)!r}, 'w').write('ran')\n".encode())
    rep = hc.verify_hashes(env.repo)
    assert rep.status == "FAIL" and any("differs" in p for p in rep.problems)
    assert hc.run_checks(env.repo, timeout_s=60).status == "FAIL"
    assert not env.side.exists(), "a tampered script was executed"
    assert json.loads(env.ledger().strip())["code"] == "verify-failed"


def test_changed_between_verify_and_run_is_not_executed(env, monkeypatch):
    env.add("m3-a", _script(env, 0))
    real = hc.verify_hashes

    def tamper_after_verify(*a, **k):
        rep = real(*a, **k)
        (env.cdir / "m3-a.py").write_bytes(f"open({str(env.side)!r}, 'w').write('ran')\n".encode())
        return rep

    monkeypatch.setattr(hc, "verify_hashes", tamper_after_verify)
    rep = hc.run_checks(env.repo, timeout_s=60)
    assert rep.status == "FAIL" and not env.side.exists()
    assert json.loads(env.ledger().strip())["code"] == "changed-before-run"


def test_lock_registry_disagreement(env):
    env.add("m3-a", _script(env, 0))
    (env.repo / hc.LOCK).write_text(json.dumps({"m3-a": "0" * 64}))
    rep = hc.verify_hashes(env.repo)
    assert rep.status == "FAIL" and any("lock" in p for p in rep.problems)


def test_registered_but_not_locked_and_locked_but_not_registered(env):
    env.add("a", _script(env, 0), lock=False)
    assert hc.verify_hashes(env.repo).status == "FAIL"
    (env.cdir / "registry.json").unlink()
    (env.repo / hc.LOCK).write_text(json.dumps({"a": "1" * 64}))
    rep = hc.verify_hashes(env.repo)
    assert rep.status == "FAIL" and any("only one" in p for p in rep.problems)


def test_missing_file_and_malformed_registry(env):
    env.add("m3-a", _script(env, 0), write=False)
    rep = hc.verify_hashes(env.repo)
    assert rep.status == "FAIL" and any("missing" in p for p in rep.problems)
    (env.cdir / "registry.json").write_text("{not json")
    assert hc.verify_hashes(env.repo).status == "FAIL"


def test_registry_path_traversal_refused(env):
    env.add("m3-a", _script(env, 0))
    reg = json.loads((env.cdir / "registry.json").read_text())
    reg["checks"][0]["file"] = "../evil.py"
    (env.cdir / "registry.json").write_text(json.dumps(reg))
    assert hc.verify_hashes(env.repo).status == "FAIL"


def test_ledger_records_no_content(env, capfd):
    env.add("m3-a", _script(env, 0))
    env.add("m3-b", _script(env, 3))
    hc.run_checks(env.repo, timeout_s=60)
    text = env.ledger()
    assert SECRET not in text and "print(" not in text
    for ln in text.strip().splitlines():
        assert set(json.loads(ln)) == {"id", "sha256", "result", "code", "at", "head", "prev"}
    out = capfd.readouterr()
    assert SECRET not in out.out + out.err


def test_broken_chain_detected(env):
    env.add("m3-a", _script(env, 0))
    hc.run_checks(env.repo, timeout_s=60)
    hc.run_checks(env.repo, timeout_s=60)
    assert hc.check_chain(env.repo)[0]
    p = env.repo / hc.LEDGER
    lines = p.read_text().splitlines()
    lines[0] = lines[0].replace('"pass"', '"fail"')  # edit an earlier record
    p.write_text("\n".join(lines) + "\n")
    assert hc.check_chain(env.repo)[0] is False
    assert hc.gate(env.repo)[0] is False
    p.write_text("\n".join(lines[1:]) + "\n")  # drop the first record
    assert hc.check_chain(env.repo)[0] is False


def test_gate_fails_on_broken_chain_even_with_no_check(env):
    (env.repo / hc.LOCK).write_text("{}")
    (env.repo / hc.LEDGER).write_text('{"prev": "bogus"}\n')
    assert hc.gate(env.repo)[0] is False


def test_status_registered_not_run_is_not_pass(env):
    env.add("m3-a", _script(env, 0))
    st, _ = hc.status(env.repo)
    assert st == "REGISTERED-NOT-RUN"


def test_committed_state_is_not_met():
    """The repository itself ships an empty lock and an empty (or absent) run ledger."""
    assert json.loads((ROOT / hc.LOCK).read_text()) == {}
    led = ROOT / hc.LEDGER
    assert not led.exists() or led.read_text().strip() == ""


def _cli(flag: str, env: Env):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "human_check.py"), flag, "--repo-root",
                           str(env.repo)], capture_output=True, text=True, check=False)


def test_cli_gate_and_check_py_hook(env):
    (env.repo / hc.LOCK).write_text("{}")
    r = _cli("--gate", env)
    assert r.returncode == 0 and hc.NOT_MET_LINE in r.stdout
    env.add("m3-a", _script(env, 1))
    _cli("--run", env)
    r = _cli("--gate", env)
    assert r.returncode == 1 and SECRET not in r.stdout + r.stderr
    assert "scripts/human_check.py" in (ROOT / "scripts" / "check.py").read_text(encoding="utf-8")
