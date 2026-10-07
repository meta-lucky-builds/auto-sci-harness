"""M3: the formal evidence class (Lean 4 adapter, tier T0; GOAL §4.7, §5 "Domain adapters", L3 "T0 split").

A formal claim = a theorem statement a HUMAN approved (its hash in a `statement_approval` ledger entry) + a Lean
check record produced by the adapter's sandbox runner. The Kernel never runs Lean: it re-checks the recorded,
hash-bound output with custom_harness.kernel.formal (toolchain pin, proof hygiene, build status, the comparator's
statement-fidelity verdict, the axiom audit, the nonce binding).

Every record here is SYNTHETIC (custom_harness.verify.lean_validation builds what the runner and the Lean checker
would print). Nothing in this file was produced by a real Lean toolchain.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from custom_harness.kernel import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    Effect,
    EvidenceClass,
    Label,
    MetricRef,
    Scope,
    formal,
)
from custom_harness.kernel import adapters as A
from custom_harness.kernel import checks as K
from custom_harness.kernel.bundle import build_bundle, reproduce_bundle
from custom_harness.kernel.certificate import CHECK_IDS, VG_LADDER, Certificate
from custom_harness.kernel.claim import SUPPORTED_KINDS, canonical_json
from custom_harness.kernel.execution import SandboxReplayer
from custom_harness.sandbox import LocalSandbox
from custom_harness.tiers import Tier
from custom_harness.verify import lean_validation as V
from kernel_world import World

ROOT = Path(__file__).resolve().parents[1]
PIN = "0123456789abcdef0123456789abcdef01234567"  # a stand-in for the owner's Mathlib commit pin (tests only)


def reasons(res) -> str:
    return " | ".join(res.reasons)


@pytest.fixture()
def pinned(monkeypatch):
    monkeypatch.setattr(formal, "MATHLIB_COMMIT", PIN)


@pytest.fixture()
def fw(tmp_path, pinned):
    w = World(tmp_path / "f", seed=13, n_train=30, n_test=30)
    w.kernel.approve_adapter("lean", approver="test-world")  # the test world's stand-in for the human approval
    yield w
    w.close()


def approve_statement(w: World, data: bytes | None = None, artifact_id: str = "thm", approver: str = "test-human"
                      ) -> ArtifactRef:
    """The test world's stand-in for `chh approve-statement` (the only production writer of this entry kind)."""
    data = V.statement_bytes() if data is None else data
    art = w.vault.register_artifact(artifact_id, data, "lean-statement")
    w.kernel.ledger.append("statement_approval", {"artifact_id": artifact_id, "hash": art.hash,
                                                  "theorem": V.THEOREM, "approver": approver, "note": ""})
    return ArtifactRef(artifact_id=artifact_id, hash=art.hash)


def formal_claim(w: World, ref: ArtifactRef | None = None, **changes) -> Claim:
    ref = ref or approve_statement(w)
    doc = Claim(kind=ClaimKind.IMPROVEMENT, statement="Chh.add_comm' holds", subject=ref, comparator=ref,
                metric=MetricRef(id="lean_theorem", higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=()), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    doc |= {"kind": "theorem", "evidence_class": "formal"}
    for path, value in changes.items():
        node = doc
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return Claim.model_validate(doc)


def record_artifact(w: World, claim: Claim, rec: dict | bytes) -> str:
    data = rec if isinstance(rec, bytes) else canonical_json(rec)
    aid = f"lean-check-{claim.claim_hash()[:10]}-{hashlib.sha256(data).hexdigest()[:10]}"
    return w.vault.register_artifact(aid, data, "lean-check").artifact_id


def formal_certify(w: World, claim: Claim, rec: dict | bytes | None = None) -> Certificate:
    w.kernel.preregister(claim)
    statement = w.store.blobs.get(claim.subject.hash)
    eid = w.kernel.record_certificate(claim, record_artifact(w, claim, V.record(statement) if rec is None else rec))
    return w.kernel.certify(claim, [eid])


# ------------------------------------------------------------------ the honest case: certified at T0
def test_formal_claim_certifies_at_t0_with_the_lean_adapter(fw):
    claim = formal_claim(fw)
    rec = V.record(V.statement_bytes(), result_over={"axioms": ["propext", "Classical.choice", "Quot.sound"]})
    cert = formal_certify(fw, claim, rec)
    assert cert.label == Label.CERTIFIED_FORMAL, {k: v.reasons for k, v in cert.checks.items() if v.status != "pass"}
    assert cert.tier == int(Tier.T0_FORMAL) == 0
    assert [k for k in CHECK_IDS if cert.checks[k].status == "pass"] == ["K0", "K1", "K2", "K3", "K8", "K9"]
    table = K.CHECK_APPLICABILITY[EvidenceClass.FORMAL]
    for kid in ("K4", "K5", "K6", "K7"):
        assert cert.checks[kid].status == "n/a" and cert.checks[kid].reasons == (table[kid],)
    assert "OWNER DECISION PENDING" in table["K7"] and "GOAL §13.3" in table["K7"]  # not an invented K7 rule
    (used,) = cert.adapters
    assert used["name"] == "lean" and used["approver"] == "test-world"
    assert "lean:lean_theorem" in cert.verifier_ids
    assert cert.vg_ladder == VG_LADDER["CERTIFIED-FORMAL"] and "Lean" in cert.vg_ladder
    texts = [a.text for a in cert.assumptions]
    assert any("Lean 4 kernel" in t for t in texts) and any("approved statement" in t for t in texts)
    for ax in ("propext", "Classical.choice", "Quot.sound"):  # the certificate lists the axioms it relies on
        assert any(t == f"K3: relies on axiom {ax}" for t in texts), ax
    assert fw.kernel.verify(cert.cert_hash()) == []
    assert b'"evidence_class":"formal"' in claim.canonical() and b'"kind":"theorem"' in claim.canonical()


def test_formal_certificate_bundle_reproduces_by_rechecking_the_record(fw, tmp_path, monkeypatch):
    cert = formal_certify(fw, formal_claim(fw))
    path = tmp_path / "formal.zip"
    build_bundle(fw.kernel, cert.cert_hash(), path)
    fresh = lambda: SandboxReplayer(lambda key: LocalSandbox(tmp_path / "rp" / key, i_understand_no_isolation=True))  # noqa: E731
    rep = reproduce_bundle(path, fresh)  # the statement approval travels in the ledger excerpt (K1 re-checks it)
    assert rep.reproduced and rep.reproduced_label == "CERTIFIED-FORMAL", rep.problems
    monkeypatch.setattr(formal, "check", lambda *a: ["planted: the checker now rejects"])
    rep = reproduce_bundle(path, fresh)
    assert not rep.reproduced and any(p.startswith("K3: pass originally, fail now") for p in rep.problems)


# ------------------------------------------------------------------ fault injection: each planted fault, right reason
def _drift_statement() -> bytes:  # the proof was checked against a WEAKER statement than the approved one
    return V.statement_bytes(source=V.STATEMENT_SOURCE.replace("(a b : Nat)", "(a b : Nat) (h : a = b)"))


FAULTS: dict[str, tuple[dict, str]] = {
    # proof hygiene (checked by the Kernel on the stored proof bytes, independent of anything Lean printed)
    "sorry in the proof": ({"proof": V.PROOF_SOURCE.replace("exact Nat.add_comm a b", "sorry")}, "forbidden 'sorry'"),
    "admit in the proof": ({"proof": V.PROOF_SOURCE.replace("exact Nat.add_comm a b", "admit")}, "forbidden 'admit'"),
    "a new axiom declaration": ({"proof": "axiom cheat : False\n" + V.PROOF_SOURCE}, "forbidden 'axiom'"),
    "native_decide": ({"proof": V.PROOF_SOURCE.replace("exact Nat.add_comm a b", "native_decide")},
                      "forbidden 'native_decide'"),
    # the environment-level audit (what the trusted Lean checker reports)
    "sorryAx in the axiom audit": ({"result_over": {"axioms": ["propext", "sorryAx"]}}, "uses sorry (sorryAx)"),
    "an extra axiom in the audit": ({"result_over": {"axioms": ["Chh.cheat"]}}, "non-allowlisted axiom 'Chh.cheat'"),
    "native_decide in the audit": ({"result_over": {"axioms": ["Lean.ofReduceBool"]}},
                                   "native_decide (Lean.ofReduceBool)"),
    "trustCompiler in the audit": ({"result_over": {"axioms": ["Lean.trustCompiler"]}},
                                   "native_decide (Lean.trustCompiler)"),
    "statement drift (comparator)": ({"result_over": {"statement_match": False,
                                                      "problems": ["the type of Chh.add_comm' differs"]}},
                                     "statement drift: the proved theorem's type differs"),
    "checker problems": ({"result_over": {"problems": ["Chh.x is unsafe"]}}, "checker reported: Chh.x is unsafe"),
    "theorem declared as an axiom": ({"result_over": {"kind": "axiom"}}, "is a 'axiom', not a theorem"),
    "nothing replayed": ({"result_over": {"replayed": 0}}, "replayed no declaration"),
    "wrong theorem in the result": ({"result_over": {"theorem": "Chh.other"}}, "checker result is for theorem"),
    "proof build failed": ({"steps": {"build_proof": {"exit_code": 1, "stdout": "Solution.lean:3:2: error: x\n"}}},
                           "building the proof failed (exit 1)"),
    "error message with exit 0": ({"steps": {"build_proof": {"stdout": "Solution.lean:3:2: error: unknown id\n"}}},
                                  "building the proof reported an error"),
    "sorry warning in the proof build": (
        {"steps": {"build_proof": {"stdout": "Solution.lean:1:8: warning: declaration uses 'sorry'\n"}}},
        "building the proof reported a sorry"),
    "proof build timed out": ({"steps": {"build_proof": {"timed_out": True}}}, "building the proof timed out"),
    "statement build failed": ({"steps": {"build_statement": {"exit_code": 1}}},
                               "building the approved statement failed (exit 1)"),
    "checker crashed": ({"steps": {"check": {"exit_code": 2, "stdout": ""}}}, "the checker failed (exit 2)"),
    "checker timed out": ({"steps": {"check": {"timed_out": True}}}, "the checker timed out"),
    "wrong Lean version": ({"lean_version": "Lean (version 4.3.0, x86_64-unknown-linux-gnu, commit 1, Release)"},
                           "wrong toolchain: Lean 4.3.0"),
    "unreadable Lean version": ({"lean_version": "lean: command not found"}, "Lean version output unreadable"),
    "wrong Mathlib commit": ({"mathlib": {"rev": "f" * 40}}, "wrong Mathlib commit"),
    "wrong Mathlib input rev": ({"mathlib": {"inputRev": "master"}}, "wrong Mathlib input revision 'master'"),
    "no Mathlib in the manifest": ({"manifest": json.dumps({"packages": []})}, "exactly one mathlib package"),
    "unreadable manifest": ({"manifest": "not json"}, "lake manifest unreadable"),
    "manifest without packages": ({"manifest": json.dumps({"packages": "x"})}, "lake manifest unreadable"),
    "a different checker": ({"top": {"checker_sha256": "a" * 64}}, "not the adapter's pinned checker"),
    "compile-time code wrote files": ({"top": {"proof_outputs": ["Mathlib.olean", "Solution.ilean",
                                                                 "Solution.olean"]}},
                                      "building the proof wrote unexpected files ['Mathlib.olean']"),
    "nonce not bound to the proof (spliced result)": ({"top": {"salt": "1" * 32}},
                                                       "nonce does not bind this statement and proof"),
    "result line with another nonce": ({"check_stdout": "CHH-LEAN-RESULT " + "e" * 64 + " {}\n"},
                                       "not bound to this record's nonce"),
    "two result lines": ({"check_stdout": "CHH-LEAN-RESULT a {}\nCHH-LEAN-RESULT b {}\n"},
                         "not exactly one result line"),
    "extra output before the result": ({"check_stdout": "hello\n"}, "not exactly one result line"),
    "garbage result json": ({"check_stdout_json": "{not json"}, "checker result unreadable"),
    "result json with wrong keys": ({"check_stdout_json": json.dumps({"ok": True})}, "checker result unreadable"),
    "result with a non-list axioms field": ({"result_over": {"axioms": "propext"}}, "checker result unreadable"),
    "result with a bool replayed count": ({"result_over": {"replayed": True}}, "checker result unreadable"),
    "record checked a different theorem": ({"top": {"theorem": "Chh.other"}}, "statement drift: the record checks"),
}


def _planted(change: dict, statement: bytes) -> dict:
    rec = V.record(statement, proof=change.get("proof", V.PROOF_SOURCE), result_over=change.get("result_over"),
                   check_stdout=change.get("check_stdout"))
    if "check_stdout_json" in change:
        rec["steps"]["check"]["stdout"] = f"{formal.RESULT_TAG} {rec['nonce']} {change['check_stdout_json']}\n"
    for step, fields in change.get("steps", {}).items():
        rec["steps"][step] |= fields
    if "lean_version" in change:
        rec["toolchain"]["lean_version"] = change["lean_version"]
    if "manifest" in change:
        rec["toolchain"]["manifest"] = change["manifest"]
    if "mathlib" in change:
        manifest = json.loads(rec["toolchain"]["manifest"])
        manifest["packages"][0] |= change["mathlib"]
        rec["toolchain"]["manifest"] = json.dumps(manifest)
    rec |= change.get("top", {})
    return rec


def test_formal_planted_faults_are_rejected_by_k3_for_the_right_reason(fw):
    statement = V.statement_bytes()
    ref = approve_statement(fw, statement)
    for name, (change, why) in FAULTS.items():
        claim = formal_claim(fw, ref, statement=f"fault: {name}")
        cert = formal_certify(fw, claim, _planted(change, statement))
        assert cert.label == Label.REJECTED and "K3" in cert.failed(), (name, cert.failed())
        assert why in reasons(cert.checks["K3"]), (name, cert.checks["K3"].reasons)
        assert cert.failed() == ["K3"], (name, cert.failed())  # nothing else is blamed for a K3 fault


def test_statement_drift_against_the_approved_statement_is_rejected(fw):
    approved = approve_statement(fw)
    claim = formal_claim(fw, approved)
    drifted = V.record(_drift_statement())  # a faithful record, but of a DIFFERENT (unapproved) statement
    cert = formal_certify(fw, claim, drifted)
    assert cert.failed() == ["K3"] and "statement drift: the record was produced for statement" in reasons(
        cert.checks["K3"])
    renamed = V.statement_bytes(theorem="Chh.add_comm''")  # renamed-but-different declaration, approved separately
    other = formal_claim(fw, approve_statement(fw, renamed, artifact_id="thm-renamed"))
    cert = formal_certify(fw, other, V.record(renamed, result_over={"theorem": V.THEOREM}))
    assert "checker result is for theorem" in reasons(cert.checks["K3"])


def test_garbage_records_and_statements_fail_closed(fw):
    ref = approve_statement(fw)
    for raw, why in ((b"garbage", "unreadable: not JSON"), (canonical_json([1]), "not a chh-lean-check/1"),
                     (canonical_json({**V.record(V.statement_bytes()), "extra": 1}), "unexpected keys"),
                     (b" " * (formal.MAX_BYTES + 1), "larger than")):
        cert = formal_certify(fw, formal_claim(fw, ref, statement=f"garbage {raw[:8]!r}"), raw)
        assert "K3" in cert.failed() and why in reasons(cert.checks["K3"]), why
        assert "K8" in cert.failed()
    bad = V.statement_bytes(source=V.STATEMENT_SOURCE + "\naxiom cheat : False\n")
    claim = formal_claim(fw, approve_statement(fw, bad, artifact_id="thm-axiom"))
    cert = formal_certify(fw, claim, V.record(bad))
    assert "statement: forbidden 'axiom'" in reasons(cert.checks["K3"])


def test_formal_size_bounds_are_k8(fw, monkeypatch):
    monkeypatch.setattr(formal, "MAX_SOURCE_BYTES", 40)
    cert = formal_certify(fw, formal_claim(fw))
    assert cert.failed() == ["K8"]
    assert "proof source is larger than 40 bytes" in reasons(cert.checks["K8"])
    assert "statement source is larger than 40 bytes" in reasons(cert.checks["K8"])
    monkeypatch.undo()
    monkeypatch.setattr(formal, "MATHLIB_COMMIT", PIN)
    monkeypatch.setattr(formal, "MAX_OUTPUT_CHARS", 10)
    cert = formal_certify(fw, formal_claim(fw, statement="long output"))
    assert cert.failed() == ["K8"] and "output of step 'check' exceeds 10 characters" in reasons(cert.checks["K8"])


# ------------------------------------------------------------------ K1: shape, adapter, the HUMAN statement approval
def test_formal_claim_well_formedness(fw):
    ref = approve_statement(fw)
    other = ArtifactRef(artifact_id="x", hash="0" * 64)
    cases = {
        "kind 'theorem'": {"kind": "improvement"},
        "a theorem claim carries no value": {"value": "1"},
        "comparator must be the subject": {"comparator": other.model_dump()},
        "uses no datasets": {"scope__datasets": ["test"]},
        "analysis plan must be empty": {"analysis_plan__seeds": [0]},
        "effect fields other than the declared probability must be 0": {"effect__predicted_hi": 0.1},
        "declared probability": {"effect__probability": 0.0},
        "risk prior must be unset": {"risk__prior_probability": 0.5},
        "no registered adapter serves evidence class 'formal' with metric 'lean_proof'": {"metric__id": "lean_proof"},
    }
    for why, change in cases.items():
        bad = formal_claim(fw, ref, **change)
        fw.kernel.preregister(bad)
        assert why in reasons(fw.kernel.certify(bad, []).checks["K1"]), (why, bad)
    assert ClaimKind.THEOREM not in SUPPORTED_KINDS  # a data claim of kind theorem is refused by the data K1


def test_statement_without_a_human_approval_is_refused(fw):
    data = V.statement_bytes()
    art = fw.vault.register_artifact("thm-unapproved", data, "lean-statement")
    ref = ArtifactRef(artifact_id=art.artifact_id, hash=art.hash)
    cert = formal_certify(fw, formal_claim(fw, ref))
    assert cert.failed() == ["K1"] and "has no human approval" in reasons(cert.checks["K1"])
    fw.kernel.ledger.append("statement_approval", {"artifact_id": "thm-unapproved", "hash": art.hash,
                                                   "theorem": V.THEOREM, "approver": "   ", "note": ""})
    cert = formal_certify(fw, formal_claim(fw, ref, statement="anonymous approval"))
    assert "has no human approval" in reasons(cert.checks["K1"])  # an approval must name its human
    fw.kernel.ledger.append("statement_approval", {"artifact_id": "thm-unapproved", "hash": "0" * 64,
                                                   "theorem": V.THEOREM, "approver": "bob", "note": ""})
    cert = formal_certify(fw, formal_claim(fw, ref, statement="approval of other bytes"))
    assert "has no human approval" in reasons(cert.checks["K1"])  # binds only the hashed item it names


def test_formal_claim_without_an_approved_lean_adapter_fails_k1(tmp_path, pinned):
    w = World(tmp_path / "noapp", seed=14, n_train=30, n_test=30)  # approves only the ML adapter
    try:
        cert = formal_certify(w, formal_claim(w))
        assert cert.label == Label.REJECTED and "K1" in cert.failed() and cert.adapters == ()
        assert "adapter 'lean'" in reasons(cert.checks["K1"]) and "no human approval" in reasons(cert.checks["K1"])
        assert "no approved adapter with a Lean record checker (see K1)" in reasons(cert.checks["K3"])
    finally:
        w.close()


def test_formal_provenance_and_order_reuse_the_exact_checks(fw):
    claim = formal_claim(fw)
    fw.kernel.preregister(claim)
    assert fw.kernel.certify(claim, []).failed() == ["K0", "K3", "K8"]  # no record at all
    assert "no single resolved check record (see K0)" in reasons(fw.kernel.certify(claim, []).checks["K3"])
    early = formal_claim(fw, statement="record before registration")
    aid = record_artifact(fw, early, V.record(V.statement_bytes()))
    fw.kernel.preregister(early)
    cert = fw.kernel.certify(early, [fw.kernel.record_certificate(early, aid)])
    assert cert.failed() == ["K2"] and "registered before the pre-registration" in reasons(cert.checks["K2"])
    canary = fw.vault.canary("test")
    leaky = formal_certify(fw, formal_claim(fw, statement="leaky"),
                           V.record(V.statement_bytes(), proof=V.PROOF_SOURCE + f"-- {canary}\n"))
    assert leaky.failed() == ["K9"] and "canary token found in certificate" in reasons(leaky.checks["K9"])


def test_n_a_outside_the_table_fails_closed_for_formal(fw, monkeypatch):
    from custom_harness.kernel import CheckResult
    na = lambda ctx: CheckResult(status="n/a", reasons=("planted",))  # noqa: E731
    monkeypatch.setitem(K.FORMAL_CHECKS, "K3", na)
    assert formal_certify(fw, formal_claim(fw)).label == Label.REJECTED
    monkeypatch.undo()
    monkeypatch.setattr(formal, "MATHLIB_COMMIT", PIN)
    no_checker = A.AdapterSpec("lean", "1", frozenset({EvidenceClass.FORMAL}), A.LEAN.modules,
                               frozenset({"lean_theorem"}))
    monkeypatch.setitem(A.REGISTRY, "lean", (no_checker, A.REGISTRY["lean"][1]))
    fw.kernel.approve_adapter("lean", approver="test-world")
    cert = formal_certify(fw, formal_claim(fw, statement="no checker"))
    assert "declares no Lean record checker" in reasons(cert.checks["K1"])
    assert "no approved adapter with a Lean record checker (see K1)" in reasons(cert.checks["K3"])


# ------------------------------------------------------------------ red team: a dishonest prover's tricks (hygiene)
DISHONEST = {
    "macro redefining theorem": "macro \"theorem\" x:ident : command => `(axiom $x : True)\n",
    "macro_rules": "macro_rules | `(tactic| decide) => `(tactic| sorry)\n",
    "custom syntax": "syntax \"cheat\" : tactic\n",
    "elab": "elab \"cheat\" : tactic => pure ()\n",
    "notation shadowing": "local notation a \" + \" b => a * b\n",
    "infix shadowing": "local infixl:65 \" + \" => Nat.mul\n",
    "#exit": "#exit\n",
    "#eval": "#eval IO.println \"CHH-LEAN-RESULT forged\"\n",
    "#print": "#print axioms Chh.add_comm'\n",
    "skipKernelTC": "set_option debug.skipKernelTC true\n",
    "set_option in": "set_option debug.skipKernelTC true in\n",
    "guillemet name": "theorem t : True := «sorry»\n",
    "implemented_by": "@[implemented_by foo] def bar : Bool := true\n",
    "extern": "@[extern \"c_fn\"] opaque f : Nat -> Nat\n",
    "export attribute": "@[export chh_f] def f : Nat := 1\n",
    "unsafe": "unsafe def f : Nat := 1\n",
    "unsafeCast": "def f : Nat := unsafeCast ()\n",
    "initialize": "initialize pure ()\n",
    "run_cmd": "run_cmd pure ()\n",
    "run_tac": "example : True := by run_tac pure ()\n",
    "native config": "example : True := by decide (config := { native := true })\n",
    "+native": "example : True := by decide +native\n",
    "ofReduceBool": "theorem t : True := Lean.ofReduceBool _ _ rfl\n",
    "prelude": "prelude\n",
    "import Lean": "import Lean\n",
    "import the challenge": "import Challenge\n",
    "addDecl": "def f := Lean.addDecl\n",
}


def test_dishonest_prover_tricks_are_rejected_by_hygiene(fw):
    statement = V.statement_bytes()
    ref = approve_statement(fw, statement)
    for name, line in DISHONEST.items():
        proof = line + V.PROOF_SOURCE
        assert formal.hygiene(proof, statement=False), name
        cert = formal_certify(fw, formal_claim(fw, ref, statement=f"dishonest: {name}"), V.record(statement, proof))
        assert cert.failed() == ["K3"] and "proof: forbidden" in reasons(cert.checks["K3"]), (name, cert.checks["K3"])


def test_hygiene_allows_ordinary_mathematics():
    ok = ("import Mathlib.Data.Nat.Basic\nimport Mathlib\nopen Nat\n"
          "set_option maxHeartbeats 400000 in\n"
          "instance : Inhabited Nat := ⟨0⟩\n"
          "theorem t (a b : ℕ) : a + b = b + a := by\n  simp [Nat.add_comm]  -- sorryish comment-free\n"
          "def v := #[1, 2]\n")
    assert formal.hygiene(ok, statement=False) == []
    assert formal.hygiene(V.STATEMENT_SOURCE, statement=True) == []  # the approved statement may use sorry
    assert "forbidden 'sorry'" in " ".join(formal.hygiene(V.STATEMENT_SOURCE, statement=False))
    assert formal.hygiene("import Mathlib.Tactic Std.Data\n", statement=False) == []
    assert "forbidden import 'Lean.Elab' (not allowlisted)" in " ".join(formal.hygiene("import Lean.Elab\n",
                                                                                         statement=False))
    assert "forbidden set_option 'pp.all' (not allowlisted)" in " ".join(formal.hygiene("set_option pp.all true\n",
                                                                                         statement=False))


# ------------------------------------------------------------------ the adapter's own validation and pins
def test_lean_adapter_validation_and_pins(pinned):
    assert A.validate(V.LEAN_CHECKS) == [] and len(V.LEAN_CHECKS) >= 12
    assert A.LEAN.exact_module == "custom_harness.kernel.formal" and A.LEAN.classes == {EvidenceClass.FORMAL}
    assert set(A.LEAN.modules) == {"custom_harness.kernel.formal", "custom_harness.verify.lean_validation",
                                   "custom_harness.sandbox.lean"}
    assert A.adapter_hash(A.ML) == "71694bb1ca60010f9ea241073c89255f5f0937bc1f215b7797e088ca8e363fca"  # unchanged
    assert formal.pinned() == []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(formal, "LEAN_VERSION", "nightly")
        assert formal.pinned() == ["LEAN_VERSION 'nightly' is not a Lean release number"]
    checker = (ROOT / "docker" / "lean" / "ChhLeanCheck.lean").read_bytes()
    assert hashlib.sha256(checker).hexdigest() == formal.CHECKER_SHA256  # the pinned comparator is this file
    assert formal.ALLOWED_AXIOMS == frozenset({"propext", "Classical.choice", "Quot.sound"})


def test_unpinned_mathlib_makes_the_adapter_unapprovable(tmp_path):
    """As shipped, the Mathlib commit is a placeholder: validation fails and `chh approve-adapter lean` refuses, so
    no formal claim can be certified until the owner pins the toolchain (and approves the new adapter hash)."""
    assert "OWNER" in formal.MATHLIB_COMMIT
    assert "not a 40-hex commit" in " ".join(formal.pinned())
    assert A.validate(V.LEAN_CHECKS) == ["the Lean version and the Mathlib commit are pinned"]
    w = World(tmp_path / "w", approve_adapters=False)
    try:
        with pytest.raises(A.AdapterError, match="failed its validation"):
            w.kernel.approve_adapter("lean", approver="alice")
        assert w.kernel.ledger.entries("adapter_approval") == []
    finally:
        w.close()


def test_formal_parsers_refuse_malformed_input(pinned):
    st = V.statement_bytes()
    good = V.record(st)
    assert formal.check(st, canonical_json(good)) == [] and formal.bounds(st, canonical_json(good)) == []
    assert formal.axioms_used(canonical_json(good)) == []
    assert formal.axioms_used(b"garbage") == []
    statements = {
        "not JSON": b"{", "not a chh-lean-statement/1": canonical_json({"format": "x"}),
        "missing keys": canonical_json({"format": formal.STATEMENT_FORMAT}),
        "unexpected keys": canonical_json({**json.loads(st), "x": 1}),
        "not a Lean declaration name": V.statement_bytes(theorem="1bad name"),
        "source must be a string": canonical_json({**json.loads(st), "source": 3}),
        "larger than": b" " * (formal.MAX_BYTES + 1),
    }
    for why, data in statements.items():
        assert any(why in r for r in formal.check(data, canonical_json(good))), why
        assert formal.bounds(data, canonical_json(good)) != [], why
    records = {
        "'toolchain' must be": {**good, "toolchain": "x"},
        "'steps' must be": {**good, "steps": {"build_proof": good["steps"]["build_proof"]}},
        "step 'check' must be": {**good, "steps": {**good["steps"], "check": {"exit_code": 0}}},
        "'exit_code' of step 'check' must be an integer": {
            **good, "steps": {**good["steps"], "check": {**good["steps"]["check"], "exit_code": True}}},
        "'stdout' of step 'check' must be a string": {
            **good, "steps": {**good["steps"], "check": {**good["steps"]["check"], "stdout": 1}}},
        "'timed_out' of step 'check' must be a boolean": {
            **good, "steps": {**good["steps"], "check": {**good["steps"]["check"], "timed_out": 0}}},
        "'argv' of step 'check' must be a list of strings": {
            **good, "steps": {**good["steps"], "check": {**good["steps"]["check"], "argv": [1]}}},
        "'proof_source' must be a string": {**good, "proof_source": None},
        "'proof_outputs' must be a list of strings": {**good, "proof_outputs": "x"},
        "'salt' must be": {**good, "salt": "xyz"},
        "'nonce' must be": {**good, "nonce": "XYZ"},
        "'statement_sha256' must be": {**good, "statement_sha256": "short"},
    }
    for why, rec in records.items():
        assert any(why in r for r in formal.check(st, canonical_json(rec))), why
    assert formal.nonce_for("a", "b", "c") == formal.nonce_for("a", "b", "c") != formal.nonce_for("a", "b", "d")
    assert formal.lean_version("Lean (version 4.19.0, x86_64-unknown-linux-gnu, commit abc, Release)") == "4.19.0"
    assert formal.lean_version("Lean (version 4.20.0-rc2, commit abc, Release)") == "4.20.0-rc2"
    assert formal.lean_version("garbage") is None
