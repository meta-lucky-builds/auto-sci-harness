"""Fault-injection validation of the `lean` adapter (GOAL §5 "Domain adapters", L3 "verifier validation").

A golden check record must be accepted and every planted fault must be rejected FOR THE RIGHT REASON by the Kernel's
record checker (custom_harness.kernel.formal). The records are SYNTHETIC: they reproduce the layout the adapter's
sandbox runner writes and the line the pinned Lean checker prints (docker/lean/ChhLeanCheck.lean), built in Python.
None was captured from a real Lean toolchain (none was installed when this was written); the owner should replace
the golden record with a captured one once the Lean image exists (docs/adapters_lean.md).

This module is part of the adapter's content hash: changing a fixture needs a new human approval.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from ..kernel import formal
from ..kernel.claim import canonical_json

THEOREM = "Chh.add_comm'"
STATEMENT_SOURCE = ("namespace Chh\n\ntheorem add_comm' (a b : Nat) : a + b = b + a := by\n  sorry\n\nend Chh\n")
PROOF_SOURCE = ("namespace Chh\n\ntheorem add_comm' (a b : Nat) : a + b = b + a := by\n  exact Nat.add_comm a b\n\n"
                "end Chh\n")
SALT = "5a" * 16


def statement_bytes(theorem: str = THEOREM, source: str = STATEMENT_SOURCE) -> bytes:
    return canonical_json({"format": formal.STATEMENT_FORMAT, "theorem": theorem, "source": source})


def _step(argv: list[str], stdout: str = "") -> dict[str, Any]:
    return {"argv": argv, "exit_code": 0, "timed_out": False, "stdout": stdout, "stderr": ""}


def record(statement: bytes | None = None, proof: str = PROOF_SOURCE, *, salt: str = SALT,
           result_over: dict[str, Any] | None = None, check_stdout: str | None = None) -> dict[str, Any]:
    """The check record the runner would write for (statement, proof) if Lean accepted the proof (synthetic)."""
    st = statement_bytes() if statement is None else statement
    st_sha, proof_sha = hashlib.sha256(st).hexdigest(), hashlib.sha256(proof.encode()).hexdigest()
    try:
        theorem = formal.parse_statement(st).theorem
    except formal.FormalError:
        theorem = THEOREM
    nonce = formal.nonce_for(st_sha, proof_sha, salt)
    res = {"theorem": theorem, "kind": "theorem", "statement_match": True, "axioms": [], "replayed": 3,
           "problems": [], **(result_over or {})}
    stdout = f"{formal.RESULT_TAG} {nonce} {json.dumps(res, sort_keys=True)}\n" if check_stdout is None \
        else check_stdout
    manifest = {"version": "1.1.0", "packagesDir": ".lake/packages", "name": "chhproj", "lakeDir": ".lake",
                "packages": [{"type": "git", "scope": "leanprover-community", "name": "mathlib",
                              "url": "https://github.com/leanprover-community/mathlib4",
                              "rev": formal.MATHLIB_COMMIT, "inputRev": formal.MATHLIB_INPUT_REV,
                              "inherited": False, "manifestFile": "lake-manifest.json"}]}
    lake = ["lake", "-d", "/opt/chh-proj", "env"]
    return {
        "format": formal.RECORD_FORMAT, "statement_sha256": st_sha, "theorem": theorem, "proof_source": proof,
        "checker_sha256": formal.CHECKER_SHA256, "salt": salt, "nonce": nonce,
        "toolchain": {"lean_version": f"Lean (version {formal.LEAN_VERSION}, x86_64-unknown-linux-gnu, commit "
                                      f"0123456789ab, Release)", "manifest": json.dumps(manifest)},
        "proof_outputs": list(formal.EXPECTED_PROOF_OUTPUTS),
        "steps": {
            "build_proof": _step([*lake, "lean", "-o", "out/Solution.olean", "-i", "out/Solution.ilean",
                                  "Solution.lean"]),
            "build_statement": _step([*lake, "lean", "-o", "out/Challenge.olean", "-i", "out/Challenge.ilean",
                                      "Challenge.lean"], "Challenge.lean:3:8: warning: declaration uses 'sorry'\n"),
            "check": _step([*lake, "/opt/chh-lean-check/.lake/build/bin/chh-lean-check", "Challenge", "Solution",
                            theorem, nonce], stdout)}}


def _check(rec: dict[str, Any], statement: bytes | None = None) -> list[str]:
    return formal.check(statement_bytes() if statement is None else statement, canonical_json(rec))


def _rejects(rec: dict[str, Any], why: str, statement: bytes | None = None) -> bool:
    """The planted fault is rejected FOR THE RIGHT REASON (a checker failing for another reason fails validation)."""
    return any(why in r for r in _check(rec, statement))


def _with(rec: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    node = rec
    keys = path.split("/")
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    return rec


def _sorry_proof() -> str:
    return PROOF_SOURCE.replace("exact Nat.add_comm a b", "sorry")


def _manifest(**over: str) -> str:
    doc = json.loads(record()["toolchain"]["manifest"])
    doc["packages"][0] |= over
    return json.dumps(doc)


LEAN_CHECKS: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("the Lean version and the Mathlib commit are pinned", lambda: formal.pinned() == []),
    ("a correct check record is accepted", lambda: _check(record()) == []),
    ("sorry in the proof is rejected", lambda: _rejects(record(proof=_sorry_proof()), "forbidden 'sorry'")),
    ("sorryAx in the axiom audit is rejected", lambda: _rejects(record(result_over={"axioms": ["sorryAx"]}),
                                                                "uses sorry (sorryAx)")),
    ("a new axiom is rejected (source and audit)",
     lambda: _rejects(record(proof="axiom cheat : False\n" + PROOF_SOURCE), "forbidden 'axiom'")
     and _rejects(record(result_over={"axioms": ["Chh.cheat"]}), "non-allowlisted axiom 'Chh.cheat'")),
    ("native_decide is rejected (source and audit)",
     lambda: _rejects(record(proof=PROOF_SOURCE.replace("exact Nat.add_comm a b", "native_decide")),
                      "forbidden 'native_decide'")
     and _rejects(record(result_over={"axioms": ["Lean.ofReduceBool"]}), "native_decide (Lean.ofReduceBool)")),
    ("statement drift reported by the comparator is rejected",
     lambda: _rejects(record(result_over={"statement_match": False}), "statement drift")),
    ("a record of a different statement is rejected",
     lambda: _rejects(record(statement_bytes(source=STATEMENT_SOURCE.replace("b + a", "a + b"))),
                      "statement drift: the record was produced for statement")),
    ("a build failure is rejected",
     lambda: _rejects(_with(record(), "steps/build_proof/exit_code", 1), "building the proof failed (exit 1)")),
    ("a wrong Lean version is rejected",
     lambda: _rejects(_with(record(), "toolchain/lean_version", "Lean (version 4.0.0, commit 1, Release)"),
                      "wrong toolchain: Lean 4.0.0")),
    ("a wrong Mathlib commit is rejected",
     lambda: _rejects(_with(record(), "toolchain/manifest", _manifest(rev="f" * 40)), "wrong Mathlib commit")),
    ("parse garbage is rejected",
     lambda: any("unreadable" in r for r in formal.check(statement_bytes(), b"\x00garbage"))
     and _rejects(record(check_stdout=f"{formal.RESULT_TAG} x {{\n"), "not bound to this record's nonce")),
    ("a result spliced from another proof is rejected",
     lambda: _rejects(_with(record(), "salt", "1" * 32), "nonce does not bind this statement and proof")),
    ("files written while compiling the proof are rejected",
     lambda: _rejects(_with(record(), "proof_outputs", ["Init.olean", *formal.EXPECTED_PROOF_OUTPUTS]),
                      "wrote unexpected files")),
    ("metaprogramming in the proof is rejected",
     lambda: _rejects(record(proof="macro \"t\" : term => `(0)\n" + PROOF_SOURCE), "forbidden 'macro'")),
)
