"""The `lean` adapter's sandbox runner: check an untrusted Lean proof against a human-approved statement.

Agent-written proofs are untrusted code (elaboration can run arbitrary code), so Lean runs only inside a sandbox
(invariant 1): in production the Docker image built from docker/lean.Dockerfile (pinned Lean + Mathlib, the pinned
checker prebuilt, no network, read-only root). The runner writes nothing the Kernel trusts by itself: it returns a
check record whose every claim the Kernel re-checks (custom_harness.kernel.formal).

Order, and why:
  0. hygiene scan of statement and proof BEFORE anything is compiled (a refused proof never runs);
  1. record the toolchain (`lean --version`, the project's lake-manifest.json, the checker source's sha256);
  2. compile the proof (`Solution.lean` -> out/Solution.olean) FIRST, then list `out/`: anything but the proof's
     own .olean/.ilean was written by compile-time code (it could shadow a library module) and the Kernel rejects;
  3. write the approved statement only now (the proof's build cannot have tampered with it) and compile it;
  4. run the pinned checker `chh-lean-check Challenge Solution <theorem> <nonce>`; the nonce binds its output to
     this statement, this proof and a salt drawn after the proof was fixed.

UNVERIFIED against a real toolchain (none installed): the exact `lake -d DIR env lean -o/-i` invocation, that
`lake env` keeps the LEAN_PATH entry `out`, and the image layout. Every such mismatch fails closed (a failed step).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import shutil
from typing import TYPE_CHECKING

from ..kernel import formal
from ..kernel.claim import canonical_json
from .base import ExecResult, Sandbox, is_link_like, safe_path

if TYPE_CHECKING:
    from ..kernel import Claim, Kernel

LEAN_IMAGE = f"custom-harness-lean:{formal.LEAN_VERSION}"
PROJECT_DIR = "/opt/chh-proj"  # lake project requiring the pinned Mathlib (built into the image)
CHECKER_DIR = "/opt/chh-lean-check"
CHECKER_BIN = f"{CHECKER_DIR}/.lake/build/bin/chh-lean-check"
OUT = "out"  # the only workspace directory on LEAN_PATH


class LeanRunError(ValueError):
    pass


def lean_available() -> bool:
    return shutil.which("lean") is not None and shutil.which("lake") is not None


def _step(argv: list[str], res: ExecResult) -> dict[str, object]:
    return {"argv": argv, "exit_code": res.returncode, "timed_out": res.timed_out, "stdout": res.stdout,
            "stderr": res.stderr}


def run_check(sandbox: Sandbox, statement: bytes, proof_source: str, *, salt: str | None = None,
              timeout_s: float = 3600) -> bytes:
    """Run the adapter's check of `proof_source` against the approved `statement` bytes; returns the record bytes."""
    try:
        st = formal.parse_statement(statement)
    except formal.FormalError as e:
        raise LeanRunError(f"unusable statement: {e}") from None
    problems = [f"statement: {r}" for r in formal.hygiene(st.source, statement=True)]
    problems += [f"proof: {r}" for r in formal.hygiene(proof_source, statement=False)]
    if problems:
        raise LeanRunError("refused before compiling (untrusted compile-time code is never run): "
                           + "; ".join(problems))
    ws = sandbox.workspace
    out = safe_path(ws, OUT)
    if out.exists() or is_link_like(out):
        raise LeanRunError("the runner needs a fresh workspace (out/ already exists)")
    salt = salt or secrets.token_hex(16)
    version = sandbox.exec(["lean", "--version"], timeout_s=120)
    manifest = sandbox.exec(["cat", f"{PROJECT_DIR}/lake-manifest.json"], timeout_s=120)
    checker = sandbox.exec(["sha256sum", f"{CHECKER_DIR}/ChhLeanCheck.lean"], timeout_s=120)
    m = re.match(r"[0-9a-f]{64}", checker.stdout)
    lake = ["lake", "-d", PROJECT_DIR, "env"]
    env = {"LEAN_PATH": OUT}
    out.mkdir()
    safe_path(ws, "Solution.lean").write_bytes(proof_source.encode())
    build_proof = [*lake, "lean", "-o", f"{OUT}/Solution.olean", "-i", f"{OUT}/Solution.ilean", "Solution.lean"]
    proof_res = sandbox.exec(build_proof, timeout_s=timeout_s, env=env)
    outputs = sorted(p.name for p in safe_path(ws, OUT).iterdir())
    safe_path(ws, "Challenge.lean").write_bytes(st.source.encode())
    build_statement = [*lake, "lean", "-o", f"{OUT}/Challenge.olean", "-i", f"{OUT}/Challenge.ilean",
                       "Challenge.lean"]
    statement_res = sandbox.exec(build_statement, timeout_s=timeout_s, env=env)
    st_sha = hashlib.sha256(statement).hexdigest()
    nonce = formal.nonce_for(st_sha, hashlib.sha256(proof_source.encode()).hexdigest(), salt)
    check = [*lake, CHECKER_BIN, "Challenge", "Solution", st.theorem, nonce]
    check_res = sandbox.exec(check, timeout_s=timeout_s, env=env)
    return canonical_json({
        "format": formal.RECORD_FORMAT, "statement_sha256": st_sha, "theorem": st.theorem,
        "proof_source": proof_source, "checker_sha256": m.group(0) if m else "0" * 64, "salt": salt, "nonce": nonce,
        "toolchain": {"lean_version": version.stdout, "manifest": manifest.stdout}, "proof_outputs": outputs,
        "steps": {"build_proof": _step(build_proof, proof_res),
                  "build_statement": _step(build_statement, statement_res), "check": _step(check, check_res)}})


def record_check(kernel: Kernel, claim: Claim, record: bytes) -> str:
    """Register a check record and link it to a formal claim as its PROOF evidence (trusted host code only)."""
    aid = f"lean-check-{claim.claim_hash()[:12]}-{hashlib.sha256(record).hexdigest()[:12]}"
    kernel.vault.register_artifact(aid, record, "lean-check")
    return kernel.record_certificate(claim, aid)
