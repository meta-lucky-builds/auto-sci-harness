"""The Lean adapter outside the Kernel: the sandbox runner, the human statement-approval CLI, the image recipe.

The runner is tested against FakeLeanSandbox, a deterministic stand-in that answers the runner's commands the way
the pinned image would (its "comparator" compares theorem header lines as TEXT; the real one compares elaborated
types). Tests that need a real Lean toolchain are skipped when `lean`/`lake` or the Lean image are absent, which is
the case on the development machine: NOTHING here has been run against real Lean.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from custom_harness.cli import main
from custom_harness.config import data_dir
from custom_harness.kernel import Label, Ledger, formal
from custom_harness.sandbox import ExecResult, Sandbox
from custom_harness.sandbox import lean as L
from custom_harness.store import EvidenceStore
from custom_harness.verify import lean_validation as V
from kernel_world import World
from test_kernel_formal import PIN, formal_claim

ROOT = Path(__file__).resolve().parents[1]
_HEADER = re.compile(r"theorem[^:]*:[^\n]*:=")


def _ok(stdout: str = "", code: int = 0) -> ExecResult:
    return ExecResult(returncode=code, stdout=stdout, stderr="", wall_s=0.01)


class FakeLeanSandbox(Sandbox):
    """Answers the runner's commands like the pinned Lean image would. Deterministic; runs nothing."""

    isolation = "fake Lean sandbox (tests only; runs nothing)"

    def __init__(self, workspace: Path, *, plant: str | None = None, overwrite_challenge: bool = False,
                 axioms: tuple[str, ...] = ()):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.plant, self.overwrite_challenge, self.axioms = plant, overwrite_challenge, axioms
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.challenge_seen: str | None = None

    def spawn(self, workspace: Path) -> Sandbox:  # pragma: no cover - the runner never spawns
        raise NotImplementedError

    def environment(self) -> dict[str, str]:  # pragma: no cover
        return {"sandbox": self.isolation}

    def exec(self, command: list[str], timeout_s: float = 600, env: dict[str, str] | None = None) -> ExecResult:
        self.calls.append((list(command), dict(env or {})))
        ws = self.workspace
        if command == ["lean", "--version"]:
            return _ok(f"Lean (version {formal.LEAN_VERSION}, x86_64-unknown-linux-gnu, commit 0123abcd, Release)\n")
        if command[0] == "cat":
            return _ok(V.record(V.statement_bytes())["toolchain"]["manifest"])
        if command[0] == "sha256sum":
            return _ok(f"{formal.CHECKER_SHA256}  {command[1]}\n")
        if "-o" in command:  # lake env lean -o out/X.olean -i out/X.ilean X.lean
            (ws / command[command.index("-o") + 1]).write_bytes(b"olean")
            (ws / command[command.index("-i") + 1]).write_bytes(b"ilean")
            if command[-1] == "Solution.lean":
                if self.plant:
                    (ws / self.plant).write_bytes(b"planted by compile-time code")
                if self.overwrite_challenge:
                    (ws / "Challenge.lean").write_text("theorem Chh.add_comm' : True := sorry\n")
                return _ok("")
            self.challenge_seen = (ws / "Challenge.lean").read_text(encoding="utf-8")
            return _ok("Challenge.lean:3:8: warning: declaration uses 'sorry'\n")
        if L.CHECKER_BIN in command:
            thm, nonce = command[-2], command[-1]
            chal = _HEADER.findall((ws / "Challenge.lean").read_text(encoding="utf-8"))
            sol = _HEADER.findall((ws / "Solution.lean").read_text(encoding="utf-8"))
            match = bool(chal) and chal == sol
            res = {"theorem": thm, "kind": "theorem", "statement_match": match, "axioms": list(self.axioms),
                   "replayed": 3, "problems": [] if match else [f"the type of {thm} differs"]}
            return _ok(f"{formal.RESULT_TAG} {nonce} {json.dumps(res)}\n")
        return _ok("", code=127)  # pragma: no cover - an unexpected command fails


@pytest.fixture()
def fw(tmp_path, monkeypatch):
    monkeypatch.setattr(formal, "MATHLIB_COMMIT", PIN)
    w = World(tmp_path / "f", seed=15, n_train=30, n_test=30)
    w.kernel.approve_adapter("lean", approver="test-world")
    yield w
    w.close()


def _certify(w: World, claim, record: bytes):
    w.kernel.preregister(claim)
    return w.kernel.certify(claim, [L.record_check(w.kernel, claim, record)])


def test_runner_record_is_certified_by_the_kernel(fw, tmp_path):
    sb = FakeLeanSandbox(tmp_path / "ws", axioms=("propext",))
    claim = formal_claim(fw)
    record = L.run_check(sb, V.statement_bytes(), V.PROOF_SOURCE, salt="ab" * 16)
    assert formal.check(V.statement_bytes(), record) == []
    cert = _certify(fw, claim, record)
    assert cert.label == Label.CERTIFIED_FORMAL, {k: v.reasons for k, v in cert.checks.items() if v.status != "pass"}
    argvs = [c for c, _ in sb.calls]
    order = [a[-1] for a in argvs if "-o" in a]
    assert order == ["Solution.lean", "Challenge.lean"]  # the proof is compiled BEFORE the statement is written
    lake = [a for a in argvs if a[0] == "lake"]
    assert all(a[:4] == ["lake", "-d", L.PROJECT_DIR, "env"] for a in lake) and len(lake) == 3
    assert all(env.get("LEAN_PATH") == "out" for a, env in sb.calls if a[0] == "lake")
    assert argvs[-1][-4:-2] == ["Challenge", "Solution"]
    doc = json.loads(record)
    assert doc["steps"]["check"]["argv"] == argvs[-1] and doc["proof_outputs"] == ["Solution.ilean", "Solution.olean"]


def test_runner_refuses_untrusted_compile_time_code_before_compiling(tmp_path):
    sb = FakeLeanSandbox(tmp_path / "ws")
    for proof in (V.PROOF_SOURCE.replace("exact Nat.add_comm a b", "sorry"), "#eval IO.println 1\n" + V.PROOF_SOURCE,
                  "macro \"x\" : term => `(1)\n" + V.PROOF_SOURCE):
        with pytest.raises(L.LeanRunError, match="refused before compiling"):
            L.run_check(sb, V.statement_bytes(), proof)
    with pytest.raises(L.LeanRunError, match="statement"):
        L.run_check(sb, b"not a statement", V.PROOF_SOURCE)
    with pytest.raises(L.LeanRunError, match="refused before compiling"):
        L.run_check(sb, V.statement_bytes(source="axiom x : False\n"), V.PROOF_SOURCE)
    assert sb.calls == []  # nothing was executed for any refused input


def test_runner_needs_a_fresh_workspace(tmp_path):
    sb = FakeLeanSandbox(tmp_path / "ws")
    (sb.workspace / "out").mkdir()
    with pytest.raises(L.LeanRunError, match="fresh workspace"):
        L.run_check(sb, V.statement_bytes(), V.PROOF_SOURCE)


def test_compile_time_writes_into_the_module_path_are_rejected(fw, tmp_path):
    sb = FakeLeanSandbox(tmp_path / "ws", plant="out/Mathlib.olean")  # would shadow a library module
    record = L.run_check(sb, V.statement_bytes(), V.PROOF_SOURCE)
    assert json.loads(record)["proof_outputs"] == ["Mathlib.olean", "Solution.ilean", "Solution.olean"]
    cert = _certify(fw, formal_claim(fw), record)
    assert cert.failed() == ["K3"] and "wrote unexpected files ['Mathlib.olean']" in " ".join(cert.checks["K3"].reasons)


def test_the_approved_statement_is_written_after_the_proof_build(fw, tmp_path):
    sb = FakeLeanSandbox(tmp_path / "ws", overwrite_challenge=True)  # the proof build tampers with Challenge.lean
    record = L.run_check(sb, V.statement_bytes(), V.PROOF_SOURCE)
    assert sb.challenge_seen == V.STATEMENT_SOURCE  # the statement compiled is the approved one, rewritten after
    assert _certify(fw, formal_claim(fw), record).label == Label.CERTIFIED_FORMAL


def test_a_weakened_restatement_is_rejected_via_the_comparator(fw, tmp_path):
    weaker = V.PROOF_SOURCE.replace("(a b : Nat) :", "(a b : Nat) (h : a = b) :")
    record = L.run_check(FakeLeanSandbox(tmp_path / "ws"), V.statement_bytes(), weaker)
    cert = _certify(fw, formal_claim(fw), record)
    assert cert.failed() == ["K3"] and "statement drift" in " ".join(cert.checks["K3"].reasons)


def test_lean_available_reports_the_toolchain():
    assert L.lean_available() == (shutil.which("lean") is not None and shutil.which("lake") is not None)


# ------------------------------------------------------------------ the HUMAN statement approval
def _ledger_entries(kind: str):
    s = EvidenceStore(data_dir() / "store")
    try:
        return Ledger(s).entries(kind)
    finally:
        s.close()


def test_cli_approve_statement_records_the_human_approval(tmp_path, capsys):
    path = tmp_path / "statement.json"
    path.write_bytes(V.statement_bytes())
    digest = __import__("hashlib").sha256(V.statement_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="does not match"):
        main(["approve-statement", str(path), "--artifact-id", "thm", "--approver", "alice",
              "--statement-hash", "0" * 64])
    with pytest.raises(SystemExit, match="must name a human"):
        main(["approve-statement", str(path), "--artifact-id", "thm", "--approver", "  "])
    assert _ledger_entries("statement_approval") == []
    assert main(["approve-statement", str(path), "--artifact-id", "thm", "--approver", "alice",
                 "--statement-hash", digest]) == 0
    out = capsys.readouterr().out
    assert V.STATEMENT_SOURCE in out and digest in out  # the human sees exactly what they approve
    (entry,) = _ledger_entries("statement_approval")
    assert entry.body == {"artifact_id": "thm", "hash": digest, "theorem": V.THEOREM, "approver": "alice",
                          "note": ""}
    other = tmp_path / "other.json"
    other.write_bytes(V.statement_bytes(theorem="Chh.other"))
    with pytest.raises(SystemExit, match="already registered with different content"):
        main(["approve-statement", str(other), "--artifact-id", "thm", "--approver", "alice"])
    bad = tmp_path / "bad.json"
    bad.write_bytes(V.statement_bytes(source=V.STATEMENT_SOURCE + "#eval 1\n"))
    with pytest.raises(SystemExit, match="refused: forbidden"):
        main(["approve-statement", str(bad), "--artifact-id", "thm2", "--approver", "alice"])
    bad.write_bytes(b"{")
    with pytest.raises(SystemExit, match="refused: not JSON"):
        main(["approve-statement", str(bad), "--artifact-id", "thm2", "--approver", "alice"])
    assert len(_ledger_entries("statement_approval")) == 1


def test_only_the_human_cli_writes_statement_approvals():
    """Invariant 15: no model-reachable code path appends a statement approval (only `chh approve-statement`)."""
    writers = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "src").rglob("*.py")
                     if re.search(r"append\(\s*\"statement_approval\"", p.read_text(encoding="utf-8")))
    assert writers == ["src/custom_harness/cli.py"]


# ------------------------------------------------------------------ the image recipe (NOT built here)
def test_lean_dockerfile_pins_the_adapter_toolchain():
    text = (ROOT / "docker" / "lean.Dockerfile").read_text(encoding="utf-8")
    assert f"leanprover/lean4:v{formal.LEAN_VERSION}" in text and f'"{formal.MATHLIB_INPUT_REV}"' in text
    assert "COPY lean/ChhLeanCheck.lean" in text and "USER 1000:1000" in text and "latest" not in text
    assert L.CHECKER_BIN.startswith("/opt/chh-lean-check/") and L.PROJECT_DIR == "/opt/chh-proj"
    assert (ROOT / "docker" / "lean" / "checker-lakefile.toml").is_file()
    assert L.LEAN_IMAGE == f"custom-harness-lean:{formal.LEAN_VERSION}"


# ------------------------------------------------------------------ real Lean (skipped without a toolchain)
@pytest.mark.skipif(shutil.which("lean") is None, reason="no Lean toolchain installed (owner's decision)")
def test_real_lean_version_output_parses():  # pragma: no cover - needs a Lean toolchain
    out = subprocess.run(["lean", "--version"], capture_output=True, text=True, check=True).stdout
    assert formal.lean_version(out) is not None, out


def _lean_image_present() -> bool:
    exe = shutil.which("docker")
    if exe is None:
        return False
    try:
        p = subprocess.run([exe, "image", "inspect", L.LEAN_IMAGE], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


@pytest.mark.skipif(not _lean_image_present(), reason=f"Lean image {L.LEAN_IMAGE} not built (owner's decision)")
def test_real_lean_image_checks_the_golden_proof_and_rejects_sorry(tmp_path):  # pragma: no cover - needs the image
    from custom_harness.sandbox.docker import DockerSandbox
    good = L.run_check(DockerSandbox(tmp_path / "good", image=L.LEAN_IMAGE, memory="8g"), V.statement_bytes(),
                       V.PROOF_SOURCE)
    assert formal.check(V.statement_bytes(), good) == [], json.loads(good)
    weaker = V.PROOF_SOURCE.replace("(a b : Nat) :", "(a b : Nat) (h : a = b) :")
    drift = L.run_check(DockerSandbox(tmp_path / "drift", image=L.LEAN_IMAGE, memory="8g"), V.statement_bytes(),
                        weaker)
    assert any("statement drift" in r for r in formal.check(V.statement_bytes(), drift))
